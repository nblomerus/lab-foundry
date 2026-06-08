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
            for t in p.tasks[:MAX_TASKS_PER_DIRECTION]:  # hard cap — leanest set per direction
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
