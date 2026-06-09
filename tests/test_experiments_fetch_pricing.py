"""
Tests for `agents.researcher.experiments.fetch_pricing`.

The runner resolves a URL (directly or via `search_web`), fetches the page
through `library.ingest.fetcher.web_fetch`, then asks the model (via the
dispatcher's curator + router) to parse it into structured tiers.

Everything that touches the outside world is mocked:
  * `httpx.AsyncClient.get` — so `web_fetch` does the real cache/extract path
    with NO network. The state's `fetch_cache_get`/`fetch_cache_put` are
    AsyncMocks (no Postgres).
  * `search_web` — patched on the module under test for the company-resolution
    branch.
  * the dispatcher's `curator.build` / `router.invoke` — the LLM seam.

Pure helpers (`_coerce_targets`) are called directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agents.researcher.experiments import fetch_pricing as fp
from agents.researcher.experiments.fetch_pricing import (
    ParsedPricing,
    PricingTier,
    _coerce_targets,
    run,
)
from agents.researcher.tools import SearchResult
from library.ingest.fetcher import FetchedPage

# A pricing page long enough to clear the fetcher's MIN_USABLE_CONTENT (200)
# guard so it isn't treated as a blocked/empty challenge page.
_PRICING_HTML = (
    "<html><body><h1>Pricing</h1>"
    + "<p>Starter $0 per month. Pro $20 per month. Enterprise contact sales.</p>" * 12
    + "</body></html>"
)


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


def _make_dispatcher(parsed: ParsedPricing | None = None, *, fetch_cache_get=None):
    """A dispatcher whose state has AsyncMock cache hooks and whose
    curator/router are AsyncMocks. `parsed` is what router.invoke returns."""
    state = AsyncMock()
    state.fetch_cache_get = AsyncMock(return_value=fetch_cache_get)
    state.fetch_cache_put = AsyncMock(return_value=None)

    dispatcher = AsyncMock()
    dispatcher.state = state
    dispatcher.curator = AsyncMock()
    dispatcher.curator.build = AsyncMock(return_value="<built-prompt>")
    dispatcher.router = AsyncMock()
    if parsed is None:
        parsed = ParsedPricing(
            tiers=[PricingTier(name="Pro", price_usd=20.0, period="month", features=["a", "b"])],
            extraction_quality="good",
        )
    dispatcher.router.invoke = AsyncMock(return_value=(parsed, {"usage": "x"}))
    return dispatcher


def _http_get(status: int, body: str, content_type: str = "text/html; charset=utf-8"):
    """Return a fake `httpx.AsyncClient.get` coroutine yielding a fixed response."""

    async def _fake_get(self, url, *a, **kw):
        return httpx.Response(
            status,
            text=body,
            headers={"content-type": content_type},
            request=httpx.Request("GET", url),
        )

    return _fake_get


# --------------------------------------------------------------------------
# _coerce_targets — every shape branch
# --------------------------------------------------------------------------


def test_coerce_targets_singular_url():
    assert _coerce_targets({"url": "https://x/"}) == [{"url": "https://x/"}]


def test_coerce_targets_company_and_product_alias():
    assert _coerce_targets({"company": "OpenAI"}) == [{"company": "OpenAI"}]
    assert _coerce_targets({"product": "GPT"}) == [{"company": "GPT"}]


def test_coerce_targets_plural_lists():
    assert _coerce_targets({"urls": ["https://a/", "https://b/"]}) == [
        {"url": "https://a/"},
        {"url": "https://b/"},
    ]
    assert _coerce_targets({"companies": ["A", "B"]}) == [{"company": "A"}, {"company": "B"}]
    assert _coerce_targets({"products": ["P"]}) == [{"company": "P"}]


def test_coerce_targets_skips_non_strings_in_lists():
    # Non-string entries in the plural lists are ignored, not coerced.
    assert _coerce_targets({"urls": ["https://a/", 5, None]}) == [{"url": "https://a/"}]
    assert _coerce_targets({"companies": [42, "Ok"]}) == [{"company": "Ok"}]


def test_coerce_targets_combines_singular_and_plural():
    out = _coerce_targets({"url": "https://x/", "companies": ["A"]})
    assert {"url": "https://x/"} in out
    assert {"company": "A"} in out


def test_coerce_targets_empty():
    assert _coerce_targets({}) == []


# --------------------------------------------------------------------------
# run() — happy path with a direct URL
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_url_fetch_and_parse_success():
    dispatcher = _make_dispatcher()
    with patch.object(httpx.AsyncClient, "get", new=_http_get(200, _PRICING_HTML)):
        result = await run({"url": "https://acme.example/pricing"}, dispatcher=dispatcher)

    assert result["count"] == 1
    assert result["targets"] == [{"url": "https://acme.example/pricing"}]
    one = result["results"][0]
    assert one["url"] == "https://acme.example/pricing"
    assert one["from_cache"] is False
    assert one["extraction_quality"] == "good"
    assert one["tiers"][0]["name"] == "Pro"
    assert one["raw_text_sample"]  # non-empty sample of the cleaned page
    # The LLM seam was actually invoked with the page content in context.
    dispatcher.curator.build.assert_awaited_once()
    _, kwargs = dispatcher.curator.build.await_args
    assert kwargs["context"]["url"] == "https://acme.example/pricing"
    assert kwargs["context"]["content"]
    dispatcher.router.invoke.assert_awaited_once()
    # No search needed when a direct URL is given.
    dispatcher.state.fetch_cache_put.assert_awaited()


# --------------------------------------------------------------------------
# run() — company resolution via search_web
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_company_resolves_via_search(monkeypatch):
    dispatcher = _make_dispatcher()

    async def _fake_search(query, limit=10):
        assert "Acme pricing" in query
        return [SearchResult(title="Acme Pricing", url="https://acme.example/p", snippet="s", source="web")]

    monkeypatch.setattr(fp, "search_web", _fake_search)

    with patch.object(httpx.AsyncClient, "get", new=_http_get(200, _PRICING_HTML)):
        result = await run({"company": "Acme"}, dispatcher=dispatcher)

    one = result["results"][0]
    assert one["url"] == "https://acme.example/p"
    assert one["extraction_quality"] == "good"


@pytest.mark.asyncio
async def test_run_company_no_search_results(monkeypatch):
    dispatcher = _make_dispatcher()

    async def _fake_search(query, limit=10):
        return []

    monkeypatch.setattr(fp, "search_web", _fake_search)

    result = await run({"company": "Nonexistent Co"}, dispatcher=dispatcher)
    one = result["results"][0]
    assert "no search results" in one["error"]
    assert one["target"] == {"company": "Nonexistent Co"}
    # The model is never asked to parse when there's nothing to fetch.
    dispatcher.router.invoke.assert_not_awaited()


# --------------------------------------------------------------------------
# run() — fetch failure (web_fetch returns None on transport error)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fetch_failure_returns_error():
    dispatcher = _make_dispatcher()

    async def _boom_get(self, url, *a, **kw):
        raise httpx.ConnectError("dns boom", request=httpx.Request("GET", url))

    with patch.object(httpx.AsyncClient, "get", new=_boom_get):
        result = await run({"url": "https://down.example/pricing"}, dispatcher=dispatcher)

    one = result["results"][0]
    assert one["url"] == "https://down.example/pricing"
    assert "failed to fetch" in one["error"]
    dispatcher.router.invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_non_2xx_status_still_parses():
    """A non-2xx with real body content is NOT a transport failure — web_fetch
    extracts and caches it, and the runner parses it like any other page."""
    dispatcher = _make_dispatcher()
    with patch.object(httpx.AsyncClient, "get", new=_http_get(503, _PRICING_HTML)):
        result = await run({"url": "https://flaky.example/pricing"}, dispatcher=dispatcher)

    one = result["results"][0]
    assert one["extraction_quality"] == "good"
    assert "error" not in one


# --------------------------------------------------------------------------
# run() — empty / blocked page
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_blocked_or_empty_page_short_circuits():
    """A page that extracts to (near-)nothing is treated as 'empty' — the runner
    returns extraction_quality=empty without spending an LLM call."""
    dispatcher = _make_dispatcher()
    tiny = "<html><body>Just a moment...</body></html>"
    with (
        patch("library.ingest.fetcher._PLAYWRIGHT_ENABLED", False),
        patch.object(httpx.AsyncClient, "get", new=_http_get(200, tiny)),
    ):
        result = await run({"url": "https://wall.example/pricing"}, dispatcher=dispatcher)

    one = result["results"][0]
    assert one["extraction_quality"] == "empty"
    assert one["tiers"] == []
    assert one["raw_text_sample"] == ""
    assert one["note"] == "page extracted to empty content"
    dispatcher.router.invoke.assert_not_awaited()


# --------------------------------------------------------------------------
# run() — cache hit (from_cache=True)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cache_hit_marks_from_cache():
    cached = {
        "content": _PRICING_HTML.replace("<", " ").replace(">", " "),  # plain text, long enough
        "extractor": "trafilatura",
        "status_code": 200,
        "bytes_fetched": 1234,
    }
    dispatcher = _make_dispatcher(fetch_cache_get=cached)

    # httpx.get must NOT be called on a cache hit — make it explode if it is.
    async def _explode(self, url, *a, **kw):
        raise AssertionError("network hit on a cache hit")

    with patch.object(httpx.AsyncClient, "get", new=_explode):
        result = await run({"url": "https://acme.example/pricing"}, dispatcher=dispatcher)

    one = result["results"][0]
    assert one["from_cache"] is True
    assert one["extraction_quality"] == "good"
    dispatcher.state.fetch_cache_put.assert_not_awaited()


# --------------------------------------------------------------------------
# run() — no usable targets
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_no_targets_raises_valueerror():
    dispatcher = _make_dispatcher()
    with pytest.raises(ValueError, match="fetch_pricing requires"):
        await run({"foo": "bar"}, dispatcher=dispatcher)


# --------------------------------------------------------------------------
# run() — single-target failure is caught and recorded, others proceed
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_single_target_exception_is_non_fatal(monkeypatch):
    dispatcher = _make_dispatcher()

    calls = {"n": 0}

    # First target raises inside web_fetch; second returns a good page. The
    # runner must catch the first failure and still process the second.

    async def _fetch(url, state, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kaboom on first target")
        return FetchedPage(
            url=url,
            content="Starter $0/mo. Pro $20/mo.",
            extractor="trafilatura",
            status_code=200,
            bytes_fetched=99,
            from_cache=False,
        )

    monkeypatch.setattr(fp, "web_fetch", _fetch)

    result = await run({"urls": ["https://a.example/", "https://b.example/"]}, dispatcher=dispatcher)

    assert result["count"] == 2
    first, second = result["results"]
    assert "kaboom" in first["error"]
    assert second["extraction_quality"] == "good"


# --------------------------------------------------------------------------
# run() — caps to 5 targets
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_caps_to_five_targets(monkeypatch):
    dispatcher = _make_dispatcher()

    seen: list[str] = []

    async def _fetch(url, state, **kw):
        seen.append(url)
        return FetchedPage(
            url=url, content="Pro $20/mo.", extractor="plain", status_code=200, bytes_fetched=10, from_cache=False
        )

    monkeypatch.setattr(fp, "web_fetch", _fetch)

    urls = [f"https://x{i}.example/" for i in range(8)]
    result = await run({"urls": urls}, dispatcher=dispatcher)

    # All 8 are coerced into targets, but only the first 5 are actually run.
    assert len(result["targets"]) == 8
    assert result["count"] == 5
    assert len(seen) == 5
