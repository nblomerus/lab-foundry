"""
LessonsClient — query the lessons table for context-applicable lessons,
and insert new lesson candidates from reflection runs.

Predicate matching is simple key-value equality plus glob-suffix on string
values (e.g., 'source': 'reddit*' matches 'reddit', 'reddit-r/python', etc).
More sophisticated matching (regex, ranges) added when concrete need appears.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import asyncpg


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
                invocation_type, limit * 4,  # over-fetch for predicate filtering
            )

        applicable: list[Lesson] = []
        for r in rows:
            applies_when = r["applies_when"]
            if isinstance(applies_when, str):
                applies_when = json.loads(applies_when)
            if self._predicate_matches(applies_when, context):
                applicable.append(Lesson(
                    id=r["id"],
                    lesson_text=r["lesson_text"],
                    confidence=float(r["confidence"]),
                    status=r["status"],
                    promotion_run_count=r["promotion_run_count"],
                    contradiction_run_count=r["contradiction_run_count"],
                    applies_when=applies_when,
                ))
                if len(applicable) >= limit:
                    break
        return applicable

    def _predicate_matches(self, applies_when: dict, context: dict) -> bool:
        if not applies_when:
            return True
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
                invocation_type, json.dumps(applies_when), lesson_text,
                rationale, derived_from_run_id, derived_via,
            )

    async def reconcile(self) -> list[dict]:
        """
        Run the periodic promotion/retirement reconciliation.
        Returns a list of {lesson_id, action, new_status} dicts.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM reconcile_lessons()")
            return [dict(r) for r in rows]
