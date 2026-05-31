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
     It calls the source's search/list API (via labfoundry.research.fetcher or a
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

from pydantic import BaseModel

from labfoundry.research.fetcher import search_arxiv
from labfoundry.research.librarian.schemas import DocumentKind

log = logging.getLogger(__name__)


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
