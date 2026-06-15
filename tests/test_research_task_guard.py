"""Migration 026 — research tasks belong to directions; the guard trigger + the reap.

The BEFORE-INSERT trigger SKIPS (returns NULL) a department='research' task on a MISSION/FINDING claim
so the leak can't recur; `reap_orphan_research_tasks()` halts any historical zombies. Direction and
hypothesis research tasks are untouched, and non-research tasks on any claim are ignored. DB-backed
(the `db` fixture → a migrated disposable Postgres).
"""

from __future__ import annotations

import pytest

from ops import reap_orphan_tasks


async def _claim(db, kind: str) -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO claims (statement, claim_kind) VALUES ($1, $2::claim_kind) RETURNING id",
            f"{kind} claim",
            kind,
        )


async def _insert_research_task(db, claim_id: int):
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO tasks (department, task_type, description, claim_id) "
            "VALUES ('research', 'research', 'd', $1) RETURNING id",
            claim_id,
        )


@pytest.mark.asyncio
async def test_guard_skips_research_task_on_mission_and_finding(db):
    for kind in ("mission", "finding"):
        cid = await _claim(db, kind)
        tid = await _insert_research_task(db, cid)
        assert tid is None  # trigger returned NULL → no row inserted
        async with db.pool.acquire() as conn:
            n = await conn.fetchval("SELECT count(*) FROM tasks WHERE claim_id=$1", cid)
        assert n == 0


@pytest.mark.asyncio
async def test_guard_keeps_research_task_on_direction_and_hypothesis(db):
    for kind in ("direction", "hypothesis"):
        cid = await _claim(db, kind)
        tid = await _insert_research_task(db, cid)
        assert tid is not None  # a legitimate research target — kept
        async with db.pool.acquire() as conn:
            n = await conn.fetchval("SELECT count(*) FROM tasks WHERE claim_id=$1", cid)
        assert n == 1


@pytest.mark.asyncio
async def test_guard_ignores_non_research_task_on_a_mission_claim(db):
    # The guard only filters department='research'; an ops/etc task on a mission claim is fine.
    cid = await _claim(db, "mission")
    async with db.pool.acquire() as conn:
        tid = await conn.fetchval(
            "INSERT INTO tasks (department, task_type, description, claim_id) VALUES ('ops', 'x', 'd', $1) RETURNING id",
            cid,
        )
    assert tid is not None


@pytest.mark.asyncio
async def test_reap_halts_only_historical_mission_finding_orphans(db):
    mission = await _claim(db, "mission")
    finding = await _claim(db, "finding")
    direction = await _claim(db, "direction")
    # Simulate pre-guard zombies by inserting with the guard trigger disabled.
    async with db.pool.acquire() as conn:
        await conn.execute("ALTER TABLE tasks DISABLE TRIGGER trg_research_tasks_directions_only")
        for cid in (mission, finding):
            await conn.execute(
                "INSERT INTO tasks (department, task_type, description, claim_id) "
                "VALUES ('research', 'research', 'zombie', $1)",
                cid,
            )
        await conn.execute("ALTER TABLE tasks ENABLE TRIGGER trg_research_tasks_directions_only")
    dir_task = await _insert_research_task(db, direction)  # a legit task via the normal path
    assert dir_task is not None

    assert await db.reap_orphan_research_tasks() == 2

    async with db.pool.acquire() as conn:
        halted = await conn.fetchval(
            "SELECT count(*) FROM tasks t JOIN claims c ON c.id=t.claim_id "
            "WHERE c.claim_kind IN ('mission','finding') AND t.status='halted'"
        )
        dir_status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", dir_task)
    assert halted == 2
    assert dir_status == "pending"  # the direction's task is untouched

    # Idempotent — a second reap halts nothing.
    assert await db.reap_orphan_research_tasks() == 0


# ---------------------------------------------------------------------------
# the ops.reap_orphan_tasks CLI (report-only default vs --halt)
# ---------------------------------------------------------------------------


async def _seed_zombie(db, kind: str) -> None:
    cid = await _claim(db, kind)
    async with db.pool.acquire() as conn:
        await conn.execute("ALTER TABLE tasks DISABLE TRIGGER trg_research_tasks_directions_only")
        await conn.execute(
            "INSERT INTO tasks (department, task_type, description, claim_id) "
            "VALUES ('research', 'research', 'zombie', $1)",
            cid,
        )
        await conn.execute("ALTER TABLE tasks ENABLE TRIGGER trg_research_tasks_directions_only")


@pytest.mark.asyncio
async def test_reap_cli_reports_clean_when_nothing_to_reap(db):
    assert await reap_orphan_tasks.run(halt=False) == 0


@pytest.mark.asyncio
async def test_reap_cli_report_only_leaves_tasks_pending_then_halt_reaps(db):
    await _seed_zombie(db, "mission")
    await _seed_zombie(db, "finding")

    # report-only: must NOT change anything
    assert await reap_orphan_tasks.run(halt=False) == 0
    async with db.pool.acquire() as conn:
        pending = await conn.fetchval("SELECT count(*) FROM tasks WHERE status='pending' AND department='research'")
    assert pending == 2

    # --halt: reaps them
    assert await reap_orphan_tasks.run(halt=True) == 0
    async with db.pool.acquire() as conn:
        halted = await conn.fetchval("SELECT count(*) FROM tasks WHERE status='halted'")
    assert halted == 2
