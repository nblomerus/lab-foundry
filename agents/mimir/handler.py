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

License capture: _resolve_signals reads a GitHub repo's SPDX id and persists it
(state.set_document_license) before the gate runs, so a restrictive license can
fire the hard-gate. arXiv's API exposes no license, so papers stay license-None
(correctly not blocked).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from agents.mimir.collectors import run_discovery_sweep
from library.ingest.fetcher import search_arxiv
from library.ingest.first_party import ingest_first_party
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


def _retraction_strict() -> bool:
    """When a retraction check is APPLICABLE but the probe can't verify (arXiv/
    Crossref outage), hold the source instead of admitting it as clean. Default ON
    (fail closed — "no unverified source enters"). Set MIMIR_RETRACTION_STRICT=off to
    admit-and-flag instead (fail open), trading safety for availability during outages."""
    return os.environ.get("MIMIR_RETRACTION_STRICT", "on").lower() not in {"off", "0", "false", "no"}


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


def _spdx_or_none(license_obj: dict | None) -> str | None:
    """Pull a usable SPDX id from the GitHub `license` object. GitHub reports
    `NOASSERTION` when it can't detect a standard license — treat that (and a
    missing license) as unknown (None), NOT as a block, to avoid over-quarantining."""
    spdx = (license_obj or {}).get("spdx_id")
    if not spdx or spdx.upper() in {"NOASSERTION", "NONE"}:
        return None
    return spdx


async def _github_repo_signals(url: str | None) -> tuple[bool | None, int | None, str | None]:
    """For a github.com/<owner>/<repo> URL, return (has_release, days_since_push,
    license_spdx) via the GitHub API (GITHUB_TOKEN used if set). The license is the
    repo's detected SPDX id (e.g. 'MIT', 'GPL-3.0'). (None, None, None) on failure."""
    parts = [p for p in urlparse(url or "").path.split("/") if p]
    if len(parts) < 2:
        return None, None, None
    owner, repo = parts[0], parts[1]
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "labfoundry-mimir"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, headers=headers) as client:
            meta_resp = await client.get(f"https://api.github.com/repos/{owner}/{repo}")
            if meta_resp.status_code != 200:
                return None, None, None
            body = meta_resp.json()
            pushed = body.get("pushed_at")
            days = None
            if pushed:
                dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
                days = (datetime.now(UTC) - dt).days
            license_spdx = _spdx_or_none(body.get("license"))
            rel = await client.get(f"https://api.github.com/repos/{owner}/{repo}/releases", params={"per_page": 1})
            has_release = rel.status_code == 200 and bool(rel.json())
            return has_release, days, license_spdx
    except Exception:  # noqa: BLE001 — best-effort probe
        return None, None, None


# A withdrawn arXiv paper's abstract is replaced with a withdrawal notice; that
# text is the reliable signal (the API exposes no structured "withdrawn" flag).
_WITHDRAWN_RE = re.compile(
    r"(this (paper|submission|manuscript|article|work) has been withdrawn|withdrawn by the author)",
    re.IGNORECASE,
)


async def _arxiv_withdrawn(arxiv_id: str) -> bool | None:
    """Tri-state withdrawal check. True = abstract carries a withdrawal notice,
    False = fetched and clean, None = COULD NOT VERIFY (arXiv unreachable after a
    retry). Retries once on an empty result (arXiv 429s often clear on a short
    backoff). Returning None (not False) on a probe miss lets the caller fail
    closed instead of treating an outage as 'clean'."""
    for attempt in range(2):
        try:
            results = await search_arxiv(f"id:{arxiv_id}", max_results=1)
        except Exception:  # noqa: BLE001 — best-effort probe
            results = []
        if results:
            return bool(_WITHDRAWN_RE.search(results[0].abstract or ""))
        if attempt == 0:
            await asyncio.sleep(2.0)
    return None  # probe could not reach a verdict (arXiv unreachable)


async def _doi_retracted(doi: str) -> bool | None:
    """Tri-state Crossref retraction check. True = flagged retracted (update-to type
    'retraction' or an 'is-retracted-by' relation), False = Crossref answered and did
    NOT flag it, None = COULD NOT VERIFY (non-200 / unreachable). Crossref coverage is
    partial (a False is not proof a paper is clean), but distinguishing 'answered
    clean' from 'could not ask' lets the caller fail closed on an outage."""
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, headers={"User-Agent": "labfoundry-mimir"}) as client:
            resp = await client.get(f"https://api.crossref.org/works/{doi}")
            if resp.status_code != 200:
                return None  # could not ask Crossref
            msg = resp.json().get("message", {})
            if any((u.get("type") or "").lower() == "retraction" for u in msg.get("update-to", []) or []):
                return True
            return "is-retracted-by" in (msg.get("relation") or {})
    except Exception:  # noqa: BLE001 — best-effort probe
        return None  # could not reach Crossref


async def _resolve_signals(meta: DocMeta) -> None:
    """Best-effort network probes that fill the signals classify_trust needs for
    the top tiers (peer_reviewed via a resolving DOI, official_repo via an active
    GitHub repo) and the retraction/withdrawal hard-gate. Mutates `meta`; any
    failure leaves the conservative default."""
    if meta.doi:
        meta.doi_resolves = await _doi_resolves(meta.doi)
    host = (urlparse(meta.source_url or "").hostname or "").lower()
    if host == "github.com" or host.endswith(".github.com"):
        meta.github_has_release, meta.github_days_since_push, license_spdx = await _github_repo_signals(meta.source_url)
        # A repo's own license is more authoritative than anything staged; only
        # overwrite when the probe actually found one.
        if license_spdx:
            meta.license = license_spdx
    # Retraction/withdrawal — arXiv withdrawal notice (reliable) + a best-effort
    # Crossref check for DOIs. Tri-state: True -> hard-gate BLOCK; None -> the check
    # was applicable but the probe could not verify (outage) -> mark unverified so
    # ingest can fail CLOSED instead of admitting a possibly-retracted source.
    unverified = False
    if meta.arxiv_id:
        w = await _arxiv_withdrawn(meta.arxiv_id)
        if w is True:
            meta.retracted = True
        elif w is None:
            unverified = True
    if meta.doi and not meta.retracted:
        r = await _doi_retracted(meta.doi)
        if r is True:
            meta.retracted = True
        elif r is None:
            unverified = True
    if unverified and not meta.retracted:
        meta.retraction_unverified = True


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
    if meta.license:  # capture a resolved license (e.g. GitHub SPDX) onto the doc
        await state.set_document_license(doc_id, meta.license)
    tc = classify_trust(meta)

    if tc.blocked:
        return await _block(state, doc_id, signals=tc.signals, reason=tc.reason, used_llm=False)

    # Fail CLOSED when a retraction check was applicable but the probe couldn't verify
    # (arXiv/Crossref outage): hold the source rather than admit a possibly-retracted
    # one as clean. MIMIR_RETRACTION_STRICT=off downgrades this to admit-and-flag.
    if meta.retraction_unverified and _retraction_strict():
        return await _block(
            state,
            doc_id,
            signals={**tc.signals, "retraction_unverified": True},
            reason="retraction status unverified (arXiv/Crossref probe unavailable) — "
            "held to avoid admitting a possibly-retracted source",
            used_llm=False,
        )

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

    # Admit. If a retraction check was unverifiable and strict mode is OFF, record the
    # flag on the certification so admitted-but-unverified docs stay auditable / re-checkable.
    approve_signals = {**tc.signals, "retraction_unverified": True} if meta.retraction_unverified else tc.signals
    await state.set_document_trust(doc_id, tier=tier, trust_state="provisional", status="certified")
    await state.append_certification(
        doc_id,
        decision="approve",
        to_tier=tier,
        to_state="provisional",
        signals=approve_signals,
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

    payload = event.get("payload") or {}
    source = payload.get("source")
    if not source:
        log.warning("mimir: source.discovered event %s has no payload.source", event.get("id"))
        return {"skipped": True, "reason": "no source in payload"}

    state = dispatcher.state

    # First-party lab outputs (experiment results / datasets) carry their content
    # in-hand on the event payload — they're never fetched or origin-vetted. Route
    # them to the content-in-hand path (reproducibility-gated certification) BEFORE
    # the external trust gate, which doesn't apply to the lab's own work.
    if source.get("source_kind") in ("lab_experiment", "lab_dataset"):
        canonical_key = source["canonical_key"]
        doc_id = await ingest_first_party(
            state,
            kind=source["kind"],
            source_kind=source["source_kind"],
            canonical_key=canonical_key,
            title=source.get("title") or canonical_key,
            content=payload.get("content") or "",
            provenance=payload.get("provenance"),
        )
        # Backlink the resulting doc onto the experiment row (canonical_key 'exp:<id>')
        # so the run knows its result is in the Library. Best-effort: a missing method
        # or malformed key never sinks the ingest.
        if doc_id is not None and canonical_key.startswith("exp:") and hasattr(state, "set_experiment_ingested_doc"):
            try:
                await state.set_experiment_ingested_doc(int(canonical_key.split(":")[1]), doc_id)
            except Exception:  # noqa: BLE001 — backlink is best-effort telemetry
                log.exception("mimir: failed to backlink doc %s onto %s", doc_id, canonical_key)
        return {"document_id": doc_id, "first_party": True}

    return await ingest_source(
        source,
        state,
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

    payload = event.get("payload") or {}
    topics = payload.get("topics")
    # A targeted closure scout (library.sweep_requested carrying a claim_id) asks for relevance
    # ranking so a niche direction's sweep finds on-topic papers; the standing sweep stays newest.
    sort = payload.get("sort", "submittedDate")
    return await run_discovery_sweep(topics, dispatcher.state, sort=sort)
