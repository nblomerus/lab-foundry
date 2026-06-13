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
        "data_realism": r["data_realism"],
        "realism_mismatch": r["realism_mismatch"],
        "claim_id": params.get("claim_id"),
        "claim_statement": r["claim_statement"],
        "claim_confidence": float(r["claim_confidence"]) if r["claim_confidence"] is not None else None,
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
        "worker": r["worker"],
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
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
            "SELECT e.id, e.kind, e.status, e.data_realism, e.realism_mismatch, e.params, "
            "e.resource_usage, e.researcher_notes, "
            "e.interpretation, e.error, e.requires_gpu, e.gpu_mem_mb, e.priority, "
            "e.wall_clock_budget_s, e.mem_budget_mb, e.kill_reason, e.ingested_doc_id, "
            "e.started_at, e.completed_at, e.worker, "
            "c.statement AS claim_statement, c.confidence AS claim_confidence "
            "FROM experiment_runs e "
            "LEFT JOIN tasks t ON t.id = e.task_id "
            "LEFT JOIN claims c ON c.id = t.claim_id "
            "ORDER BY e.id DESC LIMIT $1",
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


@router.get("/experiments/{experiment_id}")
async def experiment_detail(experiment_id: int, request: Request) -> dict:
    """Everything one experiment did — the full code that ran, its result, the
    researcher's note + interpretation, the reproducibility provenance (image
    digest / seed / code hash), and the dataset lineage + Library docs."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT e.id, e.kind, e.status, e.data_realism, e.realism_mismatch, e.params, e.code, "
            "e.result, e.error, e.resource_usage, "
            "e.provenance, e.dataset_refs, e.researcher_notes, e.interpretation, e.requires_gpu, "
            "e.gpu_mem_mb, e.priority, e.wall_clock_budget_s, e.mem_budget_mb, e.kill_reason, "
            "e.worker, e.ingested_doc_id, e.started_at, e.completed_at, "
            "c.statement AS claim_statement, c.confidence AS claim_confidence "
            "FROM experiment_runs e "
            "LEFT JOIN tasks t ON t.id = e.task_id "
            "LEFT JOIN claims c ON c.id = t.claim_id "
            "WHERE e.id = $1",
            experiment_id,
        )
        if r is None:
            return {"error": "not found", "id": experiment_id}
    params = _obj(r["params"])
    started, completed = r["started_at"], r["completed_at"]
    return {
        "id": r["id"],
        "kind": r["kind"],
        "status": r["status"],
        "data_realism": r["data_realism"],
        "realism_mismatch": r["realism_mismatch"],
        "claim_id": params.get("claim_id"),
        "claim_statement": r["claim_statement"],
        "claim_confidence": float(r["claim_confidence"]) if r["claim_confidence"] is not None else None,
        "hypothesis": params.get("hypothesis"),
        "dataset_plan": params.get("dataset_plan"),
        "code": r["code"],
        "result": _obj(r["result"]) or r["result"],
        "error": r["error"],
        "interpretation": r["interpretation"],
        "researcher_notes": r["researcher_notes"],
        "provenance": _obj(r["provenance"]),
        "dataset_refs": r["dataset_refs"],
        "resource_usage": _obj(r["resource_usage"]),
        "requires_gpu": r["requires_gpu"],
        "gpu_mem_mb": r["gpu_mem_mb"],
        "wall_clock_budget_s": r["wall_clock_budget_s"],
        "mem_budget_mb": r["mem_budget_mb"],
        "priority": r["priority"],
        "kill_reason": r["kill_reason"],
        "worker": r["worker"],
        "ingested_doc_id": r["ingested_doc_id"],
        "started_at": started.isoformat() if started else None,
        "completed_at": completed.isoformat() if completed else None,
        "duration_s": (round((completed - started).total_seconds(), 2) if started and completed else None),
    }


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
