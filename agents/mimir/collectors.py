"""Mimir's data collectors — discovery sweeps that feed `source.discovered`.

The scouts (library.ingest.scouts) are pure source-finders. This layer runs them
over standing topics, dedupes against the corpus, and emits one
`source.discovered` per NEW source — which Mimir then stages, trust-gates, and
ingests. Keeping emission + dedupe here (not in the scouts) is the §3 design:
scouts stay side-effect-free and trivially testable.

Topics come from LIBRARY_TOPICS (comma-separated env) or a small default set.
Today the only built scout is scout_arxiv; scout_web/scout_github slot in here
the same way (run them and merge the descriptor lists).
"""

from __future__ import annotations

import hashlib
import logging
import os

from library.ingest.scouts import scout_arxiv

log = logging.getLogger(__name__)

# Fallback discovery topics when LIBRARY_TOPICS is unset.
_DEFAULT_TOPICS = (
    "large language models",
    "retrieval augmented generation",
    "ai agents",
)


def discovery_topics() -> list[str]:
    """The standing topics the collectors sweep (LIBRARY_TOPICS env, else default)."""
    raw = os.environ.get("LIBRARY_TOPICS", "")
    topics = [t.strip() for t in raw.split(",") if t.strip()]
    return topics or list(_DEFAULT_TOPICS)


def _source_target_id(canonical_key: str) -> int:
    """A stable positive bigint derived from a source's canonical_key.

    target_id is part of the events unique key, so giving a not-yet-ingested
    source a deterministic id lets a re-emitted `source.discovered` dedupe at the
    event level (corpus-level dedupe via document_exists is the primary guard)."""
    return int.from_bytes(hashlib.blake2b(canonical_key.encode(), digest_size=7).digest(), "big")


async def run_discovery_sweep(topics: list[str] | None, state, *, per_topic: int = 5) -> dict:
    """Run the scouts over `topics` and emit `source.discovered` for sources NOT
    already in the corpus (skip-if-exists avoids a wasted re-fetch every sweep).

    Returns {"scanned": <descriptors found>, "discovered": <new emitted>, "topics": [...]}.
    """
    topics = topics or discovery_topics()
    descriptors = await scout_arxiv(topics, per_topic=per_topic)

    discovered = 0
    for d in descriptors:
        if await state.document_exists(d.source_kind, d.canonical_key):
            continue
        await state.emit_corpus_event(
            "source.discovered",
            target_type="source",
            target_id=_source_target_id(d.canonical_key),
            payload={"source": d.model_dump()},
            dedup_key=f"discovered-{d.source_kind}-{d.canonical_key}",
        )
        discovered += 1

    log.info("discovery sweep: %d/%d new sources (topics=%s)", discovered, len(descriptors), topics)
    return {"scanned": len(descriptors), "discovered": discovered, "topics": topics}
