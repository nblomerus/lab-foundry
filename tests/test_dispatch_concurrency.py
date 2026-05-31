"""
Tests for the dispatcher's concurrency bound and startup orphan reap.

These use a fake asyncpg pool/connection so they run without a database.
The point isn't to exercise SQL — it's to prove that:
  1. no more than `max_concurrent_handlers` handlers run at once, and
  2. startup reaps any leftover 'running' agent_runs / 'running' tasks.
"""

from __future__ import annotations

import asyncio

import pytest

from labfoundry.harness.dispatch import Dispatcher

# --------------------------------------------------------------------------
# Fake asyncpg pool / connection
# --------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, event: dict, executed: list[str]):
        self._event = event
        self._executed = executed

    async def fetchrow(self, query: str, *args):
        if "FROM events" in query:
            return dict(self._event)
        # cost_tracking cap check etc. → no row
        return None

    async def fetchval(self, query: str, *args):
        return None  # not cooled down, no slop, etc.

    async def execute(self, query: str, *args):
        self._executed.append(query)
        if "agent_runs" in query:
            return "UPDATE 3"
        if "tasks" in query:
            return "UPDATE 2"
        return "UPDATE 1"


class _AcquireCtx:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, event: dict):
        self._event = event
        self.executed: list[str] = []

    def acquire(self):
        return _AcquireCtx(_FakeConn(self._event, self.executed))


def _event(event_type: str = "task.created", target_type: str = "task") -> dict:
    # target_type != 'thesis' skips the slop gate; this (event_type, target_type)
    # isn't in COOLDOWNS so the cooldown gate short-circuits too.
    return {
        "id": 1,
        "event_type": event_type,
        "target_type": target_type,
        "target_id": 1,
        "payload": {},
        "status": "pending",
    }


# --------------------------------------------------------------------------
# Concurrency bound
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_concurrency_is_bounded():
    pool = _FakePool(_event())
    disp = Dispatcher(pool=pool, max_concurrent_handlers=3)

    live = 0
    peak = 0

    async def handler(ev, d):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)  # hold the slot so others pile up
        live -= 1
        return {"ok": True}

    disp.register("task.created", handler)

    # Fire many more events than the limit, all at once.
    await asyncio.gather(*[disp._process_event(1) for _ in range(12)])

    assert peak <= 3, f"exceeded the concurrency cap: peaked at {peak}"
    assert peak == 3, f"never reached the cap (peaked at {peak}); test wouldn't catch a regression"


@pytest.mark.asyncio
async def test_concurrency_limit_of_one_serializes():
    pool = _FakePool(_event())
    disp = Dispatcher(pool=pool, max_concurrent_handlers=1)

    order: list[str] = []

    async def handler(ev, d):
        order.append("enter")
        await asyncio.sleep(0.02)
        order.append("exit")
        return None

    disp.register("task.created", handler)
    await asyncio.gather(*[disp._process_event(1) for _ in range(4)])

    # With a cap of 1, every enter is immediately followed by its own exit —
    # no interleaving.
    assert order == ["enter", "exit"] * 4


# --------------------------------------------------------------------------
# Startup orphan reap
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_reap_marks_running_runs_and_resets_tasks():
    pool = _FakePool(_event())
    disp = Dispatcher(pool=pool)

    await disp._reap_startup_orphans()

    joined = "\n".join(pool.executed)
    # agent_runs orphans → failed, tagged so the metric can exclude them
    assert "UPDATE agent_runs" in joined
    assert "orphan reaped at startup" in joined
    assert "status = 'failed'" in joined
    # tasks orphans → reset to pending so the work resumes
    assert "UPDATE tasks" in joined
    assert "status = 'pending'" in joined


# --------------------------------------------------------------------------
# Liveness pump
# --------------------------------------------------------------------------


class _ReviveConn:
    """Fake conn returning configurable pending-task / pending-trigger counts."""

    def __init__(self, pending_tasks: int, pending_triggers: int, inserts: list[tuple]):
        self._pending_tasks = pending_tasks
        self._pending_triggers = pending_triggers
        self._inserts = inserts

    async def fetchval(self, query: str, *args):
        if "FROM tasks" in query:
            return self._pending_tasks
        if "task.created" in query and "status = 'pending'" in query:
            return self._pending_triggers
        return 0

    async def execute(self, query: str, *args):
        if "INSERT INTO events" in query and "task.created" in query:
            self._inserts.append(args)
        return "INSERT 0 1"


async def _run_revive(pending_tasks: int, pending_triggers: int) -> list[tuple]:
    inserts: list[tuple] = []
    conn = _ReviveConn(pending_tasks, pending_triggers, inserts)
    disp = Dispatcher(pool=_FakePool(_event()))
    await disp._revive_stranded_tasks(conn)
    return inserts


@pytest.mark.asyncio
async def test_revive_emits_one_trigger_per_stranded_task():
    # 10 pending tasks, 0 pending triggers → 10 revive events with unique keys
    inserts = await _run_revive(pending_tasks=10, pending_triggers=0)
    assert len(inserts) == 10
    dedup_keys = [a[0] for a in inserts]
    assert len(set(dedup_keys)) == 10, "revive dedup_keys must be unique"
    assert all(k.startswith("revive-") for k in dedup_keys)


@pytest.mark.asyncio
async def test_revive_only_covers_the_deficit():
    # 10 pending tasks but 7 triggers already pending → emit only the 3 missing
    inserts = await _run_revive(pending_tasks=10, pending_triggers=7)
    assert len(inserts) == 3


@pytest.mark.asyncio
async def test_revive_noop_when_triggers_cover_tasks():
    # enough (or more) triggers already pending → emit nothing
    assert await _run_revive(pending_tasks=4, pending_triggers=4) == []
    assert await _run_revive(pending_tasks=4, pending_triggers=9) == []
    # no pending tasks at all → nothing
    assert await _run_revive(pending_tasks=0, pending_triggers=0) == []


class _StatefulReviveConn:
    """Shared DB view: pending_triggers grows as triggers are inserted.

    Every method yields (sleep 0) so the two revive passes interleave on the
    event loop — which is exactly how the startup + watchdog passes raced and
    double-emitted before the lock.
    """

    def __init__(self, pending_tasks: int):
        self.pending_tasks = pending_tasks
        self.pending_triggers = 0
        self.insert_count = 0

    async def fetchval(self, query: str, *args):
        await asyncio.sleep(0)
        if "FROM tasks" in query:
            return self.pending_tasks
        if "task.created" in query and "status = 'pending'" in query:
            return self.pending_triggers
        return 0

    async def execute(self, query: str, *args):
        await asyncio.sleep(0)
        if "INSERT INTO events" in query and "task.created" in query:
            self.insert_count += 1
            self.pending_triggers += 1
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_concurrent_revive_passes_do_not_double_emit():
    # Startup pass + watchdog's immediate first pass run concurrently against
    # the same DB state. The lock must serialize them so only the deficit (10)
    # is emitted once — not 20.
    conn = _StatefulReviveConn(pending_tasks=10)
    disp = Dispatcher(pool=_FakePool(_event()))
    await asyncio.gather(
        disp._revive_stranded_tasks(conn),
        disp._revive_stranded_tasks(conn),
    )
    assert conn.insert_count == 10, f"double-emit: expected 10 triggers, got {conn.insert_count}"
