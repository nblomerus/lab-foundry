"""
Adversary handler — triggered by 'finding.high_signal' events.

For each high-signal finding (audit=pass, relevance>=8), examine the affected
thesis and decide whether killing evidence has accumulated. Three outcomes:

  - watch:  no killing evidence yet; reasoning recorded in dissent session
  - weaken: real concerns; confidence lowered by proposed delta
  - kill:   evidence sufficient; adversary_verdict created and thesis killed
            (state.kill_thesis emits thesis.invalidated → CEO handler triggers)

The handler installs a 4-hour per-thesis cooldown on adversary.kill_verdict
so multiple high-signal findings in a window batch into one Adversary run.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from boardroom.harness.curator import RECIPES, PromptLayer, Recipe

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Adversary output schema
# -------------------------------------------------------------------------

class AdversaryVerdictOut(BaseModel):
    action: Literal["watch", "weaken", "kill"]
    confidence: float = Field(..., ge=0.0, le=1.0,
                              description="Confidence in this action (not in the thesis).")
    reasoning: str = Field(..., min_length=20,
                           description="2-4 sentences. What contradicts the thesis (or doesn't)?")
    cited_finding_ids: list[int] = Field(default_factory=list,
                                          description="Findings supporting the action.")
    proposed_confidence_delta: Optional[float] = Field(
        default=None, ge=-1.0, le=0.0,
        description="If action='weaken', proposed delta to apply (e.g., -0.15).",
    )


# -------------------------------------------------------------------------
# Adversary task-data builder + recipe
# -------------------------------------------------------------------------

async def _build_adversary_task_data(ctx: dict, state, memory) -> PromptLayer:
    import asyncio
    thesis_id = ctx["thesis_id"]

    thesis, recent_findings = await asyncio.gather(
        state.get_thesis(thesis_id),
        state.get_recent_findings_for_thesis(thesis_id=thesis_id, limit=20),
    )

    if recent_findings:
        findings_block = "\n".join(
            f"- F{f.id} [{f.source}, rel {f.relevance_score}, "
            f"supports={f.supports_thesis}, audit={f.audit_verdict}]: "
            f"{f.title}\n    {f.summary[:200]}"
            for f in recent_findings
        )
    else:
        findings_block = "(no findings yet — early in research)"

    triggering = ctx.get("triggering_finding_id")
    trigger_line = (
        f"\nThis review was triggered by finding F{triggering} reaching high signal."
        if triggering else ""
    )

    content = f"""## Target thesis under adversarial review

**Claim:** {thesis.claim}
**Status:** {thesis.status}  |  Current confidence: {thesis.confidence:.2f}
**Born:** {thesis.created_at:%Y-%m-%d}{trigger_line}

## Recent findings ({len(recent_findings)})

{findings_block}

---

Your job: hunt for evidence the thesis is wrong. Be aggressive but cited.

Decide one of:

  - `watch`:  no killing evidence yet. Note what you looked at and why it
              didn't move you. cited_finding_ids may be empty.

  - `weaken`: real concerns exist but not enough to kill. Set
              proposed_confidence_delta (negative, e.g. -0.15) and cite the
              findings that justify the weakening.

  - `kill`:   the evidence is sufficient to invalidate. Cite specific findings
              by id in cited_finding_ids; this list will be permanent record.

Confidence in the action ≠ confidence in the thesis. If you weakly think
weaken is right, say weaken with confidence 0.6 — don't bump to kill.

Empty kill verdicts are acceptable. Hedging without committing to one of
three actions is not.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "adversary.kill_verdict" not in RECIPES:
    RECIPES["adversary.kill_verdict"] = Recipe(
        invocation_type="adversary.kill_verdict",
        description="Adversary examines a thesis for killing evidence and emits a verdict.",
        agent="adversary",
        total_budget=12_000,
        use_cold_path=True,
        recall_sessions=["theses-lifecycle", "dissent"],
        recall_k=8,
        output_schema="AdversaryVerdictOut",
        task_data_builder=_build_adversary_task_data,
    )


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------

async def handle_finding_high_signal(event: dict, dispatcher) -> Optional[dict]:
    """
    Triggered by finding.high_signal. Examines the thesis and possibly kills it.
    """
    thesis_id = event["target_id"]
    payload = event.get("payload") or {}
    triggering_finding_id = payload.get("finding_id")

    # Install 4h cooldown so further high_signal events in the window suppress
    await dispatcher.set_cooldown(
        invocation_type="adversary.kill_verdict",
        target_type="thesis",
        target_id=thesis_id,
        seconds=4 * 3600,
    )

    prompt = await dispatcher.curator.build(
        invocation_type="adversary.kill_verdict",
        context={
            "thesis_id": thesis_id,
            "triggering_finding_id": triggering_finding_id,
        },
    )

    verdict, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=AdversaryVerdictOut,
        triggered_by_event_id=event["id"],
    )

    verdict_id = await dispatcher.state.create_adversary_verdict(
        thesis_id=thesis_id,
        verdict=verdict.action,
        confidence=verdict.confidence,
        reasoning=verdict.reasoning,
        cited_finding_ids=verdict.cited_finding_ids,
        run_id=run_id,
    )

    result: dict = {
        "thesis_id":  thesis_id,
        "action":     verdict.action,
        "verdict_id": verdict_id,
        "run_id":     run_id,
    }

    if verdict.action == "kill":
        killed = await dispatcher.state.kill_thesis(
            thesis_id=thesis_id,
            reason=verdict.reasoning[:500],
            verdict_id=verdict_id,
            run_id=run_id,
        )
        result["killed"] = killed.status == "killed"
        await dispatcher.memory.write_message(
            session_id="theses-lifecycle",
            content=(
                f"Thesis T{thesis_id} killed by Adversary verdict V{verdict_id}. "
                f"Reasoning: {verdict.reasoning} "
                f"Cited findings: {verdict.cited_finding_ids}."
            ),
            role_type="adversary",
            metadata={"thesis_id": thesis_id, "verdict_id": verdict_id, "run_id": run_id},
        )

    elif verdict.action == "weaken" and verdict.proposed_confidence_delta is not None:
        thesis = await dispatcher.state.get_thesis(thesis_id)
        new_conf = max(0.0, thesis.confidence + verdict.proposed_confidence_delta)
        await dispatcher.state.update_thesis_confidence(
            thesis_id=thesis_id,
            new_confidence=new_conf,
            reason=f"adversary verdict V{verdict_id}: {verdict.reasoning[:200]}",
            run_id=run_id,
        )
        result["new_confidence"] = new_conf

    # Always narrate to dissent
    await dispatcher.memory.write_message(
        session_id="dissent",
        content=(
            f"Adversary on T{thesis_id}: {verdict.action} "
            f"(action conf {verdict.confidence:.2f}). {verdict.reasoning}"
        ),
        role_type="adversary",
        metadata={"verdict_id": verdict_id, "run_id": run_id},
    )

    return result
