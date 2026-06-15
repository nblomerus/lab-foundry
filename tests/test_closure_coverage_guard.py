"""Closure-coverage guard (#3) — the synthesis-starvation fix, DB-backed.

The closure ladder used to gap-kill a direction as a "research gap" at 1-2 experiments, BEFORE it
reached SYNTHESIS_MIN_EXPERIMENTS=3 — so a finding was never synthesized and Ariadne re-proposed the
same ground. The guard lives at the single chokepoint `Dispatcher._declare_gap`:

  * done >= SYNTHESIS_MIN, or an existing finding  → NEVER gap (synthesizable work; rearm_conclude
    then fires finding.synthesize).
  * 1..MIN-1 and worked recently                    → deferred (let it march to the threshold).
  * 0 experiments, or stalled below the threshold   → gap proceeds (frees the slot).

Verified against a migrated disposable Postgres (the `db` fixture).
"""

from __future__ import annotations

import pytest

from harness.dispatch import REARM_SYNTH_MIN, Dispatcher


def _disp(db) -> Dispatcher:
    d = Dispatcher(pool=db.pool)
    d.state = db
    return d


async def _direction(db, *, status="tested") -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO claims (statement, claim_kind, status) VALUES ('dir', 'direction', $1::claim_status) "
            "RETURNING id",
            status,
        )


async def _completed_exp(db, claim_id, *, ago_min=5) -> None:
    async with db.pool.acquire() as conn:
        task_id = await conn.fetchval(
            "INSERT INTO tasks (department, task_type, description, claim_id) "
            "VALUES ('research', 'research', 'd', $1) RETURNING id",
            claim_id,
        )
        await conn.execute(
            "INSERT INTO experiment_runs (task_id, kind, params, status, interpretation, completed_at) "
            "VALUES ($1, 'benchmark', '{}'::jsonb, 'completed', 'r', now() - make_interval(mins => $2::int))",
            task_id,
            ago_min,
        )


async def _finding(db, claim_id) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO research_findings (direction_claim_id, headline, claim_text, supported, confidence) "
            "VALUES ($1, 'H', 'C', 'supported', 0.7)",
            claim_id,
        )


async def _status(db, cid) -> str:
    async with db.pool.acquire() as conn:
        return await conn.fetchval("SELECT status::text FROM claims WHERE id=$1", cid)


async def _indicator_kinds(db, cid) -> list[str]:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload->>'kind' AS kind FROM events WHERE event_type='loop.unclosed' AND payload->>'claim_id' = $1",
            str(cid),
        )
    return [r["kind"] for r in rows]


@pytest.mark.asyncio
async def test_unworked_direction_still_gaps(db):
    cid = await _direction(db)  # 0 experiments
    await _disp(db)._declare_gap(cid, "research gap: corpus still thin after acquire + targeted scout")
    assert await _status(db, cid) == "invalidated"  # nothing to protect — the gap proceeds


@pytest.mark.asyncio
async def test_direction_at_threshold_is_never_gapped(db):
    cid = await _direction(db)
    for _ in range(REARM_SYNTH_MIN):
        await _completed_exp(db, cid)
    await _disp(db)._declare_gap(cid, "research gap: corpus still thin")
    assert await _status(db, cid) != "invalidated"  # synthesizable — protected
    assert "synthesis_overdue" in await _indicator_kinds(db, cid)


@pytest.mark.asyncio
async def test_direction_with_a_finding_is_never_gapped(db):
    cid = await _direction(db)
    await _completed_exp(db, cid)
    await _finding(db, cid)  # already produced a finding
    await _disp(db)._declare_gap(cid, "research gap: corpus still thin")
    assert await _status(db, cid) != "invalidated"
    assert "gap_deferred_for_coverage" in await _indicator_kinds(db, cid)


@pytest.mark.asyncio
async def test_marching_direction_recent_work_is_deferred(db):
    cid = await _direction(db)
    await _completed_exp(db, cid, ago_min=5)  # 1 experiment, just now → still marching
    assert REARM_SYNTH_MIN > 1  # precondition for "marching" to be meaningful
    await _disp(db)._declare_gap(cid, "research gap: corpus still thin")
    assert await _status(db, cid) != "invalidated"
    assert "gap_deferred_for_coverage" in await _indicator_kinds(db, cid)


@pytest.mark.asyncio
async def test_stalled_below_threshold_gaps(db):
    cid = await _direction(db)
    # 1 experiment, but it completed 2 days ago → past the defer window → genuinely stalled
    await _completed_exp(db, cid, ago_min=2 * 24 * 60)
    await _disp(db)._declare_gap(cid, "research gap: corpus still thin")
    assert await _status(db, cid) == "invalidated"  # stalled marcher frees the slot
