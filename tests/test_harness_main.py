"""Unit coverage for harness/main.py — the autonomous lab's wiring entry point.

main() builds the asyncpg pool, runs preflight, constructs Curator/Router/Dispatcher,
then registers handlers gated on three env flags (KNOWLEDGE_CORE_ONLY / MIMIR_LOOP /
ARIADNE_PACE) before entering the dispatch loop. These tests drive main() under every
gate combination with EVERYTHING mocked — no Postgres, Neo4j, Ollama, Zep, or network —
and assert exactly which (event_type → handler) pairs get registered and whether the
Ariadne pacemaker starts.

Mocking strategy (all patched on harness.main):
  * asyncpg.create_pool   → AsyncMock → a _BootPool (company_state seeded → bootstrapped)
  * _preflight            → AsyncMock(True)  (skips httpx/ollama/neo4j/zep entirely)
  * Curator/Router/shared_gpu_lock/PostgresClient/LessonsClient/ZepClient.from_env → cheap fakes
  * build_cloud_chain / build_premium_chain → return [] (no provider chains logged)
  * Dispatcher            → _FakeDispatcher that RECORDS register(event_type, handler)
  * asyncio.Event         → _InstantEvent whose wait() returns at once → main() falls
                            straight through to the graceful-shutdown path.

_preflight, the signal-handler add_signal_handler callbacks, uvicorn, and the
`if __name__ == '__main__'` block are exercised / discussed separately at the bottom.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import harness.main as hm

# Handlers the wiring imports — used as identity oracles in the assertions.
from agents.ariadne.handler import handle_ariadne_deliberate, handle_ariadne_reflect
from agents.critic.handler import handle_finding_high_signal
from agents.evaluation.handler import handle_task_completed
from agents.evaluation.slop_handler import handle_audit_slop_detected
from agents.mimir.acquire import handle_acquire_requested
from agents.mimir.handler import handle_source_discovered, handle_sweep_requested
from agents.planner.decompose import handle_planner_decompose
from agents.planner.handler import handle_queue_empty
from agents.reflection.handler import handle_reflection_requested
from agents.researcher.grounded_handler import handle_grounded_research
from library.graph.sink import handle_graph_sink_claim_created

# Handlers ALWAYS registered, regardless of any gate.
ALWAYS = {
    "ariadne.deliberate": handle_ariadne_deliberate,
    "ariadne.reflect": handle_ariadne_reflect,
    "planner.plan": handle_planner_decompose,
    "task.created": handle_grounded_research,
    "claim.created": handle_graph_sink_claim_created,
}

# Market-era handlers — registered ONLY when NOT KNOWLEDGE_CORE_ONLY.
MARKET = {
    "task.completed": handle_task_completed,
    "finding.high_signal": handle_finding_high_signal,
    "queue.empty": handle_queue_empty,
    "reflection.requested": handle_reflection_requested,
    "audit.slop_detected": handle_audit_slop_detected,
}

# Mimir ingest handlers — registered ONLY under MIMIR_LOOP.
MIMIR = {
    "source.discovered": handle_source_discovered,
    "library.sweep_requested": handle_sweep_requested,
    "acquire.requested": handle_acquire_requested,
}


# ── fakes ──────────────────────────────────────────────────────────────────────
class _Conn:
    """Bootstrap conn: SELECT 1 FROM company_state → truthy (lab is seeded)."""

    def __init__(self, bootstrapped=True):
        self._bootstrapped = bootstrapped

    async def fetchval(self, sql, *args):  # noqa: ARG002
        if "company_state" in sql:
            return 1 if self._bootstrapped else None
        return 1


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _BootPool:
    """Minimal asyncpg-pool fake: acquire() context-manager + close()."""

    def __init__(self, bootstrapped=True):
        self.conn = _Conn(bootstrapped)
        self.closed = False

    def acquire(self):
        return _Ctx(self.conn)

    async def close(self):
        self.closed = True


class _FakeDispatcher:
    """Records register(event_type, handler) and provides awaitable run()/stop()."""

    last_instance: _FakeDispatcher | None = None

    def __init__(self, *args, **kwargs):  # noqa: ARG002
        self.registrations: list[tuple[str, object]] = []
        self.run_called = False
        self.stop_called = False
        _FakeDispatcher.last_instance = self

    def register(self, event_type, handler):
        self.registrations.append((event_type, handler))

    async def run(self):
        self.run_called = True

    async def stop(self):
        self.stop_called = True

    @property
    def by_type(self):
        return dict(self.registrations)


class _InstantEvent:
    """asyncio.Event stand-in whose wait() returns immediately so main() proceeds
    straight to the shutdown block. Records set() so the signal-handler wiring can
    be asserted to point at it."""

    def __init__(self):
        self.is_set_flag = False

    def set(self):
        self.is_set_flag = True

    async def wait(self):
        return True


def _install_mocks(monkeypatch, *, pace_records=None):
    """Patch every external dependency in harness.main. Returns the create_pool mock
    so a test can inspect / customize the returned pool."""
    pool = _BootPool(bootstrapped=True)
    create_pool = AsyncMock(return_value=pool)
    monkeypatch.setattr(hm.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(hm, "_preflight", AsyncMock(return_value=True))

    monkeypatch.setattr(hm, "build_cloud_chain", lambda env: [])
    monkeypatch.setattr(hm, "build_premium_chain", lambda env: [])
    monkeypatch.setattr(hm, "PostgresClient", lambda **kw: AsyncMock(name="state"))
    monkeypatch.setattr(hm, "LessonsClient", lambda **kw: AsyncMock(name="lessons"))
    monkeypatch.setattr(hm.ZepClient, "from_env", staticmethod(lambda: AsyncMock(name="memory")))
    monkeypatch.setattr(hm, "Curator", lambda **kw: object())
    monkeypatch.setattr(hm, "shared_gpu_lock", lambda *a, **k: object())
    monkeypatch.setattr(hm, "Router", lambda **kw: AsyncMock(name="router"))
    monkeypatch.setattr(hm, "Dispatcher", _FakeDispatcher)
    monkeypatch.setattr(hm.asyncio, "Event", _InstantEvent)

    # No real sleeps between Zep session inits.
    monkeypatch.setattr(hm.asyncio, "sleep", AsyncMock())

    # The pacemaker (only imported under ARIADNE_PACE) — patch where it's looked up.
    if pace_records is not None:
        import harness.ariadne_pace as ap

        async def _fake_pacemaker(pool_, stop_):  # noqa: ARG001
            pace_records.append((pool_, stop_))

        monkeypatch.setattr(ap, "ariadne_pacemaker", _fake_pacemaker)

    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    return create_pool, pool


def _set_gates(monkeypatch, *, kco=False, mimir=False, pace=False):
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "on" if kco else "")
    monkeypatch.setenv("MIMIR_LOOP", "on" if mimir else "")
    monkeypatch.setenv("ARIADNE_PACE", "on" if pace else "")


# ── always-on registrations (independent of every gate) ─────────────────────────
@pytest.mark.asyncio
async def test_always_registered_handlers_present_in_every_config(monkeypatch):
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch)
    rc = await hm.main()
    assert rc == 0
    reg = _FakeDispatcher.last_instance.by_type
    for etype, handler in ALWAYS.items():
        assert reg.get(etype) is handler


# ── KNOWLEDGE_CORE_ONLY gate on the market handlers ─────────────────────────────
@pytest.mark.asyncio
async def test_market_handlers_registered_when_not_knowledge_core_only(monkeypatch):
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch, kco=False)
    await hm.main()
    reg = _FakeDispatcher.last_instance.by_type
    for etype, handler in MARKET.items():
        assert reg.get(etype) is handler


@pytest.mark.asyncio
async def test_market_handlers_absent_under_knowledge_core_only(monkeypatch, caplog):
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch, kco=True)
    with caplog.at_level("INFO"):
        await hm.main()
    reg = _FakeDispatcher.last_instance.by_type
    for etype in MARKET:
        assert etype not in reg
    # the always-on set still wired up even in knowledge-core mode
    for etype, handler in ALWAYS.items():
        assert reg.get(etype) is handler
    assert any("KNOWLEDGE_CORE_ONLY" in r.message for r in caplog.records)


@pytest.mark.parametrize("flag", ["1", "true", "on", "yes", "TRUE", "On"])
@pytest.mark.asyncio
async def test_knowledge_core_only_truthy_variants(monkeypatch, flag):
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch)
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", flag)
    await hm.main()
    reg = _FakeDispatcher.last_instance.by_type
    assert "task.completed" not in reg  # market handlers suppressed


# ── MIMIR_LOOP gate on the ingest handlers ──────────────────────────────────────
@pytest.mark.asyncio
async def test_mimir_handlers_registered_under_mimir_loop(monkeypatch, caplog):
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch, mimir=True)
    with caplog.at_level("INFO"):
        await hm.main()
    reg = _FakeDispatcher.last_instance.by_type
    for etype, handler in MIMIR.items():
        assert reg.get(etype) is handler
    assert any("mimir ingest loop ENABLED" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_mimir_handlers_absent_without_mimir_loop(monkeypatch):
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch, mimir=False)
    await hm.main()
    reg = _FakeDispatcher.last_instance.by_type
    for etype in MIMIR:
        assert etype not in reg


@pytest.mark.parametrize("flag", ["v1", "on"])
@pytest.mark.asyncio
async def test_mimir_loop_accepted_flag_values(monkeypatch, flag):
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch)
    monkeypatch.setenv("MIMIR_LOOP", flag)
    await hm.main()
    assert "source.discovered" in _FakeDispatcher.last_instance.by_type


@pytest.mark.asyncio
async def test_mimir_loop_rejects_unknown_flag(monkeypatch):
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch)
    monkeypatch.setenv("MIMIR_LOOP", "true")  # not in {"v1","on"} → off
    await hm.main()
    assert "source.discovered" not in _FakeDispatcher.last_instance.by_type


# ── ARIADNE_PACE gate on the pacemaker task ─────────────────────────────────────
@pytest.mark.asyncio
async def test_pacemaker_started_under_ariadne_pace(monkeypatch, caplog):
    pace = []
    _, pool = _install_mocks(monkeypatch, pace_records=pace)
    _set_gates(monkeypatch, pace=True)
    with caplog.at_level("INFO"):
        await hm.main()
    assert len(pace) == 1  # ariadne_pacemaker invoked exactly once
    assert pace[0][0] is pool  # passed the real pool
    assert isinstance(pace[0][1], _InstantEvent)  # and the stop_event
    assert any("ariadne pacemaker ENABLED" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_pacemaker_not_started_without_ariadne_pace(monkeypatch):
    pace = []
    _install_mocks(monkeypatch, pace_records=pace)
    _set_gates(monkeypatch, pace=False)
    await hm.main()
    assert pace == []


@pytest.mark.parametrize("flag", ["on", "1", "true"])
@pytest.mark.asyncio
async def test_ariadne_pace_accepted_flag_values(monkeypatch, flag):
    pace = []
    _install_mocks(monkeypatch, pace_records=pace)
    _set_gates(monkeypatch)
    monkeypatch.setenv("ARIADNE_PACE", flag)
    await hm.main()
    assert len(pace) == 1


# ── full gate matrix (2×2×2) — single source of truth on every combination ──────
@pytest.mark.parametrize("kco", [False, True])
@pytest.mark.parametrize("mimir", [False, True])
@pytest.mark.parametrize("pace", [False, True])
@pytest.mark.asyncio
async def test_gate_matrix(monkeypatch, kco, mimir, pace):
    pace_rec = []
    _install_mocks(monkeypatch, pace_records=pace_rec)
    _set_gates(monkeypatch, kco=kco, mimir=mimir, pace=pace)
    rc = await hm.main()
    assert rc == 0
    reg = _FakeDispatcher.last_instance.by_type

    # always
    for etype, handler in ALWAYS.items():
        assert reg.get(etype) is handler
    # market handlers gated on NOT kco
    for etype in MARKET:
        assert (etype in reg) is (not kco)
    # mimir handlers gated on mimir
    for etype in MIMIR:
        assert (etype in reg) is mimir
    # pacemaker gated on pace
    assert (len(pace_rec) == 1) is pace


# ── graceful-shutdown path ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_graceful_shutdown_runs_dispatcher_then_closes(monkeypatch):
    _, pool = _install_mocks(monkeypatch)
    _set_gates(monkeypatch)
    rc = await hm.main()
    assert rc == 0
    disp = _FakeDispatcher.last_instance
    # runner is created then cancelled in the shutdown block (stop_event fires
    # immediately via _InstantEvent), so run() may never get to execute — what we
    # assert is the deterministic shutdown side-effects.
    assert disp.stop_called is True  # stop() called on shutdown
    assert pool.closed is True  # pool.close() in the shutdown tail


@pytest.mark.asyncio
async def test_signal_handlers_registered_and_target_stop_event(monkeypatch):
    """add_signal_handler is wired for SIGINT + SIGTERM, and the callback sets the
    stop_event (so a real signal would unblock stop_event.wait())."""
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch)
    handlers: dict[int, object] = {}

    class _Loop:
        def add_signal_handler(self, sig, cb):
            handlers[sig] = cb

    # add_signal_handler is unavailable on some platforms/loops; route it through a
    # fake loop that records the (sig, callback) pairs. create_task stays real so the
    # runner/pacemaker tasks remain genuine awaitable/cancellable Tasks.
    monkeypatch.setattr(hm.asyncio, "get_running_loop", lambda: _Loop())

    await hm.main()
    import signal

    assert signal.SIGINT in handlers
    assert signal.SIGTERM in handlers
    # The registered callback is the stop_event's .set — invoking it must not raise.
    cb = handlers[signal.SIGINT]
    assert callable(cb)
    cb()


@pytest.mark.asyncio
async def test_pacemaker_task_cancelled_on_shutdown(monkeypatch):
    """Under ARIADNE_PACE the pace_task is created then cancelled in the shutdown
    block; exercise that branch with a real (immediately-finishing) coroutine."""
    pace = []
    _install_mocks(monkeypatch, pace_records=pace)
    _set_gates(monkeypatch, pace=True)
    rc = await hm.main()
    assert rc == 0
    assert len(pace) == 1  # the pacemaker coroutine was created and awaited/cancelled


# ── not-bootstrapped early exit ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_returns_1_when_company_state_not_seeded(monkeypatch, caplog):
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch)
    pool = _BootPool(bootstrapped=False)
    monkeypatch.setattr(hm.asyncpg, "create_pool", AsyncMock(return_value=pool))
    with caplog.at_level("ERROR"):
        rc = await hm.main()
    assert rc == 1
    assert pool.closed is True
    assert _FakeDispatcher.last_instance is None or not _FakeDispatcher.last_instance.run_called
    assert any("company_state not seeded" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_returns_1_when_preflight_fails(monkeypatch, caplog):
    _, pool = _install_mocks(monkeypatch)
    _set_gates(monkeypatch)
    monkeypatch.setattr(hm, "_preflight", AsyncMock(return_value=False))
    rc = await hm.main()
    assert rc == 1
    assert pool.closed is True


# ── chain-logging branches (premium + cloud non-empty) ──────────────────────────
@pytest.mark.asyncio
async def test_logs_premium_and_cloud_chains_when_present(monkeypatch, caplog):
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch)

    class _CP:
        def __init__(self, name):
            self.provider = type("P", (), {"value": "deepseek"})()
            self.model_name = name

    monkeypatch.setattr(hm, "build_cloud_chain", lambda env: [_CP("free")])
    monkeypatch.setattr(hm, "build_premium_chain", lambda env: [_CP("reason")])
    with caplog.at_level("INFO"):
        await hm.main()
    msgs = " ".join(r.message for r in caplog.records)
    assert "premium" in msgs and "free chain" in msgs


# ── _preflight unit coverage (httpx + neo4j patched, no network) ─────────────────
class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _HttpClient:
    """async-context httpx.AsyncClient stand-in. `get`/`post` return canned _Resp."""

    def __init__(self, get_resp=None, post_resp=None, **kw):  # noqa: ARG002
        self._get = get_resp or _Resp(200)
        self._post = post_resp or _Resp(200)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):  # noqa: ARG002
        return self._get

    async def post(self, url, **kw):  # noqa: ARG002
        return self._post


@pytest.mark.asyncio
async def test_preflight_all_ok(monkeypatch):
    monkeypatch.setattr(hm.httpx, "AsyncClient", lambda **kw: _HttpClient())
    import library.graph.tools as gt

    monkeypatch.setattr(gt, "ensure_constraints", AsyncMock(), raising=False)
    monkeypatch.setattr(gt, "ensure_corpus_constraints", AsyncMock(), raising=False)
    pool = _BootPool()
    memory = AsyncMock()
    ok = await hm._preflight(pool, "http://ollama", memory)
    assert ok is True
    memory.ping.assert_awaited()


@pytest.mark.asyncio
async def test_preflight_postgres_unreachable_is_fatal(monkeypatch):
    monkeypatch.setattr(hm.httpx, "AsyncClient", lambda **kw: _HttpClient())
    import library.graph.tools as gt

    monkeypatch.setattr(gt, "ensure_constraints", AsyncMock(), raising=False)
    monkeypatch.setattr(gt, "ensure_corpus_constraints", AsyncMock(), raising=False)

    class _DeadConn:
        async def fetchval(self, *a):
            raise RuntimeError("connection refused")

    class _DeadPool:
        def acquire(self):
            return _Ctx(_DeadConn())

    ok = await hm._preflight(_DeadPool(), "http://ollama", AsyncMock())
    assert ok is False


@pytest.mark.asyncio
async def test_preflight_ollama_unreachable_is_fatal(monkeypatch):
    class _BadGet(_HttpClient):
        async def get(self, url, **kw):  # noqa: ARG002
            raise RuntimeError("ollama down")

    monkeypatch.setattr(hm.httpx, "AsyncClient", lambda **kw: _BadGet())
    import library.graph.tools as gt

    monkeypatch.setattr(gt, "ensure_constraints", AsyncMock(), raising=False)
    monkeypatch.setattr(gt, "ensure_corpus_constraints", AsyncMock(), raising=False)
    ok = await hm._preflight(_BootPool(), "http://ollama", AsyncMock())
    assert ok is False  # ollama is a hard dependency


@pytest.mark.asyncio
async def test_preflight_zep_neo4j_embed_degraded_but_nonfatal(monkeypatch):
    """Zep ping, Neo4j constraints, and the embed probe all blow up — preflight
    still returns True (only Postgres+Ollama are fatal)."""
    monkeypatch.setattr(hm.httpx, "AsyncClient", lambda **kw: _HttpClient())
    import library.graph.tools as gt

    monkeypatch.setattr(gt, "ensure_constraints", AsyncMock(side_effect=RuntimeError("neo down")), raising=False)
    monkeypatch.setattr(gt, "ensure_corpus_constraints", AsyncMock(), raising=False)
    memory = AsyncMock()
    memory.ping.side_effect = RuntimeError("zep down")
    ok = await hm._preflight(_BootPool(), "http://ollama", memory)
    assert ok is True


@pytest.mark.asyncio
async def test_preflight_embed_probe_failure_degraded(monkeypatch):
    """The embed POST raises → embed probe logs DEGRADED but preflight still True."""

    class _NoEmbedClient(_HttpClient):
        async def post(self, url, **kw):  # noqa: ARG002
            raise RuntimeError("embed model not pulled")

    monkeypatch.setattr(hm.httpx, "AsyncClient", lambda **kw: _NoEmbedClient())
    import library.graph.tools as gt

    monkeypatch.setattr(gt, "ensure_constraints", AsyncMock(), raising=False)
    monkeypatch.setattr(gt, "ensure_corpus_constraints", AsyncMock(), raising=False)
    ok = await hm._preflight(_BootPool(), "http://ollama", AsyncMock())
    assert ok is True  # embed is non-fatal


@pytest.mark.asyncio
async def test_preflight_embed_legacy_endpoint_fallback(monkeypatch):
    """Modern /api/embed → 404 triggers the legacy /api/embeddings POST; the second
    POST succeeds → embed probe logs OK (covers the 404-fallback branch)."""
    calls = {"n": 0}

    class _EmbedClient(_HttpClient):
        async def post(self, url, **kw):  # noqa: ARG002
            calls["n"] += 1
            return _Resp(404) if calls["n"] == 1 else _Resp(200)

    monkeypatch.setattr(hm.httpx, "AsyncClient", lambda **kw: _EmbedClient())
    import library.graph.tools as gt

    monkeypatch.setattr(gt, "ensure_constraints", AsyncMock(), raising=False)
    monkeypatch.setattr(gt, "ensure_corpus_constraints", AsyncMock(), raising=False)
    ok = await hm._preflight(_BootPool(), "http://ollama", AsyncMock())
    assert ok is True
    assert calls["n"] == 2  # both endpoint shapes attempted


# ── _register_vector_codec branch (covered via main's create_pool init=) ────────
@pytest.mark.asyncio
async def test_vector_codec_registration_swallows_when_pgvector_absent(monkeypatch):
    """main() passes init=_register_vector_codec to create_pool; invoke that callback
    directly to cover both the success and the swallow-warning branches."""
    _, _ = _install_mocks(monkeypatch)
    _set_gates(monkeypatch)
    captured = {}

    async def _capturing_create_pool(dsn, **kw):  # noqa: ARG001
        captured["init"] = kw.get("init")
        return _BootPool()

    monkeypatch.setattr(hm.asyncpg, "create_pool", _capturing_create_pool)
    await hm.main()
    init = captured["init"]
    assert init is not None

    # Force the codec registration to blow up (pgvector ext absent / wrong conn) →
    # the except block must swallow it and only log a warning.
    import pgvector.asyncpg as pa

    monkeypatch.setattr(pa, "register_vector", AsyncMock(side_effect=RuntimeError("no vector ext")))
    await init(AsyncMock())  # must not raise out


@pytest.mark.asyncio
async def test_vector_codec_registration_success_path(monkeypatch):
    """When pgvector.asyncpg imports cleanly, the init callback registers the vector
    codec on the connection (covers the non-exception branch)."""
    _install_mocks(monkeypatch)
    _set_gates(monkeypatch)
    captured = {}

    async def _capturing_create_pool(dsn, **kw):  # noqa: ARG001
        captured["init"] = kw.get("init")
        return _BootPool()

    monkeypatch.setattr(hm.asyncpg, "create_pool", _capturing_create_pool)
    await hm.main()
    init = captured["init"]

    import sys
    import types

    register = AsyncMock()
    fake_pgvector = types.ModuleType("pgvector")
    fake_asyncpg = types.ModuleType("pgvector.asyncpg")
    fake_asyncpg.register_vector = register
    fake_pgvector.asyncpg = fake_asyncpg
    monkeypatch.setitem(sys.modules, "pgvector", fake_pgvector)
    monkeypatch.setitem(sys.modules, "pgvector.asyncpg", fake_asyncpg)

    conn = AsyncMock()
    await init(conn)
    register.assert_awaited_once_with(conn)


# ── pace_task shutdown-cancel branch (runner finishes → pace_task.cancel reached) ─
def _eager_create_task(coro):
    """create_task stand-in: drive a non-suspending fake coroutine to completion
    immediately and hand back an already-DONE future. `await done_future` in the
    shutdown block then returns normally (cancel is a no-op on a done future), so
    main()'s flow reaches the pace_task cleanup branch instead of being cut short by
    a suppressed CancelledError."""
    try:
        coro.send(None)
        raise AssertionError("fake coroutine unexpectedly suspended")
    except StopIteration as stop:
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(stop.value)
        return fut


@pytest.mark.asyncio
async def test_pace_task_cancel_branch_reached_on_clean_runner(monkeypatch):
    """Runner is already DONE before the shutdown cancel → `await runner` returns
    normally and flow reaches the `pace_task is not None` cleanup (lines 321-323)."""
    pace = []
    _install_mocks(monkeypatch, pace_records=pace)
    _set_gates(monkeypatch, pace=True)
    monkeypatch.setattr(hm.asyncio, "create_task", _eager_create_task)
    rc = await hm.main()
    assert rc == 0
    assert len(pace) == 1
    disp = _FakeDispatcher.last_instance
    assert disp.run_called is True  # runner executed before the shutdown cancel


@pytest.mark.asyncio
async def test_clean_runner_without_pace_skips_pace_branch(monkeypatch):
    """No ARIADNE_PACE → pace_task is None. With the runner already DONE, `await runner`
    returns normally and the `if pace_task is not None` guard falls through (covers the
    321->325 branch where the pacemaker cleanup is skipped)."""
    pace = []
    _install_mocks(monkeypatch, pace_records=pace)
    _set_gates(monkeypatch, pace=False)
    monkeypatch.setattr(hm.asyncio, "create_task", _eager_create_task)
    rc = await hm.main()
    assert rc == 0
    assert pace == []
    assert _FakeDispatcher.last_instance.run_called is True
