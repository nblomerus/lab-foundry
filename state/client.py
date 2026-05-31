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
from datetime import datetime
from typing import Literal

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
        """Flip documents.queryable (the Librarian sets it after embed/upsert
        succeeds, so the retrieval path can see the doc)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET queryable = $1 WHERE id = $2",
                value,
                document_id,
            )
