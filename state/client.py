"""
Direct Postgres client for labfoundry state.

In-process equivalent of the labfoundry-state MCP server — same surface,
no MCP transport. The harness uses this client directly; external agents
or other tools that need MCP go through the server.

All mutations emit events into the same transaction so consumers wake up
via pg_notify.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Literal

import asyncpg
from pydantic import BaseModel

log = logging.getLogger(__name__)

# ── The direction lifecycle state machine (the ONE legal write path is advance_direction) ──
# claims.status has no DB CHECK constraint (verified live), so legality is enforced here. A
# direction graduates UPWARD on findings, concludes or invalidates terminally, and the ONLY
# edge out of 'invalidated' is a reopen back to 'proposed' (the closure reopen rung). 'concluded'
# and 'merged' (legacy/market-era) are terminal.
_LEGAL_DIRECTION_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"tested", "weakly_supported", "replicated", "concluded", "invalidated"},
    "tested": {"weakly_supported", "replicated", "concluded", "invalidated"},
    "weakly_supported": {"replicated", "concluded", "invalidated"},
    "replicated": {"concluded", "invalidated"},
    "invalidated": {"proposed"},  # reopen only
    "concluded": set(),  # terminal
    "merged": set(),  # legacy/market-era; terminal
}
# Monotone rank — a finding never demotes a direction to a weaker status (the prior _RANK guard).
_STATUS_RANK: dict[str, int] = {"proposed": 0, "tested": 1, "weakly_supported": 2, "replicated": 3, "concluded": 4}
# Which lifecycle event each transition emits (None = silent, e.g. graduate; reopen's richer
# direction.reopened stays with the closure caller to avoid a double-emit).
_TRANSITION_EVENT: dict[str, str] = {
    "conclude": "direction.concluded",
    "gap": "claim.invalidated",
    "retire": "claim.invalidated",
    "supersede": "claim.invalidated",
}

# -------------------------------------------------------------------------
# Pydantic return types
# -------------------------------------------------------------------------


class CompanyState(BaseModel):
    current_phase: str
    phase_started_at: datetime
    bootstrap_at: datetime
    deadline: datetime
    problem_statement: str
    stance: str | None
    success_criterion: str | None
    thesis: str | None
    niche: str | None
    audience: str | None
    charter: str | None
    paused: bool
    paused_reason: str | None


class Claim(BaseModel):
    id: int
    statement: str
    status: str
    confidence: float
    confidence_prev: float | None
    parent_id: int | None
    created_at: datetime
    last_evidence_at: datetime | None
    invalidation_reason: str | None


# Backward compatibility alias for tests
Thesis = Claim


class Task(BaseModel):
    id: int
    claim_id: int | None
    objective_id: int | None
    department: str
    task_type: str
    description: str
    payload: dict
    priority: int
    status: str

    # Backward compatibility
    @property
    def thesis_id(self) -> int | None:
        return self.claim_id


class Finding(BaseModel):
    id: int
    task_id: int
    claim_id: int | None
    source: str | None
    url: str | None
    title: str | None
    summary: str
    relevance_score: float
    why_it_matters: str | None
    audit_score: float | None
    audit_verdict: str | None
    supports_thesis: bool | None
    created_at: datetime

    # Backward compatibility
    @property
    def thesis_id(self) -> int | None:
        return self.claim_id


class CriticVerdict(BaseModel):
    id: int
    claim_id: int
    verdict: str
    confidence: float
    reasoning: str
    cited_finding_ids: list[int]
    created_at: datetime


# Backward compatibility alias
AdversaryVerdict = CriticVerdict


# -------------------------------------------------------------------------
# Client
# -------------------------------------------------------------------------


class PostgresClient:
    """Async typed access to labfoundry state."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ---- Reads ---------------------------------------------------------

    async def get_company_state(self) -> CompanyState:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM company_state WHERE id = 1")
            if row is None:
                raise RuntimeError("company_state not seeded; run bootstrap first")
            return CompanyState(**dict(row))

    async def get_active_claims(
        self,
        limit: int = 10,
        sort_by: Literal["confidence", "recent"] = "confidence",
        exclude_ids: list[int] | None = None,
    ) -> list[Claim]:
        order = "confidence DESC" if sort_by == "confidence" else "updated_at DESC"
        async with self.pool.acquire() as conn:
            if exclude_ids:
                rows = await conn.fetch(
                    "SELECT * FROM claims WHERE status = 'proposed' OR status = 'tested' "
                    "OR status = 'weakly_supported' OR status = 'replicated' "
                    f"AND id != ALL($2) ORDER BY {order} LIMIT $1",
                    limit,
                    exclude_ids,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT * FROM claims WHERE status IN ('proposed', 'tested', 'weakly_supported', 'replicated') "
                    f"ORDER BY {order} LIMIT $1",
                    limit,
                )
            return [Claim(**dict(r)) for r in rows]

    # Backward compatibility
    async def get_active_theses(
        self,
        limit: int = 10,
        sort_by: Literal["confidence", "recent"] = "confidence",
        exclude_ids: list[int] | None = None,
    ) -> list[Claim]:
        return await self.get_active_claims(limit, sort_by, exclude_ids)

    async def get_claim(self, claim_id: int) -> Claim:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM claims WHERE id = $1", claim_id)
            if row is None:
                raise ValueError(f"claim {claim_id} not found")
            return Claim(**dict(row))

    # Backward compatibility
    async def get_thesis(self, thesis_id: int) -> Claim:
        return await self.get_claim(thesis_id)

    async def count_active_claims(self) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM claims WHERE status IN ('proposed', 'tested', 'weakly_supported', 'replicated')"
            )

    # Backward compatibility
    async def count_active_theses(self) -> int:
        return await self.count_active_claims()

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
                "SELECT * FROM findings WHERE task_id = $1 AND audit_verdict IS NULL ORDER BY id",
                task_id,
            )
            return [Finding(**dict(r)) for r in rows]

    async def get_critic_verdict(self, verdict_id: int) -> CriticVerdict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM critic_verdicts WHERE id = $1",
                verdict_id,
            )
            if row is None:
                raise ValueError(f"verdict {verdict_id} not found")
            return CriticVerdict(**dict(row))

    # Backward compatibility
    async def get_adversary_verdict(self, verdict_id: int) -> CriticVerdict:
        return await self.get_critic_verdict(verdict_id)

    # ---- Mutations (with event emission in same transaction) -----------

    async def update_finding_audit(
        self,
        finding_id: int,
        audit_score: float,
        audit_verdict: Literal["pass", "slop", "unclear"],
        run_id: int | None = None,
    ) -> None:
        """
        Persist evaluation verdict on a finding. If verdict=pass and relevance>=8,
        emit finding.high_signal so the PI knows there's signal to reconsider.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                    UPDATE findings
                    SET audit_score = $1, audit_verdict = $2
                    WHERE id = $3 AND audit_verdict IS NULL
                    RETURNING task_id, claim_id, relevance_score
                    """,
                audit_score,
                audit_verdict,
                finding_id,
            )
            if row is None:
                return  # already audited; no-op

            if audit_verdict == "pass" and row["relevance_score"] >= 8 and row["claim_id"] is not None:
                await conn.execute(
                    """
                        INSERT INTO events (
                            event_type, target_type, target_id, payload,
                            emitted_by_run_id, dedup_key
                        )
                        VALUES (
                            'finding.high_signal', 'claim', $1, $2::jsonb, $3, $4
                        )
                        ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                        """,
                    row["claim_id"],
                    json.dumps(
                        {
                            "finding_id": finding_id,
                            "score": float(row["relevance_score"]),
                        }
                    ),
                    run_id,
                    f"highsig-{finding_id}",
                )

    async def detect_slop_breaker(self, claim_id: int) -> bool:
        """
        Compute slop rate over last 24h for a claim; if >= 40% on >= 5 audited
        findings, emit audit.slop_detected and return True.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE audit_verdict = 'slop') AS slop_count
                FROM findings
                WHERE claim_id = $1
                  AND created_at > NOW() - INTERVAL '24 hours'
                  AND audit_verdict IS NOT NULL
                """,
                claim_id,
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
                    'audit.slop_detected', 'claim', $1, $2::jsonb, $3
                )
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                claim_id,
                json.dumps({"slop_rate": slop_rate, "window_size": total}),
                f"slop-{claim_id}-{int(time.time())}",
            )
            return True

    # ---- Task queue ----------------------------------------------------

    async def claim_task(
        self,
        worker_id: str,
        department: str,
    ) -> Task | None:
        """
        Claim the next pending task for `department` using FOR UPDATE SKIP
        LOCKED so concurrent workers cannot claim the same row.
        Returns None when the queue is empty.
        """
        async with self.pool.acquire() as conn, conn.transaction():
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
                worker_id,
                department,
            )
            if row is None:
                return None
            d = dict(row)
            if isinstance(d.get("payload"), str):
                d["payload"] = json.loads(d["payload"])
            return Task(**d)

    async def complete_task(self, task_id: int, result: dict) -> None:
        """Mark a running task completed; trigger emits task.completed event."""
        async with self.pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                    UPDATE tasks
                    SET status = 'completed',
                        completed_at = NOW(),
                        result = $1::jsonb
                    WHERE id = $2 AND status = 'running'
                    RETURNING id
                    """,
                json.dumps(result),
                task_id,
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
                task_id,
                json.dumps(result),
                f"taskdone-{task_id}",
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
                error[:500],
                task_id,
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
        claim_id: int | None = None,
        url: str | None = None,
        supports_thesis: bool | None = None,
    ) -> int:
        """Insert a finding; the Evaluation will score it before it's eligible."""
        if not 1 <= relevance_score <= 10:
            raise ValueError("relevance_score must be in [1, 10]")
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO findings (
                    task_id, claim_id, source, url, title, summary,
                    relevance_score, why_it_matters, supports_thesis
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id
                """,
                task_id,
                claim_id,
                source,
                url,
                title,
                summary,
                relevance_score,
                why_it_matters,
                supports_thesis,
            )

    async def get_recent_findings_for_claim(
        self,
        claim_id: int,
        limit: int = 20,
    ) -> list[Finding]:
        """Return the N most recent findings for a claim, newest first."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM findings
                WHERE claim_id = $1 AND COALESCE(audit_verdict, '') != 'stale'
                ORDER BY created_at DESC
                LIMIT $2
                """,
                claim_id,
                limit,
            )
            return [Finding(**dict(r)) for r in rows]

    # Backward compatibility
    async def get_recent_findings_for_thesis(self, thesis_id: int, limit: int = 20) -> list[Finding]:
        return await self.get_recent_findings_for_claim(thesis_id, limit)

    # ---- Claim lifecycle -----------------------------------------------

    async def create_claim(
        self,
        statement: str,
        initial_confidence: float = 0.50,
        parent_id: int | None = None,
        created_by_run_id: int | None = None,
    ) -> Claim:
        if not 0 <= initial_confidence <= 1:
            raise ValueError("initial_confidence must be in [0, 1]")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                    INSERT INTO claims (statement, confidence, parent_id, created_by_run_id, status)
                    VALUES ($1, $2, $3, $4, 'proposed')
                    RETURNING *
                    """,
                statement,
                initial_confidence,
                parent_id,
                created_by_run_id,
            )
            await conn.execute(
                """
                    INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
                    VALUES ('claim.created', 'claim', $1, $2::jsonb, $3)
                    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                    """,
                row["id"],
                json.dumps({"statement": statement, "parent_id": parent_id}),
                f"create-{row['id']}",
            )
            return Claim(**dict(row))

    # Backward compatibility
    async def create_thesis(
        self,
        claim: str,
        initial_confidence: float = 0.50,
        parent_id: int | None = None,
        created_by_run_id: int | None = None,
    ) -> Claim:
        return await self.create_claim(claim, initial_confidence, parent_id, created_by_run_id)

    async def update_claim_confidence(
        self,
        claim_id: int,
        new_confidence: float,
        reason: str,
        run_id: int | None = None,
    ) -> Claim:
        if not 0 <= new_confidence <= 1:
            raise ValueError("new_confidence must be in [0, 1]")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                    UPDATE claims
                    SET confidence_prev = confidence,
                        confidence = $1,
                        updated_at = NOW()
                    WHERE id = $2 AND status IN ('proposed', 'tested', 'weakly_supported', 'replicated')
                    RETURNING *
                    """,
                new_confidence,
                claim_id,
            )
            if row is None:
                raise ValueError(f"claim {claim_id} not active")
            import time

            await conn.execute(
                """
                    INSERT INTO events (
                        event_type, target_type, target_id, payload,
                        emitted_by_run_id, dedup_key
                    )
                    VALUES (
                        'claim.confidence_changed', 'claim', $1, $2::jsonb, $3, $4
                    )
                    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                    """,
                claim_id,
                json.dumps(
                    {
                        "from": float(row["confidence_prev"]) if row["confidence_prev"] is not None else None,
                        "to": new_confidence,
                        "reason": reason,
                    }
                ),
                run_id,
                f"conf-{claim_id}-{int(time.time())}",
            )
            return Claim(**dict(row))

    # Backward compatibility
    async def update_thesis_confidence(
        self,
        thesis_id: int,
        new_confidence: float,
        reason: str,
        run_id: int | None = None,
    ) -> Claim:
        return await self.update_claim_confidence(thesis_id, new_confidence, reason, run_id)

    async def invalidate_claim(
        self,
        claim_id: int,
        reason: str,
        verdict_id: int,
        run_id: int | None = None,
    ) -> Claim:
        """
        Invalidate an active claim. Marks unaudited findings stale (so the curator
        filters them from future recall). Idempotent — invalidating an already-
        invalidated claim returns it without error. Emits claim.invalidated.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                    UPDATE claims
                    SET status = 'invalidated',
                        invalidated_at = NOW(),
                        invalidated_by_verdict_id = $1,
                        invalidation_reason = $2,
                        updated_at = NOW()
                    WHERE id = $3 AND status IN ('proposed', 'tested', 'weakly_supported', 'replicated')
                    RETURNING *
                    """,
                verdict_id,
                reason,
                claim_id,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT * FROM claims WHERE id = $1",
                    claim_id,
                )
                if row is None:
                    raise ValueError(f"claim {claim_id} not found")
                return Claim(**dict(row))

            await conn.execute(
                "UPDATE findings SET audit_verdict = 'stale' WHERE claim_id = $1 AND audit_verdict IS NULL",
                claim_id,
            )
            await conn.execute(
                """
                    INSERT INTO events (
                        event_type, target_type, target_id, payload,
                        emitted_by_run_id, dedup_key
                    )
                    VALUES (
                        'claim.invalidated', 'claim', $1, $2::jsonb, $3, $4
                    )
                    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                    """,
                claim_id,
                json.dumps({"reason": reason[:300], "verdict_id": verdict_id}),
                run_id,
                f"invalidate-{claim_id}",
            )
            return Claim(**dict(row))

    # Backward compatibility
    async def kill_thesis(
        self,
        thesis_id: int,
        reason: str,
        verdict_id: int,
        run_id: int | None = None,
    ) -> Claim:
        return await self.invalidate_claim(thesis_id, reason, verdict_id, run_id)

    async def advance_direction(
        self,
        claim_id: int,
        to_status: str,
        *,
        transition: str,  # graduate | conclude | gap | retire | supersede | reopen
        decided_by: str,  # synthesis | reflect | closure | deliberate | human | auto | critic
        reason: str | None = None,
        run_id: int | None = None,
        monotone: bool = False,
        verdict_id: int | None = None,
        payload: dict | None = None,
        emit_event: bool = True,
        conn=None,
    ) -> dict | None:
        """The ONE legal write path for a direction's lifecycle status.

        Validates the edge against ``_LEGAL_DIRECTION_TRANSITIONS`` (claims.status has no DB
        CHECK), applies it, records a ``direction_transitions`` audit row, marks unaudited
        findings stale on invalidating transitions, and emits the lifecycle event
        (``direction.concluded`` on a conclude; ``claim.invalidated`` on gap/retire/supersede).
        Returns ``{claim_id, from, to, transition}`` on success, ``None`` if the edge was
        illegal, a no-op, or the row is not a live direction (logged, never raised).

        Pass ``conn`` to run inside a caller's existing transaction (synthesis); otherwise a
        fresh transaction is opened. ``monotone`` rejects any edge that does not raise the rank
        (the finding-graduation guard)."""
        async with contextlib.AsyncExitStack() as stack:
            if conn is None:
                conn = await stack.enter_async_context(self.pool.acquire())
                await stack.enter_async_context(conn.transaction())
            cur = await conn.fetchval(
                "SELECT status::text FROM claims WHERE id = $1 AND claim_kind = 'direction' FOR UPDATE",
                claim_id,
            )
            if cur is None:
                log.info("advance_direction: claim %s is not a live direction — skipped (%s)", claim_id, transition)
                return None
            if to_status == cur or to_status not in _LEGAL_DIRECTION_TRANSITIONS.get(cur, set()):
                log.info(
                    "advance_direction: illegal/no-op %s→%s (%s) for direction %s — skipped",
                    cur,
                    to_status,
                    transition,
                    claim_id,
                )
                return None
            if monotone and _STATUS_RANK.get(to_status, -1) <= _STATUS_RANK.get(cur, -1):
                return None

            if to_status == "invalidated":
                await conn.execute(
                    "UPDATE claims SET status = 'invalidated', invalidated_at = now(), "
                    "invalidation_reason = $2, invalidated_by_verdict_id = $3, updated_at = now() WHERE id = $1",
                    claim_id,
                    (reason or "")[:2000] or None,
                    verdict_id,
                )
                # A dead direction's unaudited findings should not surface in recall (mirrors invalidate_claim).
                await conn.execute(
                    "UPDATE findings SET audit_verdict = 'stale' WHERE claim_id = $1 AND audit_verdict IS NULL",
                    claim_id,
                )
            elif transition == "reopen":
                await conn.execute(
                    "UPDATE claims SET status = $2::claim_status, invalidated_at = NULL, "
                    "invalidated_by_verdict_id = NULL, invalidation_reason = NULL, updated_at = now() WHERE id = $1",
                    claim_id,
                    to_status,
                )
            else:
                await conn.execute(
                    "UPDATE claims SET status = $2::claim_status, updated_at = now() WHERE id = $1",
                    claim_id,
                    to_status,
                )

            await conn.execute(
                "INSERT INTO direction_transitions "
                "(claim_id, from_status, to_status, transition, reason, decided_by, payload, created_by_run_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)",
                claim_id,
                cur,
                to_status,
                transition,
                (reason or None) and reason[:2000],
                decided_by,
                json.dumps(payload or {}),
                run_id,
            )

            event_type = _TRANSITION_EVENT.get(transition)
            if emit_event and event_type:
                dedup = f"concluded-{claim_id}" if transition == "conclude" else f"invalidate-{claim_id}"
                await conn.execute(
                    "INSERT INTO events (event_type, target_type, target_id, payload, emitted_by_run_id, dedup_key) "
                    "VALUES ($1, 'claim', $2, $3::jsonb, $4, $5) "
                    "ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING",
                    event_type,
                    claim_id,
                    json.dumps({"transition": transition, "reason": (reason or "")[:300], "from": cur, "to": to_status}),
                    run_id,
                    dedup,
                )
            return {"claim_id": claim_id, "from": cur, "to": to_status, "transition": transition}

    # ---- Critic verdicts -----------------------------------------------

    async def create_critic_verdict(
        self,
        claim_id: int,
        verdict: str,
        confidence: float,
        reasoning: str,
        cited_finding_ids: list[int],
        run_id: int | None = None,
        first_pass_verdict: str | None = None,
        first_pass_reasoning: str | None = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO critic_verdicts (
                    claim_id, verdict, confidence, reasoning,
                    cited_finding_ids, run_id,
                    first_pass_verdict, first_pass_reasoning,
                    revised
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id
                """,
                claim_id,
                verdict,
                confidence,
                reasoning,
                cited_finding_ids,
                run_id,
                first_pass_verdict,
                first_pass_reasoning,
                first_pass_verdict is not None,
            )

    # Backward compatibility
    async def create_adversary_verdict(
        self,
        thesis_id: int,
        verdict: str,
        confidence: float,
        reasoning: str,
        cited_finding_ids: list[int],
        run_id: int | None = None,
        first_pass_verdict: str | None = None,
        first_pass_reasoning: str | None = None,
    ) -> int:
        return await self.create_critic_verdict(
            thesis_id, verdict, confidence, reasoning, cited_finding_ids, run_id, first_pass_verdict, first_pass_reasoning
        )

    async def get_evidence_for_task(self, task_id: int) -> list[dict]:
        """All evidence rows for a task, in id order. Used by the evaluation to
        check finding groundedness."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, sub_question_idx, url, title, quote, claim, stance, confidence
                FROM evidence WHERE task_id = $1 ORDER BY id
                """,
                task_id,
            )
            return [dict(r) for r in rows]

    async def get_experiment_runs_for_task(self, task_id: int) -> list[dict]:
        """All experiment runs for a task with their results + interpretation.
        Used by the evaluation so findings derived from experiments can be judged
        as grounded against the experiment output, not only against evidence
        quote rows."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, kind, params, result, status, interpretation, error
                FROM experiment_runs WHERE task_id = $1 ORDER BY id
                """,
                task_id,
            )
            out = []
            for r in rows:
                d = dict(r)
                for k in ("params", "result"):
                    v = d.get(k)
                    if isinstance(v, str):
                        with contextlib.suppress(Exception):
                            d[k] = json.loads(v)
                out.append(d)
            return out

    # ---- Fetch cache (self-hosted retrieval) --------------------------

    async def fetch_cache_get(self, url: str) -> dict | None:
        """
        Return the cached page for `url` if present and unexpired; else None.
        Expired rows are left in place (cheap to leave; a refetch overwrites).
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT content, extractor, status_code, bytes_fetched, fetched_at
                FROM fetch_cache
                WHERE url = $1 AND expires_at > NOW()
                """,
                url,
            )
        return dict(row) if row is not None else None

    async def fetch_cache_put(
        self,
        url: str,
        content: str,
        extractor: str,
        status_code: int,
        bytes_fetched: int,
        ttl_seconds: int,
    ) -> None:
        """Upsert a fetched page. TTL is applied as `NOW() + ttl_seconds`."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fetch_cache (
                    url, content, extractor, status_code, bytes_fetched,
                    fetched_at, expires_at
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    NOW(), NOW() + ($6 || ' seconds')::interval
                )
                ON CONFLICT (url) DO UPDATE
                SET content       = EXCLUDED.content,
                    extractor     = EXCLUDED.extractor,
                    status_code   = EXCLUDED.status_code,
                    bytes_fetched = EXCLUDED.bytes_fetched,
                    fetched_at    = EXCLUDED.fetched_at,
                    expires_at    = EXCLUDED.expires_at
                """,
                url,
                content,
                extractor,
                status_code,
                bytes_fetched,
                str(int(ttl_seconds)),
            )

    # ---- Research loop: inquiries, evidence, experiments --------------

    async def record_inquiry(
        self,
        task_id: int,
        iteration: int,
        question: str,
        sub_questions: list[dict],
        proposed_experiments: list[dict],
        plan_run_id: int | None = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO research_inquiries (
                    task_id, iteration, question, sub_questions,
                    proposed_experiments, plan_run_id
                )
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
                RETURNING id
                """,
                task_id,
                iteration,
                question,
                json.dumps(sub_questions),
                json.dumps(proposed_experiments),
                plan_run_id,
            )

    async def record_evidence(
        self,
        task_id: int,
        inquiry_id: int | None,
        sub_question_idx: int,
        url: str,
        quote: str,
        claim: str,
        stance: Literal["supports", "refutes", "neutral"],
        confidence: float,
        title: str | None = None,
        extract_run_id: int | None = None,
    ) -> int:
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO evidence (
                    task_id, inquiry_id, sub_question_idx,
                    url, title, quote, claim, stance, confidence,
                    extract_run_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id
                """,
                task_id,
                inquiry_id,
                sub_question_idx,
                url,
                title,
                quote,
                claim,
                stance,
                confidence,
                extract_run_id,
            )

    async def start_experiment(
        self,
        task_id: int,
        inquiry_id: int | None,
        kind: str,
        params: dict,
    ) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO experiment_runs (
                    task_id, inquiry_id, kind, params, status
                )
                VALUES ($1, $2, $3, $4::jsonb, 'running')
                RETURNING id
                """,
                task_id,
                inquiry_id,
                kind,
                json.dumps(params),
            )

    async def complete_experiment(
        self,
        experiment_id: int,
        result: dict,
        interpretation: str | None = None,
        interpret_run_id: int | None = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE experiment_runs
                SET status = 'completed',
                    result = $1::jsonb,
                    interpretation = $2,
                    interpret_run_id = $3,
                    completed_at = NOW()
                WHERE id = $4
                """,
                json.dumps(result),
                interpretation,
                interpret_run_id,
                experiment_id,
            )

    async def fail_experiment(self, experiment_id: int, error: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE experiment_runs
                SET status = 'failed',
                    error = $1,
                    completed_at = NOW()
                WHERE id = $2
                """,
                error[:1000],
                experiment_id,
            )

    # ---- Sandboxed code experiments + Quartermaster lifecycle -------------

    @staticmethod
    def _parse_experiment_row(r) -> dict:
        d = dict(r)
        for k in ("params", "result", "provenance", "dataset_refs", "resource_usage"):
            v = d.get(k)
            if isinstance(v, str):
                with contextlib.suppress(Exception):
                    d[k] = json.loads(v)
        return d

    async def queue_experiment(
        self,
        task_id: int,
        inquiry_id: int | None,
        kind: str,
        params: dict,
        *,
        code: str | None = None,
        wall_clock_budget_s: int = 600,
        mem_budget_mb: int = 2048,
        requires_gpu: bool = False,
        gpu_mem_mb: int | None = None,
        priority: int = 5,
        provenance: dict | None = None,
        dataset_refs: list | dict | None = None,
    ) -> int:
        """Enqueue a code experiment for the Quartermaster to schedule (status='queued')."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO experiment_runs (
                    task_id, inquiry_id, kind, params, status, code,
                    wall_clock_budget_s, mem_budget_mb, requires_gpu, gpu_mem_mb,
                    priority, provenance, dataset_refs
                )
                VALUES ($1, $2, $3, $4::jsonb, 'queued', $5, $6, $7, $8, $9, $10, $11::jsonb, $12::jsonb)
                RETURNING id
                """,
                task_id,
                inquiry_id,
                kind,
                json.dumps(params),
                code,
                wall_clock_budget_s,
                mem_budget_mb,
                requires_gpu,
                gpu_mem_mb,
                priority,
                json.dumps(provenance) if provenance is not None else None,
                json.dumps(dataset_refs) if dataset_refs is not None else None,
            )

    async def get_queued_experiments(self, limit: int = 20) -> list[dict]:
        """Queued experiments, highest priority + oldest first (the QM's run order)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM experiment_runs WHERE status = 'queued' ORDER BY priority DESC, started_at ASC LIMIT $1",
                limit,
            )
            return [self._parse_experiment_row(r) for r in rows]

    async def get_running_experiments(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM experiment_runs WHERE status = 'running' ORDER BY started_at ASC")
            return [self._parse_experiment_row(r) for r in rows]

    async def mark_experiment_running(self, experiment_id: int, worker: str) -> bool:
        """Atomically claim a queued experiment for execution. Returns False if it
        was already taken (lost the race) — the QM only launches on a True."""
        async with self.pool.acquire() as conn:
            status = await conn.fetchval(
                "UPDATE experiment_runs "
                "SET status = 'running', worker = $2, started_at = NOW(), heartbeat_at = NOW() "
                "WHERE id = $1 AND status = 'queued' RETURNING status",
                experiment_id,
                worker,
            )
            return status == "running"

    async def heartbeat_experiment(self, experiment_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE experiment_runs SET heartbeat_at = NOW() WHERE id = $1", experiment_id)

    async def update_experiment_code(self, experiment_id: int, code: str, provenance: dict | None = None) -> None:
        """Persist the current code (the coding loop rewrites it each debug attempt;
        the final WORKING code is what's stored for reproducibility)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE experiment_runs SET code = $2, provenance = COALESCE($3::jsonb, provenance) WHERE id = $1",
                experiment_id,
                code,
                json.dumps(provenance) if provenance is not None else None,
            )

    async def record_experiment_result(
        self,
        experiment_id: int,
        *,
        status: str,
        result: dict | None = None,
        error: str | None = None,
        resource_usage: dict | None = None,
    ) -> None:
        """Write the sandbox outcome (status in completed|failed) — the QM calls this
        when the container exits. Interpretation + notes land later via the handler."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE experiment_runs
                SET status = $2,
                    result = COALESCE($3::jsonb, result),
                    error = COALESCE($4, error),
                    resource_usage = COALESCE($5::jsonb, resource_usage),
                    completed_at = NOW()
                WHERE id = $1
                """,
                experiment_id,
                status,
                json.dumps(result) if result is not None else None,
                (error or "")[:2000] if error is not None else None,
                json.dumps(resource_usage) if resource_usage is not None else None,
            )

    async def kill_experiment(self, experiment_id: int, reason: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE experiment_runs "
                "SET status = 'killed', kill_reason = $2, killed_at = NOW(), completed_at = NOW() "
                "WHERE id = $1 AND status IN ('running', 'queued')",
                experiment_id,
                reason[:500],
            )

    async def set_experiment_interpretation(
        self,
        experiment_id: int,
        interpretation: str | None,
        interpret_run_id: int | None = None,
        researcher_notes: str | None = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE experiment_runs "
                "SET interpretation = $2, interpret_run_id = $3, researcher_notes = $4 WHERE id = $1",
                experiment_id,
                interpretation,
                interpret_run_id,
                researcher_notes,
            )

    async def set_experiment_ingested_doc(self, experiment_id: int, doc_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE experiment_runs SET ingested_doc_id = $2 WHERE id = $1", experiment_id, doc_id)

    async def set_experiment_dataset_refs(self, experiment_id: int, refs: list | dict) -> None:
        """Record the dataset(s) an experiment used/produced (content-hash + how-produced)
        so its data lineage is captured — the reproducibility record for the inputs."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE experiment_runs SET dataset_refs = $2::jsonb WHERE id = $1",
                experiment_id,
                json.dumps(refs),
            )

    async def set_experiment_realism(self, experiment_id: int, realism: str, mismatch: bool = False) -> None:
        """Record how REAL an experiment's data was — 'real' | 'builtin' | 'synthetic' — plus a
        plan-vs-actual mismatch flag. The loop uses this to discount synthetic-only findings and
        to escalate them to a real-data confirmation run (migration 020)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE experiment_runs SET data_realism = $2, realism_mismatch = $3 WHERE id = $1",
                experiment_id,
                realism,
                mismatch,
            )

    async def get_experiment(self, experiment_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM experiment_runs WHERE id = $1", experiment_id)
            return self._parse_experiment_row(r) if r else None

    async def get_recent_experiment_notes_for_claims(self, claim_ids: list[int], limit: int = 12) -> list[dict]:
        """Recent experiment narrative notes for a set of directions — Ariadne reads
        these so she reasons over what the lab actually ran, not just confidence deltas."""
        if not claim_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.claim_id, e.id AS experiment_id, e.kind, e.status,
                       e.researcher_notes, e.interpretation, e.completed_at
                FROM experiment_runs e
                JOIN tasks t ON t.id = e.task_id
                WHERE t.claim_id = ANY($1)
                  AND (e.researcher_notes IS NOT NULL OR e.interpretation IS NOT NULL)
                ORDER BY e.completed_at DESC NULLS LAST, e.id DESC
                LIMIT $2
                """,
                claim_ids,
                limit,
            )
            return [dict(r) for r in rows]

    # ── synthesis: a direction's experiments → a paper-shaped finding ────────────────
    async def get_completed_experiments_for_claim(self, claim_id: int, limit: int = 30) -> list[dict]:
        """Every COMPLETED experiment on a direction (newest first) with its result, params, and
        the researcher's read — the evidence the synthesis agent composes into one finding."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT e.id AS experiment_id, e.kind, e.params, e.result,
                       e.interpretation, e.researcher_notes, e.completed_at, e.data_realism
                FROM experiment_runs e
                JOIN tasks t ON t.id = e.task_id
                WHERE t.claim_id = $1 AND e.status = 'completed' AND e.result IS NOT NULL
                ORDER BY e.completed_at DESC NULLS LAST, e.id DESC
                LIMIT $2
                """,
                claim_id,
                limit,
            )
            return [self._parse_experiment_row(r) for r in rows]

    async def count_completed_experiments_for_claim(self, claim_id: int) -> int:
        """How many completed experiments a direction has — the condition the synthesis trigger reads."""
        async with self.pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    SELECT count(*) FROM experiment_runs e JOIN tasks t ON t.id = e.task_id
                    WHERE t.claim_id = $1 AND e.status = 'completed' AND e.result IS NOT NULL
                    """,
                    claim_id,
                )
                or 0
            )

    async def direction_is_thin_stuck(self, claim_id: int | None, *, n: int = 3) -> bool:
        """True if a direction's last `n` completed research tasks were ALL thin_corpus — it's stuck.
        The planner reads this to STOP refilling it (more tasks just churn + flood acquires, and each
        fresh planner task resets the closure ladder's scout→retry→retire state, so it never hands
        off). Drained, the watchdog ladder fires its sweep and declares the gap, or the experiment
        lane settles it. False on a None claim_id / too little history."""
        if claim_id is None:
            return False
        async with self.pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT count(*) = $2 AND bool_and(result->>'disposition' = 'thin_corpus') "
                    "FROM (SELECT result FROM tasks WHERE claim_id = $1 AND status = 'completed' "
                    "      AND result->>'disposition' IS NOT NULL ORDER BY id DESC LIMIT $2) t",
                    claim_id,
                    n,
                )
            )

    async def get_research_document(self, claim_id: int, kind: str) -> dict | None:
        """The direction's current (final) research document of `kind` — lit_review /
        proposal / article. The research arc's readers (proposal builder, experiment
        designer, article composer) all come through here."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, claim_id, kind, title, body_md, meta, citations, created_at "
                "FROM research_documents WHERE claim_id = $1 AND kind = $2 AND status = 'final' "
                "ORDER BY id DESC LIMIT 1",
                claim_id,
                kind,
            )
        if row is None:
            return None
        d = dict(row)
        for k in ("meta", "citations"):
            if isinstance(d.get(k), str):
                d[k] = json.loads(d[k])
        return d

    async def latest_finding_n_for_claim(self, claim_id: int) -> int | None:
        """The evidence size (n_experiments) of the most recent finding for a direction, or None —
        so the synthesizer only re-runs when materially more experiments have accumulated."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT max(n_experiments) FROM research_findings WHERE direction_claim_id = $1", claim_id
            )

    async def get_claim_goals_text(self, claim_id: int) -> str:
        """The direction's goals as a compact block (expectation / kill-condition) for the compose prompt."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT expectation, kill_condition FROM claim_goals WHERE claim_id = $1 ORDER BY id", claim_id
            )
        return "\n".join(f"- expect: {r['expectation']} · kill if: {r['kill_condition']}" for r in rows)

    async def persist_research_finding(
        self,
        *,
        direction_claim_id: int,
        headline: str,
        claim_text: str,
        supported: str,
        method: str,
        key_numbers: str,
        limitations: str,
        so_what: str,
        next_step: str,
        confidence: float,
        n_experiments: int,
        grounded_in: list[str],
        graduate_to: str,
        run_id: int | None = None,
        data_realism: str | None = None,
    ) -> dict:
        """Write the finding: a `finding` claim (graph lineage + status), a research_findings row,
        and graduate the direction's lifecycle status (upward only) via the single guarded write
        path (advance_direction). All in one transaction. `data_realism` (worst-case across the
        grounding experiments) records whether the finding rests on real / builtin / synthetic data."""
        async with self.pool.acquire() as conn, conn.transaction():
            finding_claim_id = await conn.fetchval(
                """
                INSERT INTO claims (statement, claim_kind, parent_id, status, confidence, created_by_run_id)
                VALUES ($1, 'finding', $2, 'proposed', $3, $4) RETURNING id
                """,
                headline[:4000],
                direction_claim_id,
                max(0.0, min(1.0, float(confidence))),
                run_id,
            )
            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
                VALUES ('claim.created', 'claim', $1, $2::jsonb, $3)
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                finding_claim_id,
                json.dumps({"statement": headline, "parent_id": direction_claim_id, "claim_kind": "finding"}),
                f"create-{finding_claim_id}",
            )
            finding_id = await conn.fetchval(
                """
                INSERT INTO research_findings
                    (direction_claim_id, finding_claim_id, headline, claim_text, supported, method,
                     key_numbers, limitations, so_what, next_step, confidence, n_experiments,
                     grounded_in, created_by_run_id, data_realism)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14, $15)
                RETURNING id
                """,
                direction_claim_id,
                finding_claim_id,
                headline,
                claim_text,
                supported,
                method,
                key_numbers,
                limitations,
                so_what,
                next_step,
                float(confidence),
                n_experiments,
                json.dumps(grounded_in),
                run_id,
                data_realism,
            )
            # Graduate the direction UPWARD only (a finding never demotes a stronger prior status),
            # and ONLY while it's still active — never resurrect an invalidated/superseded direction.
            # The legal-transition table + monotone guard + audit row all live in advance_direction;
            # a decisive 'concluded' graduation also emits the direction.concluded lifecycle event.
            adv = await self.advance_direction(
                direction_claim_id,
                graduate_to,
                transition=("conclude" if graduate_to == "concluded" else "graduate"),
                decided_by="synthesis",
                reason=f"finding: {headline[:200]}",
                run_id=run_id,
                monotone=True,
                payload={"finding_id": finding_id, "supported": supported, "confidence": float(confidence)},
                conn=conn,
            )
            graduated_to = adv["to"] if adv else None
        return {"finding_id": finding_id, "finding_claim_id": finding_claim_id, "graduated_to": graduated_to}

    async def get_recent_findings_for_claims(self, claim_ids: list[int], limit: int = 8) -> list[dict]:
        """Recent paper-shaped findings for a set of directions — Ariadne reads these so she
        re-frames over the lab's CONCLUSIONS, not just raw experiment notes."""
        if not claim_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT direction_claim_id, headline, claim_text, supported, confidence, so_what, n_experiments
                FROM research_findings
                WHERE direction_claim_id = ANY($1)
                ORDER BY created_at DESC, id DESC
                LIMIT $2
                """,
                claim_ids,
                limit,
            )
            return [dict(r) for r in rows]

    async def get_recent_findings(self, limit: int = 8) -> list[dict]:
        """The lab's most recent paper-shaped findings across ALL directions — NOT scoped to the
        currently-active claim ids. A finding survives a re-frame (the supersede invalidates
        'mission'/'direction' claims, never 'finding'), but its direction bond goes inactive, so the
        per-claim read returns nothing after a re-frame. This global read is the durable channel:
        every deliberation sees what the lab has CONCLUDED so it builds beyond it instead of re-rolling."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT direction_claim_id, headline, claim_text, supported, confidence, so_what, n_experiments
                FROM research_findings
                ORDER BY created_at DESC, id DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    # ── independent novelty/impact adjudication (agents/novelty) ─────────────────────
    async def get_unadjudicated_directions(self, limit: int = 20) -> list[dict]:
        """Scored, active directions that have NO independent adjudication yet — the work the
        novelty agent picks up so the gate has an external verdict to require."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.id, c.statement
                FROM claims c JOIN direction_scores ds ON ds.claim_id = c.id
                WHERE c.claim_kind = 'direction'
                  AND c.status IN ('proposed', 'tested', 'weakly_supported', 'replicated')
                  AND NOT EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id = c.id)
                ORDER BY c.id
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    async def get_prior_directions_with_outcomes(self, exclude_claim_id: int, limit: int = 12) -> list[dict]:
        """Recent directions (any status, newest first) WITH how each one ENDED — status plus
        its latest finding, if any. The adjudicator must see outcomes, not just statements:
        a question is only "already answered" if a prior attempt concluded with a decisive
        finding; an invalidated/retired attempt without one left the question OPEN (observed
        2026-06-12: a fresh agenda held wholesale as redundant with INVALIDATED prior work)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.id, c.statement, c.status,
                       rf.supported AS finding_supported, rf.confidence AS finding_confidence
                FROM claims c
                LEFT JOIN LATERAL (
                    SELECT supported, confidence FROM research_findings
                    WHERE direction_claim_id = c.id ORDER BY id DESC LIMIT 1
                ) rf ON true
                WHERE c.claim_kind = 'direction' AND c.id <> $1
                ORDER BY c.id DESC LIMIT $2
                """,
                exclude_claim_id,
                limit,
            )
            return [dict(r) for r in rows]

    async def get_held_directions(self, limit: int = 20) -> list[dict]:
        """Live directions whose independent adjudication is 'hold' and that no gate has
        approved — the reconsideration set for the pacemaker's daily all-held re-look."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.id, c.statement
                FROM claims c JOIN direction_adjudications da ON da.claim_id = c.id
                WHERE c.claim_kind = 'direction'
                  AND c.status IN ('proposed', 'tested', 'weakly_supported', 'replicated')
                  AND da.verdict = 'hold'
                  AND NOT EXISTS (SELECT 1 FROM direction_gate dg WHERE dg.claim_id = c.id AND dg.status = 'approved')
                ORDER BY c.id
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    async def get_held_directions_with_rationale(self, limit: int = 8) -> list[dict]:
        """Recently-held directions WITH the adjudicator's hold rationale — the deliberation
        feedback edge. When the independent adjudicator holds an agenda wholesale (prior-art
        overlap / re-tread), the re-frame must SEE why, or it re-proposes near-duplicates and
        the lab churns deliberate→hold→exhausted. Newest first; rationale falls back to the
        redundancy note."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.id, c.statement,
                       COALESCE(NULLIF(da.rationale, ''), da.redundant_note, 'redundant with prior work') AS rationale
                FROM claims c JOIN direction_adjudications da ON da.claim_id = c.id
                WHERE c.claim_kind = 'direction'
                  AND c.status IN ('proposed', 'tested', 'weakly_supported', 'replicated')
                  AND da.verdict = 'hold'
                  AND NOT EXISTS (SELECT 1 FROM direction_gate dg WHERE dg.claim_id = c.id AND dg.status = 'approved')
                ORDER BY da.created_at DESC, c.id DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    async def persist_direction_adjudication(
        self,
        *,
        claim_id: int,
        novelty_independent: int,
        impact_independent: int,
        is_novel: bool,
        is_impactful: bool,
        redundant: bool,
        redundant_note: str,
        verdict: str,
        rationale: str,
        nearest_prior_art: list[str],
        run_id: int | None = None,
    ) -> None:
        """Write (or replace) a direction's independent adjudication."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO direction_adjudications
                    (claim_id, novelty_independent, impact_independent, is_novel, is_impactful,
                     redundant, redundant_note, verdict, rationale, nearest_prior_art, created_by_run_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
                ON CONFLICT (claim_id) DO UPDATE SET
                    novelty_independent = EXCLUDED.novelty_independent,
                    impact_independent = EXCLUDED.impact_independent,
                    is_novel = EXCLUDED.is_novel,
                    is_impactful = EXCLUDED.is_impactful,
                    redundant = EXCLUDED.redundant,
                    redundant_note = EXCLUDED.redundant_note,
                    verdict = EXCLUDED.verdict,
                    rationale = EXCLUDED.rationale,
                    nearest_prior_art = EXCLUDED.nearest_prior_art,
                    created_by_run_id = EXCLUDED.created_by_run_id,
                    created_at = now()
                """,
                claim_id,
                novelty_independent,
                impact_independent,
                is_novel,
                is_impactful,
                redundant,
                redundant_note,
                verdict,
                rationale,
                json.dumps(nearest_prior_art),
                run_id,
            )

    async def get_research_tree(self, task_id: int) -> dict:
        """
        Return everything the Debug research-tree view needs: the task itself,
        all inquiries with their sub-questions, all evidence and experiments,
        all agent_runs whose ids appear in those rows, plus the final findings.
        Joined on the client side for simpler SQL.
        """
        async with self.pool.acquire() as conn:
            task = await conn.fetchrow(
                "SELECT * FROM tasks WHERE id = $1",
                task_id,
            )
            inquiries = await conn.fetch(
                "SELECT * FROM research_inquiries WHERE task_id = $1 ORDER BY iteration, id",
                task_id,
            )
            evidence = await conn.fetch(
                "SELECT * FROM evidence WHERE task_id = $1 ORDER BY id",
                task_id,
            )
            experiments = await conn.fetch(
                "SELECT * FROM experiment_runs WHERE task_id = $1 ORDER BY id",
                task_id,
            )
            findings = await conn.fetch(
                "SELECT * FROM findings WHERE task_id = $1 ORDER BY id",
                task_id,
            )
            # Collect every agent_run for this task. The loop passes the
            # `task.created` event id as `triggered_by_event_id` for every LLM
            # call (plan, extract, interpret, synthesize, gap_check) so we can
            # fetch them all in one go. This catches synthesize and gap_check
            # which aren't linked from any persisted row.
            agent_runs = await conn.fetch(
                """
                SELECT * FROM agent_runs
                WHERE triggered_by_event_id IN (
                    SELECT id FROM events
                    WHERE event_type = 'task.created'
                      AND target_type = 'task' AND target_id = $1
                )
                ORDER BY id
                """,
                task_id,
            )
            # Demo / manual runs don't go through the event bus, so fall back
            # to the directly-referenced ids if no event-triggered runs were
            # found.
            if not agent_runs:
                run_ids: set[int] = set()
                for r in inquiries:
                    if r["plan_run_id"]:
                        run_ids.add(r["plan_run_id"])
                for r in evidence:
                    if r["extract_run_id"]:
                        run_ids.add(r["extract_run_id"])
                for r in experiments:
                    if r["interpret_run_id"]:
                        run_ids.add(r["interpret_run_id"])
                if run_ids:
                    agent_runs = await conn.fetch(
                        "SELECT * FROM agent_runs WHERE id = ANY($1::bigint[]) ORDER BY id",
                        list(run_ids),
                    )

        def _row(r):
            d = dict(r)
            for k, v in list(d.items()):
                if isinstance(v, str) and k in {"payload", "params", "result", "sub_questions", "proposed_experiments"}:
                    with contextlib.suppress(Exception):
                        d[k] = json.loads(v)
            return d

        return {
            "task": _row(task) if task else None,
            "inquiries": [_row(r) for r in inquiries],
            "evidence": [_row(r) for r in evidence],
            "experiments": [_row(r) for r in experiments],
            "findings": [_row(r) for r in findings],
            "agent_runs": [_row(r) for r in agent_runs],
        }

    # ---- Knowledge corpus (Library, migration 015) --------------------
    #
    # The Librarian's persistence path: register documents, stage a chunk plan,
    # fill embeddings, flip the doc queryable. Mirrors the inline-event /
    # ON CONFLICT DO NOTHING conventions used above. The Librarian owns the
    # content columns; Mimir owns the trust_* columns (left at DB defaults).

    async def emit_corpus_event(
        self,
        event_type: str,
        *,
        target_type: str,
        target_id: int,
        payload: dict,
        dedup_key: str | None = None,
    ) -> None:
        """
        Reusable corpus event emitter. There is no generic state.emit_event today
        (events are inlined per mutation, e.g. update_finding_audit); this is the
        shared helper the corpus path calls for. Inlines the same
        INSERT INTO events (...) ON CONFLICT DO NOTHING pattern, json.dumps-ing
        the payload to jsonb.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                event_type,
                target_type,
                target_id,
                json.dumps(payload),
                dedup_key,
            )

    async def upsert_document(
        self,
        *,
        kind: str,
        source_kind: str,
        canonical_key: str,
        title: str | None = None,
        authors: list[str] | None = None,
        source_url: str | None = None,
        doi: str | None = None,
        arxiv_id: str | None = None,
        raw_uri: str | None = None,
        content_hash: str | None = None,
        parse_run_id: int | None = None,
    ) -> tuple[int, bool]:
        """
        Register a document by its dedupe key. Idempotent on
        (source_kind, canonical_key): inserts if new, else returns the existing
        id. Returns (document_id, is_new). Trust columns (status/trust_tier/
        trust_state/provenance) are left at their DB defaults — Mimir owns them.

        Normalizes '' -> NULL for doi/arxiv_id so the partial unique indexes and
        ck_documents_doi / ck_documents_arxiv CHECKs (015) don't reject blanks.
        """
        doi = doi or None
        arxiv_id = arxiv_id or None
        async with self.pool.acquire() as conn:
            # Cross-source / version dedup (Tier 3): the same paper arrives as arxiv (key=id), ar5iv
            # (source_kind=web, url key), or a versioned id (…v3). uq_documents_arxiv is on the RAW
            # column, so version variants slip past it as separate rows. Probe FIRST — exact (indexed)
            # then version-suffixed variants of the same base — and return the existing id instead of
            # inserting a duplicate (also avoids the unique-index conflict normalization would trigger).
            # Existing duplicate ROWS need a one-off backfill to merge; this only stops NEW ones.
            if arxiv_id is not None:
                existing = await conn.fetchval("SELECT id FROM documents WHERE arxiv_id = $1 LIMIT 1", arxiv_id)
                if existing is None:
                    existing = await conn.fetchval(
                        "SELECT id FROM documents WHERE arxiv_id LIKE $1 || 'v%' "
                        "AND regexp_replace(arxiv_id, 'v[0-9]+$', '') = $1 LIMIT 1",
                        arxiv_id,
                    )
                if existing is not None:
                    return existing, False
            if doi is not None:
                existing = await conn.fetchval("SELECT id FROM documents WHERE doi = $1 LIMIT 1", doi)
                if existing is not None:
                    return existing, False
            new_id = await conn.fetchval(
                """
                INSERT INTO documents (
                    kind, source_kind, canonical_key, title, authors,
                    source_url, doi, arxiv_id, raw_uri, content_hash, parse_run_id
                )
                VALUES (
                    $1, $2, $3, $4, COALESCE($5, '{}'::text[]),
                    $6, $7, $8, $9, $10, $11
                )
                ON CONFLICT (source_kind, canonical_key) DO NOTHING
                RETURNING id
                """,
                kind,
                source_kind,
                canonical_key,
                title,
                authors,
                source_url,
                doi,
                arxiv_id,
                raw_uri,
                content_hash,
                parse_run_id,
            )
            if new_id is not None:
                return new_id, True
            existing_id = await conn.fetchval(
                "SELECT id FROM documents WHERE source_kind = $1 AND canonical_key = $2",
                source_kind,
                canonical_key,
            )
            return existing_id, False

    async def register_dataset(
        self,
        *,
        document_id: int,
        name: str,
        url: str | None = None,
        modality: str | None = None,
        task: str | None = None,
        size: str | None = None,
        license: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Register a dataset in the catalog (idempotent on document_id). The dataset scouts ingest
        HF/OpenML datasets as documents; this also surfaces them as a queryable CATALOG (the `datasets`
        table was empty, so the lab had no structured view of which datasets exist) and an id-keyed
        :Dataset graph node. Returns the catalog row id (existing or new)."""
        async with self.pool.acquire() as conn:
            existing = await conn.fetchval("SELECT id FROM datasets WHERE document_id = $1 LIMIT 1", document_id)
            if existing is not None:
                return existing
            return await conn.fetchval(
                """
                INSERT INTO datasets (name, url, modality, task, size, license, notes, document_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                name,
                url,
                modality,
                task,
                size,
                license,
                notes,
                document_id,
            )

    async def list_datasets(self, limit: int = 50) -> list[dict]:
        """The dataset catalog (most-recent first) — name + url + modality/task + the doc it came from."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, url, modality, task, size, license, document_id "
                "FROM datasets ORDER BY id DESC LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]

    async def stage_chunk_plan(self, document_id: int, items: list) -> int:
        """
        Bulk-insert a chunk plan for a document. Accepts ChunkPlanItem instances
        or dicts (ordinal, text, token_count, content_hash). `embedding` is left
        NULL until set_chunk_embeddings runs. Idempotent on
        (document_id, ordinal, content_hash) so a re-chunk skips existing rows.
        Returns the number of rows actually inserted.
        """
        rows = []
        for it in items:
            if isinstance(it, dict):
                ordinal = it["ordinal"]
                text = it["text"]
                token_count = it.get("token_count")
                content_hash = it["content_hash"]
            else:
                ordinal = it.ordinal
                text = it.text
                token_count = it.token_count
                content_hash = it.content_hash
            rows.append((document_id, ordinal, text, token_count, content_hash))
        if not rows:
            return 0
        inserted = 0
        async with self.pool.acquire() as conn, conn.transaction():
            for r in rows:
                res = await conn.fetchval(
                    """
                        INSERT INTO chunks (
                            document_id, ordinal, text, token_count, content_hash
                        )
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (document_id, ordinal, content_hash) DO NOTHING
                        RETURNING id
                        """,
                    *r,
                )
                if res is not None:
                    inserted += 1
        return inserted

    async def get_chunk_plan(self, document_id: int) -> list[dict]:
        """Return staged chunks for a document, ordered by ordinal. Each dict has
        id, ordinal, text, content_hash, token_count, and has_embedding."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, ordinal, text, content_hash, token_count,
                       (embedding IS NOT NULL) AS has_embedding
                FROM chunks
                WHERE document_id = $1
                ORDER BY ordinal
                """,
                document_id,
            )
            return [dict(r) for r in rows]

    async def chunk_has_vector(
        self,
        document_id: int,
        ordinal: int,
        content_hash: str,
    ) -> bool:
        """True if the identified chunk already has an embedding (idempotent
        re-embed skip)."""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                """
                SELECT (embedding IS NOT NULL)
                FROM chunks
                WHERE document_id = $1 AND ordinal = $2 AND content_hash = $3
                """,
                document_id,
                ordinal,
                content_hash,
            )
            return bool(val)

    async def set_chunk_embeddings(self, document_id: int, rows: list[dict]) -> None:
        """
        Fill embeddings for staged chunks. Each row: {ordinal, content_hash,
        embedding, embed_model}. `embedding` is passed as a plain python list.

        NOTE: the pool backing this client MUST have the pgvector codec
        registered on its connections (pgvector.asyncpg.register_vector in the
        pool `init`, exactly as labfoundry_corpus._init_conn does) — without it
        asyncpg cannot bind a list as a vector(768) param and the UPDATE fails.
        """
        if not rows:
            return
        async with self.pool.acquire() as conn, conn.transaction():
            for r in rows:
                await conn.execute(
                    """
                        UPDATE chunks
                        SET embedding = $1, embed_model = $2
                        WHERE document_id = $3 AND ordinal = $4 AND content_hash = $5
                        """,
                    r["embedding"],
                    r.get("embed_model"),
                    document_id,
                    r["ordinal"],
                    r["content_hash"],
                )

    async def get_document(self, document_id: int) -> dict | None:
        """Return the full documents row as a dict, or None if absent. The
        provenance jsonb round-trips as a dict when the pool registers the jsonb
        codec; otherwise it may arrive as a str."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM documents WHERE id = $1",
                document_id,
            )
            return dict(row) if row is not None else None

    async def set_document_queryable(
        self,
        document_id: int,
        value: bool = True,
    ) -> None:
        """Flip documents.queryable (the ingest pipeline sets it after embed/
        upsert succeeds, so the retrieval path can see the doc)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET queryable = $1 WHERE id = $2",
                value,
                document_id,
            )

    async def set_document_trust(
        self,
        document_id: int,
        *,
        tier: str,
        trust_state: str,
        status: str,
        certified_by_run_id: int | None = None,
    ) -> None:
        """Write Mimir's trust verdict onto the document — the denormalized hot
        path (certifications holds the immutable history). Mimir owns these
        columns; the ingest tools never touch them."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE documents
                SET trust_tier = $1::trust_tier,
                    trust_state = $2::trust_state,
                    status = $3::document_status,
                    certified_by_run_id = $4,
                    certified_at = NOW(),
                    last_trust_review_at = NOW()
                WHERE id = $5
                """,
                tier,
                trust_state,
                status,
                certified_by_run_id,
                document_id,
            )

    async def set_document_license(self, document_id: int, license: str | None) -> None:
        """Persist a resolved license (e.g. a GitHub SPDX id) onto the document.
        Captured by Mimir during signal resolution, before the trust gate runs —
        so a restrictive license is both queryable here and visible to the gate."""
        if not license:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET license = $1 WHERE id = $2",
                license,
                document_id,
            )

    async def append_certification(
        self,
        document_id: int,
        *,
        decision: str,
        to_tier: str,
        to_state: str,
        from_tier: str | None = None,
        signals: dict | None = None,
        used_llm: bool = False,
        reasons: str = "",
        decided_by_run_id: int | None = None,
        requested_by: str | None = None,
    ) -> int:
        """Append an immutable certifications row — Mimir's audit trail (the
        critic_verdicts-style append ledger behind the denormalized trust_*)."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO certifications (
                    document_id, decision, from_tier, to_tier, to_state,
                    signals, used_llm, reasons, decided_by_run_id, requested_by
                )
                VALUES ($1, $2, $3::trust_tier, $4::trust_tier, $5::trust_state,
                        $6::jsonb, $7, $8, $9, $10)
                RETURNING id
                """,
                document_id,
                decision,
                from_tier,
                to_tier,
                to_state,
                json.dumps(signals or {}),
                used_llm,
                reasons,
                decided_by_run_id,
                requested_by,
            )

    async def document_exists(self, source_kind: str, canonical_key: str) -> bool:
        """True if a document with this (source_kind, canonical_key) is already in
        the corpus. The discovery sweep uses this to skip re-emitting (and thus
        re-fetching) sources it has already ingested."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM documents WHERE source_kind = $1 AND canonical_key = $2)",
                source_kind,
                canonical_key,
            )

    async def discovery_offset(
        self,
        source_kind: str,
        topic: str,
        *,
        page_size: int,
        refresh_after_s: float,
        max_offset: int,
    ) -> int:
        """The pagination offset a scout should fetch THIS sweep for (source, topic),
        and advance the cursor (migration 003). Alternates two modes so a scout
        never re-fetches the same slice:

          REFRESH — if we haven't grabbed the newest in `refresh_after_s`, fetch
          offset 0 (catch new submissions) and stamp last_refreshed_at.
          DEEPEN  — otherwise fetch the stored offset and advance it by
          `page_size`, wrapping to 0 past `max_offset` (walk the back-catalogue).
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT offset_n, last_refreshed_at FROM discovery_cursors "
                "WHERE source_kind = $1 AND topic = $2 FOR UPDATE",
                source_kind,
                topic,
            )
            offset_n = row["offset_n"] if row else 0
            last_ref = row["last_refreshed_at"] if row else None
            refresh = last_ref is None or (datetime.now(UTC) - last_ref).total_seconds() >= refresh_after_s
            if refresh:
                use_offset, new_offset_n, new_last_ref = 0, offset_n, datetime.now(UTC)
            else:
                use_offset = offset_n
                new_offset_n = 0 if (offset_n + page_size) > max_offset else offset_n + page_size
                new_last_ref = last_ref
            await conn.execute(
                "INSERT INTO discovery_cursors (source_kind, topic, offset_n, last_refreshed_at, updated_at) "
                "VALUES ($1, $2, $3, $4, now()) "
                "ON CONFLICT (source_kind, topic) DO UPDATE SET "
                "offset_n = EXCLUDED.offset_n, last_refreshed_at = EXCLUDED.last_refreshed_at, updated_at = now()",
                source_kind,
                topic,
                new_offset_n,
                new_last_ref,
            )
            return use_offset

    async def discovery_filter_new(self, source_kind: str, keys: list[str], *, retry_after_s: float) -> set[str]:
        """The novelty gate (migration 003). Given candidate canonical_keys (already
        known absent from the corpus), return the subset worth surfacing — never
        seen, or last attempted longer than `retry_after_s` ago — and record the
        attempt. Sources attempted within the window are skipped, so a source that
        failed to ingest retries on a schedule rather than spinning every sweep."""
        if not keys:
            return set()
        async with self.pool.acquire() as conn:
            recent = {
                r["canonical_key"]
                for r in await conn.fetch(
                    "SELECT canonical_key FROM discovery_seen WHERE source_kind = $1 "
                    "AND canonical_key = ANY($2) "
                    "AND last_attempt_at > now() - ($3 * interval '1 second')",
                    source_kind,
                    keys,
                    retry_after_s,
                )
            }
            due = [k for k in keys if k not in recent]
            if due:
                await conn.executemany(
                    "INSERT INTO discovery_seen (source_kind, canonical_key) VALUES ($1, $2) "
                    "ON CONFLICT (source_kind, canonical_key) DO UPDATE SET "
                    "attempts = discovery_seen.attempts + 1, last_attempt_at = now()",
                    [(source_kind, k) for k in due],
                )
            return set(due)

    async def count_acquires_today(self, requester: str) -> int:
        """Count `acquire.requested` events from `requester` since midnight UTC —
        Mimir's per-agent daily acquisition cap (no extra table; the event bus is
        the ledger)."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM events
                WHERE event_type = 'acquire.requested'
                  AND payload->>'requester' = $1
                  AND emitted_at >= date_trunc('day', NOW())
                """,
                requester,
            )
