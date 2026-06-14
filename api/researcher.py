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


def _win(done: int, failed: int) -> float | None:
    tot = done + failed
    return round(100 * done / tot, 0) if tot else None


@router.get("/roster")
async def roster(request: Request) -> dict:
    """The named full-stack researchers (migration 022) + each one's ownership + experiment record —
    powers the /researchers roster and links experiments to the person who ran them."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT r.id, r.name, r.persona, r.specialty, r.status, "
            "(SELECT count(*) FROM claims c WHERE c.researcher_id = r.id AND c.claim_kind = 'direction' "
            "   AND c.status <> 'concluded') AS owned_directions, "
            "(SELECT count(*) FROM experiment_runs e WHERE e.researcher_id = r.id AND e.status = 'completed') AS done, "
            "(SELECT count(*) FROM experiment_runs e WHERE e.researcher_id = r.id "
            "   AND e.status IN ('failed','killed')) AS failed, "
            "(SELECT max(e.completed_at) FROM experiment_runs e WHERE e.researcher_id = r.id) AS last_at "
            "FROM researchers r ORDER BY r.id"
        )
    return {
        "researchers": [
            {
                "id": r["id"],
                "name": r["name"],
                "persona": r["persona"],
                "specialty": r["specialty"],
                "status": r["status"],
                "owned_directions": r["owned_directions"],
                "done": r["done"],
                "failed": r["failed"],
                "win_rate": _win(r["done"], r["failed"]),
                "last_at": r["last_at"].isoformat() if r["last_at"] else None,
            }
            for r in rows
        ]
    }


@router.get("/roster/{researcher_id}")
async def roster_detail(researcher_id: int, request: Request) -> dict:
    """One researcher: profile + the directions they own + their experiments (with failure class /
    realism) + a status/failure breakdown — the per-researcher drill-down."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, name, persona, specialty, status, model, created_at FROM researchers WHERE id = $1",
            researcher_id,
        )
        if r is None:
            return {"error": "not found", "id": researcher_id}
        directions = await conn.fetch(
            "SELECT c.id, c.statement, c.status::text AS status, c.confidence, dg.status AS gate "
            "FROM claims c LEFT JOIN direction_gate dg ON dg.claim_id = c.id "
            "WHERE c.researcher_id = $1 AND c.claim_kind = 'direction' ORDER BY c.id DESC",
            researcher_id,
        )
        exps = await conn.fetch(
            "SELECT e.id, e.status, e.data_realism, e.realism_mismatch, e.failure_class, e.requires_gpu, "
            "e.params, e.started_at, e.completed_at, c.statement AS claim_statement "
            "FROM experiment_runs e LEFT JOIN tasks t ON t.id = e.task_id LEFT JOIN claims c ON c.id = t.claim_id "
            "WHERE e.researcher_id = $1 ORDER BY e.id DESC LIMIT 80",
            researcher_id,
        )
        by_status = {
            row["status"]: row["n"]
            for row in await conn.fetch(
                "SELECT status, count(*) AS n FROM experiment_runs WHERE researcher_id = $1 GROUP BY status",
                researcher_id,
            )
        }
        by_fc = {
            row["fc"]: row["n"]
            for row in await conn.fetch(
                "SELECT coalesce(failure_class, '(unclassified)') AS fc, count(*) AS n FROM experiment_runs "
                "WHERE researcher_id = $1 AND status IN ('failed','killed') GROUP BY fc ORDER BY n DESC",
                researcher_id,
            )
        }
    done, failed = by_status.get("completed", 0), by_status.get("failed", 0) + by_status.get("killed", 0)
    return {
        "id": r["id"],
        "name": r["name"],
        "persona": r["persona"],
        "specialty": r["specialty"],
        "status": r["status"],
        "model": r["model"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "win_rate": _win(done, failed),
        "by_status": by_status,
        "by_failure_class": by_fc,
        "directions": [
            {
                "id": d["id"],
                "statement": d["statement"],
                "status": d["status"],
                "gate": d["gate"],
                "confidence": float(d["confidence"]) if d["confidence"] is not None else None,
            }
            for d in directions
        ],
        "experiments": [
            {
                "id": e["id"],
                "status": e["status"],
                "data_realism": e["data_realism"],
                "realism_mismatch": e["realism_mismatch"],
                "failure_class": e["failure_class"],
                "requires_gpu": e["requires_gpu"],
                "hypothesis": _result(e["params"]).get("hypothesis"),
                "claim_statement": e["claim_statement"],
                "at": (e["completed_at"] or e["started_at"]).isoformat()
                if (e["completed_at"] or e["started_at"])
                else None,
            }
            for e in exps
        ],
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
