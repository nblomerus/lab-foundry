"""
Tests for library.ingest.fetcher.

No real DB and no real network: a fake state client supplies the cache, and
the httpx call is patched to return canned responses. The point is to prove
the cache short-circuit, the extractor fallback, the per-domain gate, and the
TTL rules — not to exercise httpx or trafilatura themselves.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from library.ingest import fetcher
from library.ingest.fetcher import (
    ttl_for,
    web_fetch,
)

# --------------------------------------------------------------------------
# Fake state client
# --------------------------------------------------------------------------


class _FakeState:
    def __init__(self, prefill: dict | None = None):
        self._store: dict[str, dict] = {}
        if prefill:
            self._store.update(prefill)
        self.gets = 0
        self.puts: list[dict] = []

    async def fetch_cache_get(self, url: str) -> dict | None:
        self.gets += 1
        return self._store.get(url)

    async def fetch_cache_put(self, url, content, extractor, status_code, bytes_fetched, ttl_seconds):
        rec = {
            "content": content,
            "extractor": extractor,
            "status_code": status_code,
            "bytes_fetched": bytes_fetched,
        }
        self._store[url] = rec
        self.puts.append({"url": url, "ttl": ttl_seconds, **rec})


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _resp(text: str, content_type: str = "text/html", status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=text.encode("utf-8"),
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://example.test/"),
    )


@pytest.fixture(autouse=True)
def _fast_domain_delay(monkeypatch):
    """Squash the courtesy delay to keep tests snappy."""
    monkeypatch.setattr(fetcher, "DOMAIN_DELAY", 0.01)
    # Also reset per-test domain gates so ordering doesn't leak.
    fetcher._domain_gates.clear()


# --------------------------------------------------------------------------
# TTL rules
# --------------------------------------------------------------------------


def test_ttl_for_news_short():
    assert ttl_for("https://www.reddit.com/r/x/comments/1") == 3_600
    assert ttl_for("https://news.ycombinator.com/item?id=1") == 3_600


def test_ttl_for_evergreen_long():
    assert ttl_for("https://github.com/foo/bar") == 30 * 24 * 3_600
    assert ttl_for("https://en.wikipedia.org/wiki/Python") == 30 * 24 * 3_600


def test_ttl_for_default_week():
    assert ttl_for("https://example.com/some/article") == 7 * 24 * 3_600


def test_ttl_for_malformed_url_returns_default():
    assert ttl_for("not-a-url") == 7 * 24 * 3_600


# --------------------------------------------------------------------------
# Cache short-circuit
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_returns_without_fetching():
    # Content must be above MIN_USABLE_CONTENT (200) so the re-detection
    # doesn't treat it as a blocked page.
    real_cached_body = (
        "This is a real article about Python async programming. "
        "It explains coroutines, the event loop, and how await works. "
        "Useful for developers learning async patterns in Python 3.11+. "
        "Includes practical examples and benchmarks."
    )
    state = _FakeState(
        prefill={
            "https://example.com/a": {
                "content": real_cached_body,
                "extractor": "trafilatura",
                "status_code": 200,
                "bytes_fetched": 1500,
            }
        }
    )

    # If httpx is touched we treat that as a test failure.
    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(side_effect=AssertionError("should not fetch"))):
        page = await web_fetch("https://example.com/a", state)

    assert page is not None
    assert page.from_cache is True
    assert page.content == real_cached_body
    assert page.extractor == "trafilatura"
    assert state.gets == 1
    assert state.puts == []


@pytest.mark.asyncio
async def test_force_refetch_bypasses_cache():
    state = _FakeState(
        prefill={
            "https://example.com/a": {
                "content": "stale",
                "extractor": "trafilatura",
                "status_code": 200,
                "bytes_fetched": 1,
            }
        }
    )

    fresh_html = (
        "<html><body>"
        "<p>Fresh body content with enough substance to clear the "
        "MIN_USABLE_CONTENT threshold of 200 characters. The article "
        "covers Python async programming in real depth, with practical "
        "examples and notes about pitfalls.</p>"
        "</body></html>"
    )

    async def _fake_get(self, url):
        return _resp(fresh_html)

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        page = await web_fetch("https://example.com/a", state, force=True)

    assert page is not None
    assert page.from_cache is False
    assert "Fresh body content" in page.content
    assert len(state.puts) == 1


# --------------------------------------------------------------------------
# Fetch + extraction
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_extraction_caches_result():
    state = _FakeState()

    # Padded with extra paragraphs so the extracted text clears the
    # MIN_USABLE_CONTENT threshold.
    html = """
    <html><head><title>Hello</title></head>
    <body>
      <nav>nav stuff</nav>
      <main>
        <p>The real article content has substance and detail.</p>
        <p>Another paragraph for trafilatura to find. It contains
           real numbers like 42 requests/sec and named products like
           PostgreSQL 16 to make the body realistic.</p>
        <p>A third paragraph adds enough substance to put the extracted
           text well above the 200 char minimum, so this isn't treated as
           a bot challenge page.</p>
      </main>
      <footer>footer</footer>
    </body></html>
    """

    async def _fake_get(self, url):
        return _resp(html)

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        page = await web_fetch("https://example.com/article", state)

    assert page is not None
    assert page.from_cache is False
    assert page.status_code == 200
    assert "real article content" in page.content
    # Either trafilatura got it (likely) or bs4 fell back — both are valid.
    assert page.extractor in {"trafilatura", "bs4"}
    assert len(state.puts) == 1
    assert state.puts[0]["ttl"] == 7 * 24 * 3_600


@pytest.mark.asyncio
async def test_non_html_returns_plain():
    state = _FakeState()
    # The threshold applies to all content types, so the JSON needs to be
    # realistically sized too.
    body = '{"a": 1, "b": [' + ",".join(str(i) for i in range(80)) + "]}"

    async def _fake_get(self, url):
        return _resp(body, content_type="application/json")

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        page = await web_fetch("https://api.example.com/data", state)

    assert page is not None
    assert page.extractor == "plain"
    assert '"a": 1' in page.content


@pytest.mark.asyncio
async def test_transport_error_returns_none():
    state = _FakeState()

    async def _fake_get(self, url):
        raise httpx.ConnectError("nope")

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        page = await web_fetch("https://dead.example.com/", state)

    assert page is None
    assert state.puts == []  # nothing cached on failure


@pytest.mark.asyncio
async def test_invalid_url_refused():
    state = _FakeState()
    page = await web_fetch("not-a-url-at-all", state)
    assert page is None


# --------------------------------------------------------------------------
# Per-domain politeness
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Bot challenge / blocked-page detection (T5 finding: 30% of extract calls
# burned on Reddit "Please wait for verification" and Cloudflare "Just a
# moment..." pages)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reddit_challenge_page_treated_as_blocked():
    state = _FakeState()
    challenge_html = (
        "<html><head><title>Reddit</title></head><body><h1>Reddit - Please wait for verification</h1></body></html>"
    )

    async def _fake_get(self, url):
        return _resp(challenge_html)

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        page = await web_fetch("https://www.reddit.com/r/x/comments/1", state)

    assert page is not None
    assert page.content == ""
    assert page.extractor == "blocked"
    assert len(state.puts) == 1
    # Cached as 'blocked' so subsequent fetches short-circuit.
    assert state.puts[0]["extractor"] == "blocked"
    assert state.puts[0]["content"] == ""


@pytest.mark.asyncio
async def test_cloudflare_challenge_treated_as_blocked():
    state = _FakeState()

    async def _fake_get(self, url):
        return _resp(
            "<html><body><h1>Just a moment...</h1><p>Checking your browser before accessing.</p></body></html>",
            status=403,
        )

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        page = await web_fetch("https://medium.com/some-article", state)

    assert page is not None
    assert page.content == ""
    assert page.extractor == "blocked"


@pytest.mark.asyncio
async def test_too_short_content_treated_as_blocked():
    """Below MIN_USABLE_CONTENT, content is treated as blocked even without a
    known challenge string — that pattern caught Reddit/CF in T5 too."""
    state = _FakeState()

    async def _fake_get(self, url):
        return _resp("<html><body><p>hi</p></body></html>")

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        page = await web_fetch("https://example.com/empty", state)

    assert page is not None
    assert page.content == ""
    assert page.extractor == "blocked"


@pytest.mark.asyncio
async def test_legacy_cached_challenge_pages_cleaned_on_hit():
    """T5 cached Reddit verification pages with non-empty 37-char content
    and extractor='bs4'. On cache hit, the re-detection cleans them to
    empty so the loop skips without a refetch."""
    state = _FakeState(
        prefill={
            "https://www.reddit.com/r/cats/comments/1": {
                "content": "Reddit - Please wait for verification",
                "extractor": "bs4",
                "status_code": 200,
                "bytes_fetched": 8492,
            }
        }
    )

    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(side_effect=AssertionError("should not fetch"))):
        page = await web_fetch("https://www.reddit.com/r/cats/comments/1", state)

    assert page is not None
    assert page.from_cache is True
    assert page.content == ""
    assert page.extractor == "blocked"


# --------------------------------------------------------------------------
# Reddit relevance filter (T5 finding: /r/cats showed up for MCP queries)
# --------------------------------------------------------------------------


def test_meaningful_terms_drops_stopwords():
    from agents.researcher.tools import _meaningful_terms

    assert _meaningful_terms("What is the current adoption rate of MCP tools among developers?") == [
        "current",
        "adoption",
        "rate",
        "mcp",
        "among",
        "developers",
    ] or _meaningful_terms("What is the current adoption rate of MCP tools among developers?") == [
        "adoption",
        "rate",
        "mcp",
        "among",
        "developers",
    ]
    # Variants are OK as long as `mcp` and `adoption` are kept and stop
    # words like "what"/"the"/"is" are dropped.
    terms = _meaningful_terms("What is the current adoption rate of MCP tools among developers?")
    assert "mcp" in terms
    assert "adoption" in terms
    assert "what" not in terms
    assert "the" not in terms
    assert "is" not in terms
    assert "tools" not in terms  # tools is in stopwords (research-prompt filler)


def test_reddit_relevant_filters_cats_out():
    from agents.researcher.tools import (
        _meaningful_terms,
        _reddit_relevant,
    )

    terms = _meaningful_terms("MCP adoption developers")
    # The actual T5 off-topic example
    assert not _reddit_relevant(
        "Kittens in storm drain successfully rescued",
        "",
        terms,
    )
    # On-topic title passes
    assert _reddit_relevant(
        "MCP adoption rate report 2026",
        "by Anthropic",
        terms,
    )
    # On-topic via snippet only also passes
    assert _reddit_relevant(
        "Some general developer post",
        "I've been trying out MCP and...",
        terms,
    )


def test_reddit_relevant_empty_terms_passes_everything():
    """If the query has no meaningful terms (extreme edge case),
    don't drop anything — let the LLM filter."""
    from agents.researcher.tools import _reddit_relevant

    assert _reddit_relevant("anything", "", []) is True


@pytest.mark.asyncio
async def test_same_domain_requests_are_spaced():
    """Two back-to-back fetches to the same host must observe DOMAIN_DELAY."""
    state = _FakeState()

    async def _fake_get(self, url):
        return _resp("<html><body><p>ok content here</p></body></html>")

    # Bump the delay enough to be measurable but still fast.
    fetcher.DOMAIN_DELAY = 0.10

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        start = time.monotonic()
        await asyncio.gather(
            web_fetch("https://samehost.test/a", state),
            web_fetch("https://samehost.test/b", state),
        )
        elapsed = time.monotonic() - start

    # The second fetch must wait at least DOMAIN_DELAY after the first.
    assert elapsed >= 0.10
