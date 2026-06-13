"""Stall guard + broken/saturation indicators for harness/dispatch.py — fully mocked, NO Postgres.

Proves the three hardening guarantees:
  * a hung handler is HARD-bounded — asyncio.wait_for cancels it on overrun, the
    concurrency slot frees, the event is marked failed, and `agent.stalled` is emitted;
  * the live in-flight registry is populated while a handler runs and cleared after
    (success, raise, AND timeout) so it can never leak slot bookkeeping;
  * the watchdog detectors surface what the DB row-reap can't:
      - `agent.slow`        — a handler past the soft-warn age (early warning)
      - `dispatch.saturated`— every slot busy while events back up ("held up by …")
      - `agent.broken`      — an agent whose recent runs ALL failed.

DB is scripted with tests._helpers.ScriptedPool; get_agent_mode is monkeypatched so the
mode gate never short-circuits a test exercising a different branch. Module constants
(HANDLER_TIMEOUT_S / HANDLER_SLOW_WARN_S / …) are read at call time, so monkeypatching
them on the module controls the thresholds without a real clock.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import harness.dispatch as dispatch_mod
from harness.dispatch import Dispatcher
from tests._helpers import ScriptedPool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_row(event_id: int = 1, event_type: str = "task.created", target_type: str = "task", target_id=1) -> dict:
    return {
        "id": event_id,
        "event_type": event_type,
        "target_type": target_type,
        "target_id": target_id,
        "payload": {},
        "status": "pending",
    }


def _pool_for_event(event: dict, **extra_rules) -> ScriptedPool:
    rules = [("FROM events WHERE id", [event])]
    rules.extend(extra_rules.items())
    return ScriptedPool(rules)


def _no_gate(monkeypatch):
    monkeypatch.setattr(dispatch_mod, "get_agent_mode", AsyncMock(return_value="active"))


def _emitted(pool: ScriptedPool, event_type: str) -> list[dict]:
    """Indicator events the dispatcher INSERTed, decoded from the (etype, payload, dedup) args."""
    out = []
    for kind, sql, args in pool.calls:
        if kind == "execute" and "INSERT INTO events" in sql and args and args[0] == event_type:
            out.append({"payload": json.loads(args[1]), "dedup": args[2]})
    return out


def _suppression_reason(pool: ScriptedPool) -> str | None:
    for kind, sql, args in reversed(pool.calls):
        if kind == "execute" and "suppressed" in sql:
            return args[0]
    return None


@pytest.fixture(autouse=True)
def _clear_mode_cache():
    from harness import agent_modes

    agent_modes._cache.clear()
    yield
    agent_modes._cache.clear()


# ===========================================================================
# Handler hard timeout → cancel + free slot + flag
# ===========================================================================


@pytest.mark.asyncio
async def test_handler_timeout_cancels_and_marks_failed(monkeypatch):
    _no_gate(monkeypatch)
    monkeypatch.setattr(dispatch_mod, "HANDLER_TIMEOUT_S", 0.05)
    cancelled = {"yes": False}

    async def hung(event, d):  # noqa: ARG001
        try:
            await asyncio.sleep(10)  # never completes within the timeout
        except asyncio.CancelledError:
            cancelled["yes"] = True
            raise

    hung.__module__ = "agents.researcher.handler"
    hung.__name__ = "hung"
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool)
    disp.register("task.created", hung)

    await disp._process_event(1)

    # the coroutine was actually cancelled (slot freed), the event marked failed…
    assert cancelled["yes"] is True
    failed = [a for k, sql, a in pool.calls if k == "execute" and "status = 'failed'" in sql]
    assert failed and "timed out" in failed[-1][0]
    # …and an agent.stalled indicator was emitted naming the agent + handler
    stalled = _emitted(pool, "agent.stalled")
    assert stalled and stalled[0]["payload"]["agent"] == "researcher"
    assert stalled[0]["payload"]["handler"] == "hung"
    assert stalled[0]["payload"]["action"] == "cancelled"


@pytest.mark.asyncio
async def test_handler_timeout_frees_the_concurrency_slot(monkeypatch):
    """The whole point: a hung handler must NOT permanently hold its slot. After a
    timeout the slot is free, so a subsequent fast handler runs to completion."""
    _no_gate(monkeypatch)
    monkeypatch.setattr(dispatch_mod, "HANDLER_TIMEOUT_S", 0.05)
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool, max_concurrent_handlers=1)  # single slot — must be reusable

    async def hung(event, d):  # noqa: ARG001
        await asyncio.sleep(10)

    hung.__module__ = "agents.researcher.handler"
    hung.__name__ = "hung"
    disp.register("task.created", hung)
    await disp._process_event(1)  # times out, cancels, frees the only slot

    assert not disp._inflight, "in-flight registry must be empty after the handler is cancelled"
    assert disp._handler_sem._value == 1, "the single concurrency slot must be released"


@pytest.mark.asyncio
async def test_inflight_registry_populated_then_cleared_on_success(monkeypatch):
    _no_gate(monkeypatch)
    seen = {}

    async def handler(event, d):  # noqa: ARG001
        seen["inflight"] = dict(d._inflight)  # snapshot while running
        return {"ok": True}

    handler.__module__ = "agents.planner.handler"
    handler.__name__ = "handler"
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool)
    disp.register("task.created", handler)
    await disp._process_event(1)

    # exactly one entry while running, naming the agent + event…
    assert len(seen["inflight"]) == 1
    rec = next(iter(seen["inflight"].values()))
    assert rec["agent"] == "planner" and rec["event_id"] == 1
    # …and nothing left behind afterwards
    assert disp._inflight == {}


@pytest.mark.asyncio
async def test_inflight_cleared_when_handler_raises(monkeypatch):
    _no_gate(monkeypatch)

    async def boom(event, d):  # noqa: ARG001
        raise RuntimeError("kaboom")

    boom.__module__ = "agents.planner.handler"
    boom.__name__ = "boom"
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool)
    disp.register("task.created", boom)
    await disp._process_event(1)
    assert disp._inflight == {}  # finally-clause cleanup ran even on the raise path


# ===========================================================================
# _detect_stalls — agent.slow + dispatch.saturated (live in-flight view)
# ===========================================================================


@pytest.mark.asyncio
async def test_detect_stalls_emits_slow_for_aged_handler(monkeypatch):
    monkeypatch.setattr(dispatch_mod, "HANDLER_SLOW_WARN_S", 600.0)
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool, max_concurrent_handlers=4)
    # one in-flight handler that started ~700s ago (monotonic is "now"-relative)
    disp._inflight[1] = {
        "agent": "researcher",
        "handler": "handle_grounded_research",
        "event_id": 42,
        "started_at": dispatch_mod.time.monotonic() - 700,
    }
    await disp._detect_stalls()
    slow = _emitted(pool, "agent.slow")
    assert slow and slow[0]["payload"]["agent"] == "researcher"
    assert slow[0]["payload"]["age_s"] >= 600


@pytest.mark.asyncio
async def test_detect_stalls_no_slow_for_young_handler(monkeypatch):
    monkeypatch.setattr(dispatch_mod, "HANDLER_SLOW_WARN_S", 600.0)
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool, max_concurrent_handlers=4)
    disp._inflight[1] = {
        "agent": "researcher",
        "handler": "h",
        "event_id": 1,
        "started_at": dispatch_mod.time.monotonic() - 5,  # only 5s old
    }
    await disp._detect_stalls()
    assert _emitted(pool, "agent.slow") == []


@pytest.mark.asyncio
async def test_detect_stalls_emits_saturated_when_full_and_backlogged(monkeypatch):
    monkeypatch.setattr(dispatch_mod, "HANDLER_SLOW_WARN_S", 600.0)
    monkeypatch.setattr(dispatch_mod, "SATURATION_BACKLOG", 5)
    pool = ScriptedPool([("status = 'pending'", [{"count": 9}])])
    disp = Dispatcher(pool=pool, max_concurrent_handlers=2)
    now = dispatch_mod.time.monotonic()
    disp._inflight = {
        1: {"agent": "researcher", "handler": "h", "event_id": 1, "started_at": now},
        2: {"agent": "critic", "handler": "h", "event_id": 2, "started_at": now},
    }
    await disp._detect_stalls()
    sat = _emitted(pool, "dispatch.saturated")
    assert sat, "all slots busy + backlog over the mark → must flag saturation"
    p = sat[0]["payload"]
    assert p["in_flight"] == 2 and p["max"] == 2 and p["backlog"] == 9
    assert p["held_by"] == ["critic", "researcher"]  # sorted, deduped


@pytest.mark.asyncio
async def test_detect_stalls_no_saturated_when_slot_free(monkeypatch):
    monkeypatch.setattr(dispatch_mod, "HANDLER_SLOW_WARN_S", 600.0)
    # full backlog, but a slot is free → not held up → no probe, no emit
    pool = ScriptedPool([("status = 'pending'", [{"count": 99}])])
    disp = Dispatcher(pool=pool, max_concurrent_handlers=4)
    disp._inflight = {1: {"agent": "x", "handler": "h", "event_id": 1, "started_at": dispatch_mod.time.monotonic()}}
    await disp._detect_stalls()
    assert _emitted(pool, "dispatch.saturated") == []
    # short-circuited before the backlog probe ran
    assert not any("status = 'pending'" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_detect_stalls_no_saturated_when_backlog_low(monkeypatch):
    monkeypatch.setattr(dispatch_mod, "HANDLER_SLOW_WARN_S", 600.0)
    monkeypatch.setattr(dispatch_mod, "SATURATION_BACKLOG", 5)
    pool = ScriptedPool([("status = 'pending'", [{"count": 1}])])  # below the mark
    disp = Dispatcher(pool=pool, max_concurrent_handlers=1)
    disp._inflight = {1: {"agent": "x", "handler": "h", "event_id": 1, "started_at": dispatch_mod.time.monotonic()}}
    await disp._detect_stalls()
    assert _emitted(pool, "dispatch.saturated") == []


@pytest.mark.asyncio
async def test_detect_stalls_swallows_backlog_probe_error(monkeypatch):
    monkeypatch.setattr(dispatch_mod, "HANDLER_SLOW_WARN_S", 600.0)

    class _BoomPool:
        def acquire(self):
            raise RuntimeError("db down")

    disp = Dispatcher(pool=_BoomPool(), max_concurrent_handlers=1)
    disp._inflight = {1: {"agent": "x", "handler": "h", "event_id": 1, "started_at": dispatch_mod.time.monotonic()}}
    await disp._detect_stalls()  # must not raise


# ===========================================================================
# _detect_broken_agents — agent.broken
# ===========================================================================


@pytest.mark.asyncio
async def test_detect_broken_agents_emits_for_all_failed(monkeypatch):
    rows = [{"agent_name": "researcher", "failed": 5, "completed": 0, "sample": "schema mismatch"}]
    pool = ScriptedPool([("FROM agent_runs", rows)])
    disp = Dispatcher(pool=pool)
    await disp._detect_broken_agents(pool.conn)
    broken = _emitted(pool, "agent.broken")
    assert broken and broken[0]["payload"]["agent"] == "researcher"
    assert broken[0]["payload"]["failed"] == 5
    assert broken[0]["payload"]["sample_error"] == "schema mismatch"
    assert broken[0]["dedup"].startswith("broken-researcher-")


@pytest.mark.asyncio
async def test_detect_broken_agents_noop_when_no_rows():
    pool = ScriptedPool()  # HAVING filters everything out → empty
    disp = Dispatcher(pool=pool)
    await disp._detect_broken_agents(pool.conn)
    assert _emitted(pool, "agent.broken") == []


# ===========================================================================
# _emit_indicator — insert shape + dedup + error swallow
# ===========================================================================


@pytest.mark.asyncio
async def test_emit_indicator_inserts_with_sentinel_target_and_dedup():
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    await disp._emit_indicator("agent.broken", {"agent": "x"}, dedup="k1")
    ins = [a for k, sql, a in pool.calls if k == "execute" and "INSERT INTO events" in sql]
    assert ins and ins[0][0] == "agent.broken"
    assert json.loads(ins[0][1]) == {"agent": "x"}
    assert ins[0][2] == "k1"
    # sentinel target_type='agent', target_id=0 baked into the SQL so dedup works
    sql = next(s for k, s, _ in pool.calls if k == "execute" and "INSERT INTO events" in s)
    assert "'agent', 0" in sql and "ON CONFLICT" in sql


@pytest.mark.asyncio
async def test_emit_indicator_swallows_db_error(caplog):
    class _BoomPool:
        def acquire(self):
            raise RuntimeError("db down")

    disp = Dispatcher(pool=_BoomPool())
    with caplog.at_level("ERROR"):
        await disp._emit_indicator("agent.slow", {"a": 1}, dedup="k")  # must not raise
    assert any("failed to emit indicator" in r.message for r in caplog.records)


# ===========================================================================
# Watchdog integration — detectors are wired into the loop
# ===========================================================================


@pytest.mark.asyncio
async def test_watchdog_runs_stall_and_broken_detectors(monkeypatch):
    pool = ScriptedPool(default_exec="UPDATE 0")
    disp = Dispatcher(pool=pool)
    disp._running = True
    seen = {"broken": 0, "stalls": 0}

    async def _broken(conn):
        seen["broken"] += 1

    async def _stalls():
        seen["stalls"] += 1
        disp._running = False  # end the loop after the first full sweep

    # Every OTHER rung the watchdog loop calls must be a cheap no-op. If an un-mocked rung
    # raises against the ScriptedPool, the loop's `except` swallows it, `_detect_stalls` (which
    # ends the loop) is skipped, and with asyncio.sleep mocked the loop SPINS FOREVER. Keep this
    # list in sync with _watchdog_loop's body (harness/dispatch.py).
    for name in (
        "_sweep_stale_tasks",
        "_revive_stranded_tasks",
        "_sweep_pending_events",
        "_check_phase_budget",
        "_refresh_slop_view",
        "_detect_unclosed_events",
        "_advance_research_closure",
        "_reopen_gapped_directions",
        "_reconcile_scholarship_ingest",
        "_rearm_research_spines",
        "_detect_stuck_directions",
        "_detect_eaten_events",
        "_emit_lab_pulse",
        "_reconcile_lessons_if_due",
        "_sweep_library_if_due",
    ):
        monkeypatch.setattr(disp, name, AsyncMock())
    monkeypatch.setattr(disp, "_detect_broken_agents", _broken)
    monkeypatch.setattr(disp, "_detect_stalls", _stalls)
    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", AsyncMock())

    await disp._watchdog_loop()
    assert seen == {"broken": 1, "stalls": 1}
