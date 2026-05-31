"""
Plain async research functions.

These are the implementations of the labfoundry-research MCP tools. The MCP
server in server.py is a thin wrapper that exposes them over the protocol;
in-process callers (the Researcher handler) import these directly.

Sources:
    - Hacker News via Algolia public API
    - Web via SearXNG (self-hosted; URL from SEARXNG_URL env var)
    - Reddit via the public JSON API
    - Arbitrary URLs via httpx + BeautifulSoup
"""

from __future__ import annotations

import os

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

HTTP_TIMEOUT = 30.0
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8080")
USER_AGENT = "labfoundry-research/0.1 (autonomous research agent; contact: see project README)"


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    source: str


# ---------------------------------------------------------------------------
# Hacker News
# ---------------------------------------------------------------------------


async def search_hacker_news(query: str, limit: int = 10) -> list[SearchResult]:
    """
    Search Hacker News stories via the Algolia public API. Returns the top
    'limit' matching stories ordered by relevance.
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": query, "tags": "story", "hitsPerPage": limit},
        )
        resp.raise_for_status()
        data = resp.json()

    out: list[SearchResult] = []
    for hit in data.get("hits", []):
        title = hit.get("title") or hit.get("story_title") or ""
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        snippet = (hit.get("story_text") or hit.get("comment_text") or "")[:300]
        if title:
            out.append(SearchResult(title=title, url=url, snippet=snippet, source="hacker_news"))
    return out


# ---------------------------------------------------------------------------
# Web (SearXNG)
# ---------------------------------------------------------------------------


async def search_web(query: str, limit: int = 10) -> list[SearchResult]:
    """
    Search the web. Tries SearXNG when configured; otherwise scrapes
    DuckDuckGo HTML directly so research keeps working without infra.
    """
    # 1) SearXNG path (when reachable; otherwise immediately fall through)
    try:
        async with httpx.AsyncClient(
            timeout=5.0,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(
                f"{SEARXNG_URL}/search",
                params={"q": query, "format": "json", "categories": "general"},
            )
            if resp.status_code == 200:
                data = resp.json()
                out: list[SearchResult] = []
                for item in data.get("results", [])[:limit]:
                    out.append(
                        SearchResult(
                            title=item.get("title", ""),
                            url=item.get("url", ""),
                            snippet=(item.get("content", "") or "")[:300],
                            source="web",
                        )
                    )
                if out:
                    return out
    except Exception:
        pass  # fall through

    # 2) DuckDuckGo HTML fallback (no key, no infra). Returns [] on any
    # network/parse failure so callers get a consistent empty-list contract
    # rather than an exception from a best-effort fallback.
    try:
        return await _search_duckduckgo(query, limit)
    except (httpx.HTTPError, httpx.InvalidURL):
        return []


async def _search_duckduckgo(query: str, limit: int = 10) -> list[SearchResult]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=3.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        resp = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

    out: list[SearchResult] = []
    for result in soup.select("div.result")[: limit * 2]:
        a = result.select_one("a.result__a")
        if a is None:
            continue
        title = a.get_text(strip=True)
        href = a.get("href") or ""
        if not title or not href:
            continue
        snippet_el = result.select_one(".result__snippet")
        snippet = snippet_el.get_text(" ", strip=True)[:300] if snippet_el else ""
        out.append(SearchResult(title=title, url=str(href), snippet=snippet, source="web"))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

# Common English stop words + low-signal sentence connectors. We post-filter
# Reddit results to require at least one meaningful query term in the
# (title + snippet) of each result. Reddit's "relevance" sort is famously
# loose — without this filter it routinely returns /r/cats threads for
# "MCP adoption" queries (observed in T5).
_REDDIT_QUERY_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "what",
        "how",
        "are",
        "any",
        "can",
        "you",
        "your",
        "this",
        "that",
        "have",
        "has",
        "their",
        "from",
        "about",
        "into",
        "use",
        "uses",
        "using",
        "used",
        "will",
        "would",
        "could",
        "should",
        "all",
        "some",
        "more",
        "most",
        "other",
        "they",
        "them",
        "than",
        "out",
        "main",
        "key",
        "real",
        "ever",
        "good",
        "bad",
        "much",
        "many",
        "current",
        "popular",
        "common",
        # research-prompt-shaped filler
        "tool",
        "tools",
        "thing",
        "things",
        "ways",
        "way",
        "data",
    }
)


def _meaningful_terms(query: str) -> list[str]:
    """Tokenise a free-text query into the keywords that have to appear in
    a result's text for it to be relevant. Length >= 3, alphanumeric, not a
    stop word. Lowercased. Preserves order, deduped."""
    import re

    out: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-_/.]*", query):
        t = raw.lower()
        if len(t) < 3 or t in _REDDIT_QUERY_STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _reddit_relevant(hit_title: str, hit_snippet: str, terms: list[str]) -> bool:
    """A hit is relevant if at least one meaningful term from the query
    appears in title OR snippet. Token-substring match (so 'mcp' matches
    'MCP', 'mcps', '#mcp'). Permissive on purpose — we just want to filter
    /r/cats out, not require an exact phrase."""
    if not terms:
        return True  # nothing to filter against; let it through
    haystack = (hit_title + " \n " + hit_snippet).lower()
    return any(t in haystack for t in terms)


async def search_reddit(
    query: str,
    subreddit: str | None = None,
    limit: int = 10,
) -> list[SearchResult]:
    """
    Search Reddit. If 'subreddit' is provided, restricts to that subreddit;
    otherwise searches all of Reddit. Uses the public JSON API; rate-limited
    by Reddit (~60/min unauthenticated).

    Post-filters by query-term overlap with title+snippet because Reddit's
    relevance sort returns wildly off-topic threads otherwise.
    """
    if subreddit:
        url = f"https://www.reddit.com/r/{subreddit}/search.json"
        params = {"q": query, "restrict_sr": "true", "limit": limit * 3, "sort": "relevance"}
    else:
        url = "https://www.reddit.com/search.json"
        params = {"q": query, "limit": limit * 3, "sort": "relevance"}

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    terms = _meaningful_terms(query)
    out: list[SearchResult] = []
    dropped: list[str] = []
    for child in data.get("data", {}).get("children", []):
        post = child.get("data", {}) or {}
        title = post.get("title", "")
        if not title:
            continue
        selftext = post.get("selftext", "")[:300]
        snippet = selftext or (
            f"r/{post.get('subreddit', '')} — {post.get('num_comments', 0)} comments, score {post.get('score', 0)}"
        )
        if not _reddit_relevant(title, selftext, terms):
            dropped.append(title[:60])
            continue
        out.append(
            SearchResult(
                title=title,
                url=f"https://www.reddit.com{post.get('permalink', '')}",
                snippet=snippet,
                source="reddit",
            )
        )
        if len(out) >= limit:
            break

    if dropped:
        import logging

        logging.getLogger(__name__).debug(
            "search_reddit dropped %d off-topic results for %r: %s",
            len(dropped),
            query,
            dropped[:5],
        )
    return out


# ---------------------------------------------------------------------------
# URL fetch
# ---------------------------------------------------------------------------


async def fetch_url(url: str) -> str:
    """
    Fetch a URL and return cleaned text. For HTML, strips boilerplate
    (script/style/nav/footer/header/aside). Truncates at 10K characters.
    Follows redirects.
    """
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")

    if "text/html" in content_type:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
    else:
        text = resp.text

    return text[:10_000]
