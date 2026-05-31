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

from library.ingest.scouts import scout_arxiv, scout_github, scout_web

log = logging.getLogger(__name__)

# Which scouts the sweep runs (LIBRARY_SCOUTS env, comma-separated; default arxiv
# only — web/github hit external APIs and may need keys/infra). Tests patch this
# dict (monkeypatch.setitem) to inject a fake scout.
_SCOUTS = {"arxiv": scout_arxiv, "web": scout_web, "github": scout_github}


def _enabled_scouts() -> list:
    names = [n.strip() for n in os.environ.get("LIBRARY_SCOUTS", "arxiv").split(",") if n.strip()]
    chosen = [_SCOUTS[n] for n in names if n in _SCOUTS]
    return chosen or [_SCOUTS["arxiv"]]


# Fallback discovery topics when LIBRARY_TOPICS is unset.
_DEFAULT_TOPICS = (
    "large language models",
    "retrieval augmented generation",
    "ai agents",
)


def discovery_topics() -> list[str]:
    """The standing FRONTIER topics (LIBRARY_TOPICS env, else default) — broad
    field coverage so the sweep still catches movement beyond the active agenda."""
    raw = os.environ.get("LIBRARY_TOPICS", "")
    topics = [t.strip() for t in raw.split(",") if t.strip()]
    return topics or list(_DEFAULT_TOPICS)


async def default_sweep_topics(state) -> list[str]:
    """Topics that TRACK THE AGENDA: the active claims' statements (what the lab
    is working on now) merged with the standing frontier set. This is how the PI
    steers discovery *implicitly* — by framing claims — without ever touching the
    collectors. Falls back to the frontier set alone when there are no claims yet.
    """
    claim_topics: list[str] = []
    try:
        claims = await state.get_active_claims(limit=6)
        claim_topics = [c.statement.strip() for c in claims if (c.statement or "").strip()]
    except Exception:  # noqa: BLE001 — claim steering is best-effort, never blocks the sweep
        log.exception("collectors: get_active_claims failed; sweeping frontier only")

    merged: list[str] = []
    seen: set[str] = set()
    for t in [*claim_topics, *discovery_topics()]:
        key = t.lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(t)
    return merged[:10]


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
    topics = topics or await default_sweep_topics(state)

    descriptors = []
    for scout in _enabled_scouts():
        try:
            descriptors.extend(await scout(topics, per_topic=per_topic))
        except Exception:  # noqa: BLE001 — one scout failing must not sink the sweep
            log.exception("collectors: scout %s failed", getattr(scout, "__name__", scout))

    new: list[dict] = []
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
        new.append({"title": d.title, "arxiv_id": d.arxiv_id, "why": d.why})

    if new:
        # A digest of what just surfaced — the PI's "pulse" of the field. Emitted
        # per sweep (no dedup); a future PI step can consult library.trends.
        await state.emit_corpus_event(
            "library.trends",
            target_type="trends",
            target_id=0,
            payload={"topics": topics, "count": len(new), "new": new[:20]},
        )

    log.info("discovery sweep: %d/%d new sources (topics=%s)", len(new), len(descriptors), topics)
    return {"scanned": len(descriptors), "discovered": len(new), "topics": topics}
