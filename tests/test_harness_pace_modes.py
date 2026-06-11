"""Unit tests for the Ariadne pacemaker (harness.ariadne_pace) and the per-agent
mode dial (harness.agent_modes). NO real Postgres/Neo4j/network — everything is a
ScriptedPool (tests._helpers). Env-derived module constants are read at import
time, so the tests monkeypatch the module globals directly to drive each branch.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from harness import agent_modes, ariadne_pace
from tests._helpers import ScriptedPool

# Only async tests carry the asyncio mark (applied per-test); the sync mode-dial helper tests
# (_default_mode/agent_of/should_run) must stay unmarked.
aio = pytest.mark.asyncio

NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


def _event_row(emitted_at, corpus=None, *, as_str=False):
    """An events row {emitted_at, payload} for last-delib/last-reflect lookups."""
    payload = {"corpus": corpus} if corpus is not None else {}
    return {"emitted_at": emitted_at, "payload": json.dumps(payload) if as_str else payload}


# ── _corpus_of ──────────────────────────────────────────────────────────────────
@aio
async def test_corpus_of_none_row():
    assert ariadne_pace._corpus_of(None) is None


@aio
async def test_corpus_of_dict_payload():
    assert ariadne_pace._corpus_of({"payload": {"corpus": 42}}) == 42


@aio
async def test_corpus_of_str_payload():
    assert ariadne_pace._corpus_of({"payload": json.dumps({"corpus": 7})}) == 7


@aio
async def test_corpus_of_bad_json_returns_none():
    assert ariadne_pace._corpus_of({"payload": "not-json{"}) is None


@aio
async def test_corpus_of_non_dict_payload_returns_none():
    # Valid JSON but not an object → .get path skipped.
    assert ariadne_pace._corpus_of({"payload": "[1, 2, 3]"}) is None


@aio
async def test_corpus_of_missing_key():
    assert ariadne_pace._corpus_of({"payload": {"trigger": "pace"}}) is None


# ── _decide ───────────────────────────────────────────────────────────────────
def _decide_rules(
    *,
    corpus,
    mission,
    approved_active=0,
    queued=0,
    last_delib=None,
    last_reflect=None,
    now=NOW,
):
    """Build the ordered ScriptedPool rules _decide issues (first match wins, so the
    more specific substrings must precede the broad count(*) ones)."""
    return [
        ("SELECT now()", now),
        ("FROM documents WHERE queryable", corpus),
        ("claim_kind='mission'", mission),
        # approved_active count — direction_gate JOIN claims.
        ("FROM direction_gate dg JOIN claims c", approved_active),
        # queued deliberate/reflect events.
        ("event_type IN ('ariadne.deliberate','ariadne.reflect')", queued),
        ("event_type='ariadne.deliberate' ORDER BY", [last_delib] if last_delib else []),
        ("event_type='ariadne.reflect' ORDER BY", [last_reflect] if last_reflect else []),
    ]


@aio
async def test_decide_bootstrap_no_mission_deliberates():
    pool = ScriptedPool(_decide_rules(corpus=10, mission=None))
    et, corpus = await ariadne_pace._decide(pool)
    assert et == "ariadne.deliberate"
    assert corpus == 10


@aio
async def test_decide_big_jump_deliberates(monkeypatch):
    monkeypatch.setattr(ariadne_pace, "DELIB_GROWTH", 3000)
    monkeypatch.setattr(ariadne_pace, "DELIB_COOLDOWN_S", 2 * 3600)
    last = _event_row(_ago(3 * 3600), corpus=1000)
    pool = ScriptedPool(_decide_rules(corpus=5000, mission=1, last_delib=last))
    et, corpus = await ariadne_pace._decide(pool)
    assert et == "ariadne.deliberate"
    assert corpus == 5000


@aio
async def test_decide_no_deliberate_while_approved_in_flight(monkeypatch):
    """Even with a big jump after cooldown, committed (approved) work blocks re-frame."""
    monkeypatch.setattr(ariadne_pace, "DELIB_GROWTH", 3000)
    monkeypatch.setattr(ariadne_pace, "DELIB_COOLDOWN_S", 2 * 3600)
    monkeypatch.setattr(ariadne_pace, "REFLECT_COOLDOWN_S", 45 * 60)
    monkeypatch.setattr(ariadne_pace, "REFLECT_GROWTH", 800)
    last_d = _event_row(_ago(3 * 3600), corpus=1000)
    last_r = _event_row(_ago(60), corpus=4999)  # recent reflect → reflect also blocked
    pool = ScriptedPool(_decide_rules(corpus=5000, mission=1, approved_active=2, last_delib=last_d, last_reflect=last_r))
    et, _ = await ariadne_pace._decide(pool)
    assert et is None


@aio
async def test_decide_growth_below_threshold_then_reflects(monkeypatch):
    """Growth under delib threshold but over reflect threshold after its cooldown → reflect."""
    monkeypatch.setattr(ariadne_pace, "DELIB_GROWTH", 3000)
    monkeypatch.setattr(ariadne_pace, "REFLECT_GROWTH", 800)
    monkeypatch.setattr(ariadne_pace, "REFLECT_COOLDOWN_S", 45 * 60)
    monkeypatch.setattr(ariadne_pace, "REFLECT_MAX_AGE_S", 6 * 3600)
    last_d = _event_row(_ago(3 * 3600), corpus=900)
    last_r = _event_row(_ago(50 * 60), corpus=100)  # grew 900 ≥ 800, cooldown passed
    pool = ScriptedPool(_decide_rules(corpus=1000, mission=1, last_delib=last_d, last_reflect=last_r))
    et, corpus = await ariadne_pace._decide(pool)
    assert et == "ariadne.reflect"
    assert corpus == 1000


@aio
async def test_decide_reflect_max_age_tick(monkeypatch):
    """Little growth, but the reflect is older than max-age → still reflect."""
    monkeypatch.setattr(ariadne_pace, "DELIB_GROWTH", 3000)
    monkeypatch.setattr(ariadne_pace, "REFLECT_GROWTH", 800)
    monkeypatch.setattr(ariadne_pace, "REFLECT_COOLDOWN_S", 45 * 60)
    monkeypatch.setattr(ariadne_pace, "REFLECT_MAX_AGE_S", 6 * 3600)
    last_d = _event_row(_ago(3 * 3600), corpus=999)
    last_r = _event_row(_ago(7 * 3600), corpus=999)  # grew 1, but age ≥ max-age
    pool = ScriptedPool(_decide_rules(corpus=1000, mission=1, last_delib=last_d, last_reflect=last_r))
    et, _ = await ariadne_pace._decide(pool)
    assert et == "ariadne.reflect"


@aio
async def test_decide_reflect_cooldown_blocks(monkeypatch):
    """Enough growth but inside the reflect cooldown → nothing."""
    monkeypatch.setattr(ariadne_pace, "DELIB_GROWTH", 3000)
    monkeypatch.setattr(ariadne_pace, "REFLECT_GROWTH", 800)
    monkeypatch.setattr(ariadne_pace, "REFLECT_COOLDOWN_S", 45 * 60)
    monkeypatch.setattr(ariadne_pace, "REFLECT_MAX_AGE_S", 6 * 3600)
    last_d = _event_row(_ago(3 * 3600), corpus=100)
    last_r = _event_row(_ago(10 * 60), corpus=100)  # only 10 min < 45 min cooldown
    pool = ScriptedPool(_decide_rules(corpus=1000, mission=1, last_delib=last_d, last_reflect=last_r))
    et, _ = await ariadne_pace._decide(pool)
    assert et is None


@aio
async def test_decide_queued_event_short_circuits():
    """A pending deliberate/reflect already queued → return None immediately."""
    pool = ScriptedPool(_decide_rules(corpus=10, mission=None, queued=1))
    et, corpus = await ariadne_pace._decide(pool)
    assert et is None
    assert corpus == 10


@aio
async def test_decide_no_prior_events_first_reflect_blocked_by_delib(monkeypatch):
    """Mission set, no prior delib/reflect rows: grew_since→corpus, age→1e12. Delib needs growth
    ≥ threshold; with corpus < threshold and approved=0 it falls through to reflect (age huge)."""
    monkeypatch.setattr(ariadne_pace, "DELIB_GROWTH", 3000)
    monkeypatch.setattr(ariadne_pace, "REFLECT_GROWTH", 800)
    monkeypatch.setattr(ariadne_pace, "REFLECT_COOLDOWN_S", 45 * 60)
    pool = ScriptedPool(_decide_rules(corpus=1000, mission=1))
    et, _ = await ariadne_pace._decide(pool)
    assert et == "ariadne.reflect"  # grew_since(None)=1000 ≥ 800, age huge


# ── _auto_approve ─────────────────────────────────────────────────────────────
@aio
async def test_auto_approve_off_is_noop(monkeypatch):
    monkeypatch.setattr(ariadne_pace, "AUTO_APPROVE", False)
    pool = ScriptedPool()
    assert await ariadne_pace._auto_approve(pool) == 0
    assert pool.calls == []  # never touched the DB


@aio
async def test_auto_approve_budget_full_returns_zero(monkeypatch):
    monkeypatch.setattr(ariadne_pace, "AUTO_APPROVE", True)
    monkeypatch.setattr(ariadne_pace, "GATE_BUDGET", 3)
    # already 3 approved → slots 0 → short-circuit before fetching candidate rows.
    pool = ScriptedPool([("dg.status = 'approved' AND c.claim_kind = 'direction'", 3)])
    assert await ariadne_pace._auto_approve(pool) == 0
    assert not any(c[0] == "execute" for c in pool.calls)


@aio
async def test_auto_approve_approves_up_to_budget(monkeypatch):
    monkeypatch.setattr(ariadne_pace, "AUTO_APPROVE", True)
    monkeypatch.setattr(ariadne_pace, "GATE_BUDGET", 3)
    monkeypatch.setattr(ariadne_pace, "AUTO_APPROVE_MIN", 3.5)
    rows = [{"id": 11}, {"id": 22}]
    pool = ScriptedPool(
        [
            ("SELECT count(*) FROM direction_gate dg JOIN claims c", 1),  # 1 approved → 2 slots
            ("JOIN direction_scores ds", rows),
        ]
    )
    n = await ariadne_pace._auto_approve(pool)
    assert n == 2
    inserts = [c for c in pool.calls if c[0] == "execute"]
    assert len(inserts) == 2
    assert {c[2][0] for c in inserts} == {11, 22}


@aio
async def test_auto_approve_no_candidates(monkeypatch):
    monkeypatch.setattr(ariadne_pace, "AUTO_APPROVE", True)
    monkeypatch.setattr(ariadne_pace, "GATE_BUDGET", 3)
    pool = ScriptedPool(
        [
            ("SELECT count(*) FROM direction_gate dg JOIN claims c", 0),
            ("JOIN direction_scores ds", []),
        ]
    )
    assert await ariadne_pace._auto_approve(pool) == 0


# ── _maybe_plan ───────────────────────────────────────────────────────────────
@aio
async def test_maybe_plan_none_unplanned(monkeypatch):
    pool = ScriptedPool([("NOT EXISTS (SELECT 1 FROM tasks", 0)])
    assert await ariadne_pace._maybe_plan(pool) is False
    assert not any(c[0] == "execute" for c in pool.calls)


@aio
async def test_maybe_plan_already_queued(monkeypatch):
    pool = ScriptedPool(
        [
            ("NOT EXISTS (SELECT 1 FROM tasks", 2),
            ("event_type = 'planner.plan' AND status = 'pending'", 1),
        ]
    )
    assert await ariadne_pace._maybe_plan(pool) is False
    assert not any(c[0] == "execute" for c in pool.calls)


@aio
async def test_maybe_plan_emits(monkeypatch):
    pool = ScriptedPool(
        [
            ("NOT EXISTS (SELECT 1 FROM tasks", 2),
            ("event_type = 'planner.plan' AND status = 'pending'", 0),
        ]
    )
    assert await ariadne_pace._maybe_plan(pool) is True
    inserts = [c for c in pool.calls if c[0] == "execute"]
    assert len(inserts) == 1
    assert "INSERT INTO events" in inserts[0][1]
    assert "planner.plan" in inserts[0][1]


# ── _maybe_adjudicate ─────────────────────────────────────────────────────────
@aio
async def test_maybe_adjudicate_none_unadjudicated(monkeypatch):
    pool = ScriptedPool([("NOT EXISTS (SELECT 1 FROM direction_adjudications", 0)])
    assert await ariadne_pace._maybe_adjudicate(pool) is False
    assert not any(c[0] == "execute" for c in pool.calls)


@aio
async def test_maybe_adjudicate_already_queued(monkeypatch):
    pool = ScriptedPool(
        [
            ("NOT EXISTS (SELECT 1 FROM direction_adjudications", 3),
            ("event_type = 'direction.adjudicate' AND status = 'pending'", 1),
        ]
    )
    assert await ariadne_pace._maybe_adjudicate(pool) is False
    assert not any(c[0] == "execute" for c in pool.calls)


@aio
async def test_maybe_adjudicate_emits(monkeypatch):
    pool = ScriptedPool(
        [
            ("NOT EXISTS (SELECT 1 FROM direction_adjudications", 3),
            ("event_type = 'direction.adjudicate' AND status = 'pending'", 0),
        ]
    )
    assert await ariadne_pace._maybe_adjudicate(pool) is True
    inserts = [c for c in pool.calls if c[0] == "execute"]
    assert len(inserts) == 1
    assert "INSERT INTO events" in inserts[0][1]
    assert "direction.adjudicate" in inserts[0][1]


# ── _emit ─────────────────────────────────────────────────────────────────────
@aio
async def test_emit_inserts_pending_event():
    pool = ScriptedPool()
    await ariadne_pace._emit(pool, "ariadne.reflect", 1234)
    inserts = [c for c in pool.calls if c[0] == "execute"]
    assert len(inserts) == 1
    op, sql, args = inserts[0]
    assert "INSERT INTO events" in sql
    assert "'pending'" in sql
    assert args[0] == "ariadne.reflect"
    payload = json.loads(args[1])
    assert payload == {"trigger": "pace", "corpus": 1234}
    assert args[2] == "pace-ariadne.reflect-1234"


# ── ariadne_pacemaker (one tick) ──────────────────────────────────────────────
@aio
async def test_pacemaker_stop_immediately_no_work(monkeypatch):
    """stop already set → loop body never runs; no DB calls, clean exit."""
    pool = ScriptedPool()
    stop = asyncio.Event()
    stop.set()
    await ariadne_pace.ariadne_pacemaker(pool, stop)
    assert pool.calls == []


@aio
async def test_pacemaker_tick_paused_by_mode(monkeypatch):
    """One tick where mode is 'off' → continue before _auto_approve/_decide. We drive the single
    tick by making wait_for raise TimeoutError once, then stop."""
    calls = {"auto": 0, "decide": 0}

    async def _mode(pool, agent):
        return "off"

    async def _auto(pool):
        calls["auto"] += 1
        return 0

    async def _decide(pool):
        calls["decide"] += 1
        return None, 0

    monkeypatch.setattr(ariadne_pace, "get_agent_mode", _mode)
    monkeypatch.setattr(ariadne_pace, "_auto_approve", _auto)
    monkeypatch.setattr(ariadne_pace, "_decide", _decide)

    stop = asyncio.Event()
    waits = {"n": 0}

    async def _fake_wait_for(coro, timeout):
        # Cancel the underlying coroutine to avoid 'never awaited' warnings.
        asyncio.ensure_future(coro).cancel()
        waits["n"] += 1
        if waits["n"] == 1:
            raise TimeoutError  # one tick elapses
        stop.set()
        return True  # second call → stop observed, loop breaks

    monkeypatch.setattr(ariadne_pace.asyncio, "wait_for", _fake_wait_for)
    await ariadne_pace.ariadne_pacemaker(ScriptedPool(), stop)
    assert calls["auto"] == 0  # mode 'off' short-circuited the tick
    assert calls["decide"] == 0


@aio
async def test_pacemaker_tick_emits(monkeypatch):
    """Active mode, _decide returns an event → field model refresh + _emit run."""
    seen = {"refresh": 0, "emit": None}

    async def _mode(pool, agent):
        return "active"

    async def _auto(pool):
        return 0

    async def _plan(pool):
        return False

    async def _decide(pool):
        return "ariadne.deliberate", 99

    async def _refresh(pool):
        seen["refresh"] += 1

    stop = asyncio.Event()

    async def _emit(pool, et, corpus):
        seen["emit"] = (et, corpus)
        stop.set()  # set at the END of the tick body → loop-back hits the while check (203→191)

    monkeypatch.setattr(ariadne_pace, "get_agent_mode", _mode)
    monkeypatch.setattr(ariadne_pace, "_auto_approve", _auto)
    monkeypatch.setattr(ariadne_pace, "_maybe_plan", _plan)
    monkeypatch.setattr(ariadne_pace, "_decide", _decide)
    monkeypatch.setattr(ariadne_pace, "_refresh_field_model", _refresh)
    monkeypatch.setattr(ariadne_pace, "_emit", _emit)

    async def _fake_wait_for(coro, timeout):
        asyncio.ensure_future(coro).cancel()
        raise TimeoutError  # always a tick; the tick body sets stop, so while-check exits

    monkeypatch.setattr(ariadne_pace.asyncio, "wait_for", _fake_wait_for)
    await ariadne_pace.ariadne_pacemaker(ScriptedPool(), stop)
    assert seen["refresh"] == 1
    assert seen["emit"] == ("ariadne.deliberate", 99)


@aio
async def test_pacemaker_tick_active_no_emit(monkeypatch):
    """Active mode but _decide returns None → the `if event_type` body is skipped, the tick
    completes and loops back to the while-check (covers the 203→191 false arc)."""
    seen = {"refresh": 0, "emit": 0}

    async def _mode(pool, agent):
        return "advisory"

    async def _noop(pool):
        return 0

    stop = asyncio.Event()

    async def _decide(pool):
        stop.set()  # end the tick so the loop-back hits the while-check
        return None, 0

    async def _refresh(pool):
        seen["refresh"] += 1

    async def _emit(pool, et, corpus):
        seen["emit"] += 1

    monkeypatch.setattr(ariadne_pace, "get_agent_mode", _mode)
    monkeypatch.setattr(ariadne_pace, "_auto_approve", _noop)
    monkeypatch.setattr(ariadne_pace, "_maybe_plan", _noop)
    monkeypatch.setattr(ariadne_pace, "_decide", _decide)
    monkeypatch.setattr(ariadne_pace, "_refresh_field_model", _refresh)
    monkeypatch.setattr(ariadne_pace, "_emit", _emit)

    async def _fake_wait_for(coro, timeout):
        asyncio.ensure_future(coro).cancel()
        raise TimeoutError

    monkeypatch.setattr(ariadne_pace.asyncio, "wait_for", _fake_wait_for)
    await ariadne_pace.ariadne_pacemaker(ScriptedPool(), stop)
    assert seen["refresh"] == 0  # no event → no refresh / emit
    assert seen["emit"] == 0


@aio
async def test_pacemaker_tick_exception_is_swallowed(monkeypatch):
    """A bad tick (auto_approve raises) must not kill the pacemaker — it logs and loops."""

    async def _mode(pool, agent):
        return "active"

    async def _boom(pool):
        raise RuntimeError("db blip")

    monkeypatch.setattr(ariadne_pace, "get_agent_mode", _mode)
    monkeypatch.setattr(ariadne_pace, "_auto_approve", _boom)

    stop = asyncio.Event()
    waits = {"n": 0}

    async def _fake_wait_for(coro, timeout):
        asyncio.ensure_future(coro).cancel()
        waits["n"] += 1
        if waits["n"] == 1:
            raise TimeoutError
        stop.set()
        return True

    monkeypatch.setattr(ariadne_pace.asyncio, "wait_for", _fake_wait_for)
    # Should not raise despite _boom.
    await ariadne_pace.ariadne_pacemaker(ScriptedPool(), stop)


@aio
async def test_refresh_field_model_failure_is_swallowed(monkeypatch):
    """_get_driver raising → warning path, no exception bubbles."""

    async def _boom():
        raise RuntimeError("neo down")

    monkeypatch.setattr(ariadne_pace, "_get_driver", _boom)
    await ariadne_pace._refresh_field_model(ScriptedPool())  # no raise


@aio
async def test_refresh_field_model_success(monkeypatch):
    async def _driver():
        return object()

    async def _build(driver, pool):
        return {"concepts": 5, "prior": "P", "recent": "R"}

    monkeypatch.setattr(ariadne_pace, "_get_driver", _driver)
    monkeypatch.setattr(ariadne_pace, "build_field_model", _build)
    await ariadne_pace._refresh_field_model(ScriptedPool())  # no raise


# ── agent_modes ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clear_mode_cache():
    agent_modes._cache.clear()
    yield
    agent_modes._cache.clear()


def test_default_mode_mimir():
    assert agent_modes._default_mode("mimir") == "active"


def test_default_mode_research_active_when_flag_off(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_CORE_ONLY", raising=False)
    assert agent_modes._default_mode("ariadne") == "active"


def test_default_mode_research_off_under_knowledge_core(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "on")
    assert agent_modes._default_mode("ariadne") == "off"
    assert agent_modes._default_mode("planner") == "off"


def test_default_mode_unknown_agent_active(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "1")
    assert agent_modes._default_mode("sink") == "active"  # not in _RESEARCH


def test_knowledge_core_only_truthy(monkeypatch):
    for v in ("1", "on", "true", "yes", "ON", "True"):
        monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", v)
        assert agent_modes._knowledge_core_only() is True
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "off")
    assert agent_modes._knowledge_core_only() is False
    monkeypatch.delenv("KNOWLEDGE_CORE_ONLY", raising=False)
    assert agent_modes._knowledge_core_only() is False


def test_should_run():
    assert agent_modes.should_run("active") is True
    assert agent_modes.should_run("advisory") is True
    assert agent_modes.should_run("shadow") is False
    assert agent_modes.should_run("off") is False


def test_agent_of_agents_module():
    def handler():  # noqa: D401 — dummy
        pass

    handler.__module__ = "agents.ariadne.loop"
    assert agent_modes.agent_of(handler) == "ariadne"


def test_agent_of_system_handler_none():
    def handler():
        pass

    handler.__module__ = "library.graph.sink"
    assert agent_modes.agent_of(handler) is None


def test_agent_of_no_module():
    obj = object()  # no __module__
    assert agent_modes.agent_of(obj) is None


@aio
async def test_get_agent_mode_explicit_row():
    pool = ScriptedPool([("SELECT mode FROM agent_modes", "advisory")])
    assert await agent_modes.get_agent_mode(pool, "ariadne") == "advisory"


@aio
async def test_get_agent_mode_default_when_no_row(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_CORE_ONLY", raising=False)
    # fetchval with no matching rule → default_val None → falls back to _default_mode.
    pool = ScriptedPool()
    assert await agent_modes.get_agent_mode(pool, "ariadne") == "active"


@aio
async def test_get_agent_mode_db_error_falls_back(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "on")

    class _BoomPool:
        def acquire(self):
            raise RuntimeError("pool gone")

    assert await agent_modes.get_agent_mode(_BoomPool(), "ariadne") == "off"


@aio
async def test_get_agent_mode_cache_hit(monkeypatch):
    """Second call within TTL must NOT hit the pool again (served from cache)."""
    pool = ScriptedPool([("SELECT mode FROM agent_modes", "active")])
    assert await agent_modes.get_agent_mode(pool, "mimir") == "active"
    n_after_first = len(pool.calls)
    assert await agent_modes.get_agent_mode(pool, "mimir") == "active"
    assert len(pool.calls) == n_after_first  # no new DB call


@aio
async def test_get_agent_mode_cache_expires(monkeypatch):
    """Past the TTL the cache is bypassed and the pool is queried again."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(agent_modes.time, "monotonic", lambda: clock["t"])
    pool = ScriptedPool([("SELECT mode FROM agent_modes", "active")])
    assert await agent_modes.get_agent_mode(pool, "mimir") == "active"
    first = len(pool.calls)
    clock["t"] += agent_modes._TTL + 1  # advance past TTL
    assert await agent_modes.get_agent_mode(pool, "mimir") == "active"
    assert len(pool.calls) > first  # re-queried


@aio
async def test_set_agent_mode_invalid_raises():
    with pytest.raises(ValueError, match="invalid mode"):
        await agent_modes.set_agent_mode(ScriptedPool(), "ariadne", "bogus")


@aio
async def test_set_agent_mode_upsert_and_cache_invalidate():
    agent_modes._cache["ariadne"] = ("active", 1e9)  # stale cache entry
    pool = ScriptedPool()
    await agent_modes.set_agent_mode(pool, "ariadne", "shadow", note="paused")
    inserts = [c for c in pool.calls if c[0] == "execute"]
    assert len(inserts) == 1
    op, sql, args = inserts[0]
    assert "INSERT INTO agent_modes" in sql
    assert args[0] == "ariadne"
    assert args[1] == "shadow"
    assert args[2] == "paused"
    assert "ariadne" not in agent_modes._cache  # invalidated
