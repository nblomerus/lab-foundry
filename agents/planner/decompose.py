"""
Planner decompose handler — Stage 2 of the research-execution loop.

Triggered by `planner.plan`. Decomposes Ariadne's APPROVED, not-yet-planned directions into
research tasks. Gated twice: the per-agent mode dial (agent_of -> 'planner'; off|shadow are
paused at the dispatcher) and the plan grade (refs real approved ids + tasks well-formed).
Persists only in advisory|active. Kept separate from the market-era handler.py so the new
direction→tasks flow is decoupled from the legacy queue.empty planner.
"""

from __future__ import annotations

import logging

from agents.planner.persist import persist_plan
from agents.planner.plan import grade_plan, run_planning
from harness.agent_modes import get_agent_mode

log = logging.getLogger(__name__)


async def handle_planner_decompose(event: dict, dispatcher) -> dict | None:
    """Plan the approved directions into tasks (advisory/active). Read-only if shadow."""
    state = dispatcher.state
    mode = await get_agent_mode(state.pool, "planner")

    out, ids = await run_planning(state)  # read approved directions (no writes)
    if out is None:
        log.info("planner: no approved un-planned directions")
        return {"mode": mode, "planned": False, "reason": "nothing_to_plan"}

    report = grade_plan(out, ids)
    summary = {"mode": mode, "plans": report.n_plans, "tasks": report.n_tasks, "graded_pass": report.passed}

    if mode not in {"advisory", "active"}:
        log.info("planner: mode=%s — planned, wrote nothing", mode)
        return {**summary, "persisted": False}
    if not report.passed:
        log.warning("planner: FAILED grading (invalid refs=%s) — not persisting", report.invalid_refs)
        return {**summary, "persisted": False, "reason": "failed_grading"}

    counts = await persist_plan(state, out, ids)
    log.info("planner: %s — created %s", mode, counts)
    return {**summary, "persisted": True, **counts}
