"""
Researcher API — what the Library-grounded Researcher is doing and finding.

Powers the floorplan Researchers drill-down and the /researchers page. Reads the research tasks
(department='research') and the findings stored on their `result` jsonb (verdict / disposition /
grounded evidence / kill-condition check / gaps / next step), plus the researcher's acquire
activity. Read-only.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request

log = logging.getLogger(__name__)

router = APIRouter(prefix="/researcher", tags=["researcher"])

_DISPOSITIONS = ("supported", "contradicted", "corpus_exhausted", "thin_corpus", "needs_experiment", "inconclusive")


def _result(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return {}
    return v if isinstance(v, dict) else {}


def _task_row(r) -> dict:
    res = _result(r["result"])
    applied = res.get("applied") or {}
    return {
        "id": r["id"],
        "task_type": r["task_type"],
        "status": r["status"],
        "description": r["description"],
        "claim_id": r["claim_id"],
        "direction": r["direction"],
        "at": (r["completed_at"] or r["started_at"] or r["created_at"]).isoformat()
        if (r["completed_at"] or r["started_at"] or r["created_at"])
        else None,
        "finding": {
            "verdict": res.get("verdict"),
            "disposition": res.get("disposition"),
            "grounded": res.get("grounded"),
            "summary": res.get("summary"),
            "key_evidence": res.get("key_evidence") or [],
            "kill_condition_check": res.get("kill_condition_check"),
            "gaps": res.get("gaps") or [],
            "acquire_queries": res.get("acquire_queries") or [],
            "next_step": res.get("next_step"),
            "queries": res.get("queries") or [],
            "n_evidence": res.get("n_evidence"),
            "confidence_move": applied.get("confidence"),
            "acquires_fired": applied.get("acquires_fired"),
        }
        if res
        else None,
    }


@router.get("/overview")
async def overview(request: Request, limit: int = 30) -> dict:
    """The Researcher at a glance: mode, task throughput, findings by disposition, the recent
    findings in full, and the researcher's acquire activity."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        mode = await conn.fetchval("SELECT mode FROM agent_modes WHERE agent_name = 'researcher'") or "off"
        by_status = {
            r["status"]: r["n"]
            for r in await conn.fetch(
                "SELECT status::text AS status, count(*) AS n FROM tasks WHERE department = 'research' GROUP BY status"
            )
        }
        tasks = await conn.fetch(
            "SELECT t.id, t.task_type, t.status::text AS status, t.description, t.claim_id, "
            "       t.created_at, t.started_at, t.completed_at, c.statement AS direction, t.result "
            "FROM tasks t LEFT JOIN claims c ON c.id = t.claim_id "
            "WHERE t.department = 'research' ORDER BY t.id DESC LIMIT $1",
            min(limit, 100),
        )
        # researcher acquire activity (self-healing fetches)
        acq_fired = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type = 'acquire.requested' "
            "AND payload->>'requester' = 'researcher' AND emitted_at > now() - interval '24 hours'"
        )
        acq_rep = await conn.fetch(
            "SELECT payload->>'status' AS status FROM events "
            "WHERE event_type = 'acquire.fulfilled' AND payload->>'requester' = 'researcher' "
            "AND emitted_at > now() - interval '24 hours'"
        )

    rows = [_task_row(r) for r in tasks]
    by_disposition: dict[str, int] = {}
    for t in rows:
        d = (t["finding"] or {}).get("disposition")
        if d:
            by_disposition[d] = by_disposition.get(d, 0) + 1
    acq_outcomes: dict[str, int] = {}
    for r in acq_rep:
        acq_outcomes[r["status"]] = acq_outcomes.get(r["status"], 0) + 1

    return {
        "mode": mode,
        "tasks_total": sum(by_status.values()),
        "by_status": by_status,
        "by_disposition": by_disposition,
        "acquire": {
            "fired_24h": acq_fired or 0,
            "replied": sum(acq_outcomes.values()),
            "outcomes": acq_outcomes,
            "pending": (acq_fired or 0) - sum(acq_outcomes.values()),
        },
        "tasks": rows,
    }
