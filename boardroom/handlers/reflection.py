"""
Reflection handler — triggered by 'reflection.requested' events.

The 002_skills.sql trigger fires reflection.requested whenever an agent run
completes that was associated with dissent (audit slop, adversary kill, or
the run itself being a critic that produced a non-pass verdict).

This handler decides whether the run yields a generalizable lesson. Most
dissents do NOT — the bias is heavily toward "no lesson." Only patterns
that will recur become lessons; one-off mistakes don't.

When a lesson is proposed, it lands as 'probationary' in the lessons table.
The reconcile_lessons() function promotes to 'active' after 5 supportive
applications, or retires after 3 contradicting applications.
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from boardroom.harness.curator import RECIPES, PromptLayer, Recipe

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Output schema
# -------------------------------------------------------------------------

class LessonCandidate(BaseModel):
    lesson_text:           str = Field(..., min_length=20, max_length=400,
                                       description="Short, generalizable heuristic.")
    applies_to_invocation: str = Field(..., description="invocation_type the lesson applies to.")
    applies_when:          dict = Field(default_factory=dict,
                                        description="Predicate matched against context dict. {} = always.")
    rationale:             str = Field(..., min_length=20)


class ReflectionOutput(BaseModel):
    should_create_lesson: bool
    candidate:           Optional[LessonCandidate] = None
    reasoning:           str = Field(..., min_length=10)


# -------------------------------------------------------------------------
# Task-data builder + recipe
# -------------------------------------------------------------------------

async def _build_reflection_task_data(ctx: dict, state, memory) -> PromptLayer:
    invocation_type = ctx["invocation_type"]
    run_summary     = ctx["run_summary"]

    content = f"""## Reflection on a dissenting run

A run just completed that involved dissent (audit slop, adversary kill, or
critic non-pass). Decide whether a generalizable lesson can be drawn.

## The run
**Invocation:** {invocation_type}
**Output summary:** {run_summary}

---

A good lesson is:
  - **Specific enough to apply** to a future run
  - **General enough to recur** — not just describing this one run
  - **Falsifiable**: future runs will validate or contradict it

Good examples:
  - "Findings citing AI-generated newsletters often score 8+ but yield
    low-quality theses — discount them by 2 points."
  - "Theses depending on 'enterprises will need X' fail when X already has
    competitors — always check the existing-solutions landscape first."

Bad examples (do NOT write these):
  - "Always be more careful." (vague)
  - "Don't propose thesis T17 again." (too specific)
  - "Try harder." (worthless)

Bias is toward should_create_lesson=FALSE. Most dissents are one-off
mistakes, not repeatable patterns. Only say true when the pattern will
predictably recur AND a future agent could act on the lesson.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "reflect.lesson_propose" not in RECIPES:
    RECIPES["reflect.lesson_propose"] = Recipe(
        invocation_type="reflect.lesson_propose",
        description="Decide whether a dissenting run yields a generalizable lesson.",
        agent="auditor",   # reuse: same skeptical mindset as slop detection
        total_budget=5_000,
        use_cold_path=False,
        recall_sessions=[],
        recall_k=0,
        output_schema="ReflectionOutput",
        task_data_builder=_build_reflection_task_data,
    )


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------

async def handle_reflection_requested(event: dict, dispatcher) -> Optional[dict]:
    target_run_id = event["target_id"]

    async with dispatcher.pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT invocation_type, output_summary, status "
            "FROM agent_runs WHERE id = $1",
            target_run_id,
        )
    if run is None:
        return {"skipped": True, "reason": "run not found"}

    prompt = await dispatcher.curator.build(
        invocation_type="reflect.lesson_propose",
        context={
            "invocation_type": run["invocation_type"],
            "run_summary":     run["output_summary"] or "(no summary)",
        },
    )

    reflection, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=ReflectionOutput,
        triggered_by_event_id=event["id"],
    )

    if not reflection.should_create_lesson or reflection.candidate is None:
        return {
            "lesson_created": False,
            "reasoning": reflection.reasoning,
            "run_id": run_id,
        }

    lesson_id = await dispatcher.lessons.insert_lesson_candidate(
        invocation_type=reflection.candidate.applies_to_invocation,
        applies_when=reflection.candidate.applies_when,
        lesson_text=reflection.candidate.lesson_text,
        rationale=reflection.candidate.rationale,
        derived_from_run_id=target_run_id,
        derived_via="reflection",
    )

    return {
        "lesson_created": True,
        "lesson_id":      lesson_id,
        "lesson_text":    reflection.candidate.lesson_text,
        "run_id":         run_id,
    }
