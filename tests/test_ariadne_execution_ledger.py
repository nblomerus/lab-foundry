"""Ariadne's experiment feedback — the execution ledger + the evidence stamp (DB-backed).

`get_direction_execution_digest` is the read that puts experiment reality into Ariadne's deliberate
prompt: it UNIONs active-but-unworked directions with worked-but-invalidated ones (the cross-re-frame
view that stops her re-proposing killed ground), with done/failed counts + the latest headline.
`mark_claim_evidence` stamps last_evidence_at when an experiment lands, so worked directions stop
under-reporting as untouched. Verified against a migrated disposable Postgres (the `db` fixture).
"""

from __future__ import annotations

import pytest


async def _direction(db, statement, *, status="tested", invalidation_reason=None) -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO claims (statement, claim_kind, status, invalidation_reason) "
            "VALUES ($1, 'direction', $2::claim_status, $3) RETURNING id",
            statement,
            status,
            invalidation_reason,
        )


async def _experiment(db, claim_id, *, status, interpretation=None, ago_days=1) -> int:
    """One experiment_run on a direction (via a task), with a completed_at `ago_days` in the past."""
    async with db.pool.acquire() as conn:
        task_id = await conn.fetchval(
            "INSERT INTO tasks (department, task_type, description, claim_id) "
            "VALUES ('research', 'research', 'd', $1) RETURNING id",
            claim_id,
        )
        return await conn.fetchval(
            "INSERT INTO experiment_runs (task_id, kind, params, status, interpretation, completed_at) "
            "VALUES ($1, 'benchmark', '{}'::jsonb, $2, $3, now() - make_interval(days => $4)) "
            "RETURNING id",
            task_id,
            status,
            interpretation,
            ago_days,
        )


# ---------------------------------------------------------------------------
# get_direction_execution_digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_unions_active_unworked_and_worked_invalidated(db):
    active = await _direction(db, "active unworked dir")  # 0 experiments, active
    killed = await _direction(
        db, "worked then killed dir", status="invalidated", invalidation_reason="research gap: corpus still thin"
    )
    await _experiment(db, killed, status="completed", interpretation="GPs matched XGBoost within 1%", ago_days=2)
    await _experiment(db, killed, status="completed", ago_days=1)
    await _experiment(db, killed, status="failed", ago_days=1)

    rows = await db.get_direction_execution_digest([active])
    by_id = {r["claim_id"]: r for r in rows}

    # both surface — the active one AND the invalidated-but-worked one (no current channel does this)
    assert active in by_id
    assert killed in by_id

    k = by_id[killed]
    assert k["done"] == 2
    assert k["failed"] == 1
    assert k["status"] == "invalidated"
    assert "research gap" in k["invalidation_reason"]
    assert k["headline"] == "GPs matched XGBoost within 1%"  # latest interpretation

    a = by_id[active]
    assert a["done"] == 0 and a["failed"] == 0


@pytest.mark.asyncio
async def test_digest_sorts_active_first(db):
    killed = await _direction(db, "old killed", status="invalidated")
    await _experiment(db, killed, status="completed", ago_days=5)
    active = await _direction(db, "active worked")
    await _experiment(db, active, status="completed", ago_days=1)

    rows = await db.get_direction_execution_digest([active])
    assert rows[0]["claim_id"] == active  # active directions render first


@pytest.mark.asyncio
async def test_digest_excludes_unworked_inactive_directions(db):
    # an invalidated direction with NO experiments is neither active nor worked → not in the ledger
    await _direction(db, "stillborn dir", status="invalidated")
    active = await _direction(db, "the only active dir")
    rows = await db.get_direction_execution_digest([active])
    assert [r["claim_id"] for r in rows] == [active]


@pytest.mark.asyncio
async def test_digest_empty_active_list_still_returns_worked_directions(db):
    killed = await _direction(db, "worked, no active set", status="invalidated")
    await _experiment(db, killed, status="completed", ago_days=1)
    rows = await db.get_direction_execution_digest([])  # empty active set must not error
    assert [r["claim_id"] for r in rows] == [killed]


@pytest.mark.asyncio
async def test_digest_counts_killed_as_failed(db):
    d = await _direction(db, "killed-run dir")
    await _experiment(db, d, status="killed", ago_days=1)
    rows = await db.get_direction_execution_digest([d])
    assert rows[0]["failed"] == 1


# ---------------------------------------------------------------------------
# mark_claim_evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_claim_evidence_stamps_last_evidence_at(db):
    cid = await _direction(db, "dir to stamp")
    async with db.pool.acquire() as conn:
        before = await conn.fetchval("SELECT last_evidence_at FROM claims WHERE id=$1", cid)
    assert before is None

    await db.mark_claim_evidence(cid)

    async with db.pool.acquire() as conn:
        after = await conn.fetchval("SELECT last_evidence_at FROM claims WHERE id=$1", cid)
    assert after is not None
