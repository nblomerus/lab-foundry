"""Assign an approved direction to a researcher — the ownership decision.

When Ariadne approves a direction it gets an owner: the researcher who will author, run, and
interpret all of its experiments (claims.researcher_id). Policy = a light SPECIALTY match against
the direction statement, tie-broken by LEAST-LOADED so no one researcher hoards directions and none
is starved. Cold-start safe: with no keyword hit it degrades to pure least-loaded round-robin.

Idempotent: a direction that already has an owner is never reassigned here.
"""

from __future__ import annotations

import logging

from agents.researcher.identity import Researcher, active_roster, load_researcher

log = logging.getLogger(__name__)

# Specialty tag -> keywords that bias a direction toward that leaning. Tags match the seeded roster
# (migration 022); an unknown tag simply earns no boost (still eligible via least-loaded).
_SPECIALTY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "systems-optimization": (
        "optim",
        "training",
        "train",
        "architecture",
        "convergence",
        "gradient",
        "from scratch",
        "from-scratch",
        "neural",
        "network",
        "sgd",
        "regulari",
        "scaling",
        "efficiency",
        "pruning",
        "quantiz",
        "kernel",
        "throughput",
        "latency",
    ),
    "statistics-calibration": (
        "calibrat",
        "uncertain",
        "estimator",
        "statistic",
        "confidence",
        "probab",
        "bayes",
        "variance",
        "bias",
        "distribution",
        "interval",
        "significance",
        "hypothesis test",
        "ece",
        "coverage",
        "robust",
    ),
    "llm-retrieval-eval": (
        "llm",
        "language model",
        "retriev",
        "rerank",
        "embedding",
        "prompt",
        "benchmark",
        "rag",
        "nli",
        "classif",
        "question answer",
        "qa",
        "reasoning",
        "self-consistency",
        "decoding",
        "sampling",
        "in-context",
    ),
}


def _specialty_score(statement: str, specialty: str) -> int:
    kws = _SPECIALTY_KEYWORDS.get(specialty, ())
    s = (statement or "").lower()
    return sum(1 for kw in kws if kw in s)


async def _load_by_researcher(pool) -> dict[int, int]:
    """researcher_id -> count of OWNED, not-yet-concluded directions (the load metric)."""
    rows = await pool.fetch(
        "SELECT researcher_id, count(*) AS n FROM claims "
        "WHERE researcher_id IS NOT NULL AND claim_kind = 'direction' AND status <> 'concluded' "
        "GROUP BY researcher_id"
    )
    return {r["researcher_id"]: r["n"] for r in rows}


def _pick(roster: list[Researcher], statement: str, load: dict[int, int]) -> Researcher | None:
    """Best specialty match, tie-broken by least current load, then by id (stable)."""
    if not roster:
        return None
    return max(
        roster,
        key=lambda r: (_specialty_score(statement, r.specialty), -load.get(r.id, 0), -r.id),
    )


async def assign_direction(pool, claim_id: int) -> Researcher | None:
    """Ensure `claim_id` has an owner; return it. Propagates the owner to the direction's existing
    research tasks (so already-planned tasks inherit it). No-op if already assigned. `pool` is an
    asyncpg pool (state.pool, or the raw pool the pace loop holds)."""
    existing = await pool.fetchval("SELECT researcher_id FROM claims WHERE id = $1", claim_id)
    if existing is not None:
        return await load_researcher(pool, existing)

    roster = await active_roster(pool)
    statement = await pool.fetchval("SELECT statement FROM claims WHERE id = $1", claim_id) or ""
    load = await _load_by_researcher(pool)
    chosen = _pick(roster, statement, load)
    if chosen is None:
        log.warning("assign: no active researcher in roster to own direction %s", claim_id)
        return None

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE claims SET researcher_id = $1 WHERE id = $2", chosen.id, claim_id)
        await conn.execute(
            "UPDATE tasks SET researcher_id = $1 WHERE claim_id = $2 AND researcher_id IS NULL",
            chosen.id,
            claim_id,
        )
    log.info("assign: direction %s -> %s (%s)", claim_id, chosen.name, chosen.specialty)
    return chosen


async def backfill_unassigned(pool) -> int:
    """Assign every approved-but-unowned direction (first-run / migration catch-up). Returns count."""
    rows = await pool.fetch(
        "SELECT c.id FROM claims c JOIN direction_gate g ON g.claim_id = c.id "
        "WHERE c.claim_kind = 'direction' AND c.researcher_id IS NULL "
        "AND g.status = 'approved' AND c.status <> 'concluded' ORDER BY c.id"
    )
    n = 0
    for r in rows:
        if await assign_direction(pool, r["id"]) is not None:
            n += 1
    if n:
        log.info("assign: backfilled %d unassigned direction(s)", n)
    return n
