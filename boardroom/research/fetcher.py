"""
Tiered web fetch — rung 1 (httpx + trafilatura), Postgres-backed cache,
per-domain politeness. Rung 2 (Playwright) and rung 3 (proxy rotation) are
deferred until we measure how often rung 1 returns empty/blocked content.

The cache is the moat: every page the researcher reads becomes a row in
`fetch_cache`, and subsequent fetches of the same URL are free until the
TTL expires.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

log = logging.getLogger(__name__)


# Truncate extracted content here. Worst-case research page after extraction
# is rarely above this; longer text gets diminishing returns and bloats both
# the cache and the LLM context downstream.
MAX_CONTENT_CHARS = 50_000

# Below this many chars, the extracted body is almost certainly a bot challenge,
# a JS shell, or a 403/empty response — not real content. The loop treats an
# empty page as "no signal here, skip" and avoids spending an LLM call on a
# 30-char string. Calibrated against T5: reddit challenge pages return 37
# chars ("Reddit - Please wait for verification") and Cloudflare challenge
# returns 16 chars ("Just a moment..."). 200 catches both with margin.
MIN_USABLE_CONTENT = 200

# Substrings that unambiguously mark a blocked / challenge page. Match is
# case-insensitive on the first ~500 chars of extracted content.
_BLOCKED_PATTERNS = (
    "please wait for verification",   # Reddit anti-bot
    "just a moment",                  # Cloudflare challenge
    "checking your browser",          # Cloudflare older variant
    "enable javascript and cookies",  # generic JS-wall
    "captcha",                        # any explicit captcha gate
    "access denied",                  # plain 403 page text
    "are you a robot",                # human verification
    "request unsuccessful",           # Akamai bot protection
)


def _looks_blocked(content: str) -> bool:
    """True when the extracted text matches a known challenge/anti-bot
    pattern OR is too short to plausibly contain useful evidence."""
    if not content:
        return True
    stripped = content.strip()
    if len(stripped) < MIN_USABLE_CONTENT:
        return True
    head = stripped[:500].lower()
    return any(p in head for p in _BLOCKED_PATTERNS)

# Per-domain courtesy delay between completions, in seconds. Combined with
# the per-domain lock below, this caps in-flight requests at 1 per domain and
# spaces successive requests by at least DOMAIN_DELAY.
DOMAIN_DELAY = 1.0

HTTP_TIMEOUT = 30.0
USER_AGENT = (
    "boardroom-research/0.1 (autonomous research agent; "
    "contact: see project README)"
)


# -------------------------------------------------------------------------
# TTL by URL pattern
# -------------------------------------------------------------------------

# Hostname → TTL seconds. Match is exact-hostname or "endswith .suffix"; pick
# the first matching prefix walked over (longest match wins via ordering).
# Default 7 days; news/social 1 hour; evergreen docs 30 days.
_TTL_RULES: tuple[tuple[str, int], ...] = (
    # 1 hour — discussion/news, where stale would mislead.
    ("news.ycombinator.com", 3_600),
    ("reddit.com",           3_600),
    ("x.com",                3_600),
    ("twitter.com",          3_600),
    ("nitter.net",           3_600),
    ("bsky.app",             3_600),
    # 30 days — slow-moving reference material.
    ("wikipedia.org",        2_592_000),
    ("github.com",           2_592_000),
    ("docs.python.org",      2_592_000),
    ("developer.mozilla.org", 2_592_000),
    ("arxiv.org",            2_592_000),
)
_DEFAULT_TTL = 7 * 24 * 3_600


def ttl_for(url: str) -> int:
    """Pick a TTL based on the URL's hostname. Domain-suffix match."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return _DEFAULT_TTL
    if not host:
        return _DEFAULT_TTL
    for needle, secs in _TTL_RULES:
        if host == needle or host.endswith("." + needle):
            return secs
    return _DEFAULT_TTL


# -------------------------------------------------------------------------
# Per-domain politeness
# -------------------------------------------------------------------------

@dataclass
class _DomainGate:
    lock: asyncio.Lock
    last_completed_at: float = 0.0


_domain_gates: dict[str, _DomainGate] = {}
_gates_lock = asyncio.Lock()


async def _acquire_domain(host: str) -> _DomainGate:
    """Return the gate for `host`, creating it if new."""
    async with _gates_lock:
        gate = _domain_gates.get(host)
        if gate is None:
            gate = _DomainGate(lock=asyncio.Lock())
            _domain_gates[host] = gate
    await gate.lock.acquire()
    # Sleep off any remaining courtesy gap from the previous completion.
    elapsed = time.monotonic() - gate.last_completed_at
    if elapsed < DOMAIN_DELAY:
        await asyncio.sleep(DOMAIN_DELAY - elapsed)
    return gate


def _release_domain(gate: _DomainGate) -> None:
    gate.last_completed_at = time.monotonic()
    gate.lock.release()


# -------------------------------------------------------------------------
# Output type
# -------------------------------------------------------------------------

class FetchedPage(BaseModel):
    url: str
    content: str             # extracted markdown / plain text
    extractor: str           # 'trafilatura' | 'bs4' | 'plain' | 'cached'
    status_code: int
    bytes_fetched: int
    from_cache: bool = False


# -------------------------------------------------------------------------
# Extraction
# -------------------------------------------------------------------------

def _extract(body: str, content_type: str) -> tuple[str, str]:
    """
    Return (content, extractor_name). Tries trafilatura on HTML, falls back to
    bs4. Non-HTML is returned as-is (truncated).
    """
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return body[:MAX_CONTENT_CHARS], "plain"

    # trafilatura is much better than bs4 boilerplate-stripping but can produce
    # nothing on JS-rendered or unusual pages, in which case we fall back.
    try:
        import trafilatura
        extracted = trafilatura.extract(
            body,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            output_format="markdown",
        )
        if extracted and extracted.strip():
            return extracted[:MAX_CONTENT_CHARS], "trafilatura"
    except Exception as e:  # noqa: BLE001 — extraction errors are recoverable
        log.debug("trafilatura failed (%s); falling back to bs4", e)

    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return text[:MAX_CONTENT_CHARS], "bs4"


# -------------------------------------------------------------------------
# Main entrypoint
# -------------------------------------------------------------------------

async def web_fetch(
    url: str,
    state,
    *,
    force: bool = False,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[FetchedPage]:
    """
    Fetch `url` and return cleaned text. Cache-first; on miss does an httpx GET
    with per-domain politeness, extracts with trafilatura (bs4 fallback), and
    writes the result back to `fetch_cache`.

    Returns None on transport failure. A returned FetchedPage with empty
    content is a legitimate result (the page rendered to nothing extractable)
    — callers should treat empty as "no signal" rather than "error".
    """
    if not force:
        cached = await state.fetch_cache_get(url)
        if cached is not None:
            # Defense-in-depth: re-run the challenge detection on cached
            # content so legacy rows (cached before this code shipped) don't
            # keep serving 37-char Reddit verification pages as "content".
            # Cleaning their content to "" makes the loop skip them without
            # a refetch or LLM call.
            cached_content = cached["content"] or ""
            if cached["extractor"] != "blocked" and _looks_blocked(cached_content):
                cached_content = ""
            return FetchedPage(
                url=url,
                content=cached_content,
                extractor=cached["extractor"] if cached_content else "blocked",
                status_code=cached["status_code"],
                bytes_fetched=cached["bytes_fetched"] or 0,
                from_cache=True,
            )

    host = (urlparse(url).hostname or "").lower()
    if not host:
        log.warning("web_fetch: refusing URL with no hostname: %r", url)
        return None

    gate = await _acquire_domain(host)
    try:
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        try:
            resp = await client.get(url)
        finally:
            if owns_client:
                await client.aclose()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        log.warning("web_fetch %s failed: %s", url, e)
        _release_domain(gate)
        return None
    else:
        _release_domain(gate)

    content_type = resp.headers.get("content-type", "")
    content, extractor = _extract(resp.text, content_type)

    # Detect known bot-challenge / blocked pages and don't pass them downstream.
    # We still cache the result with a 'blocked' extractor + empty content so
    # repeated fetches within the TTL window are a cache hit (no re-fetch,
    # no LLM call). The loop's `if page is None or not page.content.strip()`
    # check skips when content is empty, which is what we want.
    if _looks_blocked(content):
        log.info("web_fetch: %s returned blocked/empty content "
                 "(status=%d, %d chars) — caching as 'blocked'",
                 url, resp.status_code, len(content or ""))
        await state.fetch_cache_put(
            url=url, content="", extractor="blocked",
            status_code=resp.status_code,
            bytes_fetched=len(resp.content),
            ttl_seconds=ttl_for(url),
        )
        return FetchedPage(
            url=url, content="", extractor="blocked",
            status_code=resp.status_code, bytes_fetched=len(resp.content),
            from_cache=False,
        )

    page = FetchedPage(
        url=url,
        content=content,
        extractor=extractor,
        status_code=resp.status_code,
        bytes_fetched=len(resp.content),
        from_cache=False,
    )

    # Persist even partial / error responses — saves us refetching a known-bad
    # URL within the TTL window. The caller can still decide to ignore empty
    # content.
    await state.fetch_cache_put(
        url=url,
        content=content,
        extractor=extractor,
        status_code=resp.status_code,
        bytes_fetched=len(resp.content),
        ttl_seconds=ttl_for(url),
    )
    return page


# -------------------------------------------------------------------------
# Convenience: fetch many in parallel
# -------------------------------------------------------------------------

async def web_fetch_many(
    urls: list[str], state, *, force: bool = False, concurrency: int = 8,
) -> list[Optional[FetchedPage]]:
    """
    Fetch a batch of URLs concurrently. Cap concurrency to keep the per-domain
    gates honest if many URLs share a host. Per-URL failures return None at
    that index (preserving order).
    """
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        async def _one(u: str) -> Optional[FetchedPage]:
            async with sem:
                return await web_fetch(u, state, force=force, client=client)
        return await asyncio.gather(*[_one(u) for u in urls])
