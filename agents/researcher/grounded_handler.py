"""
Research-era Researcher handler — triggered by `task.created`.

Claims one pending research task and EXECUTES it against the certified Library
(agents.researcher.grounded.investigate_task), then feeds the verdict back onto the direction it
serves (agents.researcher.feedback) so Ariadne's reflection becomes OUTCOME-aware. This supersedes
the market-era web/Reddit researcher — the lab researches its own trusted corpus.

Mode-gated on the 'researcher' dial: off/shadow → it does NOT claim (no-op, the dry state);
advisory/active → it runs the full loop (finding → confidence / last_evidence_at / self-healing
acquire). Concurrency-safe: claim_task is atomic, so concurrent invocations take distinct tasks.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from agents.researcher.feedback import (
    aggregate_direction,
    apply_feedback,
    disposition,
    finding_feedback,
    refine_disposition,
)
from agents.researcher.grounded import grade_finding, investigate_task
from agents.researcher.identity import researcher_for_task
from harness.agent_modes import get_agent_mode
from library.graph.tools import FINDING_ID_RESEARCHER, link_finding_cites_paper, merge_finding_grounds_claim

log = logging.getLogger(__name__)


async def handle_grounded_research(event: dict, dispatcher) -> dict | None:
    """Claim + investigate one research task against the Library; feed the verdict back to its
    direction. No-op unless researcher mode is advisory|active (off/shadow = dry)."""
    state = dispatcher.state
    mode = await get_agent_mode(state.pool, "researcher")
    if mode not in ("advisory", "active"):
        return {"skipped": True, "reason": f"researcher mode {mode}"}

    worker = f"researcher-{uuid.uuid4().hex[:8]}"
    task = await state.claim_task(worker_id=worker, department="research")
    if task is None:
        return {"skipped": True, "reason": "no claimable research task"}

    # The acting identity: the task's owning researcher (migration 022). The worker label is just
    # compute; the IDENTITY is the owner, carried onto every experiment this run authors.
    researcher = await researcher_for_task(state.pool, task)
    who = researcher.name if researcher else "researcher"
    log.info("%s (%s) claimed T%s: %s", who, worker, task.id, (task.description or "")[:80])
    try:
        # emit=True → the Ariadne↔Mimir conversation shows live (floorplan + history).
        result = await investigate_task(state, task.id, emit=True)
    except Exception as e:  # noqa: BLE001 — a task-level failure must not sink the harness
        log.exception("grounded researcher failed for T%s", task.id)
        await state.fail_task(task.id, error=f"grounded researcher: {e}")
        return {"task_id": task.id, "failed": True, "reason": str(e)[:200]}
    if result is None:
        await state.fail_task(task.id, error="task context missing")
        return {"task_id": task.id, "failed": True, "reason": "no context"}

    ctx, refs, _mimir, finding = result
    grade = grade_finding(finding, refs)
    # The feedback seam touches the DB (confidence / last_evidence / acquires). It must NEVER strand
    # the task — a failure here would leave it 'running', get reaped to 'pending', and re-loop. So we
    # always complete the task below, even if steering failed.
    try:
        disp = await refine_disposition(state, ctx["claim_id"], disposition(finding, grade["grounded"]))
        fb = aggregate_direction(
            ctx["claim_id"],
            ctx["direction"],
            [finding_feedback(ctx, finding, grade["grounded"], disposition_override=disp)],
        )
        applied = await apply_feedback(state, fb)  # confidence / last_evidence_at / self-healing acquire
    except Exception as e:  # noqa: BLE001 — steering is best-effort; the finding still completes
        log.exception("grounded researcher: feedback failed for T%s", task.id)
        disp, applied = disposition(finding, grade["grounded"]), {"feedback_error": str(e)[:200]}

    # Project this finding into the trace graph: it GROUNDS its claim and CITES the EXTERNAL papers
    # it actually rests on (the researcher's resolved refs → real provenance the synthesis path loses).
    # Namespaced finding id so it never collides with market-era findings. Best-effort — never strands
    # the task (Neo4j is a projection, not the source of truth).
    claim_id = ctx.get("claim_id")
    if claim_id is not None and refs:
        try:
            fid = FINDING_ID_RESEARCHER + task.id
            now = datetime.now(UTC).isoformat()
            supports = True if finding.verdict == "supports" else False if finding.verdict == "contradicts" else None
            await merge_finding_grounds_claim(
                finding_id=fid,
                claim_id=claim_id,
                source="researcher",
                url=None,
                title=(finding.summary or "")[:200],
                summary=finding.summary or "",
                relevance_score=round(float(finding.confidence) * 10, 1),
                supports_claim=supports,
                audit_verdict=disp,
                created_at=now,
            )
            for r in refs:
                if getattr(r, "document_id", None):
                    await link_finding_cites_paper(fid, r.document_id, created_at=now)
        except Exception:  # noqa: BLE001 — trace-graph projection is best-effort
            log.exception("grounded researcher: trace-graph projection failed for T%s", task.id)

    # needs_experiment: literature can't settle this number — hand it to the experiments agent.
    # The feedback seam makes NO confidence move for this blocker; emitting here turns the dead-end
    # into a runnable experiment. Best-effort (deduped per task) — a failure must never strand the task.
    if finding.blocker == "needs_experiment" and ctx.get("claim_id") is not None:
        try:
            await state.emit_corpus_event(
                "experiment.requested",
                target_type="claim",
                target_id=ctx["claim_id"],
                payload={
                    "claim_id": ctx["claim_id"],
                    "task_id": task.id,
                    "researcher_id": researcher.id if researcher else None,
                    "hypothesis": "; ".join(finding.gaps) if finding.gaps else finding.summary,
                    "goal": ctx.get("expectation") or ctx.get("direction") or "",
                },
                dedup_key=f"exp-req-{task.id}",
            )
        except Exception:  # noqa: BLE001 — the experiment request is best-effort; the finding still completes
            log.exception("grounded researcher: experiment.requested emit failed for T%s", task.id)

    await state.complete_task(
        task_id=task.id,
        result={
            "worker": worker,
            "verdict": finding.verdict,
            "blocker": finding.blocker,
            "disposition": disp,
            "grounded": grade["grounded"],
            "confidence": finding.confidence,
            "summary": finding.summary,
            "key_evidence": finding.key_evidence[:6],
            "kill_condition_check": finding.kill_condition_check,
            "gaps": finding.gaps[:6],
            "acquire_queries": finding.acquire_queries[:6],
            "next_step": finding.next_step,
            "queries": ctx.get("queries", []),
            "n_evidence": len(refs),
            "applied": applied,
        },
    )
    log.info(
        "researcher T%s → %s (Δconf=%s acquires=%s)",
        task.id,
        disp,
        applied.get("confidence"),
        applied.get("acquires_fired", 0),
    )
    return {"task_id": task.id, "disposition": disp, "applied": applied}
