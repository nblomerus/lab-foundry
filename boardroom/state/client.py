"""
Direct Postgres client for boardroom state.

In-process equivalent of the boardroom-state MCP server — same surface,
no MCP transport. The harness uses this client directly; external agents
or other tools that need MCP go through the server.

All mutations emit events into the same transaction so consumers wake up
via pg_notify.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Optional

import asyncpg
from pydantic import BaseModel


# -------------------------------------------------------------------------
# Pydantic return types
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


class Finding(BaseModel):
    id: int
    task_id: int
    thesis_id: Optional[int]
    source: Optional[str]
    url: Optional[str]
    title: Optional[str]
    summary: str
    relevance_score: float
    why_it_matters: Optional[str]
    audit_score: Optional[float]
    audit_verdict: Optional[str]
    supports_thesis: Optional[bool]
    created_at: datetime


class AdversaryVerdict(BaseModel):
    id: int
    thesis_id: int
    verdict: str
    confidence: float
    reasoning: str
    cited_finding_ids: list[int]
    created_at: datetime


# -------------------------------------------------------------------------
# Client
# -------------------------------------------------------------------------

class PostgresClient:
    """Async typed access to boardroom state."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ---- Reads ---------------------------------------------------------

    async def get_company_state(self) -> CompanyState:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM company_state WHERE id = 1")
            if row is None:
                raise RuntimeError("company_state not seeded; run bootstrap first")
            return CompanyState(**dict(row))

    async def get_active_theses(
        self,
        limit: int = 10,
        sort_by: Literal["confidence", "recent"] = "confidence",
        exclude_ids: Optional[list[int]] = None,
    ) -> list[Thesis]:
        order = "confidence DESC" if sort_by == "confidence" else "updated_at DESC"
        async with self.pool.acquire() as conn:
            if exclude_ids:
                rows = await conn.fetch(
                    f"SELECT * FROM theses WHERE status = 'active' "
                    f"AND id != ALL($2) ORDER BY {order} LIMIT $1",
                    limit, exclude_ids,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT * FROM theses WHERE status = 'active' "
                    f"ORDER BY {order} LIMIT $1",
                    limit,
                )
            return [Thesis(**dict(r)) for r in rows]

    async def get_thesis(self, thesis_id: int) -> Thesis:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM theses WHERE id = $1", thesis_id)
            if row is None:
                raise ValueError(f"thesis {thesis_id} not found")
            return Thesis(**dict(row))

    async def count_active_theses(self) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM theses WHERE status = 'active'"
            )

    async def get_task(self, task_id: int) -> Task:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
            if row is None:
                raise ValueError(f"task {task_id} not found")
            d = dict(row)
            if isinstance(d.get("payload"), str):
                d["payload"] = json.loads(d["payload"])
            return Task(**d)

    async def get_finding(self, finding_id: int) -> Finding:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM findings WHERE id = $1", finding_id)
            if row is None:
                raise ValueError(f"finding {finding_id} not found")
            return Finding(**dict(row))

    async def get_findings(self, ids: list[int]) -> list[Finding]:
        if not ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM findings WHERE id = ANY($1) ORDER BY id",
                ids,
            )
            return [Finding(**dict(r)) for r in rows]

    async def get_unaudited_findings_for_task(self, task_id: int) -> list[Finding]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM findings WHERE task_id = $1 AND audit_verdict IS NULL "
                "ORDER BY id",
                task_id,
            )
            return [Finding(**dict(r)) for r in rows]

    async def get_adversary_verdict(self, verdict_id: int) -> AdversaryVerdict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM adversary_verdicts WHERE id = $1", verdict_id,
            )
            if row is None:
                raise ValueError(f"verdict {verdict_id} not found")
            return AdversaryVerdict(**dict(row))

    # ---- Mutations (with event emission in same transaction) -----------

    async def update_finding_audit(
        self,
        finding_id: int,
        audit_score: float,
        audit_verdict: Literal["pass", "slop", "unclear"],
        run_id: Optional[int] = None,
    ) -> None:
        """
        Persist auditor verdict on a finding. If verdict=pass and relevance>=8,
        emit finding.high_signal so the CEO knows there's signal to reconsider.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE findings
                    SET audit_score = $1, audit_verdict = $2
                    WHERE id = $3 AND audit_verdict IS NULL
                    RETURNING task_id, thesis_id, relevance_score
                    """,
                    audit_score, audit_verdict, finding_id,
                )
                if row is None:
                    return  # already audited; no-op

                if (
                    audit_verdict == "pass"
                    and row["relevance_score"] >= 8
                    and row["thesis_id"] is not None
                ):
                    await conn.execute(
                        """
                        INSERT INTO events (
                            event_type, target_type, target_id, payload,
                            emitted_by_run_id, dedup_key
                        )
                        VALUES (
                            'finding.high_signal', 'thesis', $1, $2::jsonb, $3, $4
                        )
                        ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                        """,
                        row["thesis_id"],
                        json.dumps({
                            "finding_id": finding_id,
                            "score": float(row["relevance_score"]),
                        }),
                        run_id,
                        f"highsig-{finding_id}",
                    )

    async def detect_slop_breaker(self, thesis_id: int) -> bool:
        """
        Compute slop rate over last 24h for a thesis; if >= 40% on >= 5 audited
        findings, emit audit.slop_detected and return True.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE audit_verdict = 'slop') AS slop_count
                FROM findings
                WHERE thesis_id = $1
                  AND created_at > NOW() - INTERVAL '24 hours'
                  AND audit_verdict IS NOT NULL
                """,
                thesis_id,
            )
            total = row["total"] or 0
            slop_count = row["slop_count"] or 0
            if total < 5:
                return False
            slop_rate = slop_count / total
            if slop_rate < 0.40:
                return False

            import time
            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
                VALUES (
                    'audit.slop_detected', 'thesis', $1, $2::jsonb, $3
                )
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                thesis_id,
                json.dumps({"slop_rate": slop_rate, "window_size": total}),
                f"slop-{thesis_id}-{int(time.time())}",
            )
            return True

    # ---- Task queue ----------------------------------------------------

    async def claim_task(
        self, worker_id: str, department: str,
    ) -> Optional[Task]:
        """
        Claim the next pending task for `department` using FOR UPDATE SKIP
        LOCKED so concurrent workers cannot claim the same row.
        Returns None when the queue is empty.
        """
        async with self.pool.acquire() as conn:
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
                if row is None:
                    return None
                d = dict(row)
                if isinstance(d.get("payload"), str):
                    d["payload"] = json.loads(d["payload"])
                return Task(**d)

    async def complete_task(self, task_id: int, result: dict) -> None:
        """Mark a running task completed; trigger emits task.completed event."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchval(
                    """
                    UPDATE tasks
                    SET status = 'completed',
                        completed_at = NOW(),
                        result = $1::jsonb
                    WHERE id = $2 AND status = 'running'
                    RETURNING id
                    """,
                    json.dumps(result), task_id,
                )
                if updated is None:
                    raise ValueError(f"task {task_id} not in running state")
                await conn.execute(
                    """
                    INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
                    VALUES (
                        'task.completed', 'task', $1, $2::jsonb, $3
                    )
                    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                    """,
                    task_id, json.dumps(result), f"taskdone-{task_id}",
                )

    async def fail_task(self, task_id: int, error: str) -> None:
        """Mark a task failed. Watchdog handles retry decisions."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    completed_at = NOW(),
                    halt_reason = $1
                WHERE id = $2
                """,
                error[:500], task_id,
            )

    # ---- Findings ------------------------------------------------------

    async def record_finding(
        self,
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
        """Insert a finding; the Auditor will score it before it's eligible."""
        if not 1 <= relevance_score <= 10:
            raise ValueError("relevance_score must be in [1, 10]")
        async with self.pool.acquire() as conn:
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

    async def get_recent_findings_for_thesis(
        self, thesis_id: int, limit: int = 20,
    ) -> list[Finding]:
        """Return the N most recent findings for a thesis, newest first."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM findings
                WHERE thesis_id = $1 AND COALESCE(audit_verdict, '') != 'stale'
                ORDER BY created_at DESC
                LIMIT $2
                """,
                thesis_id, limit,
            )
            return [Finding(**dict(r)) for r in rows]

    # ---- Thesis lifecycle ---------------------------------------------

    async def create_thesis(
        self,
        claim: str,
        initial_confidence: float = 0.50,
        parent_id: Optional[int] = None,
        created_by_run_id: Optional[int] = None,
    ) -> Thesis:
        if not 0 <= initial_confidence <= 1:
            raise ValueError("initial_confidence must be in [0, 1]")
        async with self.pool.acquire() as conn:
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
                    VALUES ('thesis.created', 'thesis', $1, $2::jsonb, $3)
                    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                    """,
                    row["id"],
                    json.dumps({"claim": claim, "parent_id": parent_id}),
                    f"create-{row['id']}",
                )
                return Thesis(**dict(row))

    async def update_thesis_confidence(
        self,
        thesis_id: int,
        new_confidence: float,
        reason: str,
        run_id: Optional[int] = None,
    ) -> Thesis:
        if not 0 <= new_confidence <= 1:
            raise ValueError("new_confidence must be in [0, 1]")
        async with self.pool.acquire() as conn:
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
                    raise ValueError(f"thesis {thesis_id} not active")
                import time
                await conn.execute(
                    """
                    INSERT INTO events (
                        event_type, target_type, target_id, payload,
                        emitted_by_run_id, dedup_key
                    )
                    VALUES (
                        'thesis.confidence_changed', 'thesis', $1, $2::jsonb, $3, $4
                    )
                    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                    """,
                    thesis_id,
                    json.dumps({
                        "from": float(row["confidence_prev"]) if row["confidence_prev"] is not None else None,
                        "to": new_confidence,
                        "reason": reason,
                    }),
                    run_id,
                    f"conf-{thesis_id}-{int(time.time())}",
                )
                return Thesis(**dict(row))

    async def kill_thesis(
        self,
        thesis_id: int,
        reason: str,
        verdict_id: int,
        run_id: Optional[int] = None,
    ) -> Thesis:
        """
        Kill an active thesis. Marks unaudited findings stale (so the curator
        filters them from future recall). Idempotent — killing an already-
        killed thesis returns it without error. Emits thesis.invalidated.
        """
        async with self.pool.acquire() as conn:
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
                    row = await conn.fetchrow(
                        "SELECT * FROM theses WHERE id = $1", thesis_id,
                    )
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
                    INSERT INTO events (
                        event_type, target_type, target_id, payload,
                        emitted_by_run_id, dedup_key
                    )
                    VALUES (
                        'thesis.invalidated', 'thesis', $1, $2::jsonb, $3, $4
                    )
                    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                    """,
                    thesis_id,
                    json.dumps({"reason": reason[:300], "verdict_id": verdict_id}),
                    run_id,
                    f"kill-{thesis_id}",
                )
                return Thesis(**dict(row))

    # ---- Adversary verdicts -------------------------------------------

    async def create_adversary_verdict(
        self,
        thesis_id: int,
        verdict: str,
        confidence: float,
        reasoning: str,
        cited_finding_ids: list[int],
        run_id: Optional[int] = None,
        first_pass_verdict: Optional[str] = None,
        first_pass_reasoning: Optional[str] = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO adversary_verdicts (
                    thesis_id, verdict, confidence, reasoning,
                    cited_finding_ids, run_id,
                    first_pass_verdict, first_pass_reasoning,
                    revised
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id
                """,
                thesis_id, verdict, confidence, reasoning,
                cited_finding_ids, run_id,
                first_pass_verdict, first_pass_reasoning,
                first_pass_verdict is not None,
            )
