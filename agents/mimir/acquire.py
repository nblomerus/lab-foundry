"""
The acquisition / pull path (MIMIR_WARDEN_SCOPE §5).

The *demand* side of the Library: when an agent working a claim finds it needs a
specific source it doesn't have, it asks Mimir to go get it. Mimir adjudicates
(deterministically) and, if it passes, ingests through the same trust gate as
discovery.

    request_acquire(...)  ->  acquire.requested event
                                     |
                          Mimir: handle_acquire_requested
                                     |
        cap -> resolve -> dedupe -> ingest_source (stage/classify/finalize)
                                     |
                  acquire.fulfilled / acquire.rejected  (reason carried verbatim)

Allowed requesters (the Plane-2 allow-list): the PI (mission gaps), the
Researcher (evidence for a specific claim), and Novelty (prior-art candidates).
Event-based: the cap + audit live on the events bus, so there's no new table.
"""

from __future__ import annotations

import hashlib
import logging
import os

from pydantic import BaseModel, Field

from library.ingest.scouts import SourceDescriptor, scout_arxiv

log = logging.getLogger(__name__)

# Plane-2 allow-list: only these roles may ask Mimir to acquire.
ALLOWED_REQUESTERS = frozenset({"pi", "researcher", "novelty"})


class AcquireRequest(BaseModel):
    """An agent's request for Mimir to acquire a specific source.

    Supply ONE identifier (arxiv_id / url / doi) for a precise fetch, or a
    `query` for Mimir to find the best match. `why` (>=30 chars) is the abuse
    pre-filter — a request must justify itself."""

    requester: str
    kind: str = "paper"
    why: str = Field(..., min_length=30)
    claim_id: int | None = None
    arxiv_id: str | None = None
    url: str | None = None
    doi: str | None = None
    query: str | None = None


def _req_target_id(req: AcquireRequest) -> int:
    """Stable bigint for the acquire.requested event's target_id (dedupe key part)."""
    key = req.arxiv_id or req.url or req.doi or req.query or req.why
    return int.from_bytes(hashlib.blake2b(f"{req.requester}:{key}".encode(), digest_size=7).digest(), "big")


async def request_acquire(state, req: AcquireRequest) -> None:
    """The lever an allowed agent calls to ask Mimir for a source. Validates the
    requester against the allow-list and emits an `acquire.requested` event for
    Mimir to adjudicate. Raises ValueError if the requester isn't allowed."""
    if req.requester not in ALLOWED_REQUESTERS:
        raise ValueError(f"requester {req.requester!r} not allowed to acquire (allow-list: {sorted(ALLOWED_REQUESTERS)})")
    await state.emit_corpus_event(
        "acquire.requested",
        target_type="acquire",
        target_id=_req_target_id(req),
        payload=req.model_dump(),
        dedup_key=f"acquire-{req.requester}-{_req_target_id(req)}",
    )


async def _resolve_descriptor(req: AcquireRequest) -> SourceDescriptor | None:
    """Turn a request into a concrete SourceDescriptor. Explicit identifiers map
    directly; a `query` is resolved to the top arXiv hit. Returns None if nothing
    resolves."""
    if req.arxiv_id:
        return SourceDescriptor(
            kind="paper",
            source_kind="arxiv",
            canonical_key=req.arxiv_id,
            url=f"https://arxiv.org/abs/{req.arxiv_id}",
            arxiv_id=req.arxiv_id,
            why=req.why,
        )
    if req.doi:
        return SourceDescriptor(
            kind=req.kind,
            source_kind="doi",
            canonical_key=req.doi,
            url=f"https://doi.org/{req.doi}",
            doi=req.doi,
            why=req.why,
        )
    if req.url:
        return SourceDescriptor(
            kind=req.kind,
            source_kind="web",
            canonical_key=req.url,
            url=req.url,
            why=req.why,
        )
    if req.query:
        hits = await scout_arxiv([req.query], per_topic=1)
        if hits:
            d = hits[0]
            return d.model_copy(update={"why": req.why})
    return None


async def _reply(state, req: AcquireRequest, *, status: str, reason: str, document_id: int | None = None) -> dict:
    """Emit the acquire reply event and return the structured result."""
    event = "acquire.fulfilled" if status in {"fulfilled", "already_have"} else "acquire.rejected"
    await state.emit_corpus_event(
        event,
        target_type="acquire",
        target_id=document_id if document_id is not None else _req_target_id(req),
        payload={
            "requester": req.requester,
            "claim_id": req.claim_id,
            "status": status,
            "reason": reason,
            "document_id": document_id,
        },
        dedup_key=f"acquirereply-{_req_target_id(req)}",
    )
    log.info("mimir acquire: %s for %s — %s", status, req.requester, reason)
    return {"status": status, "reason": reason, "document_id": document_id}


async def handle_acquire_requested(event: dict, dispatcher) -> dict | None:
    """Triggered by `acquire.requested`. Adjudicate deterministically (cap →
    resolve → dedupe → trust-gated ingest) and reply."""
    from agents.mimir.handler import _loop_enabled, ingest_source

    if not _loop_enabled():
        return None

    payload = event.get("payload") or {}
    try:
        req = AcquireRequest(**payload)
    except Exception as e:  # noqa: BLE001 — a malformed request is rejected, not fatal
        log.warning("mimir acquire: malformed request %r: %s", payload, e)
        return {"skipped": True, "reason": "malformed request"}

    state = dispatcher.state

    # (1) per-requester daily cap — deterministic flood guard.
    try:
        cap = int(os.environ.get("MIMIR_ACQUIRE_CAP_PER_AGENT", "20"))
    except ValueError:
        cap = 20
    if await state.count_acquires_today(req.requester) > cap:
        return await _reply(state, req, status="rate_limited", reason=f"daily acquire cap ({cap}) reached")

    # (2) resolve to a concrete source.
    desc = await _resolve_descriptor(req)
    if desc is None:
        return await _reply(state, req, status="rejected", reason="could not resolve request to a source")

    # (3) dedupe against the corpus — cheap, avoids a re-fetch.
    if await state.document_exists(desc.source_kind, desc.canonical_key):
        return await _reply(state, req, status="already_have", reason="already in the corpus")

    # (4) trust-gated ingest (stage → classify → certify/finalize, or block).
    result = await ingest_source(
        desc.model_dump(),
        state,
        router=getattr(dispatcher, "router", None),
        curator=getattr(dispatcher, "curator", None),
        session=getattr(dispatcher, "session", None),
    )
    if result.get("decision") == "approve":
        return await _reply(
            state,
            req,
            status="fulfilled",
            reason=f"ingested at {result.get('tier')}",
            document_id=result.get("document_id"),
        )
    if result.get("decision") == "block":
        return await _reply(
            state, req, status="rejected", reason=result.get("reason", "blocked"), document_id=result.get("document_id")
        )
    # stage skip / dedupe / failure
    return await _reply(state, req, status="rejected", reason=str(result.get("reason") or result))
