"""
The FEEDBACK SEAM — turn a Researcher's GroundedFinding into a steering signal on the direction
it tested. This is the line that makes Ariadne OUTCOME-aware: her reflection reads
claims.confidence + last_evidence_at, but until now nothing moved them, so she steered on the
landscape alone. A finding now proposes, per direction:

  supported        → confidence ↑, last_evidence_at = now   (the bet is being confirmed)
  contradicted     → confidence ↓ toward the kill-condition, last_evidence_at = now
  thin_corpus      → no confidence move; FIRE the named acquires (self-healing: research finds the
                     gap, the Library fetches it, the next pass can judge)
  corpus_exhausted → thin_corpus whose acquires already came back 'already_have' — the Library
                     CAN'T be enriched here. Stop firing acquires; nudge confidence DOWN (stuck) so
                     reflection PIVOTS (or escalates to an experiment) instead of looping.
  needs_experiment → no move; parked for the experiments agent (literature can't settle a number)
  inconclusive     → no move (genuinely unsettled)

Only GROUNDED findings (cited evidence resolves to real retrieved papers) may move confidence on a
verdict — same discipline as Mimir's trust gate. `disposition` / `finding_feedback` /
`aggregate_direction` are PURE; `refine_disposition` reads the acquire ledger (async); and
`apply_feedback` is the advisory/active write path. Shadow never calls apply_feedback.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from agents.mimir.acquire import AcquireRequest as MimirAcquireRequest
from agents.mimir.acquire import request_acquire
from agents.researcher.grounded import GroundedFinding

log = logging.getLogger(__name__)

# Confidence nudges, scaled by the finding's own confidence (except the structural corpus_exhausted
# nudge). Asymmetric: a contradiction bites harder than support climbs; corpus_exhausted is a small
# steady "this is stuck" pressure toward a pivot.
_SUPPORT_STEP = 0.08
_CONTRADICT_STEP = -0.12
_EXHAUSTED_STEP = -0.05
_GROUNDED_MIN = 0.5  # only findings whose cited evidence resolves may move confidence
_MAX_ROUND_DELTA = 0.20  # cap how far ONE research round can move a direction

DISPOSITIONS = ("supported", "contradicted", "corpus_exhausted", "thin_corpus", "needs_experiment", "inconclusive")
# Priority for the headline steering signal: decisive verdicts first, then the actionable blockers.
_DOMINANCE = ("contradicted", "supported", "corpus_exhausted", "thin_corpus", "needs_experiment", "inconclusive")
_STEP = {"supported": _SUPPORT_STEP, "contradicted": _CONTRADICT_STEP, "corpus_exhausted": _EXHAUSTED_STEP}


def disposition(f: GroundedFinding) -> str:
    """Collapse (verdict, blocker) into the single label that drives steering (pre-escalation)."""
    if f.verdict == "supports":
        return "supported"
    if f.verdict == "contradicts":
        return "contradicted"
    if f.blocker in ("thin_corpus", "needs_experiment"):
        return f.blocker
    return "inconclusive"


async def refine_disposition(state, claim_id: int | None, base: str) -> str:
    """Escalate thin_corpus → corpus_exhausted when this direction's own researcher-acquires have
    already come back 'already_have' (the Library can't be enriched here, so acquiring again is a
    no-op and the right answer is PIVOT). Needs claim_id-attributed acquire replies; returns the
    base disposition unchanged if there's no such history yet."""
    if base != "thin_corpus" or claim_id is None:
        return base
    rows = await state.pool.fetch(
        "SELECT payload->>'status' AS status FROM events "
        "WHERE event_type = 'acquire.fulfilled' AND payload->>'requester' = 'researcher' "
        "AND (payload->>'claim_id')::int = $1 AND emitted_at > now() - interval '2 days'",
        claim_id,
    )
    if len(rows) >= 2 and all(r["status"] == "already_have" for r in rows):
        return "corpus_exhausted"
    return base


class FindingFeedback(BaseModel):
    task_id: int
    disposition: str
    confidence_delta: float  # 0 unless a grounded verdict / corpus_exhausted
    set_last_evidence: bool  # only a grounded supports/contradicts gathers evidence
    grounded: float
    acquire_queries: list[str] = Field(default_factory=list)
    note: str = ""


def finding_feedback(
    ctx: dict, finding: GroundedFinding, grounded: float, *, disposition_override: str | None = None
) -> FindingFeedback:
    """What ONE finding proposes for its direction. Pass `disposition_override` with the escalated
    label from `refine_disposition`. Only a grounded supports/contradicts gathers evidence; only
    a thin_corpus (not yet exhausted) fires acquires."""
    d = disposition_override or disposition(finding)
    grounded_ok = grounded >= _GROUNDED_MIN
    moves = (d in ("supported", "contradicted") and grounded_ok) or d == "corpus_exhausted"
    scale = finding.confidence if d in ("supported", "contradicted") else 1.0
    return FindingFeedback(
        task_id=ctx["task_id"],
        disposition=d,
        confidence_delta=round(_STEP.get(d, 0.0) * scale, 3) if moves else 0.0,
        set_last_evidence=d in ("supported", "contradicted") and moves,
        grounded=round(grounded, 2),
        acquire_queries=finding.acquire_queries if d == "thin_corpus" else [],
        note=finding.summary[:200],
    )


class DirectionFeedback(BaseModel):
    claim_id: int | None
    direction: str
    n_findings: int
    confidence_delta: float  # net across the round, clamped to ±_MAX_ROUND_DELTA
    dominant: str  # the disposition that should drive steering
    set_last_evidence: bool  # did any grounded supports/contradicts land?
    acquire_queries: list[str]  # deduped union — the self-healing fetches
    items: list[FindingFeedback]


def aggregate_direction(claim_id: int | None, direction: str, items: list[FindingFeedback]) -> DirectionFeedback:
    """Roll a direction's findings into one steering proposal (net Δconfidence, dominant signal,
    acquire queue). Pure — no writes."""
    net = sum(i.confidence_delta for i in items)
    net = max(-_MAX_ROUND_DELTA, min(_MAX_ROUND_DELTA, net))
    present = {i.disposition for i in items}
    dominant = next((d for d in _DOMINANCE if d in present), "inconclusive")
    acquires = list(dict.fromkeys(q for i in items for q in i.acquire_queries))[:6]
    return DirectionFeedback(
        claim_id=claim_id,
        direction=direction,
        n_findings=len(items),
        confidence_delta=round(net, 3),
        dominant=dominant,
        set_last_evidence=any(i.set_last_evidence for i in items),
        acquire_queries=acquires,
        items=items,
    )


async def apply_feedback(state, fb: DirectionFeedback, *, run_id: int | None = None) -> dict:
    """LIVE write path (advisory/active ONLY — shadow never calls this). Moves the direction's
    confidence (and last_evidence_at when real evidence landed), and fires self-healing acquires
    for a thin corpus (tagged with claim_id so the next round can detect corpus_exhausted).
    Best-effort; returns what it did."""
    if fb.claim_id is None:
        return {"skipped": "no claim_id"}
    applied: dict = {"claim_id": fb.claim_id, "dominant": fb.dominant}

    if fb.confidence_delta != 0:
        # Only move an ACTIVE claim's confidence — a direction that was invalidated/merged/retired
        # since the task was framed is no longer steerable (and update_claim_confidence would raise
        # 'not active'). Best-effort: a race that flips status mid-update must not strand the task.
        async with state.pool.acquire() as conn:
            cur = await conn.fetchval(
                "SELECT confidence FROM claims WHERE id = $1 "
                "AND status IN ('proposed','tested','weakly_supported','replicated')",
                fb.claim_id,
            )
        if cur is not None:
            new_conf = max(0.0, min(1.0, float(cur) + fb.confidence_delta))
            try:
                await state.update_claim_confidence(
                    fb.claim_id, new_conf, reason=f"researcher findings: {fb.dominant}", run_id=run_id
                )
                applied["confidence"] = [round(float(cur), 3), round(new_conf, 3)]
            except ValueError as e:  # claim went non-active between the read and the update
                log.warning("feedback: confidence move skipped for claim %s: %s", fb.claim_id, e)
    if fb.set_last_evidence:
        async with state.pool.acquire() as conn:
            await conn.execute("UPDATE claims SET last_evidence_at = now() WHERE id = $1", fb.claim_id)
        applied["last_evidence_at"] = "now"

    fired = 0
    for q in fb.acquire_queries:
        try:
            await request_acquire(
                state,
                MimirAcquireRequest(
                    requester="researcher",
                    kind="paper",
                    query=q,
                    claim_id=fb.claim_id,
                    why=f"researcher: corpus thin on '{q[:60]}' while testing direction {fb.claim_id}",
                ),
            )
            fired += 1
        except Exception as e:  # noqa: BLE001 — demand side is best-effort
            log.warning("feedback acquire failed for %r: %s", q, e)
    if fired:
        applied["acquires_fired"] = fired
    return applied
