"""
PI phase-transition ratification handler — 'phase.transition_proposed'.

The Adjudicator (or the watchdog forcing function) emits transition
proposals. The PI ratifies, rejects, or defers. R-tier (highest stakes).

Special case: when ratifying a move to commitment OR execution, the SAME
invocation writes the full charter and transitions straight to execution
(commitment is a moment, not a phase to dwell in). The losing claims are
marked 'merged' with parent_id pointing to the winning one.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from labfoundry.harness.curator import RECIPES, PromptLayer, Recipe

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Output schemas
# -------------------------------------------------------------------------


class CharterContent(BaseModel):
    claim: str = Field(..., min_length=40, description="One paragraph: the chosen claim.")
    niche: str = Field(..., min_length=20, description="The specific market niche / segment.")
    audience: str = Field(..., min_length=20, description="Who the customers are; where to reach them.")
    product: str = Field(..., min_length=20, description="What we make / sell. Concrete.")
    gtm: str = Field(..., min_length=20, description="Go-to-market: how strangers find out and pay.")
    success_metric: str = Field(..., min_length=20, description="Restatement of when we'll know it worked.")


class PhaseTransitionDecision(BaseModel):
    action: Literal["ratify", "reject", "defer"]
    reasoning: str = Field(..., min_length=20)
    chosen_claim_id: int | None = Field(
        default=None,
        description="If transitioning to commitment/execution: the winning claim.",
    )
    charter: CharterContent | None = Field(
        default=None,
        description="Required when transitioning to commitment or execution.",
    )


# -------------------------------------------------------------------------
# Task-data builder + recipe
# -------------------------------------------------------------------------


async def _build_phase_transition_task_data(ctx: dict, state, memory) -> PromptLayer:
    from_phase = ctx["from_phase"]
    target_phase = ctx["target_phase"]
    adjudicator_reasoning = ctx["adjudicator_reasoning"]
    forced = ctx.get("forced", False)

    claims = await state.get_active_claims(limit=20)
    claims_lines = "\n".join(f"- T{t.id}: conf {t.confidence:.2f}  ·  '{t.claim}'" for t in claims) or "(none active)"

    forced_note = (
        "\n\n⚠ This transition is FORCED by the watchdog (phase budget exceeded). "
        "You can still reject, but you must justify why staying past budget is right."
        if forced
        else ""
    )

    if target_phase in ("commitment", "execution"):
        instructions = """
You are transitioning toward execution. This is the company's defining commit.

If ratifying:
  1. Pick ONE winning claim (chosen_claim_id). Losers become 'merged'.
  2. Write the full charter (claim / niche / audience / product / gtm /
     success_metric). Be specific. "Build a SaaS" is useless; "Self-hosted
     Postgres-tuning advisor for solo founders, $39/mo, distributed via
     developer Twitter" is workable.

The charter becomes immutable for the rest of the run.

Actions:
  - ratify:  name chosen_claim_id, fill in the charter
  - reject:  stay in current phase; explain what's missing
  - defer:   ask for more evidence; sets a 12h adjudicator cooldown
"""
    else:
        instructions = """
You are transitioning to convergence. This is a softer step — narrow the
field and hunt contradictions on the top claims, but no commitment yet.

Actions:
  - ratify:  enter convergence
  - reject:  stay in exploration; explain what's missing
  - defer:   the call is genuinely close; ask for more evidence
"""

    content = f"""## Phase transition decision

**Proposed:** {from_phase} → {target_phase}
**Adjudicator reasoning:**
{adjudicator_reasoning}{forced_note}

## Active claims
{claims_lines}

---
{instructions}
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "pi.phase_transition_ratify" not in RECIPES:
    RECIPES["pi.phase_transition_ratify"] = Recipe(
        invocation_type="pi.phase_transition_ratify",
        description="PI ratifies, rejects, or defers a phase transition; writes charter on commit.",
        agent="pi",
        total_budget=15_000,
        use_cold_path=True,
        recall_sessions=["claims-lifecycle", "pi-deliberations", "phase-transitions"],
        recall_k=10,
        output_schema="PhaseTransitionDecision",
        task_data_builder=_build_phase_transition_task_data,
    )


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------


async def handle_phase_transition_proposed(event: dict, dispatcher) -> dict | None:
    """
    Triggered by phase.transition_proposed. PI ratifies, rejects, or defers.
    On ratify → commitment/execution: writes charter, marks losing claims
    'merged', transitions company_state straight to execution.
    """
    payload = event["payload"] or {}
    from_phase = payload["from_phase"]
    target_phase = payload["to_phase"]

    state_obj = await dispatcher.state.get_company_state()
    if state_obj.current_phase != from_phase:
        return {
            "skipped": True,
            "reason": f"current phase is {state_obj.current_phase}, not {from_phase}",
        }

    prompt = await dispatcher.curator.build(
        invocation_type="pi.phase_transition_ratify",
        context={
            "from_phase": from_phase,
            "target_phase": target_phase,
            "adjudicator_reasoning": payload.get("reasoning", ""),
            "forced": payload.get("forced", False),
        },
    )

    # Thread through the dispatcher's session for /trace observability.
    # Phase transition stays single-step — there's no behaviour-meaningful
    # split — but routing it through the session means the call shows up
    # in the trace DAG just like multi-step handlers.
    decision, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=PhaseTransitionDecision,
        triggered_by_event_id=event["id"],
        session=dispatcher.session,
        step_name="phase_transition_decision",
    )

    result: dict = {
        "action": decision.action,
        "from_phase": from_phase,
        "target_phase": target_phase,
        "run_id": run_id,
    }

    # -- Reject --
    if decision.action == "reject":
        await dispatcher.memory.write_message(
            session_id="phase-transitions",
            content=f"PI REJECTED {from_phase} → {target_phase}. {decision.reasoning}",
            role_type="pi",
            metadata={"run_id": run_id},
        )
        return result

    # -- Defer --
    if decision.action == "defer":
        await dispatcher.set_cooldown(
            invocation_type="phase_adjudicator.check",
            target_type="phase",
            target_id=0,
            seconds=12 * 3600,
        )
        await dispatcher.memory.write_message(
            session_id="phase-transitions",
            content=f"PI DEFERRED {from_phase} → {target_phase}. {decision.reasoning}",
            role_type="pi",
            metadata={"run_id": run_id},
        )
        return result

    # -- Ratify --
    effective_target = target_phase
    write_charter = target_phase in ("commitment", "execution") and decision.charter is not None
    if write_charter:
        effective_target = "execution"

    async with dispatcher.pool.acquire() as conn, conn.transaction():
        if write_charter and decision.charter is not None:
            charter_md = (
                f"# Charter\n\n"
                f"## Thesis\n{decision.charter.claim}\n\n"
                f"## Niche\n{decision.charter.niche}\n\n"
                f"## Audience\n{decision.charter.audience}\n\n"
                f"## Product\n{decision.charter.product}\n\n"
                f"## Go-to-market\n{decision.charter.gtm}\n\n"
                f"## Success metric\n{decision.charter.success_metric}"
            )
            await conn.execute(
                """
                    UPDATE company_state
                    SET current_phase = $1::phase,
                        phase_started_at = NOW(),
                        claim   = $2,
                        niche    = $3,
                        audience = $4,
                        charter  = $5
                    WHERE id = 1
                    """,
                effective_target,
                decision.charter.claim,
                decision.charter.niche,
                decision.charter.audience,
                charter_md,
            )
            if decision.chosen_claim_id is not None:
                await conn.execute(
                    """
                        UPDATE claims
                        SET status = 'merged',
                            parent_id = $1,
                            updated_at = NOW()
                        WHERE status = 'active' AND id != $1
                        """,
                    decision.chosen_claim_id,
                )
                await conn.execute(
                    """
                        UPDATE claims
                        SET status = 'promoted', updated_at = NOW()
                        WHERE id = $1
                        """,
                    decision.chosen_claim_id,
                )
        else:
            await conn.execute(
                """
                    UPDATE company_state
                    SET current_phase = $1::phase,
                        phase_started_at = NOW()
                    WHERE id = 1
                    """,
                effective_target,
            )

        await conn.execute(
            """
                INSERT INTO phase_transitions (
                    from_phase, to_phase, reason,
                    cited_claim_ids, proposed_by_run_id, forced
                )
                VALUES ($1::phase, $2::phase, $3, $4, $5, $6)
                """,
            from_phase,
            effective_target,
            decision.reasoning,
            payload.get("cited_claim_ids", []),
            run_id,
            payload.get("forced", False),
        )

    result["effective_target_phase"] = effective_target
    if decision.chosen_claim_id is not None:
        result["chosen_claim_id"] = decision.chosen_claim_id
    if write_charter:
        result["charter_written"] = True

    # Narrate
    narrative = f"PHASE TRANSITION: {from_phase} → {effective_target}. {decision.reasoning}"
    if write_charter and decision.charter is not None:
        narrative += "\n\nCharter committed."
    await dispatcher.memory.write_message(
        session_id="phase-transitions",
        content=narrative,
        role_type="pi",
        metadata={
            "run_id": run_id,
            "from_phase": from_phase,
            "to_phase": effective_target,
        },
    )
    if write_charter and decision.charter is not None:
        await dispatcher.memory.write_message(
            session_id="charter",
            content=decision.charter.model_dump_json(indent=2),
            role_type="pi",
            metadata={"run_id": run_id},
        )

    return result
