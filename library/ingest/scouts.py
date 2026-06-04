"""
Scout bots — the Library's DISCOVERY layer (MIMIR_WARDEN_SCOPE.md §3).

The Director's design: discovery is done by MANY small, autonomous "scout"
bots — one per source (arXiv, GitHub, OpenML, …) — that look for new research
material and surface it as `source.discovered` events feeding the Librarian.
This is deliberately NOT one monolithic sweep: each scout owns exactly one
source, can be scheduled / rate-limited / disabled independently, and is small
enough to reason about and test in isolation.

THE SCOUT PATTERN (copy this for github/openml/…):

  1. A scout is a PURE async function:
         async def scout_<source>(topics, ...) -> list[SourceDescriptor]
     It calls the source's search/list API (via library.ingest.fetcher or a
     source-specific client), normalizes each hit into a `SourceDescriptor`, and
     RETURNS the list. That's it.

  2. A scout does NOT emit events and does NOT touch the DB. Event emission
     (`source.discovered`) and dedupe-against-already-ingested are the
     Librarian handler's job. Keeping scouts side-effect-free makes them
     trivially testable (no DB/bus fixture, no network — mock the fetcher) and
     lets the handler decide policy (which descriptors to act on) in one place.

  3. Each scout sets `source_kind` to its own source name and `canonical_key`
     to that source's stable id (arXiv id, "owner/repo", OpenML dataset id, …),
     so the handler can dedupe across runs by (source_kind, canonical_key)
     without knowing the source's internals.

  4. `kind` is the ingest-side `DocumentKind` ('paper' for arXiv/GitHub-papers,
     'code' for repos, 'dataset' for OpenML, …) — it tells the Librarian which
     ingest path the descriptor belongs on.
"""

from __future__ import annotations

import logging
import os

import httpx
from pydantic import BaseModel

from library.ingest.fetcher import search_arxiv
from library.ingest.schemas import DocumentKind

log = logging.getLogger(__name__)

_SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8081")
_GITHUB_API = "https://api.github.com"
_SCOUT_UA = "labfoundry-scout"


# -------------------------------------------------------------------------
# SourceDescriptor — a scout's output row.
#
# A discovered candidate for ingestion, source-agnostic on purpose: the
# Librarian handler turns each descriptor into a `source.discovered` event and
# (after dedupe) an ingest job, without caring which scout produced it.
# `canonical_key` is the source's stable id (deduped per source_kind); the
# typed id fields (arxiv_id/doi) and url are best-effort enrichments the ingest
# path can use directly.
# -------------------------------------------------------------------------


class SourceDescriptor(BaseModel):
    kind: DocumentKind  # ingest taxonomy: 'paper' for arXiv
    source_kind: str  # which scout found it, e.g. 'arxiv'
    canonical_key: str  # stable per-source id; dedupe key
    url: str | None = None
    arxiv_id: str | None = None
    doi: str | None = None
    title: str | None = None
    why: str | None = None  # short note on why this surfaced (e.g. topic)


# -------------------------------------------------------------------------
# arXiv scout — the first scout; reference shape for the rest.
# -------------------------------------------------------------------------


async def scout_arxiv(
    topics: list[str],
    per_topic: int = 5,
) -> list[SourceDescriptor]:
    """Scout arXiv for papers matching `topics`.

    Queries arXiv once per topic (up to `per_topic` results each), dedupes by
    arXiv id across topics (first topic to surface a paper wins, and its topic
    is recorded in `why`), and returns `SourceDescriptor`s with kind='paper',
    source_kind='arxiv', canonical_key=<arxiv_id>.

    PURE: returns descriptors only. It does NOT emit `source.discovered` events
    and does NOT touch the DB — the Librarian handler wires emission + dedupe
    against already-ingested docs. That keeps this scout testable and decoupled.
    """
    seen: dict[str, SourceDescriptor] = {}
    for topic in topics:
        query = topic if ":" in topic else f"all:{topic}"
        try:
            results = await search_arxiv(query, max_results=per_topic)
        except Exception as e:  # noqa: BLE001 — one bad topic must not sink the sweep
            log.warning("scout_arxiv: topic %r failed: %s", topic, e)
            continue

        for r in results:
            if r.arxiv_id in seen:
                continue
            seen[r.arxiv_id] = SourceDescriptor(
                kind="paper",
                source_kind="arxiv",
                canonical_key=r.arxiv_id,
                url=r.pdf_url,
                arxiv_id=r.arxiv_id,
                doi=None,
                title=r.title or None,
                why=f"arxiv topic: {topic}",
            )

    return list(seen.values())


# -------------------------------------------------------------------------
# Web scout — SearXNG (kind='web'). Self-contained (no agents.* import) so the
# library layer stays independent. Best-effort: empty list on any failure.
# -------------------------------------------------------------------------


async def scout_web(topics: list[str], per_topic: int = 5) -> list[SourceDescriptor]:
    """Scout the open web (SearXNG) for pages on `topics`. canonical_key is the
    URL. PURE: returns descriptors only (the collector emits/dedupes)."""
    seen: dict[str, SourceDescriptor] = {}
    async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": _SCOUT_UA}) as client:
        for topic in topics:
            try:
                resp = await client.get(
                    f"{_SEARXNG_URL}/search",
                    params={"q": topic, "format": "json", "categories": "general"},
                )
                if resp.status_code != 200:
                    # A non-200 means SearXNG is down, rate-limiting, or — as we
                    # once hit — SEARXNG_URL points at the wrong service entirely
                    # (a 404 from whatever else grabbed the port). Never silent:
                    # a misconfigured search backend should be loud, not yield [].
                    log.warning(
                        "scout_web: SearXNG at %s returned HTTP %d for %r — "
                        "web discovery degraded (check SEARXNG_URL / container)",
                        _SEARXNG_URL, resp.status_code, topic,
                    )
                    continue
                results = resp.json().get("results", [])
            except Exception as e:  # noqa: BLE001 — one bad topic must not sink the sweep
                log.warning("scout_web: topic %r failed (%s): %s", topic, _SEARXNG_URL, e)
                continue
            for r in results[:per_topic]:
                url = r.get("url")
                if not url or url in seen:
                    continue
                seen[url] = SourceDescriptor(
                    kind="web",
                    source_kind="web",
                    canonical_key=url,
                    url=url,
                    title=(r.get("title") or None),
                    why=f"web topic: {topic}",
                )
    return list(seen.values())


# -------------------------------------------------------------------------
# GitHub scout — repo search (kind='code'). Uses GITHUB_TOKEN if set.
# -------------------------------------------------------------------------


async def scout_github(topics: list[str], per_topic: int = 5) -> list[SourceDescriptor]:
    """Scout GitHub for repos matching `topics` (sorted by stars). canonical_key
    is 'owner/repo'. PURE: returns descriptors only. Best-effort."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": _SCOUT_UA}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    seen: dict[str, SourceDescriptor] = {}
    async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
        for topic in topics:
            try:
                resp = await client.get(
                    f"{_GITHUB_API}/search/repositories",
                    params={"q": topic, "sort": "stars", "per_page": per_topic},
                )
                items = resp.json().get("items", []) if resp.status_code == 200 else []
            except Exception as e:  # noqa: BLE001 — one bad topic must not sink the sweep
                log.warning("scout_github: topic %r failed: %s", topic, e)
                continue
            for it in items:
                full = it.get("full_name")
                if not full or full in seen:
                    continue
                seen[full] = SourceDescriptor(
                    kind="code",
                    source_kind="github",
                    canonical_key=full,
                    url=it.get("html_url"),
                    title=full,
                    why=f"github topic: {topic}",
                )
    return list(seen.values())
