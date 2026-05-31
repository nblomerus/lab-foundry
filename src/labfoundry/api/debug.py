"""
LabFoundry Debug view — read-only window into live harness agent activity.

Every model call the lab makes (knowledge acquisition, evaluation, criticism, planning)
is recorded in `agent_runs` (status, model, tokens, error, output summary). This view
exposes that telemetry so you can observe exactly what each agent produced, why runs
failed, and trace the research logic step-by-step. Purely observational — no writes,
no effect on the research loop.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Request

router = APIRouter(prefix="/debug", tags=["debug"])

# DeepSeek v4-flash pricing (USD/token). Cache-miss input rate, so the spend
# figure is a slight upper bound (prefix caching makes real cost lower).
DS_INPUT_PER_TOK = 0.14 / 1_000_000
DS_OUTPUT_PER_TOK = 0.28 / 1_000_000
# Electricity rate for the GPU-power projection. Override via env.
ELEC_RATE = float(os.environ.get("ELECTRICITY_RATE_USD_PER_KWH", "0.15"))

_balance_cache: dict = {"ts": 0.0, "data": None}


async def _gpu_power() -> list[dict]:
    """Live per-GPU watts via nvidia-smi (empty list if unavailable)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=index,name,power.draw,utilization.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        gpus = []
        for line in out.decode().strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            gpus.append({"index": int(parts[0]), "name": parts[1], "watts": float(parts[2]), "util": float(parts[3])})
        return gpus
    except Exception:
        return []


async def _deepseek_balance() -> dict | None:
    """Live DeepSeek balance, cached 30s to avoid hammering on poll."""
    now = time.time()
    if _balance_cache["data"] is not None and now - _balance_cache["ts"] < 30:
        return _balance_cache["data"]
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"})
            d = r.json()
            bi = (d.get("balance_infos") or [{}])[0]
            bal = {
                "total": float(bi.get("total_balance", 0)),
                "topped_up": float(bi.get("topped_up_balance", 0)),
                "granted": float(bi.get("granted_balance", 0)),
                "currency": bi.get("currency", "USD"),
                "available": bool(d.get("is_available")),
            }
            _balance_cache.update(ts=now, data=bal)
            return bal
    except Exception:
        return _balance_cache["data"]


def _latency_ms(started, completed) -> int | None:
    if started and completed:
        return int((completed - started).total_seconds() * 1000)
    return None


@router.get("/agent-runs")
async def agent_runs(
    request: Request,
    limit: int = 100,
    status: str | None = None,
    invocation_type: str | None = None,
) -> dict:
    pool = request.app.state.pool
    where, args = [], []
    # Columns must be prefixed `r.` since we join `events` below as alias `e`.
    if status:
        args.append(status)
        where.append(f"r.status = ${len(args)}")
    if invocation_type:
        args.append(invocation_type)
        where.append(f"r.invocation_type = ${len(args)}")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(min(limit, 500))

    async with pool.acquire() as conn:
        # Join the originating event so the UI can deep-link to a per-task
        # research tree. `target_type='task'` means target_id is a task id;
        # `target_type='thesis'` means it's a thesis id (no task tree).
        rows = await conn.fetch(
            f"""
            SELECT r.id, r.started_at, r.completed_at, r.agent_name,
                   r.invocation_type, r.model_tier, r.model_name, r.status,
                   r.error, r.output_summary,
                   r.input_token_count, r.output_token_count,
                   e.target_type AS trigger_target_type,
                   e.target_id   AS trigger_target_id
            FROM agent_runs r
            LEFT JOIN events e ON e.id = r.triggered_by_event_id
            {clause}
            ORDER BY r.id DESC LIMIT ${len(args)}
            """,
            *args,
        )
        # Facets for the UI filters, over a recent window.
        status_rows = await conn.fetch(
            "SELECT status, COUNT(*) AS n FROM agent_runs "
            "WHERE started_at > NOW() - INTERVAL '24 hours' GROUP BY status ORDER BY n DESC"
        )
        itype_rows = await conn.fetch(
            "SELECT DISTINCT invocation_type FROM agent_runs "
            "WHERE started_at > NOW() - INTERVAL '24 hours' ORDER BY invocation_type"
        )

    runs = [
        {
            "id": r["id"],
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "latency_ms": _latency_ms(r["started_at"], r["completed_at"]),
            "agent": r["agent_name"],
            "invocation_type": r["invocation_type"],
            "tier": r["model_tier"],
            "model_name": r["model_name"],
            "status": r["status"],
            "error": r["error"],
            "output_summary": r["output_summary"],
            "input_tokens": r["input_token_count"],
            "output_tokens": r["output_token_count"],
            "task_id": (r["trigger_target_id"] if r["trigger_target_type"] == "task" else None),
        }
        for r in rows
    ]

    return {
        "runs": runs,
        "facets": {
            "statuses": {r["status"]: r["n"] for r in status_rows},
            "invocation_types": [r["invocation_type"] for r in itype_rows],
        },
    }


@router.get("/research-tree/{task_id}")
async def research_tree(request: Request, task_id: int) -> dict:
    """
    Full research-tree for one task: inquiries, evidence, experiments, the
    final findings, and every linked agent_run. Drives the per-task Debug
    view so you can dissect each step of the loop.

    Returns {} for `task` if the task_id is unknown.
    """
    pool = request.app.state.pool
    # Use the state client method (joined / parsed for us). We accept the pool
    # detour rather than importing the client globally to keep the API layer
    # free of harness imports.
    from labfoundry.state.client import PostgresClient

    client = PostgresClient(pool)
    return await client.get_research_tree(task_id)


@router.get("/costs")
async def costs(request: Request) -> dict:
    """Live spend (DeepSeek API) vs power (GPU electricity). Spend is computed
    from real `agent_runs` token counts for the paid model; everything else
    (free cloud, local) is $0. Power is a projection at the current draw."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date_trunc('day', started_at)::date AS day,
                   COALESCE(SUM(input_token_count), 0)  AS in_tok,
                   COALESCE(SUM(output_token_count), 0) AS out_tok,
                   COUNT(*) AS n
            FROM agent_runs
            WHERE model_name LIKE 'deepseek-v4%'      -- paid cloud model only
              AND started_at > NOW() - INTERVAL '7 days'
            GROUP BY 1 ORDER BY 1 DESC
            """
        )
    days = []
    for r in rows:
        cost = r["in_tok"] * DS_INPUT_PER_TOK + r["out_tok"] * DS_OUTPUT_PER_TOK
        days.append(
            {
                "day": r["day"].isoformat(),
                "calls": r["n"],
                "input_tokens": r["in_tok"],
                "output_tokens": r["out_tok"],
                "cost_usd": round(cost, 4),
            }
        )
    today_cost = days[0]["cost_usd"] if days else 0.0

    gpus = await _gpu_power()
    total_watts = round(sum(g["watts"] for g in gpus), 1)
    elec_day = round(total_watts / 1000 * 24 * ELEC_RATE, 2)  # at current draw
    balance = await _deepseek_balance()

    # Source-of-truth spend: snapshot DeepSeek's authoritative balance and
    # derive spend from its drop (DeepSeek has no usage-history endpoint).
    spend = {"tracked_since": None, "spent_tracked_usd": None, "spent_today_usd": None}
    if balance is not None:
        async with pool.acquire() as conn:
            last = await conn.fetchrow("SELECT recorded_at FROM deepseek_balance_log ORDER BY recorded_at DESC LIMIT 1")
            if last is None or (datetime.now(UTC) - last["recorded_at"]).total_seconds() > 300:
                await conn.execute(
                    "INSERT INTO deepseek_balance_log (total_balance, topped_up, granted) VALUES ($1, $2, $3)",
                    balance["total"],
                    balance["topped_up"],
                    balance["granted"],
                )
            first = await conn.fetchrow(
                "SELECT recorded_at, total_balance FROM deepseek_balance_log ORDER BY recorded_at ASC LIMIT 1"
            )
            first_today = await conn.fetchrow(
                "SELECT total_balance FROM deepseek_balance_log "
                "WHERE recorded_at >= date_trunc('day', NOW()) ORDER BY recorded_at ASC LIMIT 1"
            )
        if first:
            spend["tracked_since"] = first["recorded_at"].isoformat()
            spend["spent_tracked_usd"] = round(float(first["total_balance"]) - balance["total"], 4)
        if first_today:
            spend["spent_today_usd"] = round(float(first_today["total_balance"]) - balance["total"], 4)

    return {
        "deepseek": {
            "today_cost_usd": today_cost,  # token-based estimate (cross-check)
            "spent": spend,  # source-of-truth (DeepSeek balance deltas)
            "days": days,
            "balance": balance,
            "pricing": {"input_per_1m": 0.14, "output_per_1m": 0.28},
        },
        "power": {
            "gpus": gpus,
            "total_watts": total_watts,
            "rate_usd_per_kwh": ELEC_RATE,
            "projected_usd_per_day": elec_day,
        },
    }
