"""
Tiered web fetch — rung 1 (httpx + trafilatura), rung 2 (headless-browser
render via Playwright, fired only on a rung-1 miss), Postgres-backed cache,
per-domain politeness. Rung 3 (proxy rotation) is deferred.

The cache is the moat: every page the researcher reads becomes a row in
`fetch_cache`, and subsequent fetches of the same URL are free until the
TTL expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
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
    "please wait for verification",  # Reddit anti-bot
    "just a moment",  # Cloudflare challenge
    "checking your browser",  # Cloudflare older variant
    "enable javascript and cookies",  # generic JS-wall
    "captcha",  # any explicit captcha gate
    "access denied",  # plain 403 page text
    "are you a robot",  # human verification
    "request unsuccessful",  # Akamai bot protection
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
# A descriptive bot UA with a real contact URL — several high-value sources
# (Wikipedia among them) 403 a vague/placeholder UA but 200 a contactable bot.
USER_AGENT = "labfoundry-research/0.1 (autonomous research agent; +https://github.com/nblomerus/lab-foundry)"


# -------------------------------------------------------------------------
# TTL by URL pattern
# -------------------------------------------------------------------------

# Hostname → TTL seconds. Match is exact-hostname or "endswith .suffix"; pick
# the first matching prefix walked over (longest match wins via ordering).
# Default 7 days; news/social 1 hour; evergreen docs 30 days.
_TTL_RULES: tuple[tuple[str, int], ...] = (
    # 1 hour — discussion/news, where stale would mislead.
    ("news.ycombinator.com", 3_600),
    ("reddit.com", 3_600),
    ("x.com", 3_600),
    ("twitter.com", 3_600),
    ("nitter.net", 3_600),
    ("bsky.app", 3_600),
    # 30 days — slow-moving reference material.
    ("wikipedia.org", 2_592_000),
    ("github.com", 2_592_000),
    ("docs.python.org", 2_592_000),
    ("developer.mozilla.org", 2_592_000),
    ("arxiv.org", 2_592_000),
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
    content: str  # extracted markdown / plain text
    extractor: str  # 'trafilatura' | 'bs4' | 'plain' | 'playwright' | 'blocked' | 'cached'
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
# Rung 2 — headless-browser render fallback
#
# Rung 1 (httpx + trafilatura) handles the vast majority of research pages. A
# minority are JS-only: the server returns a near-empty shell and the real
# content is painted client-side, so rung-1 extraction comes back blocked/empty.
# Rung 2 renders those in headless Chromium and re-extracts. It fires ONLY on a
# rung-1 miss (so the cost — a browser launch — is paid rarely), is capped to a
# small number of concurrent browsers, and can be disabled with
# WEB_FETCH_PLAYWRIGHT=off. Any failure falls through to rung-1's result, so
# rung 2 can only ever help, never break the httpx path.
# -------------------------------------------------------------------------

_PLAYWRIGHT_ENABLED = os.environ.get("WEB_FETCH_PLAYWRIGHT", "on").strip().lower() not in (
    "0",
    "off",
    "false",
    "no",
)
_PLAYWRIGHT_NAV_TIMEOUT_MS = 20_000  # hard cap on navigation
_PLAYWRIGHT_SETTLE_MS = 5_000  # best-effort wait for client-side render to settle
# A render is expensive (a whole browser). Cap concurrent renders so a burst of
# blocked pages (e.g. web_fetch_many over a JS-heavy host) can't spawn a browser
# per URL. Rung 2 is the rare path, so a small cap is plenty.
_PLAYWRIGHT_MAX_CONCURRENCY = 2
_playwright_sem = asyncio.Semaphore(_PLAYWRIGHT_MAX_CONCURRENCY)


async def _render_with_playwright(url: str) -> str | None:
    """Render `url` in headless Chromium and return the post-JS HTML, or None if
    rendering is unavailable or fails. Each call launches and tears down its own
    browser — rung 2 fires rarely, so a persistent browser isn't worth the
    lifecycle complexity. Any failure (missing browser binary, nav timeout, …)
    returns None so the caller keeps rung-1's result."""
    try:
        async with _playwright_sem, async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(user_agent=USER_AGENT)
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=_PLAYWRIGHT_NAV_TIMEOUT_MS)
                # networkidle can hang on pages with long-poll/analytics, so cap
                # it and ignore the timeout — domcontentloaded already ran the JS.
                with contextlib.suppress(Exception):  # settle is best-effort
                    await page.wait_for_load_state("networkidle", timeout=_PLAYWRIGHT_SETTLE_MS)
                return await page.content()
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001 — render failures fall through to rung-1
        log.info("web_fetch: rung-2 render failed for %s: %s", url, e)
        return None


# -------------------------------------------------------------------------
# Main entrypoint
# -------------------------------------------------------------------------


async def web_fetch(
    url: str,
    state,
    *,
    force: bool = False,
    client: httpx.AsyncClient | None = None,
) -> FetchedPage | None:
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

    # Rung 2: if rung-1 came back blocked/empty on an HTML page, it's likely
    # JS-only — render it in a headless browser and re-extract. A non-HTML body
    # isn't rescuable this way, and a hard challenge page (Cloudflare et al.)
    # will still look blocked after rendering and simply fall through.
    if _PLAYWRIGHT_ENABLED and "text/html" in content_type and _looks_blocked(content):
        rendered = await _render_with_playwright(url)
        if rendered:
            r_content, _ = _extract(rendered, "text/html")
            if not _looks_blocked(r_content):
                content, extractor = r_content, "playwright"
                log.info("web_fetch: rung-2 (playwright) rescued %s (%d chars)", url, len(content))

    # Detect known bot-challenge / blocked pages and don't pass them downstream.
    # We still cache the result with a 'blocked' extractor + empty content so
    # repeated fetches within the TTL window are a cache hit (no re-fetch,
    # no LLM call). The loop's `if page is None or not page.content.strip()`
    # check skips when content is empty, which is what we want.
    if _looks_blocked(content):
        log.info(
            "web_fetch: %s returned blocked/empty content (status=%d, %d chars) — caching as 'blocked'",
            url,
            resp.status_code,
            len(content or ""),
        )
        await state.fetch_cache_put(
            url=url,
            content="",
            extractor="blocked",
            status_code=resp.status_code,
            bytes_fetched=len(resp.content),
            ttl_seconds=ttl_for(url),
        )
        return FetchedPage(
            url=url,
            content="",
            extractor="blocked",
            status_code=resp.status_code,
            bytes_fetched=len(resp.content),
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
    urls: list[str],
    state,
    *,
    force: bool = False,
    concurrency: int = 8,
) -> list[FetchedPage | None]:
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

        async def _one(u: str) -> FetchedPage | None:
            async with sem:
                return await web_fetch(u, state, force=force, client=client)

        return await asyncio.gather(*[_one(u) for u in urls])


# -------------------------------------------------------------------------
# arXiv Atom search
#
# A structured *discovery* call distinct from web_fetch: instead of scraping a
# rendered page, it queries arXiv's Atom API and returns typed metadata rows.
# Scout bots (see labfoundry/research/librarian/scouts.py) build on this to emit
# `source.discovered` descriptors. We deliberately do NOT touch fetch_cache here
# — that cache stores extracted page text keyed by URL; arXiv search results are
# query-keyed metadata and have a different shape/lifecycle.
# -------------------------------------------------------------------------

ARXIV_API_URL = "https://export.arxiv.org/api/query"

# arXiv's API is strict (~1 request / 3s) and tarpits sustained abuse — it drops
# our HTTPS connections (curl shows HTTP 000 / hangs) once we exceed it. So:
#   * serialize ALL arXiv calls behind one lock, spaced by _ARXIV_MIN_INTERVAL;
#   * when it starts failing, back off for _ARXIV_COOLDOWN so it can unblock us
#     (hammering a tarpit only extends the block).
_ARXIV_MIN_INTERVAL = float(os.environ.get("ARXIV_MIN_INTERVAL_S", "3.5"))
_ARXIV_COOLDOWN = float(os.environ.get("ARXIV_COOLDOWN_S", "900"))
_arxiv_lock = asyncio.Lock()
_arxiv_last_call = 0.0
_arxiv_cooldown_until = 0.0
_arxiv_fail_streak = 0

# Atom + arXiv XML namespaces, used to resolve qualified element names.
_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


class ArxivResult(BaseModel):
    """One paper from an arXiv Atom search.

    Fields mirror what the Librarian needs to ingest a paper (and what
    `ParsedDoc` later wants): identity (`arxiv_id`), display (`title`,
    `authors`, `abstract`), retrieval (`pdf_url`), and taxonomy
    (`categories`, `published`). All beyond `arxiv_id`/`title` are best-effort —
    the parser tolerates missing fields rather than dropping a whole entry.
    """

    arxiv_id: str
    title: str
    authors: list[str] = []
    abstract: str = ""
    pdf_url: str | None = None
    categories: list[str] = []
    published: date | None = None


def _arxiv_id_from_entry_id(raw: str) -> str:
    """Normalize an Atom entry <id> (e.g.
    'http://arxiv.org/abs/2401.01234v2') down to the bare arXiv id
    ('2401.01234v2'). Falls back to the raw string if it has no path."""
    raw = (raw or "").strip()
    tail = raw.rsplit("/abs/", 1)[-1]
    return tail or raw


def _parse_arxiv_atom(xml_text: str) -> list[ArxivResult]:
    """Parse an arXiv Atom feed body into ArxivResult rows.

    Robust to missing children: an entry with no title/abstract still yields a
    row (empty strings); an unparseable feed yields []. We never raise on a
    single malformed entry — discovery should degrade, not crash.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:  # noqa: BLE001 — a bad feed is "no results"
        log.warning("search_arxiv: could not parse Atom feed: %s", e)
        return []

    results: list[ArxivResult] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        id_el = entry.find("atom:id", _ATOM_NS)
        arxiv_id = _arxiv_id_from_entry_id(id_el.text if id_el is not None else "")
        if not arxiv_id:
            continue

        title_el = entry.find("atom:title", _ATOM_NS)
        title = " ".join((title_el.text or "").split()) if title_el is not None else ""

        summary_el = entry.find("atom:summary", _ATOM_NS)
        abstract = " ".join((summary_el.text or "").split()) if summary_el is not None else ""

        authors: list[str] = []
        for author_el in entry.findall("atom:author", _ATOM_NS):
            name_el = author_el.find("atom:name", _ATOM_NS)
            if name_el is not None and name_el.text and name_el.text.strip():
                authors.append(name_el.text.strip())

        # PDF link: a <link> with rel='related' title='pdf' or type
        # 'application/pdf'. Fall back to deriving it from the id.
        pdf_url: str | None = None
        for link_el in entry.findall("atom:link", _ATOM_NS):
            if link_el.get("title") == "pdf" or link_el.get("type") == "application/pdf":
                pdf_url = link_el.get("href")
                break
        if pdf_url is None and arxiv_id:
            pdf_url = f"http://arxiv.org/pdf/{arxiv_id}"

        # Categories: <arxiv:primary_category> + every <atom:category term=...>.
        categories: list[str] = []
        primary = entry.find("arxiv:primary_category", _ATOM_NS)
        if primary is not None and primary.get("term"):
            categories.append(primary.get("term"))
        for cat_el in entry.findall("atom:category", _ATOM_NS):
            term = cat_el.get("term")
            if term and term not in categories:
                categories.append(term)

        published: date | None = None
        pub_el = entry.find("atom:published", _ATOM_NS)
        if pub_el is not None and pub_el.text:
            # Format is e.g. '2024-01-02T18:00:00Z' — date prefix is enough.
            try:
                published = date.fromisoformat(pub_el.text.strip()[:10])
            except ValueError:
                published = None

        results.append(
            ArxivResult(
                arxiv_id=arxiv_id,
                title=title,
                authors=authors,
                abstract=abstract,
                pdf_url=pdf_url,
                categories=categories,
                published=published,
            )
        )

    return results


async def search_arxiv(
    query: str,
    max_results: int = 10,
    *,
    start: int = 0,
    sort: str = "submittedDate",
    client: httpx.AsyncClient | None = None,
) -> list[ArxivResult]:
    """Search the arXiv Atom API and return typed `ArxivResult` rows.

    `query` is passed through as arXiv's `search_query` (e.g. "all:retrieval
    augmented generation"). On any transport error or unparseable feed this
    returns [] — discovery is best-effort and must not raise into a scout loop.

    `sort` ∈ {"submittedDate", "relevance"} maps to arXiv's `sortBy`:
      * "submittedDate" (default) — newest-first; right for the STANDING sweep on
        broad topics, where a repeated sweep should surface fresh papers (paged via
        `start`) rather than re-return the same relevance top-N already in the corpus.
      * "relevance" — best-match-first; REQUIRED for TARGETED searches (acquire / the
        closure scout) on a NICHE query. With newest-first a niche query that matches
        little makes arXiv fall back to the newest submissions arXiv-WIDE — i.e. random
        off-topic papers. Relevance returns on-topic hits, or genuinely nothing (a real
        gap) instead of off-topic noise.

    `start` pages deeper into the result set — the discovery sweep rotates it to widen
    coverage. Pass `client` to reuse a shared httpx.AsyncClient (the scout passes one so
    a multi-topic sweep shares connections); otherwise one is created and closed here.
    """
    params = urlencode(
        {
            "search_query": query,
            "start": max(0, start),
            "max_results": max_results,
            "sortBy": sort,
            "sortOrder": "descending",
        }
    )
    url = f"{ARXIV_API_URL}?{params}"

    global _arxiv_last_call, _arxiv_cooldown_until, _arxiv_fail_streak
    if time.monotonic() < _arxiv_cooldown_until:
        # Backing off — arXiv rate-limited us; hammering only extends the block.
        return []

    async with _arxiv_lock:
        gap = _ARXIV_MIN_INTERVAL - (time.monotonic() - _arxiv_last_call)
        if gap > 0:
            await asyncio.sleep(gap)
        _arxiv_last_call = time.monotonic()
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(
                # Short timeout: when arXiv is rate-limiting us it tarpits the
                # connection (hangs), so fail fast rather than blocking the sweep
                # ~30s per topic before the cooldown engages.
                timeout=httpx.Timeout(8.0),
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            _arxiv_fail_streak += 1
            if _arxiv_fail_streak >= 3:
                _arxiv_cooldown_until = time.monotonic() + _ARXIV_COOLDOWN
                log.warning(
                    "search_arxiv: arXiv unreachable x%d (%s) — backing off %.0fs",
                    _arxiv_fail_streak,
                    e,
                    _ARXIV_COOLDOWN,
                )
            else:
                log.warning("search_arxiv(%r) failed: %s", query, e)
            return []
        finally:
            if owns_client:
                await client.aclose()
        _arxiv_fail_streak = 0  # reachable again

    return _parse_arxiv_atom(resp.text)
