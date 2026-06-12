"""
LessonsClient — query the lessons table for context-applicable lessons,
and insert new lesson candidates from reflection runs.

Predicate matching is simple key-value equality plus glob-suffix on string
values (e.g., 'source': 'reddit*' matches 'reddit', 'reddit-r/python', etc).
More sophisticated matching (regex, ranges) added when concrete need appears.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import asyncpg

log = logging.getLogger(__name__)

# The stable vocabulary a lesson's applies_when predicate may key on. Every
# loop step injects these into its context; a predicate referencing anything
# else can never match (a silently-dead lesson), so we log it.
STANDARD_CONTEXT_KEYS = {"phase", "agent", "task_type", "claim_status", "invocation_type", "source"}


@dataclass
class Lesson:
    id: int
    lesson_text: str
    confidence: float
    status: str
    promotion_run_count: int
    contradiction_run_count: int
    applies_when: dict


class LessonsClient:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def fetch_applicable(
        self,
        invocation_type: str,
        context: dict,
        limit: int = 5,
    ) -> list[Lesson]:
        """
        Fetch up to `limit` lessons whose applies_when predicate is satisfied
        by `context`. Empty applies_when matches all contexts.

        Over-fetches and filters in Python because applies_when matching is
        more flexible than what we'd reasonably push into SQL.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, lesson_text, confidence, status,
                       promotion_run_count, contradiction_run_count, applies_when
                FROM active_lessons_by_invocation
                WHERE applies_to_invocation = $1
                ORDER BY confidence DESC, promotion_run_count DESC
                LIMIT $2
                """,
                invocation_type,
                limit * 4,  # over-fetch for predicate filtering
            )

        applicable: list[Lesson] = []
        for r in rows:
            applies_when = r["applies_when"]
            if isinstance(applies_when, str):
                applies_when = json.loads(applies_when)
            if self._predicate_matches(applies_when, context):
                applicable.append(
                    Lesson(
                        id=r["id"],
                        lesson_text=r["lesson_text"],
                        confidence=float(r["confidence"]),
                        status=r["status"],
                        promotion_run_count=r["promotion_run_count"],
                        contradiction_run_count=r["contradiction_run_count"],
                        applies_when=applies_when,
                    )
                )
                if len(applicable) >= limit:
                    break
        return applicable

    def _predicate_matches(self, applies_when: dict, context: dict) -> bool:
        if not applies_when:
            return True
        # Predicate-vocab hygiene: a key outside the standard vocab can never be
        # satisfied by any loop's context, so the lesson is silently dead. Surface
        # it (Debug) rather than letting it rot unmatched.
        unknown = set(applies_when) - STANDARD_CONTEXT_KEYS
        if unknown:
            log.debug("lesson predicate references unknown context keys %s (will never match)", sorted(unknown))
        for key, expected in applies_when.items():
            if key not in context:
                return False
            actual = context[key]
            if isinstance(expected, str) and expected.endswith("*"):
                if not (isinstance(actual, str) and actual.startswith(expected[:-1])):
                    return False
            elif actual != expected:
                return False
        return True

    async def insert_lesson_candidate(
        self,
        invocation_type: str,
        applies_when: dict,
        lesson_text: str,
        rationale: str,
        derived_from_run_id: int,
        derived_via: str,
    ) -> int:
        """Insert a probationary lesson. Returns its id."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO lessons (
                    applies_to_invocation, applies_when, lesson_text, rationale,
                    derived_from_run_id, derived_via
                )
                VALUES ($1, $2::jsonb, $3, $4, $5, $6::lesson_source)
                RETURNING id
                """,
                invocation_type,
                json.dumps(applies_when),
                lesson_text,
                rationale,
                derived_from_run_id,
                derived_via,
            )

    async def reconcile(self) -> list[dict]:
        """
        Run the periodic promotion/retirement reconciliation.
        Returns a list of {lesson_id, action, new_status} dicts.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM reconcile_lessons()")
            return [dict(r) for r in rows]

    async def decay(self) -> list[dict]:
        """Retire stale probationary lessons (14d, 0 supportive). See migration 014."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM decay_lessons()")
            return [dict(r) for r in rows]

    # -- Hinge A: write the outcome of applied lessons (the missing joint) -----

    async def fetch_pending_applications(self, limit: int = 40) -> list[dict]:
        """
        Lessons that were applied to a now-completed run but never judged.
        Grouped per run so a single judge call can score all of a run's lessons
        against that run's actual outcome.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT la.agent_run_id,
                       r.invocation_type,
                       r.status        AS run_status,
                       r.output_summary,
                       r.expectation,
                       r.outcome,
                       la.lesson_id,
                       l.lesson_text
                FROM lesson_applications la
                JOIN agent_runs r ON r.id = la.agent_run_id
                JOIN lessons    l ON l.id = la.lesson_id
                WHERE la.outcome IS NULL
                  AND r.status IN ('completed', 'failed')
                  AND r.completed_at < NOW() - INTERVAL '1 minute'
                ORDER BY r.completed_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]

    async def set_application_outcome(
        self,
        lesson_id: int,
        agent_run_id: int,
        outcome: str,
        judged_by_run_id: int | None = None,
    ) -> None:
        """
        Record whether an applied lesson was supportive/contradicting/inconclusive.
        Guarded: only fills rows still NULL (idempotent, no clobber).
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE lesson_applications
                SET outcome = $3,
                    outcome_judged_at = NOW(),
                    outcome_judged_by_run_id = $4
                WHERE lesson_id = $1 AND agent_run_id = $2 AND outcome IS NULL
                """,
                lesson_id,
                agent_run_id,
                outcome,
                judged_by_run_id,
            )

    async def find_near_duplicate(
        self,
        invocation_type: str,
        lesson_text: str,
        threshold: float = 0.6,
    ) -> int | None:
        """
        Return the id of an existing active/probationary lesson for this
        invocation whose text is a trigram near-duplicate (≥ threshold), so the
        caller can credit recurrence instead of inserting spam. Uses migration
        014's gin_trgm index.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT id FROM lessons
                WHERE applies_to_invocation = $1
                  AND status IN ('probationary', 'active')
                  AND similarity(lesson_text, $2) >= $3
                ORDER BY similarity(lesson_text, $2) DESC
                LIMIT 1
                """,
                invocation_type,
                lesson_text,
                threshold,
            )

    async def record_applications(self, lesson_ids: list[int], agent_run_id: int | None) -> int:
        """Record that a run CONSUMED these lessons (outcome judged later by the lesson
        judge). The Curator→Router path records its own applications (harness/router.py);
        this is for paths that bypass it — Ariadne's deliberate/reflect recall — which
        previously left her lessons unjudgeable (0 promotions ever). Idempotent per
        (lesson, run)."""
        if not lesson_ids or agent_run_id is None:
            return 0
        n = 0
        async with self.pool.acquire() as conn:
            for lid in lesson_ids:
                res = await conn.execute(
                    "INSERT INTO lesson_applications (lesson_id, agent_run_id) "
                    "SELECT $1, $2 WHERE NOT EXISTS ("
                    "  SELECT 1 FROM lesson_applications WHERE lesson_id = $1 AND agent_run_id = $2)",
                    lid,
                    agent_run_id,
                )
                if res.endswith(" 1"):
                    n += 1
        return n

    async def credit_recurrence(self, lesson_id: int, derived_from_run_id: int) -> None:
        """
        A near-duplicate lesson was re-discovered. Instead of inserting a row,
        credit the original with a synthetic *supportive* application — so
        re-discovery becomes promotion pressure, not table spam. Idempotent per
        (lesson, run): re-running the same reflection won't double-credit.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO lesson_applications
                    (lesson_id, agent_run_id, outcome, outcome_judged_at, outcome_judged_by_run_id)
                SELECT $1, $2, 'supportive', NOW(), $2
                WHERE NOT EXISTS (
                    SELECT 1 FROM lesson_applications
                    WHERE lesson_id = $1 AND agent_run_id = $2
                )
                """,
                lesson_id,
                derived_from_run_id,
            )
