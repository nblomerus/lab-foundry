"""
Phase adjudicator — fires on 'thesis.confidence_changed' events and decides
whether the criteria for a phase transition are met.

Cheap F-tier model; runs every confidence change (with the per-thesis
cooldown configured in dispatch.py). Emits phase.transition_proposed when
ready; the CEO ratifies separately.

Criteria (strict — bias toward "not yet"):
  exploration  → convergence: ≥3 active theses with confidence ≥ 0.55,
                              ≥4 days in phase, evidence accumulating.
  convergence  → commitment:  top thesis confidence ≥ 0.75, ≥5 supporting
                              findings, evidence growth slowing OR ≥6 days
                              in phase.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from boardroom.harness.curator import RECIPES, PromptLayer, Recipe

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Output schema
# -------------------------------------------------------------------------

class AdjudicatorVerdict(BaseModel):
    transition: bool
    target_phase: Optional[Literal["convergence", "commitment", "execution"]] = None
    reasoning: str = Field(..., min_length=20)
    cited_thesis_ids: list[int] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0,
                              description="Confidence in this verdict (not in the company).")


# -------------------------------------------------------------------------
# Task-data builder + recipe
# -------------------------------------------------------------------------

async def _build_adjudicator_task_data(ctx: dict, state, memory) -> PromptLayer:
    current_phase = ctx["current_phase"]
    days_in_phase = ctx["days_in_phase"]
    theses_summary = ctx["theses_summary"]

    content = f"""## Phase transition adjudication

Current phase: **{current_phase}**  |  Day in phase: {days_in_phase}

## Theses snapshot
{theses_summary}

---

Determine whether phase-transition criteria are met. Be strict — bias toward
'not yet'. Most calls should return transition=false.

Criteria:
  - exploration → convergence: ≥3 active theses with confidence ≥ 0.55,
                              ≥4 days in phase, evidence is accumulating.
  - convergence → commitment:  top thesis confidence ≥ 0.75, ≥5 supporting
                              findings, evidence growth slowing OR ≥6 days
                              in phase.

If transition=true, set target_phase and cite the thesis IDs that justify it.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "phase_adjudicator.check" not in RECIPES:
    RECIPES["phase_adjudicator.check"] = Recipe(
        invocation_type="phase_adjudicator.check",
        description="Decides whether phase-transition criteria are met.",
        agent="adversary",  # reuse the strict / skeptical role anchor
        total_budget=3_000,
        use_cold_path=False,
        recall_sessions=[],
        recall_k=0,
        output_schema="AdjudicatorVerdict",
        task_data_builder=_build_adjudicator_task_data,
    )


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------

async def handle_thesis_confidence_changed(event: dict, dispatcher) -> Optional[dict]:
    """
    Triggered by thesis.confidence_changed. Cheap check for phase transition.
    """
    state_obj, theses = await asyncio.gather(
        dispatcher.state.get_company_state(),
        dispatcher.state.get_active_theses(limit=20),
    )

    if state_obj.current_phase == "execution":
        return {"skipped": True, "reason": "already in execution"}
    if state_obj.paused:
        return {"skipped": True, "reason": "company paused"}
    if not theses:
        return {"skipped": True, "reason": "no active theses"}

    days_in_phase = (datetime.now(timezone.utc) - state_obj.phase_started_at).days

    # Build snapshot: per-thesis confidence + supporting evidence count
    findings_per_thesis = await asyncio.gather(*[
        dispatcher.state.get_recent_findings_for_thesis(t.id, limit=30)
        for t in theses
    ])

    lines: list[str] = []
    for t, findings in zip(theses, findings_per_thesis):
        supporting = sum(
            1 for f in findings
            if f.audit_verdict == "pass" and f.supports_thesis is True
        )
        lines.append(
            f"- T{t.id}: conf={t.confidence:.2f}  ·  "
            f"supporting_findings={supporting}  ·  '{t.claim[:80]}'"
        )
    theses_summary = "\n".join(lines)

    prompt = await dispatcher.curator.build(
        invocation_type="phase_adjudicator.check",
        context={
            "current_phase":  state_obj.current_phase,
            "days_in_phase":  days_in_phase,
            "theses_summary": theses_summary,
        },
    )

    verdict, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=AdjudicatorVerdict,
        triggered_by_event_id=event["id"],
    )

    if not verdict.transition or verdict.target_phase is None:
        return {
            "transition": False,
            "reasoning": verdict.reasoning,
            "run_id": run_id,
        }

    async with dispatcher.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO events (
                event_type, target_type, target_id, payload,
                emitted_by_run_id, dedup_key
            )
            VALUES (
                'phase.transition_proposed', 'phase', 0, $1::jsonb, $2,
                'transprop-' || $3 || '-' || EXTRACT(EPOCH FROM NOW())::bigint::text
            )
            ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
            """,
            json.dumps({
                "from_phase":       state_obj.current_phase,
                "to_phase":         verdict.target_phase,
                "reasoning":        verdict.reasoning,
                "cited_thesis_ids": verdict.cited_thesis_ids,
                "confidence":       verdict.confidence,
                "forced":           False,
            }),
            run_id,
            verdict.target_phase,
        )

    return {
        "transition":   True,
        "target_phase": verdict.target_phase,
        "reasoning":    verdict.reasoning,
        "run_id":       run_id,
    }
