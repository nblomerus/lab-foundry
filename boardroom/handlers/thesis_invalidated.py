"""
CEO handler — triggered by 'thesis.invalidated' events.

When the Adversary kills a thesis, the CEO decides what to do next:

  - spawn:     1-2 new replacement categories with disambiguating tasks
  - no_action: the loss is fine; existing siblings cover the space
  - pivot:     raise priority on a specific sibling instead of spawning

This is what makes the "invalidated 2 hours later → company gets back to
thinking" promise work end-to-end. Without it, kills are dead-ends.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from boardroom.harness.curator import RECIPES, PromptLayer, Recipe

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Output schema
# -------------------------------------------------------------------------

class ReplacementCategory(BaseModel):
    claim: str = Field(..., description="One-sentence category statement.")
    rationale: str = Field(..., min_length=20)
    risks: str = Field(..., min_length=10)
    disambiguating_questions: list[str] = Field(
        ..., min_length=3, max_length=3,
        description="Three specific research questions that test the category.",
    )


class SpawnReplacementDecision(BaseModel):
    action: Literal["spawn", "no_action", "pivot"]
    reasoning: str = Field(..., min_length=20)
    categories: list[ReplacementCategory] = Field(
        default_factory=list, max_length=2,
        description="If action='spawn': 1-2 new categories. Empty otherwise.",
    )
    pivot_thesis_id: Optional[int] = Field(
        default=None,
        description="If action='pivot': sibling thesis whose priority should rise.",
    )


# -------------------------------------------------------------------------
# Task-data builder + recipe registration
# -------------------------------------------------------------------------

async def _build_spawn_replacement_task_data(ctx: dict, state, memory) -> PromptLayer:
    killed_thesis_id = ctx["killed_thesis_id"]

    killed_thesis, siblings = await asyncio.gather(
        state.get_thesis(killed_thesis_id),
        state.get_active_theses(limit=20),
    )

    sibling_lines = "\n".join(
        f"- T{t.id}: {t.claim} (conf {t.confidence:.2f})"
        for t in siblings
    ) or "(no active siblings — this kill leaves the slate near-empty)"

    content = f"""## A thesis was just killed — what's next?

## The killed thesis
**Claim:** {killed_thesis.claim}
**Born:** {killed_thesis.created_at:%Y-%m-%d}
**Final confidence:** {killed_thesis.confidence:.2f}
**Kill reason:** {killed_thesis.kill_reason or '(none recorded)'}

## Currently active siblings
{sibling_lines}

---

Decide ONE of:

  - `spawn`:     1-2 NEW candidate categories that explore adjacent space.
                Each must NOT repeat the killed thesis's failure mode.
                Distinct from existing siblings.

  - `no_action`: the killed thesis is a dead-end already covered by
                replacements. Don't pad the slate. Empty categories list.

  - `pivot`:     instead of spawning, name ONE sibling thesis_id whose
                energy should rise. The Planner will weight tasks toward it.

Be honest. If you don't have a sharp replacement idea, say no_action.
Padding the slate is worse than fewer-but-real categories.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "ceo.spawn_replacement" not in RECIPES:
    RECIPES["ceo.spawn_replacement"] = Recipe(
        invocation_type="ceo.spawn_replacement",
        description="CEO decides whether to spawn replacements after a thesis is killed.",
        agent="ceo",
        total_budget=8_000,
        use_cold_path=True,
        recall_sessions=["theses-lifecycle", "ceo-deliberations"],
        recall_k=8,
        output_schema="SpawnReplacementDecision",
        task_data_builder=_build_spawn_replacement_task_data,
    )


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------

async def handle_thesis_invalidated(event: dict, dispatcher) -> Optional[dict]:
    """
    Triggered by thesis.invalidated. CEO decides how to respond.
    """
    killed_thesis_id = event["target_id"]

    prompt = await dispatcher.curator.build(
        invocation_type="ceo.spawn_replacement",
        context={"killed_thesis_id": killed_thesis_id},
    )

    decision, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=SpawnReplacementDecision,
        triggered_by_event_id=event["id"],
    )

    result: dict = {
        "killed_thesis_id": killed_thesis_id,
        "action": decision.action,
        "run_id": run_id,
    }

    if decision.action == "spawn":
        new_thesis_ids: list[int] = []
        for cat in decision.categories:
            thesis = await dispatcher.state.create_thesis(
                claim=cat.claim,
                initial_confidence=0.40,
                parent_id=killed_thesis_id,
                created_by_run_id=run_id,
            )
            new_thesis_ids.append(thesis.id)

            async with dispatcher.pool.acquire() as conn:
                async with conn.transaction():
                    for q in cat.disambiguating_questions:
                        await conn.execute(
                            """
                            INSERT INTO tasks (
                                thesis_id, department, task_type,
                                description, payload, priority
                            )
                            VALUES ($1, 'research', 'disambiguate', $2, $3::jsonb, 5)
                            """,
                            thesis.id, q,
                            json.dumps({
                                "query": q,
                                "sources": ["web", "hacker_news", "reddit"],
                            }),
                        )
        result["new_thesis_ids"] = new_thesis_ids

    elif decision.action == "pivot" and decision.pivot_thesis_id is not None:
        async with dispatcher.pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET priority = LEAST(10, priority + 2) "
                "WHERE thesis_id = $1 AND status = 'pending'",
                decision.pivot_thesis_id,
            )
        result["pivoted_thesis_id"] = decision.pivot_thesis_id

    await dispatcher.memory.write_message(
        session_id="ceo-deliberations",
        content=(
            f"On the kill of T{killed_thesis_id}: action={decision.action}. "
            f"Reasoning: {decision.reasoning}"
        ),
        role_type="ceo",
        metadata={
            "killed_thesis_id": killed_thesis_id,
            "run_id": run_id,
            "action": decision.action,
        },
    )

    return result
