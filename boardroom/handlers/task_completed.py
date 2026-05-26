"""
Handler for task.completed events.

Flow:
  1. Read the completed task + its unaudited findings.
  2. Invoke the Auditor (FAST tier) to score every finding for slop.
  3. Persist verdicts (update_finding_audit emits finding.high_signal as needed).
  4. Check the slop circuit-breaker per affected thesis.
  5. Write an entry to the Zep 'dissent' session if any slop was found.

This handler is also the canonical example for the rest: it touches every
piece of the harness — state, curator, router, memory, events.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from boardroom.harness.curator import (
    RECIPES, Recipe, PromptLayer,
)

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Auditor output schema
# -------------------------------------------------------------------------

class AuditScore(BaseModel):
    finding_id: int
    audit_score: float = Field(..., ge=0.0, le=1.0,
                               description="0 = slop, 1 = high-quality research.")
    verdict: Literal["pass", "slop", "unclear"]
    reasoning: str = Field(..., description="1-2 sentences justifying the verdict.")


class AuditBatch(BaseModel):
    scores: list[AuditScore]


# -------------------------------------------------------------------------
# Auditor recipe (registered at import time)
# -------------------------------------------------------------------------

async def _build_auditor_task_data(ctx: dict, state, memory) -> PromptLayer:
    findings = ctx["findings"]
    task = ctx["task"]

    blocks = []
    for f in findings:
        blocks.append(
            f"### Finding F{f.id}\n"
            f"- Source: {f.source or 'n/a'}\n"
            f"- URL: {f.url or 'n/a'}\n"
            f"- Title: {f.title or 'n/a'}\n"
            f"- Summary: {f.summary}\n"
            f"- Researcher relevance score: {f.relevance_score}\n"
            f"- Why it matters: {f.why_it_matters or '(none)'}\n"
            f"- Supports thesis: {f.supports_thesis}"
        )

    content = f"""## Task being audited

**Task:** {task.description}
**Type:** {task.task_type}

## Findings produced ({len(findings)})

{chr(10).join(blocks) if blocks else '(no findings)'}

---

For each finding, score for slop on a 0-1 scale and assign a verdict:

  - `pass`    (0.7-1.0): cites real research, specific claims, could not have
                          been written without doing the work.
  - `slop`    (0.0-0.3): generic, plausible-sounding, pattern-matched, could
                          have been produced without the cited material.
  - `unclear` (0.3-0.7): mixed signals — some specifics but suspicious.

Be ruthless. Pass is reserved for findings that genuinely add information.
Most findings should land in pass or slop; unclear is a fallback, not a default.

Return one entry per finding above. Use the same finding_id you saw.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "auditor.slop_score" not in RECIPES:
    RECIPES["auditor.slop_score"] = Recipe(
        invocation_type="auditor.slop_score",
        description="Auditor scores each finding from a completed task for slop.",
        agent="auditor",
        total_budget=8_000,
        use_cold_path=False,
        recall_sessions=["dissent"],  # see recent dissent for calibration
        recall_k=5,
        output_schema="AuditBatch",
        task_data_builder=_build_auditor_task_data,
    )


# -------------------------------------------------------------------------
# The handler
# -------------------------------------------------------------------------

async def handle_task_completed(event: dict, dispatcher) -> Optional[dict]:
    """
    Audit the findings of one completed task. Emits high-signal and slop events
    downstream via the state client.

    Required on `dispatcher`:  state, memory, curator, router
    """
    task_id = event["target_id"]
    task = await dispatcher.state.get_task(task_id)

    if task.department != "research":
        return {"skipped": True, "reason": "non-research task"}

    findings = await dispatcher.state.get_unaudited_findings_for_task(task_id)
    if not findings:
        return {"skipped": True, "reason": "no unaudited findings"}

    prompt = await dispatcher.curator.build(
        invocation_type="auditor.slop_score",
        context={"task": task, "findings": findings, "task_id": task_id},
    )

    audit_batch, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=AuditBatch,
        triggered_by_event_id=event["id"],
    )

    # Persist each verdict; high-signal events emitted inside update_finding_audit
    for score in audit_batch.scores:
        await dispatcher.state.update_finding_audit(
            finding_id=score.finding_id,
            audit_score=score.audit_score,
            audit_verdict=score.verdict,
            run_id=run_id,
        )

    # Slop circuit-breaker per affected thesis
    theses_seen = {f.thesis_id for f in findings if f.thesis_id is not None}
    breakers_tripped: list[int] = []
    for thesis_id in theses_seen:
        if await dispatcher.state.detect_slop_breaker(thesis_id):
            breakers_tripped.append(thesis_id)

    # Narrate the audit into dissent session if anything was flagged
    slop_count = sum(1 for s in audit_batch.scores if s.verdict == "slop")
    if slop_count > 0:
        await dispatcher.memory.write_message(
            session_id="dissent",
            content=(
                f"Auditor reviewed task T{task_id} "
                f"('{task.description[:80]}...' if longer than 80 else task.description). "
                f"{slop_count} of {len(audit_batch.scores)} findings flagged as slop. "
                f"Theses affected: {sorted(theses_seen)}. "
                f"Circuit-breakers tripped: {breakers_tripped or 'none'}."
            ),
            role_type="auditor",
            metadata={"task_id": task_id, "run_id": run_id},
        )

    return {
        "audited": len(audit_batch.scores),
        "slop": slop_count,
        "pass": sum(1 for s in audit_batch.scores if s.verdict == "pass"),
        "unclear": sum(1 for s in audit_batch.scores if s.verdict == "unclear"),
        "breakers_tripped": breakers_tripped,
        "run_id": run_id,
    }
