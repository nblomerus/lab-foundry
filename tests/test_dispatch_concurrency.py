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

from harness.dispatch import Dispatcher

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


# --------------------------------------------------------------------------
# Per-ingest-agent slot reservation (Mimir can't starve the other lanes)
# --------------------------------------------------------------------------


class _MultiConn:
    """Fake conn returning a per-id event so different ids dispatch different handlers."""

    def __init__(self, events: dict[int, dict], executed: list[str]):
        self._events = events
        self._executed = executed

    async def fetchrow(self, query: str, *args):
        if "FROM events" in query:
            ev = self._events.get(args[0])
            return dict(ev) if ev else None
        return None

    async def fetchval(self, query: str, *args):
        return None

    async def execute(self, query: str, *args):
        self._executed.append(query)
        return "UPDATE 1"


class _MultiPool:
    def __init__(self, events: dict[int, dict]):
        self._events = events
        self.executed: list[str] = []

    def acquire(self):
        return _AcquireCtx(_MultiConn(self._events, self.executed))


async def _always_active(pool, agent):  # patches get_agent_mode so the mode dial never gates
    return "active"


@pytest.mark.asyncio
async def test_per_agent_cap_prevents_one_lane_starving_others(monkeypatch):
    # AGENT_CONCURRENCY caps the BULK library-intake lane at 2 of the 3 global slots, so >=1
    # always stays free for the experiment lane even while bulk intake floods the queue. (Scout
    # source.discovered with no lab_* source_kind routes to the 'bulk' lane via _lane_for.)
    monkeypatch.setattr("harness.dispatch.get_agent_mode", _always_active)
    monkeypatch.setenv("AGENT_CONCURRENCY", "bulk=2")

    events: dict[int, dict] = {}
    for i in range(1, 9):  # 8 mimir ingest events, all firing at once
        events[i] = {
            "id": i,
            "event_type": "source.discovered",
            "target_type": "source",
            "target_id": i,
            "payload": {},
            "status": "pending",
        }
    for i in range(101, 104):  # 3 experiment-lane events
        events[i] = {
            "id": i,
            "event_type": "experiment.completed",
            "target_type": "experiment",
            "target_id": i,
            "payload": {},
            "status": "pending",
        }

    disp = Dispatcher(pool=_MultiPool(events), max_concurrent_handlers=3)
    assert disp._agent_caps.get("bulk") == 2

    mimir_live = mimir_peak = 0
    exp_ran_while_mimir_saturated = False

    async def mimir_h(ev, d):
        nonlocal mimir_live, mimir_peak
        mimir_live += 1
        mimir_peak = max(mimir_peak, mimir_live)
        await asyncio.sleep(0.05)  # hold the slot so the pool stays under pressure
        mimir_live -= 1
        return None

    mimir_h.__module__ = "agents.mimir.handler"  # agent_of -> "mimir"; lane -> "bulk" (capped at 2)

    async def exp_h(ev, d):
        nonlocal exp_ran_while_mimir_saturated
        if mimir_live >= 2:  # mimir is at its cap, yet we still got a slot
            exp_ran_while_mimir_saturated = True
        return None

    exp_h.__module__ = "agents.experiments.handler"  # agent_of -> "experiments" (uncapped)

    disp.register("source.discovered", mimir_h)
    disp.register("experiment.completed", exp_h)

    await asyncio.gather(*[disp._process_event(i) for i in list(range(1, 9)) + list(range(101, 104))])

    assert mimir_peak <= 2, f"bulk exceeded its cap: peaked at {mimir_peak}"
    assert mimir_peak == 2, "bulk never reached its cap; test wouldn't catch a regression"
    assert exp_ran_while_mimir_saturated, "experiment lane was starved while bulk saturated the pool"


@pytest.mark.asyncio
async def test_first_party_ingest_not_starved_by_bulk_backlog(monkeypatch):
    # The whole point of the lane split: a flood of BULK source.discovered (scout pushes) must not
    # starve a FIRST-PARTY source.discovered (a lab_finding) — they ride different semaphores.
    monkeypatch.setattr("harness.dispatch.get_agent_mode", _always_active)
    monkeypatch.setenv("AGENT_CONCURRENCY", "bulk=2,mimir=2")

    events: dict[int, dict] = {}
    for i in range(1, 7):  # 6 bulk scout pushes flooding the bulk lane
        events[i] = {
            "id": i,
            "event_type": "source.discovered",
            "target_type": "source",
            "target_id": i,
            "payload": {"source": {"source_kind": "arxiv"}},
            "status": "pending",
        }
    events[200] = {  # one first-party finding ingest
        "id": 200,
        "event_type": "source.discovered",
        "target_type": "source",
        "target_id": 200,
        "payload": {"source": {"source_kind": "lab_finding"}},
        "status": "pending",
    }

    disp = Dispatcher(pool=_MultiPool(events), max_concurrent_handlers=3)
    bulk_live = 0
    first_party_ran_while_bulk_saturated = False

    async def ingest_h(ev, d):
        nonlocal bulk_live, first_party_ran_while_bulk_saturated
        sk = (ev.get("payload") or {}).get("source", {}).get("source_kind", "")
        if sk.startswith("lab_"):
            if bulk_live >= 2:  # the bulk lane is at its cap, yet the lab ingest still got a slot
                first_party_ran_while_bulk_saturated = True
            return None
        bulk_live += 1
        await asyncio.sleep(0.05)  # hold the bulk slot so the lane stays saturated
        bulk_live -= 1
        return None

    ingest_h.__module__ = "agents.mimir.handler"  # agent 'mimir'; lane = bulk or mimir per source_kind
    disp.register("source.discovered", ingest_h)

    await asyncio.gather(*[disp._process_event(i) for i in list(range(1, 7)) + [200]])

    assert first_party_ran_while_bulk_saturated, "first-party lab ingest was starved behind the bulk backlog"


@pytest.mark.asyncio
async def test_researcher_pool_caps_at_its_configured_size(monkeypatch):
    # AGENT_CONCURRENCY="researcher=4" gives researchers a dedicated pool of 4: up
    # to 4 run in parallel (each on its own task), never more, even with 10 queued.
    monkeypatch.setattr("harness.dispatch.get_agent_mode", _always_active)
    monkeypatch.setenv("AGENT_CONCURRENCY", "researcher=4")

    events = {
        i: {
            "id": i,
            "event_type": "task.created",
            "target_type": "task",
            "target_id": i,
            "payload": {},
            "status": "pending",
        }
        for i in range(1, 11)  # 10 research tasks fire at once
    }
    disp = Dispatcher(pool=_MultiPool(events), max_concurrent_handlers=8)
    assert disp._agent_caps.get("researcher") == 4

    live = peak = 0

    async def res_h(ev, d):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.03)
        live -= 1
        return None

    res_h.__module__ = "agents.researcher.grounded_handler"  # agent_of -> "researcher"
    disp.register("task.created", res_h)

    await asyncio.gather(*[disp._process_event(i) for i in range(1, 11)])

    assert peak <= 4, f"researcher pool exceeded its cap of 4: peaked at {peak}"
    assert peak == 4, "researcher pool never reached 4; test wouldn't catch a regression"
