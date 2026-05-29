"""
Critic handler — triggered by 'finding.high_signal' events.

For each high-signal finding (audit=pass, relevance>=8), examine the affected
claim and decide whether killing evidence has accumulated. Three outcomes:

  - watch:  no killing evidence yet; reasoning recorded in dissent session
  - weaken: real concerns; confidence lowered by proposed delta
  - kill:   evidence sufficient; adversary_verdict created and claim killed
            (state.invalidate_claim emits claim.invalidated → CEO handler triggers)

The handler installs a 4-hour per-claim cooldown on adversary.kill_verdict
so multiple high-signal findings in a window batch into one Adversary run.
"""
from __future__ import annotations

import logging
import os
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from boardroom.harness.curator import RECIPES, PromptLayer, Recipe

log = logging.getLogger(__name__)

# Applied when a model returns action="weaken" but omits (or zeroes) the delta.
# Without this, a weaken verdict with no delta was silently dropped and the
# claim confidence never moved — a primary cause of the exploration flatline.
DEFAULT_WEAKEN_DELTA = -0.1


# -------------------------------------------------------------------------
# Adversary output schema
# -------------------------------------------------------------------------

class AdversaryVerdictOut(BaseModel):
    action: Literal["watch", "weaken", "kill"]
    confidence: float = Field(..., ge=0.0, le=1.0,
                              description="Confidence in this action (not in the claim).")
    reasoning: str = Field(..., min_length=20,
                           description="2-4 sentences. What contradicts the claim (or doesn't)?")
    cited_finding_ids: list[int] = Field(default_factory=list,
                                          description="Findings supporting the action.")
    proposed_confidence_delta: Optional[float] = Field(
        default=None, ge=-1.0, le=0.0,
        description="REQUIRED when action='weaken': negative delta to apply (e.g., -0.15).",
    )

    @model_validator(mode="after")
    def _ensure_weaken_delta(self) -> "AdversaryVerdictOut":
        # A "weaken" with no (or zero) delta is incoherent — it expresses intent
        # to lower confidence while moving it by nothing. Normalize to a modest
        # default so the verdict actually lands instead of being a silent no-op.
        if self.action == "weaken" and not self.proposed_confidence_delta:
            self.proposed_confidence_delta = DEFAULT_WEAKEN_DELTA
        # A delta only makes sense for "weaken"; clear it otherwise.
        if self.action != "weaken":
            self.proposed_confidence_delta = None
        return self


# -------------------------------------------------------------------------
# Critic task-data builder + recipe
# -------------------------------------------------------------------------

async def _build_adversary_task_data(ctx: dict, state, memory) -> PromptLayer:
    import asyncio
    claim_id = ctx["claim_id"]

    claim, recent_findings = await asyncio.gather(
        state.get_claim(claim_id),
        state.get_recent_findings_for_claim(claim_id=claim_id, limit=20),
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

    content = f"""## Target claim under adversarial review

**Claim:** {claim.claim}
**Status:** {claim.status}  |  Current confidence: {claim.confidence:.2f}
**Born:** {claim.created_at:%Y-%m-%d}{trigger_line}

## Recent findings ({len(recent_findings)})

{findings_block}

---

Your job: hunt for evidence the claim is wrong. Be aggressive but cited.

Decide one of:

  - `watch`:  no killing evidence yet. Note what you looked at and why it
              didn't move you. cited_finding_ids may be empty.

  - `weaken`: real concerns exist but not enough to kill. You MUST set
              proposed_confidence_delta to a NEGATIVE number (e.g. -0.15) — a
              weaken without a delta is invalid and will be treated as -0.10.
              Cite the findings that justify the weakening.

  - `kill`:   the evidence is sufficient to invalidate. Cite specific findings
              by id in cited_finding_ids; this list will be permanent record.

Confidence in the action ≠ confidence in the claim. If you weakly think
weaken is right, say weaken with confidence 0.6 — don't bump to kill.

Empty kill verdicts are acceptable. Hedging without committing to one of
three actions is not.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "adversary.kill_verdict" not in RECIPES:
    RECIPES["adversary.kill_verdict"] = Recipe(
        invocation_type="critic.kill_verdict",
        description="Critic examines a claim for killing evidence and emits a verdict.",
        agent="adversary",
        total_budget=12_000,
        use_cold_path=True,
        recall_sessions=["claims-lifecycle", "dissent"],
        recall_k=8,
        output_schema="AdversaryVerdictOut",
        task_data_builder=_build_adversary_task_data,
    )


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------

async def handle_finding_high_signal(event: dict, dispatcher) -> Optional[dict]:
    """
    Triggered by finding.high_signal. Examines the claim and possibly kills it.
    """
    claim_id = event["target_id"]
    payload = event.get("payload") or {}
    triggering_finding_id = payload.get("finding_id")

    # Install 4h cooldown so further high_signal events in the window suppress
    await dispatcher.set_cooldown(
        invocation_type="critic.kill_verdict",
        target_type="claim",
        target_id=claim_id,
        seconds=4 * 3600,
    )

    # ADVERSARY_LOOP=v2 → plan_attack → extract_counter (per page, parallel)
    # → optional stress_test → judge_verdict (boardroom.adversarial.loop).
    # Legacy single-shot kill_verdict path is the default until validated.
    impl = os.environ.get("ADVERSARY_LOOP", "v2").lower()
    if impl == "v2":
        from boardroom.adversarial.loop import run_adversary_loop
        verdict, run_id, _counter_evidence = await run_adversary_loop(
            claim_id=claim_id,
            triggering_finding_id=triggering_finding_id,
            dispatcher=dispatcher,
            triggered_by_event_id=event["id"],
        )
    else:
        prompt = await dispatcher.curator.build(
            invocation_type="critic.kill_verdict",
            context={
                "claim_id": claim_id,
                "triggering_finding_id": triggering_finding_id,
            },
        )
        verdict, run_id = await dispatcher.router.invoke(
            prompt=prompt,
            output_schema_class=AdversaryVerdictOut,
            triggered_by_event_id=event["id"],
        )

    verdict_id = await dispatcher.state.create_critic_verdict(
        claim_id=claim_id,
        verdict=verdict.action,
        confidence=verdict.confidence,
        reasoning=verdict.reasoning,
        cited_finding_ids=verdict.cited_finding_ids,
        run_id=run_id,
    )

    result: dict = {
        "claim_id":  claim_id,
        "action":     verdict.action,
        "verdict_id": verdict_id,
        "run_id":     run_id,
    }

    if verdict.action == "kill":
        killed = await dispatcher.state.invalidate_claim(
            claim_id=claim_id,
            reason=verdict.reasoning[:500],
            verdict_id=verdict_id,
            run_id=run_id,
        )
        result["killed"] = killed.status == "killed"
        await dispatcher.memory.write_message(
            session_id="claims-lifecycle",
            content=(
                f"Thesis T{claim_id} killed by Adversary verdict V{verdict_id}. "
                f"Reasoning: {verdict.reasoning} "
                f"Cited findings: {verdict.cited_finding_ids}."
            ),
            role_type="adversary",
            metadata={"claim_id": claim_id, "verdict_id": verdict_id, "run_id": run_id},
        )

    elif verdict.action == "weaken":
        # The validator normally fills this, but default here too so a weaken
        # verdict can never silently fail to move confidence.
        delta = verdict.proposed_confidence_delta
        if not delta:
            delta = DEFAULT_WEAKEN_DELTA
        claim = await dispatcher.state.get_claim(claim_id)
        new_conf = max(0.0, claim.confidence + delta)
        await dispatcher.state.update_claim_confidence(
            claim_id=claim_id,
            new_confidence=new_conf,
            reason=f"adversary verdict V{verdict_id} (Δ{delta:+.2f}): {verdict.reasoning[:200]}",
            run_id=run_id,
        )
        result["new_confidence"] = new_conf
        result["applied_delta"] = delta

    # Always narrate to dissent
    await dispatcher.memory.write_message(
        session_id="dissent",
        content=(
            f"Adversary on T{claim_id}: {verdict.action} "
            f"(action conf {verdict.confidence:.2f}). {verdict.reasoning}"
        ),
        role_type="adversary",
        metadata={"verdict_id": verdict_id, "run_id": run_id},
    )

    return result
