"""
boardroom-state MCP server.

The typed boundary between agents and the boardroom Postgres state.
Agents never touch the database directly — every read and mutation goes
through these tools so it can be logged, rate-limited, and audited.

Mutations also emit events into the events table; pg_notify wakes up the
harness dispatcher.

Run as:
    python -m src.mcp_servers.boardroom_state.server

Configured via env:
    DATABASE_URL  postgres://user:pass@host:5432/boardroom
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Literal, Optional

import asyncpg
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

# -------------------------------------------------------------------------
# Setup
# -------------------------------------------------------------------------

DATABASE_URL = os.environ["DATABASE_URL"]

mcp = FastMCP("boardroom-state")
_pool: Optional[asyncpg.Pool] = None


async def _pool_handle() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


# -------------------------------------------------------------------------
# Return types
# -------------------------------------------------------------------------

class CompanyState(BaseModel):
    current_phase: str
    phase_started_at: datetime
    bootstrap_at: datetime
    deadline: datetime
    problem_statement: str
    stance: Optional[str]
    success_criterion: Optional[str]
    thesis: Optional[str]
    niche: Optional[str]
    audience: Optional[str]
    charter: Optional[str]
    paused: bool
    paused_reason: Optional[str]


class Thesis(BaseModel):
    id: int
    claim: str
    status: str
    confidence: float
    confidence_prev: Optional[float]
    parent_id: Optional[int]
    created_at: datetime
    last_evidence_at: Optional[datetime]
    kill_reason: Optional[str]


class Task(BaseModel):
    id: int
    thesis_id: Optional[int]
    objective_id: Optional[int]
    department: str
    task_type: str
    description: str
    payload: dict
    priority: int
    status: str


# -------------------------------------------------------------------------
# Read tools
# -------------------------------------------------------------------------

@mcp.tool()
async def get_company_state() -> CompanyState:
    """
    Get the company's top-level state: phase, seed problem, charter (if committed),
    deadline, pause status.
    """
    pool = await _pool_handle()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM company_state WHERE id = 1")
        if row is None:
            raise ValueError("company_state not seeded; bootstrap first")
        return CompanyState(**dict(row))


@mcp.tool()
async def get_active_theses(
    limit: int = 10,
    sort_by: Literal["confidence", "recent"] = "confidence",
) -> list[Thesis]:
    """
    List active theses.
        sort_by='confidence' (default): highest-confidence first.
        sort_by='recent': most recently updated first.
    Use a small limit (≤20) — this is for decision context, not exhaustive listing.
    """
    pool = await _pool_handle()
    order = "confidence DESC" if sort_by == "confidence" else "updated_at DESC"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM theses WHERE status = 'active' ORDER BY {order} LIMIT $1",
            limit,
        )
        return [Thesis(**dict(r)) for r in rows]


@mcp.tool()
async def get_thesis(thesis_id: int) -> Thesis:
    """Get a single thesis by id (any status, including killed/merged)."""
    pool = await _pool_handle()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM theses WHERE id = $1", thesis_id)
        if row is None:
            raise ValueError(f"thesis {thesis_id} not found")
        return Thesis(**dict(row))


@mcp.tool()
async def count_active_theses() -> int:
    """Count currently-active theses. Useful for the phase machine."""
    pool = await _pool_handle()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM theses WHERE status = 'active'"
        )


# -------------------------------------------------------------------------
# Thesis lifecycle (write)
# -------------------------------------------------------------------------

@mcp.tool()
async def create_thesis(
    claim: str,
    initial_confidence: float = 0.50,
    parent_id: Optional[int] = None,
    created_by_run_id: Optional[int] = None,
) -> Thesis:
    """
    Create a new thesis. Claims are immutable — to refine a thesis, create
    a child by setting parent_id (it represents evolution of an idea).

    Emits `thesis.created` event.
    """
    if not 0 <= initial_confidence <= 1:
        raise ValueError("initial_confidence must be in [0, 1]")

    pool = await _pool_handle()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO theses (claim, confidence, parent_id, created_by_run_id)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                claim, initial_confidence, parent_id, created_by_run_id,
            )
            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
                VALUES ('thesis.created', 'thesis', $1, $2, 'create-' || $1::text)
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                row["id"],
                {"claim": claim, "parent_id": parent_id},
            )
            return Thesis(**dict(row))


@mcp.tool()
async def update_thesis_confidence(
    thesis_id: int,
    new_confidence: float,
    reason: str,
    run_id: Optional[int] = None,
) -> Thesis:
    """
    Update confidence on an active thesis. Records previous value for delta
    display in the dashboard. Emits `thesis.confidence_changed`.

    Only modifies active theses. Killed/merged theses raise an error.
    """
    if not 0 <= new_confidence <= 1:
        raise ValueError("new_confidence must be in [0, 1]")

    pool = await _pool_handle()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE theses
                SET confidence_prev = confidence,
                    confidence = $1,
                    updated_at = NOW()
                WHERE id = $2 AND status = 'active'
                RETURNING *
                """,
                new_confidence, thesis_id,
            )
            if row is None:
                raise ValueError(f"thesis {thesis_id} not active or not found")

            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, target_id, payload, emitted_by_run_id, dedup_key)
                VALUES (
                    'thesis.confidence_changed',
                    'thesis',
                    $1,
                    $2,
                    $3,
                    'conf-' || $1::text || '-' || EXTRACT(EPOCH FROM NOW())::bigint::text
                )
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                thesis_id,
                {
                    "from": float(row["confidence_prev"]) if row["confidence_prev"] is not None else None,
                    "to": new_confidence,
                    "reason": reason,
                },
                run_id,
            )
            return Thesis(**dict(row))


@mcp.tool()
async def kill_thesis(
    thesis_id: int,
    reason: str,
    verdict_id: int,
    run_id: Optional[int] = None,
) -> Thesis:
    """
    Kill an active thesis. Requires the adversary_verdict_id that justified the kill.
    Findings tied to this thesis are marked stale so the curator filters them
    from future recall.

    Idempotent: killing an already-killed thesis returns its record without error.
    Emits `thesis.invalidated`.
    """
    pool = await _pool_handle()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE theses
                SET status = 'killed',
                    killed_at = NOW(),
                    killed_by_verdict_id = $1,
                    kill_reason = $2,
                    updated_at = NOW()
                WHERE id = $3 AND status = 'active'
                RETURNING *
                """,
                verdict_id, reason, thesis_id,
            )
            if row is None:
                # Already killed or not found
                row = await conn.fetchrow("SELECT * FROM theses WHERE id = $1", thesis_id)
                if row is None:
                    raise ValueError(f"thesis {thesis_id} not found")
                return Thesis(**dict(row))

            await conn.execute(
                "UPDATE findings SET audit_verdict = 'stale' "
                "WHERE thesis_id = $1 AND audit_verdict IS NULL",
                thesis_id,
            )
            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, target_id, payload, emitted_by_run_id, dedup_key)
                VALUES ('thesis.invalidated', 'thesis', $1, $2, $3, 'kill-' || $1::text)
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                thesis_id,
                {"reason": reason, "verdict_id": verdict_id},
                run_id,
            )
            return Thesis(**dict(row))


# -------------------------------------------------------------------------
# Task queue
# -------------------------------------------------------------------------

@mcp.tool()
async def claim_task(worker_id: str, department: str) -> Optional[Task]:
    """
    Claim the next pending task for a department using FOR UPDATE SKIP LOCKED,
    so multiple workers in the same department claim safely in parallel.

    Returns None if the queue is empty for that department.
    Caller must complete_task or fail_task; stale 'running' tasks are reaped
    by the watchdog.
    """
    pool = await _pool_handle()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE tasks
                SET status = 'running',
                    claimed_by = $1,
                    started_at = NOW()
                WHERE id = (
                    SELECT id FROM tasks
                    WHERE status = 'pending' AND department = $2
                    ORDER BY priority DESC, created_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                worker_id, department,
            )
            return Task(**dict(row)) if row else None


@mcp.tool()
async def complete_task(task_id: int, result: dict) -> None:
    """
    Mark a task completed with its result payload. Emits `task.completed`,
    which downstream wakes Auditor + Adversary handlers.
    """
    pool = await _pool_handle()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE tasks
                SET status = 'completed', completed_at = NOW(), result = $1
                WHERE id = $2 AND status = 'running'
                RETURNING id
                """,
                result, task_id,
            )
            if updated is None:
                raise ValueError(f"task {task_id} not in 'running' state")
            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
                VALUES ('task.completed', 'task', $1, $2, 'task-done-' || $1::text)
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                task_id,
                result,
            )


@mcp.tool()
async def fail_task(task_id: int, error: str) -> None:
    """Mark a task failed. Does not auto-emit retries; the watchdog handles that."""
    pool = await _pool_handle()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE tasks
            SET status = 'failed', completed_at = NOW(), halt_reason = $1
            WHERE id = $2
            """,
            error, task_id,
        )


# -------------------------------------------------------------------------
# Findings
# -------------------------------------------------------------------------

@mcp.tool()
async def record_finding(
    task_id: int,
    source: str,
    title: str,
    summary: str,
    relevance_score: float,
    why_it_matters: str,
    thesis_id: Optional[int] = None,
    url: Optional[str] = None,
    supports_thesis: Optional[bool] = None,
) -> int:
    """
    Record a research finding. Returns its id.

    The Researcher's relevance_score is provisional. The Auditor will score it
    independently before the CEO sees it; until audited, audit_verdict is NULL
    and the curator filters it from high-signal recall.
    """
    if not 1 <= relevance_score <= 10:
        raise ValueError("relevance_score must be in [1, 10]")

    pool = await _pool_handle()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO findings (
                task_id, thesis_id, source, url, title, summary,
                relevance_score, why_it_matters, supports_thesis
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            task_id, thesis_id, source, url, title, summary,
            relevance_score, why_it_matters, supports_thesis,
        )


# -------------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()  # stdio transport (default); HTTP/SSE available via mcp.run("sse")
