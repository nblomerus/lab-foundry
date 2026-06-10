"""
Quartermaster API — live compute state + experiment allocation, and a manual kill.

Powers the Ops / Quartermaster floorplan inspector: what the lab is running, what's
queued, each experiment's budgets/iterations/outcome, and live CPU/GPU/mem. Read-mostly;
the one mutation is a human kill switch for a runaway experiment.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request

from agents.experiments import sandbox
from ops.resources import sample_resources

log = logging.getLogger(__name__)

router = APIRouter(prefix="/quartermaster", tags=["quartermaster"])


def _obj(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return {}
    return v if isinstance(v, dict) else {}


def _exp_row(r) -> dict:
    params = _obj(r["params"])
    usage = _obj(r["resource_usage"])
    notes = r["researcher_notes"]
    ts = r["completed_at"] or r["started_at"]
    return {
        "id": r["id"],
        "kind": r["kind"],
        "status": r["status"],
        "claim_id": params.get("claim_id"),
        "hypothesis": params.get("hypothesis"),
        "requires_gpu": r["requires_gpu"],
        "gpu_mem_mb": r["gpu_mem_mb"],
        "priority": r["priority"],
        "wall_clock_budget_s": r["wall_clock_budget_s"],
        "mem_budget_mb": r["mem_budget_mb"],
        "iterations": usage.get("iterations"),
        "kill_reason": r["kill_reason"],
        "error": (r["error"] or "")[:300] or None,
        "interpretation": r["interpretation"],
        "researcher_notes": (notes[:400] if notes else None),
        "ingested_doc_id": r["ingested_doc_id"],
        "at": ts.isoformat() if ts else None,
    }


@router.get("/experiments")
async def experiments(request: Request, limit: int = 50) -> dict:
    """The experiment ledger at a glance: counts by status + the recent runs with budgets,
    iterations, and outcomes."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        by_status = {
            r["status"]: r["n"]
            for r in await conn.fetch("SELECT status, count(*) AS n FROM experiment_runs GROUP BY status")
        }
        rows = await conn.fetch(
            "SELECT id, kind, status, params, resource_usage, researcher_notes, interpretation, error, "
            "requires_gpu, gpu_mem_mb, priority, wall_clock_budget_s, mem_budget_mb, kill_reason, "
            "ingested_doc_id, started_at, completed_at "
            "FROM experiment_runs ORDER BY id DESC LIMIT $1",
            limit,
        )
        mode = await conn.fetchval("SELECT mode FROM agent_modes WHERE agent_name = 'quartermaster'") or "off"
    return {
        "mode": mode,
        "by_status": by_status,
        "running": by_status.get("running", 0),
        "queued": by_status.get("queued", 0),
        "experiments": [_exp_row(r) for r in rows],
    }


@router.get("/resources")
async def resources() -> dict:
    """Live CPU / memory / disk + per-GPU VRAM — the headroom the Quartermaster allocates against."""
    return await sample_resources()


@router.post("/experiments/{experiment_id}/kill")
async def kill(experiment_id: int, request: Request) -> dict:
    """Human kill switch — terminate a runaway experiment's container now and mark the row."""
    await sandbox.kill(experiment_id)  # docker kill lf-exp-<id> (deterministic name)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE experiment_runs SET status = 'killed', kill_reason = $2, killed_at = now(), completed_at = now() "
            "WHERE id = $1 AND status IN ('running', 'queued')",
            experiment_id,
            "manual kill (API)",
        )
    return {"killed": experiment_id}
