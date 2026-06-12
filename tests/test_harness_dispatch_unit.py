"""Unit coverage for harness/dispatch.py + harness/session.py — fully mocked, NO Postgres.

Drives the dispatcher's decision branches directly:
  * agent_of  — module path → agent name (None for system handlers)
  * the per-agent mode gate (suppress below advisory; URGENT still gated;
    system handlers never gated; suppression_reason recorded)
  * the friction gates — cooldown, cost cap, slop — both pass and suppress
  * _on_notify — json parse + the bad-payload swallow path
  * _process_event — no_handler, consumed, handler-raise → failed
  * _revive_stranded_tasks — deficit emit / zero-deficit no-op (via ScriptedPool)
  * Session — next_step_order, last_step_id, start/finish/emit_event step DAG

DB is scripted with tests._helpers.ScriptedPool. get_agent_mode is monkeypatched
on harness.dispatch to control per-agent gating without a DB round-trip.
"""

from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock

import pytest

import harness.dispatch as dispatch_mod
from harness.dispatch import COOLDOWNS, URGENT_EVENTS, Dispatcher
from harness.session import Session
from tests._helpers import ScriptedPool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_row(
    event_id: int = 1,
    event_type: str = "task.created",
    target_type: str = "task",
    target_id: int | None = 1,
    payload=None,
    status: str = "pending",
) -> dict:
    return {
        "id": event_id,
        "event_type": event_type,
        "target_type": target_type,
        "target_id": target_id,
        "payload": {} if payload is None else payload,
        "status": status,
    }


def _make_handler(name: str, *, module: str, fn=None):
    """A real async function with a controllable __module__ (agent_of reads it)."""

    if fn is None:

        async def fn(event, dispatcher):  # noqa: ARG001
            return {"ok": True}

    fn.__name__ = name
    fn.__module__ = module
    return fn


def _pool_for_event(event: dict, **extra_rules) -> ScriptedPool:
    """ScriptedPool that returns `event` for the _process_event SELECT and lets the
    friction gates fall through to their defaults (no cooldown / cap / slop)."""
    rules = [("FROM events WHERE id", [event])]
    rules.extend(extra_rules.items())
    return ScriptedPool(rules)


def _suppression_reason(pool: ScriptedPool) -> str | None:
    """The reason arg of the last `status = 'suppressed'` UPDATE the dispatcher ran."""
    for kind, sql, args in reversed(pool.calls):
        if kind == "execute" and "suppressed" in sql:
            return args[0]
    return None


def _consumed_handler(pool: ScriptedPool) -> str | None:
    for kind, sql, args in reversed(pool.calls):
        if kind == "execute" and "consumed" in sql and "status = 'consumed'" in sql:
            return args[0]
    return None


def _no_gate(monkeypatch):
    """Make every agent runnable so the mode gate never short-circuits a test that
    is exercising a *different* branch."""
    monkeypatch.setattr(dispatch_mod, "get_agent_mode", AsyncMock(return_value="active"))


@pytest.fixture(autouse=True)
def _clear_mode_cache():
    """agent_modes caches per-agent modes for 5s; clear it so tests don't bleed."""
    dispatch_mod.get_agent_mode  # noqa: B018 — touch to ensure import
    from harness import agent_modes

    agent_modes._cache.clear()
    yield
    agent_modes._cache.clear()


# ===========================================================================
# agent_of  (driven through the dispatcher's import binding)
# ===========================================================================


def test_agent_of_extracts_agent_from_agents_module():
    h = _make_handler("h", module="agents.critic.handler")
    assert dispatch_mod.agent_of(h) == "critic"


def test_agent_of_none_for_system_handler():
    h = _make_handler("h", module="library.graph.sink")
    assert dispatch_mod.agent_of(h) is None


def test_agent_of_none_for_empty_or_short_module():
    h = _make_handler("h", module="agents")  # only one part → None
    assert dispatch_mod.agent_of(h) is None
    h2 = _make_handler("h2", module="")
    assert dispatch_mod.agent_of(h2) is None


# ===========================================================================
# _lane_for  (concurrency lane split: first-party ingest vs bulk population)
# ===========================================================================
def test_lane_for_bulk_pull_and_sweep_go_to_bulk_lane():
    assert dispatch_mod._lane_for({"event_type": "acquire.requested"}, "mimir") == "bulk"
    assert dispatch_mod._lane_for({"event_type": "library.sweep_requested"}, "mimir") == "bulk"


def test_lane_for_first_party_source_keeps_mimir_lane():
    for sk in ("lab_finding", "lab_experiment", "lab_dataset"):
        ev = {"event_type": "source.discovered", "payload": {"source": {"source_kind": sk}}}
        assert dispatch_mod._lane_for(ev, "mimir") == "mimir"


def test_lane_for_scout_source_goes_to_bulk_lane():
    for sk in ("github", "arxiv", "web", "openml"):
        ev = {"event_type": "source.discovered", "payload": {"source": {"source_kind": sk}}}
        assert dispatch_mod._lane_for(ev, "mimir") == "bulk"
    # missing / malformed source → treated as bulk (the safe default for population)
    assert dispatch_mod._lane_for({"event_type": "source.discovered", "payload": {}}, "mimir") == "bulk"
    assert dispatch_mod._lane_for({"event_type": "source.discovered"}, "mimir") == "bulk"


def test_lane_for_other_events_use_the_agent():
    assert dispatch_mod._lane_for({"event_type": "task.created"}, "researcher") == "researcher"
    assert dispatch_mod._lane_for({"event_type": "experiment.requested"}, "experiments") == "experiments"
    assert dispatch_mod._lane_for({"event_type": "claim.created"}, None) is None  # system handler


# ===========================================================================
# register  (duplicate warns + overwrites)
# ===========================================================================


def test_register_stores_handler():
    disp = Dispatcher(pool=ScriptedPool())
    h = _make_handler("h", module="agents.planner.handler")
    disp.register("task.created", h)
    assert disp._handlers["task.created"] is h


def test_register_duplicate_warns_and_overwrites(caplog):
    disp = Dispatcher(pool=ScriptedPool())
    first = _make_handler("first", module="agents.planner.handler")
    second = _make_handler("second", module="agents.planner.handler")
    disp.register("task.created", first)
    with caplog.at_level("WARNING"):
        disp.register("task.created", second)
    assert disp._handlers["task.created"] is second
    assert any("overwriting handler" in r.message for r in caplog.records)


# ===========================================================================
# _process_event — handler resolution & terminal transitions
# ===========================================================================


@pytest.mark.asyncio
async def test_process_event_missing_row_is_noop():
    # No rule for the events SELECT → fetchrow falls through to its None default.
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    disp.register("task.created", _make_handler("h", module="agents.planner.handler"))
    await disp._process_event(1)
    # no suppressed / consumed write happened
    assert not any("UPDATE events" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_process_event_no_handler_suppressed():
    pool = _pool_for_event(_event_row(event_type="mystery.event"))
    disp = Dispatcher(pool=pool)  # no handler registered
    await disp._process_event(1)
    assert _suppression_reason(pool) == "no_handler"


@pytest.mark.asyncio
async def test_process_event_payload_string_is_json_decoded(monkeypatch):
    _no_gate(monkeypatch)
    seen = {}

    async def handler(event, d):  # noqa: ARG001
        seen["payload"] = event["payload"]
        return {"done": True}

    handler.__module__ = "agents.planner.handler"
    handler.__name__ = "handler"
    ev = _event_row(payload=json.dumps({"k": "v"}))
    pool = _pool_for_event(ev)
    disp = Dispatcher(pool=pool)
    disp.register("task.created", handler)
    await disp._process_event(1)
    assert seen["payload"] == {"k": "v"}  # string → dict


@pytest.mark.asyncio
async def test_process_event_empty_string_payload_becomes_empty_dict(monkeypatch):
    _no_gate(monkeypatch)
    seen = {}

    async def handler(event, d):  # noqa: ARG001
        seen["payload"] = event["payload"]
        return None

    handler.__module__ = "agents.planner.handler"
    handler.__name__ = "handler"
    pool = _pool_for_event(_event_row(payload=""))
    disp = Dispatcher(pool=pool)
    disp.register("task.created", handler)
    await disp._process_event(1)
    assert seen["payload"] == {}


@pytest.mark.asyncio
async def test_process_event_consumed_on_success(monkeypatch):
    _no_gate(monkeypatch)
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool)
    disp.register("task.created", _make_handler("good", module="agents.planner.handler"))
    await disp._process_event(1)
    assert _consumed_handler(pool) == "good"
    assert _suppression_reason(pool) is None


@pytest.mark.asyncio
async def test_process_event_handler_raise_marks_failed(monkeypatch):
    _no_gate(monkeypatch)

    async def boom(event, d):  # noqa: ARG001
        raise RuntimeError("kaboom")

    boom.__module__ = "agents.planner.handler"
    boom.__name__ = "boom"
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool)
    disp.register("task.created", boom)
    await disp._process_event(1)
    # the failed-marker UPDATE carries the error text (truncated to 500)
    failed = [a for k, sql, a in pool.calls if k == "execute" and "status = 'failed'" in sql]
    assert failed and "kaboom" in failed[-1][0]


@pytest.mark.asyncio
async def test_session_start_failure_does_not_block_handler(monkeypatch):
    """If Session.start blows up, the handler still runs (degrades to no-trace)."""
    _no_gate(monkeypatch)

    async def _bad_start(self, pool):  # noqa: ARG001
        raise RuntimeError("session table missing")

    monkeypatch.setattr(Session, "start", _bad_start)
    ran = {}

    async def handler(event, d):  # noqa: ARG001
        ran["yes"] = True
        return {"ok": True}

    handler.__module__ = "agents.planner.handler"
    handler.__name__ = "handler"
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool)
    disp.register("task.created", handler)
    await disp._process_event(1)
    assert ran.get("yes") is True
    assert _consumed_handler(pool) == "handler"


# ===========================================================================
# Mode gate / should_run
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_mode_gate_suppresses_below_advisory(monkeypatch, mode):
    monkeypatch.setattr(dispatch_mod, "get_agent_mode", AsyncMock(return_value=mode))
    ran = {}

    async def handler(event, d):  # noqa: ARG001
        ran["yes"] = True
        return {}

    handler.__module__ = "agents.critic.handler"
    handler.__name__ = "handler"
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool)
    disp.register("task.created", handler)
    await disp._process_event(1)
    assert "yes" not in ran  # handler never ran
    assert _suppression_reason(pool) == f"agent_{mode}"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["advisory", "active"])
async def test_mode_gate_runs_when_advisory_or_active(monkeypatch, mode):
    monkeypatch.setattr(dispatch_mod, "get_agent_mode", AsyncMock(return_value=mode))
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool)
    disp.register("task.created", _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _consumed_handler(pool) == "h"


@pytest.mark.asyncio
async def test_urgent_event_still_mode_gated(monkeypatch):
    """A deliberate pause (off) holds even for URGENT events."""
    urgent = next(iter(URGENT_EVENTS))
    monkeypatch.setattr(dispatch_mod, "get_agent_mode", AsyncMock(return_value="off"))
    pool = _pool_for_event(_event_row(event_type=urgent))
    disp = Dispatcher(pool=pool)
    disp.register(urgent, _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _suppression_reason(pool) == "agent_off"


@pytest.mark.asyncio
async def test_system_handler_never_mode_gated(monkeypatch):
    """agent_of(handler) is None → get_agent_mode is never consulted."""
    spy = AsyncMock(return_value="off")
    monkeypatch.setattr(dispatch_mod, "get_agent_mode", spy)
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool)
    # module not under agents.* → system handler
    disp.register("task.created", _make_handler("sys", module="library.graph.sink"))
    await disp._process_event(1)
    spy.assert_not_awaited()
    assert _consumed_handler(pool) == "sys"


# ===========================================================================
# Friction gates — cooldown / cost cap / slop  (pass + suppress)
# ===========================================================================


@pytest.mark.asyncio
async def test_cooldown_gate_suppresses(monkeypatch):
    _no_gate(monkeypatch)
    # Pick a real cooldown-configured event shape.
    (etype, ttype), _cfg = next(iter(COOLDOWNS.items()))
    ev = _event_row(event_type=etype, target_type=ttype, target_id=7)
    # The cooldowns SELECT returns 1 → active cooldown.
    pool = _pool_for_event(ev, **{"FROM cooldowns": [{"?column?": 1}]})
    disp = Dispatcher(pool=pool)
    disp.register(etype, _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _suppression_reason(pool) == "cooldown"


@pytest.mark.asyncio
async def test_cooldown_gate_passes_when_no_active_cooldown(monkeypatch):
    _no_gate(monkeypatch)
    (etype, ttype), _cfg = next(iter(COOLDOWNS.items()))
    ev = _event_row(event_type=etype, target_type=ttype, target_id=7)
    # cooldowns SELECT → empty (no row) → gate passes
    pool = _pool_for_event(ev, **{"FROM cooldowns": []})
    disp = Dispatcher(pool=pool)
    disp.register(etype, _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _consumed_handler(pool) == "h"


@pytest.mark.asyncio
async def test_cooldown_skipped_when_target_id_none(monkeypatch):
    """A cooldown-configured event with no target_id can't be gated → passes."""
    _no_gate(monkeypatch)
    (etype, ttype), _cfg = next(iter(COOLDOWNS.items()))
    ev = _event_row(event_type=etype, target_type=ttype, target_id=None)
    pool = _pool_for_event(ev)
    disp = Dispatcher(pool=pool)
    disp.register(etype, _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _consumed_handler(pool) == "h"
    # never even queried the cooldowns table
    assert not any("FROM cooldowns" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_cost_cap_gate_suppresses(monkeypatch):
    _no_gate(monkeypatch)
    ev = _event_row()  # not in COOLDOWNS → cooldown gate short-circuits
    pool = _pool_for_event(ev, **{"FROM cost_tracking": [{"cap_reached": True}]})
    disp = Dispatcher(pool=pool)
    disp.register("task.created", _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _suppression_reason(pool) == "cost_cap"


@pytest.mark.asyncio
async def test_cost_cap_gate_passes_when_not_capped(monkeypatch):
    _no_gate(monkeypatch)
    pool = _pool_for_event(_event_row(), **{"FROM cost_tracking": [{"cap_reached": False}]})
    disp = Dispatcher(pool=pool)
    disp.register("task.created", _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _consumed_handler(pool) == "h"


@pytest.mark.asyncio
async def test_slop_gate_suppresses_for_claim(monkeypatch):
    _no_gate(monkeypatch)
    ev = _event_row(target_type="claim", target_id=42)  # claim + not in COOLDOWNS table here
    # claim.* IS in COOLDOWNS? no — task.created is the etype; use a non-cooldown etype.
    ev = _event_row(event_type="noncfg.event", target_type="claim", target_id=42)
    pool = _pool_for_event(ev, **{"FROM slop_rate_by_claim": [{"slop_rate": 0.75}]})
    disp = Dispatcher(pool=pool)
    disp.register("noncfg.event", _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _suppression_reason(pool) == "slop_pause"


@pytest.mark.asyncio
async def test_slop_gate_passes_below_threshold(monkeypatch):
    _no_gate(monkeypatch)
    ev = _event_row(event_type="noncfg.event", target_type="claim", target_id=42)
    pool = _pool_for_event(ev, **{"FROM slop_rate_by_claim": [{"slop_rate": 0.10}]})
    disp = Dispatcher(pool=pool)
    disp.register("noncfg.event", _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _consumed_handler(pool) == "h"


@pytest.mark.asyncio
async def test_slop_gate_skipped_for_non_claim_target(monkeypatch):
    _no_gate(monkeypatch)
    ev = _event_row(event_type="noncfg.event", target_type="task", target_id=42)
    pool = _pool_for_event(ev)
    disp = Dispatcher(pool=pool)
    disp.register("noncfg.event", _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _consumed_handler(pool) == "h"
    assert not any("FROM slop_rate_by_claim" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_urgent_event_bypasses_friction_gates(monkeypatch):
    """URGENT events skip the cooldown/cost/slop block entirely."""
    _no_gate(monkeypatch)
    urgent = next(iter(URGENT_EVENTS))
    ev = _event_row(event_type=urgent, target_type="claim", target_id=1)
    # even with cap reached + high slop, an urgent event runs
    pool = _pool_for_event(
        ev,
        **{
            "FROM cost_tracking": [{"cap_reached": True}],
            "FROM slop_rate_by_claim": [{"slop_rate": 0.99}],
        },
    )
    disp = Dispatcher(pool=pool)
    disp.register(urgent, _make_handler("h", module="agents.critic.handler"))
    await disp._process_event(1)
    assert _consumed_handler(pool) == "h"
    # confirm the gates were never even queried
    assert not any("FROM cost_tracking" in sql for _, sql, _ in pool.calls)


# ===========================================================================
# Direct gate-predicate coverage (both branches per helper)
# ===========================================================================


@pytest.mark.asyncio
async def test_is_cooled_down_unconfigured_event_returns_false():
    disp = Dispatcher(pool=ScriptedPool())
    conn = ScriptedPool().conn
    ev = _event_row(event_type="unknown", target_type="task")
    assert await disp._is_cooled_down(conn, ev) is False


@pytest.mark.asyncio
async def test_is_cost_capped_no_row_is_false():
    disp = Dispatcher(pool=ScriptedPool())
    conn = ScriptedPool().conn  # no rule → fetchrow returns None
    assert await disp._is_cost_capped(conn) is False


@pytest.mark.asyncio
async def test_is_slop_paused_none_rate_is_false():
    disp = Dispatcher(pool=ScriptedPool())
    conn = ScriptedPool([("FROM slop_rate_by_claim", [])]).conn
    ev = _event_row(target_type="claim", target_id=1)
    assert await disp._is_slop_paused(conn, ev) is False


# ===========================================================================
# _on_notify — json parse + bad-payload swallow
# ===========================================================================


def test_on_notify_parses_payload_and_spawns_task(monkeypatch):
    disp = Dispatcher(pool=ScriptedPool())
    captured = {}

    def fake_create_task(coro):
        captured["coro"] = coro
        coro.close()  # don't actually schedule; just prove it was created
        return types.SimpleNamespace()

    monkeypatch.setattr(dispatch_mod.asyncio, "create_task", fake_create_task)
    seen = {}
    monkeypatch.setattr(disp, "_process_event", lambda eid: seen.setdefault("id", eid) or _noop_coro())
    disp._on_notify(None, 1, "events", json.dumps({"id": 99}))
    assert seen["id"] == 99


def test_on_notify_swallows_bad_payload(monkeypatch, caplog):
    disp = Dispatcher(pool=ScriptedPool())
    called = {"create": False}
    monkeypatch.setattr(dispatch_mod.asyncio, "create_task", lambda c: called.__setitem__("create", True))
    with caplog.at_level("ERROR"):
        disp._on_notify(None, 1, "events", "not-json{{{")
    assert called["create"] is False
    assert any("failed to parse notify" in r.message for r in caplog.records)


def _noop_coro():
    async def _c():
        return None

    return _c()


# ===========================================================================
# _revive_stranded_tasks — deficit math via ScriptedPool conn
# ===========================================================================


def _revive_pool(pending_tasks: int, pending_triggers: int) -> ScriptedPool:
    return ScriptedPool(
        [
            ("FROM tasks WHERE status = 'pending'", [{"count": pending_tasks}]),
            ("event_type = 'task.created' AND status = 'pending'", [{"count": pending_triggers}]),
        ]
    )


@pytest.mark.asyncio
async def test_revive_emits_deficit_triggers():
    pool = _revive_pool(pending_tasks=10, pending_triggers=3)
    disp = Dispatcher(pool=pool)
    await disp._revive_stranded_tasks(pool.conn)
    inserts = [a for k, sql, a in pool.calls if k == "execute" and "INSERT INTO events" in sql]
    assert len(inserts) == 7  # 10 - 3
    keys = [a[0] for a in inserts]
    assert len(set(keys)) == 7 and all(k.startswith("revive-") for k in keys)


@pytest.mark.asyncio
async def test_revive_noop_when_no_pending_tasks():
    pool = _revive_pool(pending_tasks=0, pending_triggers=0)
    disp = Dispatcher(pool=pool)
    await disp._revive_stranded_tasks(pool.conn)
    assert not any("INSERT INTO events" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_revive_noop_when_triggers_cover_tasks():
    pool = _revive_pool(pending_tasks=4, pending_triggers=4)
    disp = Dispatcher(pool=pool)
    await disp._revive_stranded_tasks(pool.conn)
    assert not any("INSERT INTO events" in sql for _, sql, _ in pool.calls)


# ===========================================================================
# Watchdog helpers, drain, reap (single-pass exercise)
# ===========================================================================


@pytest.mark.asyncio
async def test_reap_startup_orphans_runs_both_updates():
    pool = ScriptedPool(
        [
            ("UPDATE agent_runs", "UPDATE 3"),
            ("UPDATE tasks", "UPDATE 2"),
        ]
    )
    disp = Dispatcher(pool=pool)
    await disp._reap_startup_orphans()
    sqls = [sql for k, sql, _ in pool.calls if k == "execute"]
    assert any("UPDATE agent_runs" in s for s in sqls)
    assert any("UPDATE tasks" in s for s in sqls)


@pytest.mark.asyncio
async def test_reap_startup_orphans_quiet_when_nothing_running():
    pool = ScriptedPool(default_exec="UPDATE 0")
    disp = Dispatcher(pool=pool)
    await disp._reap_startup_orphans()  # no log path, but executes both updates
    assert sum(1 for k, _, _ in pool.calls if k == "execute") == 2


@pytest.mark.asyncio
async def test_drain_pending_spawns_per_pending_event(monkeypatch):
    # Batched drain: a page of pending ids, then empty (in real asyncpg the `id <> ALL(seen)`
    # filter excludes already-kicked ids, so the next fetch returns []; a stateful rule mimics that).
    pages = iter([[{"id": 5}, {"id": 6}], []])
    pool = ScriptedPool([("FROM events WHERE status = 'pending'", lambda: next(pages, []))])
    disp = Dispatcher(pool=pool)
    spawned: list[int] = []
    monkeypatch.setattr(
        dispatch_mod.asyncio,
        "create_task",
        lambda coro: (coro.close(), spawned.append(1))[-1],
    )
    monkeypatch.setattr(disp, "_process_event", lambda eid: _noop_coro())
    await disp._drain_pending()
    assert len(spawned) == 2


@pytest.mark.asyncio
async def test_drain_pending_batches_past_one_page(monkeypatch):
    # The old single LIMIT-100 stranded events past position 100; the batched drain keeps going.
    pages = iter([[{"id": i} for i in range(1, 201)], [{"id": 201}, {"id": 202}], []])
    pool = ScriptedPool([("FROM events WHERE status = 'pending'", lambda: next(pages, []))])
    disp = Dispatcher(pool=pool)
    spawned: list[int] = []
    monkeypatch.setattr(dispatch_mod.asyncio, "create_task", lambda coro: (coro.close(), spawned.append(1))[-1])
    monkeypatch.setattr(disp, "_process_event", lambda eid: _noop_coro())
    await disp._drain_pending()
    assert len(spawned) == 202  # all three pages drained, not just the first 100/200


@pytest.mark.asyncio
async def test_sweep_stale_tasks_executes_resets():
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    await disp._sweep_stale_tasks(pool.conn)
    sqls = [sql for k, sql, _ in pool.calls if k == "execute"]
    assert any("UPDATE tasks" in s for s in sqls)
    assert any("UPDATE agent_runs" in s for s in sqls)


@pytest.mark.asyncio
async def test_sweep_pending_events_spawns(monkeypatch):
    pool = ScriptedPool([("FROM events", [{"id": 11}])])
    disp = Dispatcher(pool=pool)
    spawned: list[int] = []
    monkeypatch.setattr(dispatch_mod.asyncio, "create_task", lambda coro: (coro.close(), spawned.append(1))[-1])
    monkeypatch.setattr(disp, "_process_event", lambda eid: _noop_coro())
    await disp._sweep_pending_events(pool.conn)
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_check_phase_budget_disabled_noop():
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    assert await disp._check_phase_budget(pool.conn) is None
    assert pool.calls == []  # the gate is fully disabled — touches nothing


@pytest.mark.asyncio
async def test_refresh_slop_view_executes_refresh():
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    await disp._refresh_slop_view(pool.conn)
    assert any("REFRESH MATERIALIZED VIEW" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_set_cooldown_inserts():
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    await disp.set_cooldown("critic.attack", "claim", 3, seconds=600, run_id=9)
    inserts = [a for k, sql, a in pool.calls if k == "execute" and "INSERT INTO cooldowns" in sql]
    assert inserts and inserts[0] == ("critic.attack", "claim", 3, "600", 9)


# ===========================================================================
# stop() cancels the watchdog + pump tasks
# ===========================================================================


@pytest.mark.asyncio
async def test_stop_cancels_background_tasks():
    disp = Dispatcher(pool=ScriptedPool())

    async def _forever():
        await asyncio.sleep(3600)

    disp._running = True
    disp._watchdog_task = asyncio.create_task(_forever())
    disp._pump_task = asyncio.create_task(_forever())
    await disp.stop()
    assert disp._running is False
    assert disp._watchdog_task.cancelled() or disp._watchdog_task.cancelling()
    assert disp._pump_task.cancelled() or disp._pump_task.cancelling()
    # let the cancellations propagate
    for t in (disp._watchdog_task, disp._pump_task):
        with pytest.raises(asyncio.CancelledError):
            await t


# ===========================================================================
# Pump / sweep cadence helpers — env-driven branches
# ===========================================================================


@pytest.mark.asyncio
async def test_intake_backlog_counts_pending(monkeypatch):
    pool = ScriptedPool([("FROM events WHERE status = 'pending'", [{"count": 12}])])
    disp = Dispatcher(pool=pool)
    assert await disp._intake_backlog() == 12


@pytest.mark.asyncio
async def test_intake_backlog_swallows_errors(monkeypatch):
    class _BoomPool:
        def acquire(self):
            raise RuntimeError("db down")

    disp = Dispatcher(pool=_BoomPool())
    assert await disp._intake_backlog() == 0


@pytest.mark.asyncio
async def test_emit_sweep_inserts_sweep_event():
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    await disp._emit_sweep("abc")
    inserts = [a for k, sql, a in pool.calls if k == "execute" and "library.sweep_requested" in sql]
    assert inserts and inserts[0] == ("abc",)


def test_ariadne_active_default_true(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_CORE_ONLY", raising=False)
    assert Dispatcher._ariadne_active() is True


def test_ariadne_active_false_in_knowledge_core_only(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "on")
    assert Dispatcher._ariadne_active() is False


@pytest.mark.asyncio
async def test_sweep_library_skips_when_loop_off(monkeypatch):
    monkeypatch.delenv("MIMIR_LOOP", raising=False)
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    await disp._sweep_library_if_due()
    assert pool.calls == []


@pytest.mark.asyncio
async def test_sweep_library_skips_while_ariadne_dark(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "on")  # ariadne dark → continuous pump owns it
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    await disp._sweep_library_if_due()
    assert pool.calls == []


@pytest.mark.asyncio
async def test_sweep_library_emits_when_active_and_due(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    monkeypatch.delenv("KNOWLEDGE_CORE_ONLY", raising=False)  # ariadne active
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    disp._last_sweep_tick = None
    await disp._sweep_library_if_due()
    assert any("library.sweep_requested" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_sweep_library_bad_hours_env_falls_back(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    monkeypatch.delenv("KNOWLEDGE_CORE_ONLY", raising=False)
    monkeypatch.setenv("LIBRARIAN_SWEEP_HOURS", "not-a-number")
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    await disp._sweep_library_if_due()  # ValueError → default 6h, still emits (tick None)
    assert any("library.sweep_requested" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_reconcile_lessons_noop_without_lessons():
    disp = Dispatcher(pool=ScriptedPool())
    # no .lessons attr → early return, nothing raised
    await disp._reconcile_lessons_if_due()


@pytest.mark.asyncio
async def test_reconcile_lessons_emits_when_changes(monkeypatch):
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    lessons = AsyncMock()
    lessons.reconcile.return_value = [{"id": 1, "verdict": "promote"}]
    lessons.decay.return_value = []
    disp.lessons = lessons
    disp._last_lessons_tick = None
    monkeypatch.delenv("LESSON_JUDGE", raising=False)
    await disp._reconcile_lessons_if_due()
    assert any("lessons.reconciled" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_reconcile_lessons_noop_when_no_changes(monkeypatch):
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    lessons = AsyncMock()
    lessons.reconcile.return_value = []
    lessons.decay.return_value = []
    disp.lessons = lessons
    disp._last_lessons_tick = None
    monkeypatch.delenv("LESSON_JUDGE", raising=False)
    await disp._reconcile_lessons_if_due()
    assert not any("lessons.reconciled" in sql for _, sql, _ in pool.calls)


# ===========================================================================
# Session — next_step_order, last_step_id, the step DAG helpers
# ===========================================================================


def test_next_step_order_increments():
    s = Session(handler_name="h")
    assert s.step_order == 0
    assert s.next_step_order() == 1
    assert s.next_step_order() == 2
    assert s.step_order == 2


def test_session_defaults():
    s = Session(handler_name="h", triggered_by_event_id=42)
    assert s.id == 0
    assert s.last_step_id is None
    assert s.mode == "live"
    assert s.triggered_by_event_id == 42


@pytest.mark.asyncio
async def test_session_start_inserts_and_emits():
    pool = ScriptedPool([("INSERT INTO agent_sessions", 77)])
    s = Session(handler_name="researcher.loop", triggered_by_event_id=5)
    await s.start(pool)
    assert s.id == 77
    # session.started event was emitted
    inserts = [a for k, sql, a in pool.calls if k == "execute" and "INSERT INTO events" in sql]
    assert inserts and inserts[0][0] == "session.started"
    assert inserts[0][5] == 77  # session_id == id


@pytest.mark.asyncio
async def test_session_finish_updates_and_emits():
    pool = ScriptedPool([("INSERT INTO agent_sessions", 77)])
    s = Session(handler_name="h")
    await s.start(pool)
    s.next_step_order()
    s.next_step_order()
    await s.finish("completed")
    updates = [sql for k, sql, _ in pool.calls if k == "execute" and "UPDATE agent_sessions" in sql]
    assert updates
    emits = [a for k, sql, a in pool.calls if k == "execute" and "INSERT INTO events" in sql]
    # last emit is session.completed with step_count == 2
    assert emits[-1][0] == "session.completed"
    payload = json.loads(emits[-1][3])
    assert payload["step_count"] == 2


@pytest.mark.asyncio
async def test_session_finish_with_error_truncates():
    pool = ScriptedPool([("INSERT INTO agent_sessions", 5)])
    s = Session(handler_name="h")
    await s.start(pool)
    await s.finish("failed", error="x" * 1000)
    updates = [a for k, sql, a in pool.calls if k == "execute" and "UPDATE agent_sessions" in sql]
    # status + truncated error (≤500) + id
    assert updates[-1][0] == "failed"
    assert len(updates[-1][1]) == 500
    emits = [a for k, sql, a in pool.calls if k == "execute" and "INSERT INTO events" in sql]
    payload = json.loads(emits[-1][3])
    assert payload["error"] is not None and len(payload["error"]) == 200


@pytest.mark.asyncio
async def test_session_finish_noop_without_id():
    pool = ScriptedPool()
    s = Session(handler_name="h")  # never started → id == 0
    await s.finish("completed")
    assert pool.calls == []  # nothing written


@pytest.mark.asyncio
async def test_session_emit_event_noop_without_pool():
    s = Session(handler_name="h")  # _pool is None
    # should silently no-op (no pool bound)
    await s.emit_event(event_type="x.y", payload={"a": 1})
    # nothing to assert beyond "did not raise"


@pytest.mark.asyncio
async def test_dispatcher_session_property_reflects_contextvar():
    disp = Dispatcher(pool=ScriptedPool())
    assert disp.session is None  # outside any handler
    sess = Session(handler_name="h")
    token = dispatch_mod._current_session.set(sess)
    try:
        assert disp.session is sess
    finally:
        dispatch_mod._current_session.reset(token)


# ===========================================================================
# run() main loop + stop() None-branches
# ===========================================================================


class _RunListenerConn:
    """A conn returned by pool.acquire() AWAITED directly (the listener path)."""

    def __init__(self):
        self.added = []
        self.removed = []

    async def add_listener(self, channel, cb):
        self.added.append(channel)

    async def remove_listener(self, channel, cb):
        self.removed.append(channel)


class _RunPool(ScriptedPool):
    """ScriptedPool whose acquire() ALSO supports `await pool.acquire()` for the
    listener conn, and exposes release()."""

    def __init__(self, listener_conn, rules=None, **kw):
        super().__init__(rules, **kw)
        self._listener_conn = listener_conn

    def acquire(self):
        # The run() listener path does `await self.pool.acquire()`; the gate paths
        # do `async with self.pool.acquire()`. _AwaitableCtx supports both.
        return _AwaitableCtx(self._listener_conn, self.conn)

    async def release(self, conn):
        pass


class _AwaitableCtx:
    """Both awaitable (→ listener conn) and an async context manager (→ scripted conn)."""

    def __init__(self, listener_conn, scripted_conn):
        self._listener = listener_conn
        self._scripted = scripted_conn

    def __await__(self):
        async def _coro():
            return self._listener

        return _coro().__await__()

    async def __aenter__(self):
        return self._scripted

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_run_starts_listener_and_stops(monkeypatch):
    lconn = _RunListenerConn()
    pool = _RunPool(
        lconn,
        rules=[
            ("FROM tasks WHERE status = 'pending'", [{"count": 0}]),
            ("FROM events WHERE status = 'pending'", []),
        ],
        default_exec="UPDATE 0",
    )
    disp = Dispatcher(pool=pool)
    # don't let the background loops actually do anything
    monkeypatch.setattr(disp, "_watchdog_loop", lambda: _noop_coro())
    monkeypatch.setattr(disp, "_discovery_pump_loop", lambda: _noop_coro())
    # shorten the 60s idle wait so the loop yields and we can stop it
    real_sleep = asyncio.sleep

    async def fast_sleep(sec):
        await real_sleep(0)

    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", fast_sleep)

    task = asyncio.create_task(disp.run())
    await real_sleep(0.02)
    await disp.stop()
    await asyncio.wait_for(task, timeout=2)
    assert "events" in lconn.added
    assert "events" in lconn.removed  # finally-block cleanup ran


@pytest.mark.asyncio
async def test_stop_handles_none_tasks():
    disp = Dispatcher(pool=ScriptedPool())
    # _watchdog_task / _pump_task are None before run() — stop must not raise.
    await disp.stop()
    assert disp._running is False


# ===========================================================================
# watchdog / pump loops — one iteration each
# ===========================================================================


@pytest.mark.asyncio
async def test_watchdog_loop_runs_one_iteration(monkeypatch):
    pool = ScriptedPool(default_exec="UPDATE 0")
    disp = Dispatcher(pool=pool)
    disp._running = True
    calls = {"sweep": 0}

    async def _stop_after(self_conn):
        calls["sweep"] += 1
        disp._running = False  # end the loop after the first sweep

    monkeypatch.setattr(disp, "_sweep_stale_tasks", _stop_after)
    monkeypatch.setattr(disp, "_revive_stranded_tasks", AsyncMock())
    monkeypatch.setattr(disp, "_sweep_pending_events", AsyncMock())
    monkeypatch.setattr(disp, "_check_phase_budget", AsyncMock())
    monkeypatch.setattr(disp, "_refresh_slop_view", AsyncMock())
    monkeypatch.setattr(disp, "_reconcile_lessons_if_due", AsyncMock())
    monkeypatch.setattr(disp, "_sweep_library_if_due", AsyncMock())
    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", AsyncMock())
    await disp._watchdog_loop()
    assert calls["sweep"] == 1


@pytest.mark.asyncio
async def test_watchdog_loop_swallows_sweep_errors(monkeypatch):
    disp = Dispatcher(pool=ScriptedPool())
    disp._running = True
    n = {"i": 0}

    async def _boom(conn):
        n["i"] += 1
        disp._running = False
        raise RuntimeError("sweep blew up")

    monkeypatch.setattr(disp, "_sweep_stale_tasks", _boom)
    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", AsyncMock())
    await disp._watchdog_loop()  # must not propagate
    assert n["i"] == 1


@pytest.mark.asyncio
async def test_discovery_pump_emits_when_backlog_low(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "on")  # ariadne dark → pump active
    pool = ScriptedPool([("status = 'pending' AND event_type IN", [{"count": 0}])])
    disp = Dispatcher(pool=pool)
    disp._running = True

    async def fast_sleep(sec):
        disp._running = False  # one iteration only

    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", fast_sleep)
    await disp._discovery_pump_loop()
    assert any("library.sweep_requested" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_discovery_pump_skips_emit_when_backlog_healthy(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "on")  # ariadne dark → pump active
    monkeypatch.setenv("LIBRARY_PUMP_LOW_WATER", "40")
    # backlog above the low-water mark → gap_ok True but condition False → no emit
    pool = ScriptedPool([("status = 'pending' AND event_type IN", [{"count": 99}])])
    disp = Dispatcher(pool=pool)
    disp._running = True

    async def fast_sleep(sec):
        disp._running = False

    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", fast_sleep)
    await disp._discovery_pump_loop()
    # no INSERT (emit) — only the read-side backlog probe ran
    assert not any(k == "execute" and "INSERT INTO events" in sql for k, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_discovery_pump_idle_when_loop_off(monkeypatch):
    monkeypatch.delenv("MIMIR_LOOP", raising=False)
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    disp._running = True

    async def fast_sleep(sec):
        disp._running = False

    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", fast_sleep)
    await disp._discovery_pump_loop()
    assert pool.calls == []  # never emitted a sweep


@pytest.mark.asyncio
async def test_discovery_pump_swallows_iteration_errors(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "on")
    disp = Dispatcher(pool=ScriptedPool())
    disp._running = True
    monkeypatch.setattr(disp, "_intake_backlog", AsyncMock(side_effect=RuntimeError("boom")))

    async def fast_sleep(sec):
        disp._running = False

    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", fast_sleep)
    await disp._discovery_pump_loop()  # must not propagate


# ===========================================================================
# lessons reconcile — cadence not-due, judge path, reconcile error
# ===========================================================================


@pytest.mark.asyncio
async def test_reconcile_lessons_skips_when_not_due(monkeypatch):
    from datetime import UTC, datetime

    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    disp.lessons = AsyncMock()
    disp._last_lessons_tick = datetime.now(UTC)  # just ticked → < 3600s → skip
    await disp._reconcile_lessons_if_due()
    disp.lessons.reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_lessons_runs_judge_when_enabled(monkeypatch):
    monkeypatch.setenv("LESSON_JUDGE", "on")
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    disp.lessons = AsyncMock()
    disp.lessons.reconcile.return_value = []
    disp.lessons.decay.return_value = []
    disp.curator = object()
    disp.router = object()
    disp._last_lessons_tick = None
    judged = {"called": False}

    async def _fake_judge(d):
        judged["called"] = True

    # the function is imported inside the method from agents.reflection.handler
    import agents.reflection.handler as rh

    monkeypatch.setattr(rh, "judge_pending_lesson_applications", _fake_judge, raising=False)
    await disp._reconcile_lessons_if_due()
    assert judged["called"] is True


@pytest.mark.asyncio
async def test_reconcile_lessons_swallows_judge_error(monkeypatch):
    monkeypatch.setenv("LESSON_JUDGE", "on")
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    disp.lessons = AsyncMock()
    disp.lessons.reconcile.return_value = []
    disp.lessons.decay.return_value = []
    disp.curator = object()
    disp.router = object()
    disp._last_lessons_tick = None

    async def _boom_judge(d):
        raise RuntimeError("judge exploded")

    import agents.reflection.handler as rh

    monkeypatch.setattr(rh, "judge_pending_lesson_applications", _boom_judge, raising=False)
    await disp._reconcile_lessons_if_due()  # judge error swallowed, reconcile still runs
    disp.lessons.reconcile.assert_awaited()


@pytest.mark.asyncio
async def test_reconcile_lessons_swallows_reconcile_error(monkeypatch):
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    disp.lessons = AsyncMock()
    disp.lessons.reconcile.side_effect = RuntimeError("reconcile failed")
    disp._last_lessons_tick = None
    monkeypatch.delenv("LESSON_JUDGE", raising=False)
    await disp._reconcile_lessons_if_due()  # must not propagate
    assert not any("lessons.reconciled" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_sweep_library_not_due_within_window(monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv("MIMIR_LOOP", "on")
    monkeypatch.delenv("KNOWLEDGE_CORE_ONLY", raising=False)
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    disp._last_sweep_tick = datetime.now(UTC)  # just swept → not due
    await disp._sweep_library_if_due()
    assert pool.calls == []


@pytest.mark.asyncio
async def test_process_event_dict_payload_left_as_is(monkeypatch):
    """payload already a dict → the json.loads branch is skipped."""
    _no_gate(monkeypatch)
    seen = {}

    async def handler(event, d):  # noqa: ARG001
        seen["payload"] = event["payload"]
        return None

    handler.__module__ = "agents.planner.handler"
    handler.__name__ = "handler"
    pool = _pool_for_event(_event_row(payload={"already": "dict"}))
    disp = Dispatcher(pool=pool)
    disp.register("task.created", handler)
    await disp._process_event(1)
    assert seen["payload"] == {"already": "dict"}


@pytest.mark.asyncio
async def test_handler_raise_and_session_finish_failure(monkeypatch):
    """Both handler AND session.finish('failed') raise → outer except swallows."""
    _no_gate(monkeypatch)

    async def boom(event, d):  # noqa: ARG001
        raise RuntimeError("handler boom")

    boom.__module__ = "agents.planner.handler"
    boom.__name__ = "boom"

    orig_finish = Session.finish

    async def _flaky_finish(self, status, error=None):
        if status == "failed":
            raise RuntimeError("finish failed too")
        return await orig_finish(self, status, error)

    monkeypatch.setattr(Session, "finish", _flaky_finish)
    pool = _pool_for_event(_event_row())
    disp = Dispatcher(pool=pool)
    disp.register("task.created", boom)
    await disp._process_event(1)  # must not propagate either error
    assert any("status = 'failed'" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_session_emit_event_writes_row():
    pool = ScriptedPool([("INSERT INTO agent_sessions", 9)])
    s = Session(handler_name="h")
    await s.start(pool)
    await s.emit_event(
        event_type="step.completed",
        payload={"step": "synthesize"},
        target_type="claim",
        target_id=3,
        emitted_by_run_id=12,
    )
    emits = [a for k, sql, a in pool.calls if k == "execute" and "INSERT INTO events" in sql]
    last = emits[-1]
    assert last[0] == "step.completed"
    assert last[1] == "claim"
    assert last[2] == 3
    assert json.loads(last[3]) == {"step": "synthesize"}
    assert last[4] == 12
    assert last[5] == 9  # session_id


# ── the never-idle pump: research-front probe ─────────────────────────────────
@pytest.mark.asyncio
async def test_research_front_idle_true_when_hands_empty():
    pool = ScriptedPool([("open_tasks", {"open_tasks": 0, "open_exps": 0})])
    disp = Dispatcher(pool=pool)
    assert await disp._research_front_idle() is True


@pytest.mark.asyncio
async def test_research_front_idle_false_with_open_work():
    pool = ScriptedPool([("open_tasks", {"open_tasks": 0, "open_exps": 1})])
    disp = Dispatcher(pool=pool)
    assert await disp._research_front_idle() is False


@pytest.mark.asyncio
async def test_research_front_probe_failure_means_not_idle():
    class _BoomPool:
        def acquire(self):
            raise RuntimeError("db gone")

    disp = Dispatcher(pool=_BoomPool())
    assert await disp._research_front_idle() is False  # fail closed: don't pump blind
