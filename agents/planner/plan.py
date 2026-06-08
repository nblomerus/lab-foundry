"""
The PLANNER — turns Ariadne's APPROVED directions into concrete research tasks.

Stage 2 of the research-execution ladder. The Planner reads each approved direction
(claim_kind='direction' with direction_gate.status='approved') + its claim_goals, and
decomposes it into 2–4 tasks the Researcher can actually execute against the Library
(retrieval / analysis / small reproducible studies — fit to the lab's hardware, never
data-centre work). Built like Ariadne's deliberate/reflect: direct DeepSeek→local chain,
structured output, grade-gated, shadow-safe. WRITES NOTHING here — persistence is
agents.planner.persist (advisory/active only).
"""

from __future__ import annotations

import logging
import os

from pydantic import BaseModel

from agents.ariadne.loop import LAB_CONSTRAINTS, recall_lessons
from agents.llm import _chain_complete, _strip_fences
from agents.planner.schemas import PlanOutput

log = logging.getLogger(__name__)

_ACTIVE = "('proposed','tested','weakly_supported','replicated')"

TASK_TYPES = ("survey", "analyze", "compare", "reproduce", "falsify", "deepen")
# Keep the lab focused: few tasks per direction (env-tunable; also a HARD cap in persist).
MAX_TASKS_PER_DIRECTION = int(os.environ.get("PLANNER_MAX_TASKS", "2"))

_PLAN_SYSTEM = f"""You are the Planner of an autonomous AI research lab. Ariadne (the PI) has
APPROVED the direction(s) below for active research. Your job: decompose EACH approved direction
into 1–{MAX_TASKS_PER_DIRECTION} concrete, executable research TASKS that advance its goals
(expectation / kill-condition). Keep it LEAN — pick only the tasks that most directly test the
goal; a small lab pursues depth, not breadth. A task is a single, self-contained unit a Researcher
can do against the Library (a ~46k-paper certified corpus + retrieval + light analysis): e.g. survey
the prior art on X, analyze/extract evidence for claim Y, compare approaches A vs B, reproduce a
reported result, falsify a sub-claim, deepen a specific gap. Each task MUST be doable within the
lab's hardware (inference + retrieval + small experiments — NO large-scale training or data-centre
compute). Be specific and actionable — a task is an instruction, not a topic."""

_PLAN_SCHEMA_HINT = (
    """Output JSON with exactly these keys:
{
 "plans": [ { "claim_id": int (an EXISTING approved direction id — never invent one),
   "tasks": [ { "title": str, "description": str (the concrete instruction for the Researcher),
     "task_type": "survey|analyze|compare|reproduce|falsify|deepen",
     "rationale": str (which goal it advances), "priority": "high|medium|low" } ] } ],
 "notes": str
}
"""
    + f"At most {MAX_TASKS_PER_DIRECTION} tasks per direction — the leanest set that tests the goal. "
    "Every task MUST be executable on the lab's hardware."
)


async def _approved_agenda(pool) -> tuple[str | None, list[int], str]:
    """(mission_statement, approved_direction_ids, formatted agenda)."""
    async with pool.acquire() as conn:
        mission = await conn.fetchval(
            f"SELECT statement FROM claims WHERE claim_kind = 'mission' AND status IN {_ACTIVE} ORDER BY id DESC LIMIT 1"
        )
        rows = await conn.fetch(
            "SELECT c.id, c.statement, ds.composite, ds.priority "
            "FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id "
            "LEFT JOIN direction_scores ds ON ds.claim_id = c.id "
            f"WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {_ACTIVE} "
            "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.claim_id = c.id) "  # idempotent: skip already-planned
            "ORDER BY COALESCE(ds.composite, 0) DESC, c.id"
        )
        goals_by_dir: dict[int, list] = {}
        if rows:
            for g in await conn.fetch(
                "SELECT claim_id, expectation, kill_condition, next_milestone FROM claim_goals WHERE claim_id = ANY($1)",
                [r["id"] for r in rows],
            ):
                goals_by_dir.setdefault(g["claim_id"], []).append(g)

    ids = [r["id"] for r in rows]
    lines = []
    for r in rows:
        lines.append(f"#{r['id']} [{r['priority'] or '—'}]: {r['statement'][:260]}")
        for g in goals_by_dir.get(r["id"], []):
            nm = f" || next: {g['next_milestone'][:100]}" if g["next_milestone"] else ""
            lines.append(f"     goal: expect={g['expectation'][:120]} || kill={g['kill_condition'][:100]}{nm}")
    return mission, ids, "\n".join(lines) if lines else "(no approved directions)"


async def run_planning(state, *, model: str | None = None) -> tuple[PlanOutput | None, list[int]]:
    """Decompose the APPROVED directions into research tasks. Returns (output, approved_ids).
    WRITES NOTHING. output is None when there is nothing approved to plan."""
    mission, ids, agenda = await _approved_agenda(state.pool)
    if not ids:
        return None, []
    lessons = await recall_lessons(state.pool)
    user = (
        f"# Mission\n{mission or '(none set)'}\n\n"
        f"# Approved directions to plan (reference each by its #id)\n{agenda}\n\n"
        f"# Lab capabilities & constraints (every task MUST fit this hardware)\n{LAB_CONSTRAINTS}\n\n"
        f"# {lessons or 'No standing lessons yet.'}\n\n"
        f"# Task\nDecompose each approved direction into executable research tasks. {_PLAN_SCHEMA_HINT}"
    )
    content = await _chain_complete(
        [{"role": "system", "content": _PLAN_SYSTEM}, {"role": "user", "content": user}],
        temperature=0.3,
        invocation_type="planner.decompose",
        step_name="plan",
        primary_model=model,
    )
    return PlanOutput.model_validate_json(_strip_fences(content)), ids


class PlanGrade(BaseModel):
    valid_refs: bool  # every plan references a REAL approved direction id
    tasks_wellformed: bool  # every task has a description + a valid task_type
    n_plans: int
    n_tasks: int
    invalid_refs: list[int]
    passed: bool


def grade_plan(out: PlanOutput, valid_ids: list[int]) -> PlanGrade:
    """Anti-hallucination + well-formedness gate: plans reference real approved directions,
    every task is a non-empty instruction with a known task_type, and 2+ tasks were produced."""
    vids = set(valid_ids)
    invalid = [p.claim_id for p in out.plans if p.claim_id not in vids]
    n_tasks = sum(len(p.tasks) for p in out.plans)
    valid_refs = bool(out.plans) and not invalid
    tasks_wf = n_tasks > 0 and all(
        t.description.strip() and t.task_type in TASK_TYPES for p in out.plans for t in p.tasks
    )
    return PlanGrade(
        valid_refs=valid_refs,
        tasks_wellformed=tasks_wf,
        n_plans=len(out.plans),
        n_tasks=n_tasks,
        invalid_refs=invalid[:8],
        passed=bool(valid_refs and tasks_wf),
    )
