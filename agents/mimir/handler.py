"""
Mimir — Warden of the Library (MIMIR_WARDEN_SCOPE §4).

ONE agent owns corpus ingest AND trust. On `source.discovered` Mimir runs the
deterministic ingest tools with the trust gate between them — there is no
separate Librarian agent and no `mimir.ingest_approved` event handshake:

    stage_source  ->  classify_trust  ->  write trust + certification
        |                                   |
        | (cheap: fetch/parse/chunk/stage)  +-- approve -> embed_and_finalize  (document.ingested)
        |                                   +-- block   -> quarantine          (mimir.ingest_blocked)

classify_trust is ~95% zero-token deterministic; the lone LLM step (the
web_reputable/web_unknown tie-breaker, mimir.certify) runs only when needs_llm
fires AND a router/curator are wired. Gated on MIMIR_LOOP (env, default OFF).

Remaining deferred: license capture at stage time — the license hard-gate is
built, but no source exposes a license to feed it yet.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from library.ingest.pipeline import embed_and_finalize, stage_source
from library.trust import DocMeta, classify_trust


class MimirVerdict(BaseModel):
    """Mimir's LLM verdict — used ONLY for the ambiguous web_unknown boundary.
    The LLM may not mint a tier above web_reputable (top tiers need a verifiable
    identifier, settled deterministically)."""

    decision: Literal["approve", "block"]
    tier: Literal["user_asserted", "web_unknown", "web_reputable"]
    reasons: str = Field(..., min_length=20)


log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 5.0


def _loop_enabled() -> bool:
    """The MIMIR_LOOP gate (default OFF), mirroring RESEARCHER_LOOP etc."""
    return os.environ.get("MIMIR_LOOP", "").lower() in {"v1", "on"}


def _doc_meta(doc: dict) -> DocMeta:
    """Build the base trust signals from a staged `documents` row. The probe-
    backed signals (doi_resolves, github_*) are filled by _resolve_signals."""
    return DocMeta(
        source_url=doc.get("source_url"),
        doi=doc.get("doi"),
        doi_resolves=False,
        arxiv_id=doc.get("arxiv_id"),
        license=doc.get("license"),
    )


async def _doi_resolves(doi: str) -> bool:
    """HEAD https://doi.org/<doi> — True if it resolves (<400). Best-effort: a
    resolving DOI is what lifts a doc to peer_reviewed, so a probe failure stays
    conservative (False -> falls through the ladder)."""
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, follow_redirects=True) as client:
            resp = await client.head(f"https://doi.org/{doi}")
            return resp.status_code < 400
    except Exception:  # noqa: BLE001 — best-effort probe
        return False


async def _github_repo_signals(url: str | None) -> tuple[bool | None, int | None]:
    """For a github.com/<owner>/<repo> URL, return (has_release, days_since_push)
    via the GitHub API (GITHUB_TOKEN used if set). (None, None) on any failure."""
    parts = [p for p in urlparse(url or "").path.split("/") if p]
    if len(parts) < 2:
        return None, None
    owner, repo = parts[0], parts[1]
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "labfoundry-mimir"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, headers=headers) as client:
            meta_resp = await client.get(f"https://api.github.com/repos/{owner}/{repo}")
            if meta_resp.status_code != 200:
                return None, None
            pushed = meta_resp.json().get("pushed_at")
            days = None
            if pushed:
                dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
                days = (datetime.now(UTC) - dt).days
            rel = await client.get(f"https://api.github.com/repos/{owner}/{repo}/releases", params={"per_page": 1})
            has_release = rel.status_code == 200 and bool(rel.json())
            return has_release, days
    except Exception:  # noqa: BLE001 — best-effort probe
        return None, None


async def _resolve_signals(meta: DocMeta) -> None:
    """Best-effort network probes that fill the signals classify_trust needs for
    the top tiers (peer_reviewed via a resolving DOI, official_repo via an active
    GitHub repo). Mutates `meta`; any failure leaves the conservative default."""
    if meta.doi:
        meta.doi_resolves = await _doi_resolves(meta.doi)
    host = (urlparse(meta.source_url or "").hostname or "").lower()
    if host == "github.com" or host.endswith(".github.com"):
        meta.github_has_release, meta.github_days_since_push = await _github_repo_signals(meta.source_url)


async def _block(state, doc_id: int, *, signals: dict, reason: str, used_llm: bool) -> dict:
    """Quarantine a document (never delete) + append the block certification +
    emit mimir.ingest_blocked. Shared by the license gate and the LLM verdict."""
    await state.set_document_trust(doc_id, tier="quarantined", trust_state="quarantined", status="blocked")
    await state.append_certification(
        doc_id,
        decision="block",
        to_tier="quarantined",
        to_state="quarantined",
        signals=signals,
        used_llm=used_llm,
        reasons=reason,
    )
    await state.emit_corpus_event(
        "mimir.ingest_blocked",
        target_type="document",
        target_id=doc_id,
        payload={"reasons": reason},
        dedup_key=f"blocked-{doc_id}",
    )
    log.info("mimir: BLOCKED doc %s — %s", doc_id, reason)
    return {"document_id": doc_id, "decision": "block", "reason": reason, "used_llm": used_llm}


async def _certify_llm(doc: dict, curator, router, session) -> MimirVerdict | None:
    """The lone LLM step: judge an ambiguous web source. Best-effort — returns
    None on any failure so ingest falls back to the deterministic floor."""
    try:
        prompt = await curator.build(
            "mimir.certify",
            context={
                "title": doc.get("title"),
                "source_url": doc.get("source_url"),
                "host": (urlparse(doc.get("source_url") or "").hostname or ""),
            },
        )
        verdict, _run_id = await router.invoke(
            prompt=prompt,
            output_schema_class=MimirVerdict,
            session=session,
            step_name="mimir.certify",
        )
        return verdict
    except Exception:  # noqa: BLE001 — best-effort; fall back to the deterministic floor
        log.exception("mimir: certify LLM call failed; using deterministic floor")
        return None


async def ingest_source(source, state, *, router=None, curator=None, session=None) -> dict:
    """The shared trust-gated ingest core used by BOTH discovery and acquire.

    Stage the source (cheap), classify its trust, write the verdict + an immutable
    certification, then finalize (approve → embed + queryable) or quarantine
    (block). When `tc.needs_llm` and a router/curator are supplied, one LLM call
    breaks the ambiguous web_reputable/web_unknown tie (capped at web_reputable);
    otherwise we admit at the deterministic floor (the safe under-credit)."""
    try:
        staged = await stage_source(source, state)
    except Exception as e:  # noqa: BLE001 — one source failure is non-fatal to the harness
        log.exception("mimir: stage_source failed for %r", source)
        return {"failed": True, "reason": str(e)[:200]}

    doc_id = staged.get("document_id")
    if doc_id is None or staged.get("awaiting") != "mimir":
        return staged  # skipped / deduped — nothing fresh to certify

    doc = await state.get_document(doc_id)
    meta = _doc_meta(doc)
    await _resolve_signals(meta)  # best-effort: lifts DOI->peer_reviewed, active repo->official_repo
    tc = classify_trust(meta)

    if tc.blocked:
        return await _block(state, doc_id, signals=tc.signals, reason=tc.reason, used_llm=False)

    tier, reason, used_llm = tc.tier, tc.reason, False

    # The lone LLM tie-breaker — only for the ambiguous web_unknown boundary, and
    # only when wired (router/curator present). Capped at web_reputable.
    if tc.needs_llm and router is not None and curator is not None:
        verdict = await _certify_llm(doc, curator, router, session)
        if verdict is not None:
            used_llm = True
            if verdict.decision == "block":
                return await _block(state, doc_id, signals=tc.signals, reason=verdict.reasons, used_llm=True)
            tier, reason = verdict.tier, verdict.reasons

    await state.set_document_trust(doc_id, tier=tier, trust_state="provisional", status="certified")
    await state.append_certification(
        doc_id,
        decision="approve",
        to_tier=tier,
        to_state="provisional",
        signals=tc.signals,
        used_llm=used_llm,
        reasons=reason,
    )
    result = await embed_and_finalize(doc_id, state)
    log.info("mimir: APPROVED doc %s at tier=%s (llm=%s) — %s", doc_id, tier, used_llm, reason)
    return {"document_id": doc_id, "decision": "approve", "tier": tier, "used_llm": used_llm, **result}


async def handle_source_discovered(event: dict, dispatcher) -> dict | None:
    """Triggered by `source.discovered` (the collectors' push path). The source
    rides the event payload; hand it to the shared trust-gated ingest core."""
    if not _loop_enabled():
        return None

    source = (event.get("payload") or {}).get("source")
    if not source:
        log.warning("mimir: source.discovered event %s has no payload.source", event.get("id"))
        return {"skipped": True, "reason": "no source in payload"}

    return await ingest_source(
        source,
        dispatcher.state,
        router=getattr(dispatcher, "router", None),
        curator=getattr(dispatcher, "curator", None),
        session=getattr(dispatcher, "session", None),
    )


async def handle_sweep_requested(event: dict, dispatcher) -> dict | None:
    """Triggered by `library.sweep_requested` (the watchdog tick, or a manual
    trigger). Runs the data collectors over the topics on the event payload, or
    the env/default standing topics, emitting `source.discovered` per new source."""
    if not _loop_enabled():
        return None

    from agents.mimir.collectors import run_discovery_sweep

    topics = (event.get("payload") or {}).get("topics")
    return await run_discovery_sweep(topics, dispatcher.state)
