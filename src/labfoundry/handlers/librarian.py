"""
Librarian handlers — the two dispatcher entry points into the ingest loop
(MIMIR_WARDEN_SCOPE.md §3).

The ingest pipeline is split at the Mimir trust gate (the mechanical "never
self-certify"):

  - `handle_source_discovered`  (on `source.discovered`)   -> phase A
  - `handle_ingest_approved`    (on `mimir.ingest_approved`) -> phase B

Both are gated on `LIBRARIAN_LOOP` (env, default OFF) and return None when off,
matching the existing *_LOOP gating pattern (RESEARCHER_LOOP, ADVERSARY_LOOP,
AUDITOR_LOOP, PLANNER_LOOP). Handlers reach state via `dispatcher.state`, the
same way every other handler does.

DEV ESCAPE — `LIBRARIAN_AUTO_APPROVE` (env, default OFF). Mimir (Phase 3) does
not exist yet, so when this is on AND phase A produced a NEW document,
`handle_source_discovered` emits `mimir.ingest_approved` itself so the pipeline
can run end-to-end before Mimir lands. With it OFF, a discovered source parses
and then STOPS at the gate (the production behaviour).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _loop_enabled() -> bool:
    """The LIBRARIAN_LOOP gate (default OFF). Mirrors RESEARCHER_LOOP etc."""
    return os.environ.get("LIBRARIAN_LOOP", "").lower() in {"v1", "on"}


def _auto_approve_enabled() -> bool:
    """The dev escape: phase A self-emits mimir.ingest_approved (default OFF)."""
    return os.environ.get("LIBRARIAN_AUTO_APPROVE", "").lower() in {"1", "on", "true"}


async def handle_source_discovered(event: dict, dispatcher) -> dict | None:
    """Triggered by `source.discovered`. Runs phase A on the source carried
    inline in the event payload (the source rides the event — there is no
    library-task producer to claim from).

    When `LIBRARIAN_AUTO_APPROVE` is on AND phase A produced a NEW document,
    self-emits `mimir.ingest_approved` so the pipeline runs end-to-end before
    Mimir exists.
    """
    if not _loop_enabled():
        return None

    from labfoundry.research.librarian.loop import run_ingest_phase_a

    source = (event.get("payload") or {}).get("source")
    if not source:
        log.warning("librarian: source.discovered event %s has no payload.source", event.get("id"))
        return {"skipped": True, "reason": "no source in payload"}

    state = dispatcher.state
    try:
        result = await run_ingest_phase_a(source, state, dispatcher=dispatcher)
    except Exception as e:  # noqa: BLE001 — one source failure is non-fatal to the harness
        log.exception("librarian phase A failed for source %r", source)
        return {"failed": True, "reason": str(e)[:200]}

    doc_id = result.get("document_id")
    # Auto-approve ONLY a freshly-staged document (awaiting Mimir), never a
    # dedupe hit or a skip — re-emitting on a dedupe would re-run phase B.
    if _auto_approve_enabled() and doc_id is not None and result.get("awaiting") == "mimir":
        await state.emit_corpus_event(
            "mimir.ingest_approved",
            target_type="document",
            target_id=doc_id,
            payload={
                "auto_approved": True,
                "n_chunks": result.get("n_chunks"),
            },
            dedup_key=f"autoapprove-{doc_id}",
        )
        log.info("librarian: AUTO_APPROVE emitted mimir.ingest_approved for doc %s", doc_id)
        result = {**result, "auto_approved": True}

    return result


async def handle_ingest_approved(event: dict, dispatcher) -> dict | None:
    """Triggered by `mimir.ingest_approved`. Runs phase B (embed -> KG -> flip
    queryable) for the approved document. The document id rides the event's
    `target_id` (corpus events are document-targeted)."""
    if not _loop_enabled():
        return None

    from labfoundry.research.librarian.loop import run_ingest_phase_b

    document_id = event.get("target_id")
    if document_id is None:
        log.warning("librarian: mimir.ingest_approved event %s has no target_id", event.get("id"))
        return {"skipped": True, "reason": "no target_id"}

    state = dispatcher.state
    try:
        return await run_ingest_phase_b(document_id, state)
    except Exception as e:  # noqa: BLE001 — one document failure is non-fatal to the harness
        log.exception("librarian phase B failed for doc %s", document_id)
        return {"document_id": document_id, "failed": True, "reason": str(e)[:200]}
