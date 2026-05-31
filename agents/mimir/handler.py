"""
Mimir — Warden of the Library (MIMIR_WARDEN_SCOPE §4).

ONE agent owns corpus ingest AND trust. On `source.discovered` Mimir runs the
deterministic ingest tools with the trust gate between them — there is no
separate Librarian agent and no `mimir.ingest_approved` event handshake:

    stage_source  ->  classify_trust  ->  write trust + certification
        |                                   |
        | (cheap: fetch/parse/chunk/stage)  +-- approve -> embed_and_finalize  (document.ingested)
        |                                   +-- block   -> quarantine          (mimir.ingest_blocked)

classify_trust is ~95% zero-token deterministic, so Mimir rarely needs the model.
Gated on MIMIR_LOOP (env, default OFF), mirroring the other *_LOOP gates.

FOLLOW-UPS (deferred, all safe to add later): the LLM tie-breaker for the
web_reputable/web_unknown boundary (needs_llm); DOI/GitHub signal resolution
(so peer_reviewed/official_repo can be reached); license capture at stage time
(so the license hard-gate can fire); and the acquire/pull path.
"""

from __future__ import annotations

import logging
import os

from library.ingest.pipeline import embed_and_finalize, stage_source
from library.trust import DocMeta, classify_trust

log = logging.getLogger(__name__)


def _loop_enabled() -> bool:
    """The MIMIR_LOOP gate (default OFF), mirroring RESEARCHER_LOOP etc."""
    return os.environ.get("MIMIR_LOOP", "").lower() in {"v1", "on"}


def _doc_meta(doc: dict) -> DocMeta:
    """Build the trust signals from a staged `documents` row.

    DOI/GitHub resolution is a follow-up, so doi_resolves stays False here — an
    unresolved DOI falls through the ladder rather than over-crediting itself to
    peer_reviewed. arXiv (preprint), reputable-domain (web_reputable) and the
    license gate are the live deterministic signals today.
    """
    return DocMeta(
        source_url=doc.get("source_url"),
        doi=doc.get("doi"),
        doi_resolves=False,
        arxiv_id=doc.get("arxiv_id"),
        license=doc.get("license"),
    )


async def handle_source_discovered(event: dict, dispatcher) -> dict | None:
    """Triggered by `source.discovered`. Stage the source, classify its trust,
    write the verdict + an immutable certification, then finalize (approve) or
    quarantine (block). The source rides the event payload."""
    if not _loop_enabled():
        return None

    source = (event.get("payload") or {}).get("source")
    if not source:
        log.warning("mimir: source.discovered event %s has no payload.source", event.get("id"))
        return {"skipped": True, "reason": "no source in payload"}

    state = dispatcher.state
    try:
        staged = await stage_source(source, state)
    except Exception as e:  # noqa: BLE001 — one source failure is non-fatal to the harness
        log.exception("mimir: stage_source failed for %r", source)
        return {"failed": True, "reason": str(e)[:200]}

    doc_id = staged.get("document_id")
    if doc_id is None or staged.get("awaiting") != "mimir":
        return staged  # skipped / deduped — nothing fresh to certify

    doc = await state.get_document(doc_id)
    tc = classify_trust(_doc_meta(doc))

    if tc.blocked:
        await state.set_document_trust(doc_id, tier="quarantined", trust_state="quarantined", status="blocked")
        await state.append_certification(
            doc_id,
            decision="block",
            to_tier="quarantined",
            to_state="quarantined",
            signals=tc.signals,
            used_llm=False,
            reasons=tc.reason,
        )
        await state.emit_corpus_event(
            "mimir.ingest_blocked",
            target_type="document",
            target_id=doc_id,
            payload={"tier": tc.tier, "reasons": tc.reason},
            dedup_key=f"blocked-{doc_id}",
        )
        log.info("mimir: BLOCKED doc %s — %s", doc_id, tc.reason)
        return {"document_id": doc_id, "decision": "block", "reason": tc.reason}

    # APPROVE — deterministic. (The needs_llm tie-breaker that could bump an
    # ambiguous web_unknown to web_reputable is a follow-up; until then we admit
    # at the deterministic floor, which is the safe under-credit.)
    await state.set_document_trust(doc_id, tier=tc.tier, trust_state="provisional", status="certified")
    await state.append_certification(
        doc_id,
        decision="approve",
        to_tier=tc.tier,
        to_state="provisional",
        signals=tc.signals,
        used_llm=False,
        reasons=tc.reason,
    )
    result = await embed_and_finalize(doc_id, state)
    log.info("mimir: APPROVED doc %s at tier=%s — %s", doc_id, tc.tier, tc.reason)
    return {"document_id": doc_id, "decision": "approve", "tier": tc.tier, **result}
