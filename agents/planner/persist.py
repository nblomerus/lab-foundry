"""
Persist the Planner's output — the advisory/active write path for Stage 2.

In SHADOW the Planner writes nothing (ops.planner_firstlight). In ADVISORY/ACTIVE its plan
becomes work: each ResearchTask lands as a `tasks` row (department='research', linked to its
direction via claim_id, status='pending'). The `trg_emit_task_created` trigger then fires
`task.created`, which the Researcher (Stage 3) consumes. Tasks are surgically reversible
(DELETE FROM tasks WHERE payload->>'from' = 'planner') and never touch the corpus.
"""

from __future__ import annotations

import json
import logging

from agents.planner.plan import MAX_TASKS_PER_DIRECTION, TASK_TYPES

log = logging.getLogger(__name__)

_PRIORITY = {"high": 8, "medium": 5, "low": 3}


async def persist_plan(state, out, valid_ids, *, run_id: int | None = None) -> dict:
    """Write the planned tasks (status='pending') for each approved direction. Returns counts."""
    vids = set(valid_ids)
    n_tasks = 0
    async with state.pool.acquire() as conn, conn.transaction():
        for p in out.plans:
            if p.claim_id not in vids:
                continue
            # Hand a thin_corpus-STUCK direction to the closure ladder instead of refilling it.
            # If its last 3 completed tasks were all thin_corpus, more tasks just churn — AND
            # each fresh planner task (no closure stage) resets the ladder's scout→retry→retire
            # state machine, so the direction never gets retired. Skip it; the watchdog ladder
            # needs the direction to DRAIN to fire its scout sweep and then declare the gap.
            stuck = await conn.fetchval(
                "SELECT count(*) = 3 AND bool_and(result->>'disposition' = 'thin_corpus') "
                "FROM (SELECT result FROM tasks WHERE claim_id = $1 AND status = 'completed' "
                "      AND result->>'disposition' IS NOT NULL ORDER BY id DESC LIMIT 3) t",
                p.claim_id,
            )
            if stuck:
                continue
            # Cap PENDING tasks per direction (not per invocation). MAX_TASKS_PER_DIRECTION
            # bounds one plan, but Ariadne re-emits planner.plan as she deliberates/reflects,
            # so without this a direction accumulates dozens of pending tasks across calls
            # (observed: 13-16 each). Only fill the remaining room up to the cap, so a
            # direction never has more than its lean pending set — fewer tasks, higher focus.
            existing = (
                await conn.fetchval(
                    "SELECT count(*) FROM tasks WHERE claim_id = $1 AND department = 'research' AND status = 'pending'",
                    p.claim_id,
                )
                or 0
            )
            room = MAX_TASKS_PER_DIRECTION - existing
            if room <= 0:
                continue  # already has its lean pending set — don't pile on
            for t in p.tasks[:room]:  # hard cap — leanest pending set per direction
                if not t.description.strip() or t.task_type not in TASK_TYPES:
                    continue
                await conn.execute(
                    "INSERT INTO tasks (department, task_type, description, payload, priority, status, claim_id) "
                    "VALUES ('research', $1, $2, $3, $4, 'pending', $5)",
                    t.task_type,
                    t.description[:4000],
                    json.dumps(
                        {
                            "title": t.title,
                            "rationale": t.rationale,
                            "from": "planner",
                            "direction_id": p.claim_id,
                            "run_id": run_id,
                        }
                    ),
                    _PRIORITY.get(t.priority, 5),
                    p.claim_id,
                )
                n_tasks += 1
    log.info("planner: persisted %d task(s) across %d direction(s)", n_tasks, len(out.plans))
    return {"tasks": n_tasks, "directions_planned": len(out.plans)}
