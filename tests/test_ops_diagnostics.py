"""Unit tests for the read-only ops diagnostics CLIs.

Everything is mocked — NO real Postgres/Neo4j/subprocess and no DATABASE_URL. Each
script's run()/main() is driven with a scripted asyncpg pool/conn (tests._helpers)
so the query/branch/print paths execute. asyncpg.create_pool / asyncpg.connect are
monkeypatched to hand back a ScriptedPool/ScriptedConn; load_dotenv is a no-op;
subprocess and the agent-mode helpers are patched at the module that imports them.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from ops import (
    _env,
    agent_mode,
    lab_debug,
    lab_doctor,
    lab_snapshot,
    liveness_check,
    why,
)
from tests._helpers import ScriptedConn, ScriptedPool


# ── shared monkeypatch helpers ───────────────────────────────────────────────
async def _noop_close():
    pass


def _patch_connect(monkeypatch, module, conn):
    """Make module.asyncpg.connect(dsn) return `conn` (lab_doctor / why path).
    The scripts call `await conn.close()`, so give the bare ScriptedConn one."""
    conn.close = _noop_close
    monkeypatch.setattr(module.asyncpg, "connect", AsyncMock(return_value=conn))


def _patch_pool(monkeypatch, module, pool):
    """Make module.asyncpg.create_pool(...) return `pool` (pool-based scripts)."""
    monkeypatch.setattr(module.asyncpg, "create_pool", AsyncMock(return_value=pool))


def _no_dotenv(monkeypatch, *modules):
    for m in modules:
        monkeypatch.setattr(m, "load_dotenv", lambda *a, **k: None, raising=False)


@pytest.fixture(autouse=True)
def _reset_doctor_flags():
    """lab_doctor accumulates flags in a module global — reset around each test."""
    lab_doctor._flags.clear()
    yield
    lab_doctor._flags.clear()


# ─────────────────────────────────────────────────────────────────────────────
# ops._env
# ─────────────────────────────────────────────────────────────────────────────
class _DivTo:
    """A node whose `/ anything` yields a fixed target (dunders resolve on the type)."""

    def __init__(self, target):
        self.target = target

    def __truediv__(self, _other):
        return self.target


def _patch_env_path(monkeypatch, target):
    """Make `Path(__file__).resolve().parent.parent / '.env'` resolve to `target`."""
    repo_root = _DivTo(target)
    ops_dir = types.SimpleNamespace(parent=repo_root)
    resolved = types.SimpleNamespace(parent=ops_dir)
    monkeypatch.setattr(_env, "Path", lambda *_a: types.SimpleNamespace(resolve=lambda: resolved))


def test_env_load_dotenv_no_file(monkeypatch, tmp_path):
    """No .env present → early return, environment untouched."""
    _patch_env_path(monkeypatch, tmp_path / "nope" / ".env")
    _env.load_dotenv()  # must not raise


def test_env_load_dotenv_parses(monkeypatch, tmp_path):
    """Parses KEY=val lines, skips comments/blank/no-'=', honours strip + quotes,
    and never overrides an already-set var."""
    envf = tmp_path / ".env"
    envf.write_text("# a comment\n\nnoequalsline\nNEW_KEY = \"quoted value\"\nOTHER='single'\nALREADY=fromfile\n")
    _patch_env_path(monkeypatch, envf)
    monkeypatch.setenv("ALREADY", "preexisting")
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.delenv("OTHER", raising=False)
    _env.load_dotenv()
    assert os.environ["NEW_KEY"] == "quoted value"
    assert os.environ["OTHER"] == "single"
    assert os.environ["ALREADY"] == "preexisting"  # not overridden
    assert "noequalsline" not in os.environ


@pytest.mark.asyncio
async def test_env_register_vector_codec_ok(monkeypatch):
    called = {}

    async def _reg(conn):
        called["conn"] = conn

    monkeypatch.setattr(_env.pgvector.asyncpg, "register_vector", _reg)
    await _env.register_vector_codec("CONN")
    assert called["conn"] == "CONN"


@pytest.mark.asyncio
async def test_env_register_vector_codec_failure(monkeypatch, capsys):
    async def _boom(conn):
        raise RuntimeError("no extension")

    monkeypatch.setattr(_env.pgvector.asyncpg, "register_vector", _boom)
    await _env.register_vector_codec("CONN")  # swallowed
    assert "pgvector codec not registered" in capsys.readouterr().err


# ─────────────────────────────────────────────────────────────────────────────
# ops.lab_doctor
# ─────────────────────────────────────────────────────────────────────────────
def _doctor_clean_rules():
    """Rules giving a fully-healthy lab (no flags raised)."""
    return [
        ("max(emitted_at) FROM events", "2026-06-09 10:00"),
        ("max(started_at) FROM agent_runs", "2026-06-09 10:01"),
        # events-by-status (last Nh)
        ("GROUP BY status", [{"status": "consumed", "count": 5}]),
        ("count(*) FROM agent_runs WHERE started_at > now()", 3),
        # errors
        ("status = 'failed' OR error", []),
        # stuck
        ("status = 'running' AND started_at <", 0),
        ("FROM tasks WHERE status = 'running'", 0),
        ("status = 'pending' AND created_at <", 0),
        ("FROM events WHERE status = 'pending'", 0),
        # gates
        ("status = 'suppressed'", []),
        # cost
        ("coalesce(sum(cost_usd),0)", 0),
        ("GROUP BY model_tier", []),
        # mimir
        ("WHERE ingested_at >", 2),
        ("trust_state = 'quarantined'", 0),
        ("decision = 'block'", 0),
        ("WHERE queryable", 100),
        # modes
        ("FROM agent_modes ORDER BY agent_name", []),
        # agents
        ("max(started_at) AS last", []),
    ]


@pytest.mark.asyncio
async def test_lab_doctor_clean(monkeypatch, capsys):
    conn = ScriptedConn(_doctor_clean_rules())
    _patch_connect(monkeypatch, lab_doctor, conn)
    _no_dotenv(monkeypatch, lab_doctor)
    monkeypatch.setenv("DATABASE_URL", "x")

    rc = await lab_doctor.run(hours=1)
    out = capsys.readouterr().out
    assert rc == 0
    assert "LAB DOCTOR" in out
    assert "VERDICT: ✓ no anomalies surfaced." in out
    # the connection was closed
    assert any(c[0] == "fetchval" for c in conn.calls)


@pytest.mark.asyncio
async def test_lab_doctor_with_warnings(monkeypatch, capsys):
    rules = _doctor_clean_rules()
    overrides = {
        "max(emitted_at) FROM events": None,  # → WARN last event
        "status = 'failed' OR error": [{"agent_name": "mimir", "n": 3, "sample": "boom"}],
        "status = 'running' AND started_at <": 2,  # orphan runs → WARN
        "status = 'suppressed'": [{"suppression_reason": "cost_cap", "count": 4}],
        "coalesce(sum(cost_usd),0)": 25.0,  # → WARN cost
        "GROUP BY model_tier": [{"model_tier": "deep", "c": 24.5}],  # by-tier line
        "decision = 'block'": 5,  # retraction holds → WARN
        "FROM agent_modes ORDER BY agent_name": [
            {"agent_name": "ariadne", "mode": "shadow", "note": "paused"},
            {"agent_name": "mimir", "mode": "active", "note": None},
        ],
        "max(started_at) AS last": [{"agent_name": "mimir", "last": "10:00", "d": 4}],
    }
    rules = [(k, overrides.get(k, v)) for k, v in rules]
    conn = ScriptedConn(rules)
    _patch_connect(monkeypatch, lab_doctor, conn)
    _no_dotenv(monkeypatch, lab_doctor)
    monkeypatch.setenv("DATABASE_URL", "x")

    rc = await lab_doctor.run(hours=2)
    out = capsys.readouterr().out
    assert rc == 0  # warnings only, no ✗
    assert "thing(s) to look at" in out
    assert "mimir: 3 failed" in out
    assert "cost_cap" in out
    assert "$25.00" in out
    assert "by tier: deep=$24.5" in out


@pytest.mark.asyncio
async def test_lab_doctor_check_errors_isolated(monkeypatch, capsys):
    """A single check raising → ✗ flag, exit 1, but the report still completes."""

    def _boom():
        raise RuntimeError("db gone")

    rules = _doctor_clean_rules()
    rules = [("max(emitted_at) FROM events", _boom)] + rules
    conn = ScriptedConn(rules)
    _patch_connect(monkeypatch, lab_doctor, conn)
    _no_dotenv(monkeypatch, lab_doctor)
    monkeypatch.setenv("DATABASE_URL", "x")

    rc = await lab_doctor.run(hours=1)
    out = capsys.readouterr().out
    assert rc == 1
    assert "_activity check errored" in out


@pytest.mark.asyncio
async def test_lab_doctor_modes_table_missing(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("relation agent_modes does not exist")

    rules = [(k, v) for k, v in _doctor_clean_rules() if k != "FROM agent_modes ORDER BY agent_name"]
    rules.append(("FROM agent_modes ORDER BY agent_name", _boom))
    conn = ScriptedConn(rules)
    _patch_connect(monkeypatch, lab_doctor, conn)
    _no_dotenv(monkeypatch, lab_doctor)
    monkeypatch.setenv("DATABASE_URL", "x")

    rc = await lab_doctor.run(hours=1)
    out = capsys.readouterr().out
    assert rc == 0
    assert "agent_modes table not present" in out


@pytest.mark.asyncio
async def test_lab_doctor_no_dsn(monkeypatch, capsys):
    _no_dotenv(monkeypatch, lab_doctor)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await lab_doctor.run(hours=1)
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_lab_doctor_main(monkeypatch):
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 7)[1])
    monkeypatch.setattr("sys.argv", ["ops.lab_doctor", "--hours", "3"])
    assert lab_doctor.main() == 7


# ─────────────────────────────────────────────────────────────────────────────
# ops.why
# ─────────────────────────────────────────────────────────────────────────────
def _ns(**kw):
    return argparse.Namespace(**kw)


@pytest.mark.asyncio
async def test_why_doc_full_journey(monkeypatch, capsys):
    doc = {
        "id": 42,
        "title": "A Paper",
        "source_kind": "arxiv",
        "canonical_key": "2406.0001",
        "trust_tier": "preprint",
        "trust_state": "certified",
        "status": "ingested",
        "queryable": True,
        "ingested_at": None,
    }
    rules = [
        ("FROM documents WHERE id = $1", doc),
        ("FROM discovery_seen", {"first_seen_at": None, "last_attempt_at": None, "attempts": 2}),
        ("event_type = 'source.discovered'", [{"id": 7, "emitted_at": None, "status": "consumed"}]),
        (
            "FROM certifications WHERE document_id = $1",
            [
                {
                    "id": 9,
                    "decision": "approve",
                    "to_tier": "preprint",
                    "used_llm": False,
                    "reasons": "looks legit",
                    "decided_by_run_id": 3,
                    "created_at": None,
                }
            ],
        ),
        ("FROM agent_runs WHERE id = $1", {"session_id": "S1", "model_name": "deepseek"}),
        (
            "target_type = 'document'",
            [
                {"event_type": "document.ingested", "emitted_at": None, "status": "consumed"},
                {"event_type": "weird.custom", "emitted_at": None, "status": "pending"},
            ],
        ),
    ]
    conn = ScriptedConn(rules)
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")

    rc = await why.run(_ns(cmd="doc", id=42))
    out = capsys.readouterr().out
    assert rc == 0
    assert "JOURNEY of document #42" in out
    assert "SCOUT" in out and "DISCOVERED" in out
    assert "MIMIR CERTIFY" in out
    assert "/trace/S1" in out
    assert "INGEST" in out and "weird.custom" in out
    assert "now retrievable in the Library" in out


@pytest.mark.asyncio
async def test_why_doc_cert_run_without_session(monkeypatch, capsys):
    """A cert decided_by_run_id whose run has no session_id → no /trace appended."""
    doc = {
        "id": 8,
        "title": "T",
        "source_kind": "arxiv",
        "canonical_key": "k",
        "trust_tier": "preprint",
        "trust_state": "certified",
        "status": "ingested",
        "queryable": True,
        "ingested_at": None,
    }
    rules = [
        ("FROM documents WHERE id = $1", doc),
        ("FROM discovery_seen", None),
        ("event_type = 'source.discovered'", []),
        (
            "FROM certifications WHERE document_id = $1",
            [
                {
                    "id": 2,
                    "decision": "approve",
                    "to_tier": "preprint",
                    "used_llm": False,
                    "reasons": "ok",
                    "decided_by_run_id": 4,
                    "created_at": None,
                }
            ],
        ),
        ("FROM agent_runs WHERE id = $1", {"session_id": None, "model_name": "m"}),
        ("target_type = 'document'", []),
    ]
    conn = ScriptedConn(rules)
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await why.run(_ns(cmd="doc", id=8))
    out = capsys.readouterr().out
    assert rc == 0
    assert "MIMIR CERTIFY" in out
    assert "/trace/" not in out  # session_id None → no trace link


@pytest.mark.asyncio
async def test_why_doc_not_found(monkeypatch, capsys):
    conn = ScriptedConn([("FROM documents WHERE id = $1", None)])
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await why.run(_ns(cmd="doc", id=99))
    assert rc == 1
    assert "no document #99" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_why_doc_minimal_reject(monkeypatch, capsys):
    """No seen/disc rows; a reject cert with no run; non-queryable doc."""
    doc = {
        "id": 5,
        "title": None,
        "source_kind": "web",
        "canonical_key": "k",
        "trust_tier": "web_unknown",
        "trust_state": "quarantined",
        "status": "blocked",
        "queryable": False,
        "ingested_at": None,
    }
    rules = [
        ("FROM documents WHERE id = $1", doc),
        ("FROM discovery_seen", None),
        ("event_type = 'source.discovered'", []),
        (
            "FROM certifications WHERE document_id = $1",
            [
                {
                    "id": 1,
                    "decision": "reject",
                    "to_tier": "quarantined",
                    "used_llm": True,
                    "reasons": None,
                    "decided_by_run_id": None,
                    "created_at": None,
                }
            ],
        ),
        ("target_type = 'document'", []),
    ]
    conn = ScriptedConn(rules)
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await why.run(_ns(cmd="doc", id=5))
    out = capsys.readouterr().out
    assert rc == 0
    assert "untitled" in out
    assert "MIMIR REJECT" in out
    assert "now retrievable" not in out


@pytest.mark.asyncio
async def test_why_source_resolves(monkeypatch, capsys):
    doc = {
        "id": 11,
        "title": "T",
        "source_kind": "arxiv",
        "canonical_key": "2406.9",
        "trust_tier": "preprint",
        "trust_state": "certified",
        "status": "ingested",
        "queryable": True,
        "ingested_at": None,
    }
    rules = [
        ("WHERE canonical_key = $1 OR arxiv_id = $1", {"id": 11}),
        ("FROM documents WHERE id = $1", doc),
        ("FROM discovery_seen", None),
        ("event_type = 'source.discovered'", []),
        ("FROM certifications WHERE document_id = $1", []),
        ("target_type = 'document'", []),
    ]
    conn = ScriptedConn(rules)
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await why.run(_ns(cmd="source", key="2406.9"))
    assert rc == 0
    assert "JOURNEY of document #11" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_why_source_not_found(monkeypatch, capsys):
    conn = ScriptedConn([("WHERE canonical_key = $1 OR arxiv_id = $1", None)])
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await why.run(_ns(cmd="source", key="missing"))
    assert rc == 1
    assert "no document with key 'missing'" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_why_event_pending_unconsumed(monkeypatch, capsys):
    """Deterministic, not consumed, not suppressed → only the header line prints."""
    ev = {
        "id": 300,
        "event_type": "task.created",
        "status": "pending",
        "emitted_at": None,
        "target_type": "task",
        "target_id": 1,
        "emitted_by_run_id": None,
        "consumed_run_id": None,
        "consumed_by_handler": None,
        "suppression_reason": None,
    }
    conn = ScriptedConn([("SELECT * FROM events WHERE id = $1", ev)])
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await why.run(_ns(cmd="event", id=300))
    out = capsys.readouterr().out
    assert rc == 0
    assert "EVENT #300" in out
    assert "emitted deterministically" in out
    assert "consumed by" not in out
    assert "suppressed" not in out


@pytest.mark.asyncio
async def test_why_event_with_run(monkeypatch, capsys):
    ev = {
        "id": 100,
        "event_type": "source.discovered",
        "status": "consumed",
        "emitted_at": None,
        "target_type": "document",
        "target_id": 42,
        "emitted_by_run_id": 8,
        "consumed_run_id": 9,
        "consumed_by_handler": "mimir.gate",
        "suppression_reason": None,
    }
    rules = [
        ("SELECT * FROM events WHERE id = $1", ev),
        ("FROM agent_runs WHERE id = $1", {"session_id": "SX", "agent_name": "mimir", "invocation_type": "handler"}),
    ]
    conn = ScriptedConn(rules)
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await why.run(_ns(cmd="event", id=100))
    out = capsys.readouterr().out
    assert rc == 0
    assert "EVENT #100" in out
    assert "emitted by run #8" in out
    assert "consumed by mimir.gate (run #9)" in out


@pytest.mark.asyncio
async def test_why_event_deterministic_suppressed(monkeypatch, capsys):
    ev = {
        "id": 200,
        "event_type": "task.created",
        "status": "suppressed",
        "emitted_at": None,
        "target_type": "task",
        "target_id": 1,
        "emitted_by_run_id": None,
        "consumed_run_id": None,
        "consumed_by_handler": None,
        "suppression_reason": "cooldown",
    }
    conn = ScriptedConn([("SELECT * FROM events WHERE id = $1", ev)])
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await why.run(_ns(cmd="event", id=200))
    out = capsys.readouterr().out
    assert rc == 0
    assert "emitted deterministically" in out
    assert "suppressed (cooldown)" in out


@pytest.mark.asyncio
async def test_why_event_not_found(monkeypatch, capsys):
    conn = ScriptedConn([("SELECT * FROM events WHERE id = $1", None)])
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await why.run(_ns(cmd="event", id=1))
    assert rc == 1
    assert "no event #1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_why_unknown_cmd_returns_zero(monkeypatch):
    conn = ScriptedConn([])
    _patch_connect(monkeypatch, why, conn)
    _no_dotenv(monkeypatch, why)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await why.run(_ns(cmd="bogus", id=1))
    assert rc == 0


@pytest.mark.asyncio
async def test_why_no_dsn(monkeypatch, capsys):
    _no_dotenv(monkeypatch, why)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await why.run(_ns(cmd="doc", id=1))
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_why_main(monkeypatch):
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    monkeypatch.setattr("sys.argv", ["ops.why", "doc", "42"])
    assert why.main() == 0


# ─────────────────────────────────────────────────────────────────────────────
# ops.agent_mode
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_agent_mode_list(monkeypatch, capsys):
    rows = [{"agent_name": "mimir", "mode": "active", "note": "hi"}]
    pool = ScriptedPool([("FROM agent_modes ORDER BY agent_name", rows)])
    _patch_pool(monkeypatch, agent_mode, pool)
    _no_dotenv(monkeypatch, agent_mode)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await agent_mode.run(_ns(cmd="list"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "explicit modes" in out
    assert "mimir" in out
    assert "(default)" in out  # ariadne etc derived


@pytest.mark.asyncio
async def test_agent_mode_list_empty(monkeypatch, capsys):
    pool = ScriptedPool([("FROM agent_modes ORDER BY agent_name", [])])
    _patch_pool(monkeypatch, agent_mode, pool)
    _no_dotenv(monkeypatch, agent_mode)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await agent_mode.run(_ns(cmd="list"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "(none set)" in out


@pytest.mark.asyncio
async def test_agent_mode_set(monkeypatch, capsys):
    set_calls = []

    async def _set(pool, agent, mode, note):
        set_calls.append((agent, mode, note))

    monkeypatch.setattr(agent_mode, "set_agent_mode", _set)
    pool = ScriptedPool([])
    _patch_pool(monkeypatch, agent_mode, pool)
    _no_dotenv(monkeypatch, agent_mode)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await agent_mode.run(_ns(cmd="set", agent="mimir", mode="off", note="dbg"))
    out = capsys.readouterr().out
    assert rc == 0
    assert set_calls == [("mimir", "off", "dbg")]
    assert "set mimir -> off  (dbg)" in out


@pytest.mark.asyncio
async def test_agent_mode_set_no_note(monkeypatch, capsys):
    monkeypatch.setattr(agent_mode, "set_agent_mode", AsyncMock())
    pool = ScriptedPool([])
    _patch_pool(monkeypatch, agent_mode, pool)
    _no_dotenv(monkeypatch, agent_mode)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await agent_mode.run(_ns(cmd="set", agent="ariadne", mode="shadow", note=None))
    assert rc == 0
    assert "set ariadne -> shadow" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_agent_mode_no_dsn(monkeypatch, capsys):
    _no_dotenv(monkeypatch, agent_mode)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await agent_mode.run(_ns(cmd="list"))
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_agent_mode_main(monkeypatch):
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    monkeypatch.setattr("sys.argv", ["ops.agent_mode", "list"])
    assert agent_mode.main() == 0


# ─────────────────────────────────────────────────────────────────────────────
# ops.lab_debug
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_lab_debug_pause(monkeypatch, capsys, tmp_path):
    rows = [{"agent_name": "mimir", "mode": "active", "note": None}]
    pool = ScriptedPool([("FROM agent_modes", rows)])
    set_calls = []

    async def _set(p, a, m, n):
        set_calls.append((a, m, n))

    monkeypatch.setattr(lab_debug, "set_agent_mode", _set)
    monkeypatch.setattr(lab_debug, "STASH", tmp_path / "modes.json")
    await lab_debug._pause(pool)
    out = capsys.readouterr().out
    assert "lab PAUSED" in out
    # every KNOWN agent set off
    assert all(c[1] == "off" for c in set_calls)
    assert (tmp_path / "modes.json").exists()


@pytest.mark.asyncio
async def test_lab_debug_resume(monkeypatch, capsys, tmp_path):
    stash = tmp_path / "modes.json"
    stash.write_text('[{"agent_name": "mimir", "mode": "active", "note": "n"}]')
    set_calls = []

    async def _set(p, a, m, n):
        set_calls.append((a, m, n))

    monkeypatch.setattr(lab_debug, "set_agent_mode", _set)
    monkeypatch.setattr(lab_debug, "STASH", stash)
    pool = ScriptedPool([])
    await lab_debug._resume(pool)
    out = capsys.readouterr().out
    assert "lab RESUMED" in out
    assert set_calls == [("mimir", "active", "n")]
    assert not stash.exists()  # unlinked


@pytest.mark.asyncio
async def test_lab_debug_resume_no_stash(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(lab_debug, "set_agent_mode", AsyncMock())
    monkeypatch.setattr(lab_debug, "STASH", tmp_path / "absent.json")
    pool = ScriptedPool([])
    await lab_debug._resume(pool)
    assert "restored 0 explicit mode(s)" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_lab_debug_status(monkeypatch, capsys):
    async def _get(pool, agent):
        return "active"

    monkeypatch.setattr(lab_debug, "get_agent_mode", _get)
    events = [{"event_type": "source.discovered", "status": "consumed"}]
    pool = ScriptedPool([("FROM events WHERE emitted_at >", events)])
    await lab_debug._status(pool)
    out = capsys.readouterr().out
    assert "agent modes" in out
    assert "source.discovered(consumed)" in out


@pytest.mark.asyncio
async def test_lab_debug_status_no_events(monkeypatch, capsys):
    monkeypatch.setattr(lab_debug, "get_agent_mode", AsyncMock(return_value="off"))
    pool = ScriptedPool([("FROM events WHERE emitted_at >", [])])
    await lab_debug._status(pool)
    assert "recent events: none" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_lab_debug_ariadne(monkeypatch, capsys):
    direction = types.SimpleNamespace(
        title="Agentic RAG", statement="bet it works", novelty_rationale="gap " * 60, grounded_in=["Paper A"]
    )
    out_obj = types.SimpleNamespace(mission_frame="frame it", directions=[direction])
    grade_obj = types.SimpleNamespace(
        schema_valid=True, claim_goals_wellformed=1.0, directions_grounded=1.0, citations_resolved=0.9, passed=True
    )

    monkeypatch.setattr(lab_debug, "PostgresClient", lambda pool: "STATE")
    monkeypatch.setattr(lab_debug, "run_shadow", AsyncMock(return_value=out_obj))
    monkeypatch.setattr(lab_debug, "grade", AsyncMock(return_value=grade_obj))

    await lab_debug._ariadne(ScriptedPool([]), topic="agentic RAG")
    out = capsys.readouterr().out
    assert "injecting deliberation request focused on: 'agentic RAG'" in out
    assert "MISSION" in out and "frame it" in out
    assert "DIRECTION 1: Agentic RAG" in out
    assert "→ PASS" in out


@pytest.mark.asyncio
async def test_lab_debug_ariadne_no_topic(monkeypatch, capsys):
    out_obj = types.SimpleNamespace(mission_frame="m", directions=[])
    grade_obj = types.SimpleNamespace(
        schema_valid=False, claim_goals_wellformed=0.0, directions_grounded=0.0, citations_resolved=0.0, passed=False
    )
    monkeypatch.setattr(lab_debug, "PostgresClient", lambda pool: "STATE")
    monkeypatch.setattr(lab_debug, "run_shadow", AsyncMock(return_value=out_obj))
    monkeypatch.setattr(lab_debug, "grade", AsyncMock(return_value=grade_obj))
    await lab_debug._ariadne(ScriptedPool([]), topic=None)
    out = capsys.readouterr().out
    assert "injecting deliberation request  (read-only)" in out
    assert "→ FAIL" in out


def test_lab_debug_gate_preset_good(capsys):
    lab_debug._gate(_ns(preset="good", url=None, arxiv=None, doi=None, doi_resolves=False, license=None, retracted=False))
    out = capsys.readouterr().out
    assert "trust gate on [good]" in out
    assert "ADMIT @" in out
    assert "blocked=False" in out


def test_lab_debug_gate_preset_bad(capsys):
    lab_debug._gate(_ns(preset="bad", url=None, arxiv=None, doi=None, doi_resolves=False, license=None, retracted=False))
    out = capsys.readouterr().out
    assert "trust gate on [bad]" in out
    assert "QUARANTINE/BLOCK" in out


def test_lab_debug_gate_preset_blocked(capsys):
    lab_debug._gate(
        _ns(preset="blocked", url=None, arxiv=None, doi=None, doi_resolves=False, license=None, retracted=False)
    )
    out = capsys.readouterr().out
    assert "trust gate on [blocked]" in out
    assert "QUARANTINE/BLOCK" in out  # license hard-gate → blocked


def test_lab_debug_gate_custom(capsys):
    lab_debug._gate(
        _ns(
            preset=None,
            url="https://arxiv.org/abs/2406.5",
            arxiv="2406.5",
            doi=None,
            doi_resolves=False,
            license=None,
            retracted=False,
        )
    )
    out = capsys.readouterr().out
    assert "trust gate on [custom]" in out
    assert "tier:" in out


@pytest.mark.asyncio
async def test_lab_debug_run_gate(monkeypatch, capsys):
    """run() short-circuits on `gate` with no DB touch."""
    _no_dotenv(monkeypatch, lab_debug)
    created = AsyncMock()
    monkeypatch.setattr(lab_debug.asyncpg, "create_pool", created)
    rc = await lab_debug.run(
        _ns(cmd="gate", preset="good", url=None, arxiv=None, doi=None, doi_resolves=False, license=None, retracted=False)
    )
    assert rc == 0
    created.assert_not_called()  # gate never opens a pool
    assert "ADMIT @" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_lab_debug_run_pause_dispatch(monkeypatch, tmp_path):
    pool = ScriptedPool([("FROM agent_modes", [])])
    _patch_pool(monkeypatch, lab_debug, pool)
    _no_dotenv(monkeypatch, lab_debug)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(lab_debug, "set_agent_mode", AsyncMock())
    monkeypatch.setattr(lab_debug, "STASH", tmp_path / "m.json")
    rc = await lab_debug.run(_ns(cmd="pause"))
    assert rc == 0


@pytest.mark.asyncio
async def test_lab_debug_run_resume_dispatch(monkeypatch, tmp_path):
    pool = ScriptedPool([])
    _patch_pool(monkeypatch, lab_debug, pool)
    _no_dotenv(monkeypatch, lab_debug)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(lab_debug, "set_agent_mode", AsyncMock())
    monkeypatch.setattr(lab_debug, "STASH", tmp_path / "absent.json")
    rc = await lab_debug.run(_ns(cmd="resume"))
    assert rc == 0


@pytest.mark.asyncio
async def test_lab_debug_run_status_dispatch(monkeypatch):
    monkeypatch.setattr(lab_debug, "get_agent_mode", AsyncMock(return_value="active"))
    pool = ScriptedPool([("FROM events WHERE emitted_at >", [])])
    _patch_pool(monkeypatch, lab_debug, pool)
    _no_dotenv(monkeypatch, lab_debug)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await lab_debug.run(_ns(cmd="status"))
    assert rc == 0


@pytest.mark.asyncio
async def test_lab_debug_run_ariadne_dispatch(monkeypatch):
    out_obj = types.SimpleNamespace(mission_frame="m", directions=[])
    grade_obj = types.SimpleNamespace(
        schema_valid=True, claim_goals_wellformed=1.0, directions_grounded=1.0, citations_resolved=1.0, passed=True
    )
    monkeypatch.setattr(lab_debug, "PostgresClient", lambda pool: "S")
    monkeypatch.setattr(lab_debug, "run_shadow", AsyncMock(return_value=out_obj))
    monkeypatch.setattr(lab_debug, "grade", AsyncMock(return_value=grade_obj))
    pool = ScriptedPool([])
    _patch_pool(monkeypatch, lab_debug, pool)
    _no_dotenv(monkeypatch, lab_debug)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await lab_debug.run(_ns(cmd="ariadne", topic=None))
    assert rc == 0


@pytest.mark.asyncio
async def test_lab_debug_run_unknown_cmd(monkeypatch):
    """An unrecognized cmd opens (and closes) the pool but dispatches nothing."""
    pool = ScriptedPool([])
    _patch_pool(monkeypatch, lab_debug, pool)
    _no_dotenv(monkeypatch, lab_debug)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await lab_debug.run(_ns(cmd="mystery"))
    assert rc == 0


@pytest.mark.asyncio
async def test_lab_debug_run_no_dsn(monkeypatch, capsys):
    _no_dotenv(monkeypatch, lab_debug)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await lab_debug.run(_ns(cmd="status"))
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_lab_debug_main(monkeypatch):
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    monkeypatch.setattr("sys.argv", ["ops.lab_debug", "gate", "good"])
    assert lab_debug.main() == 0


# ─────────────────────────────────────────────────────────────────────────────
# ops.lab_snapshot
# ─────────────────────────────────────────────────────────────────────────────
class _FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = _FakePipe()

    def communicate(self):
        return (b"", b"")

    def wait(self):
        return self.returncode


class _FakePipe:
    def close(self):
        pass


def _patch_snapshot(monkeypatch, tmp_path, dump_rc=0, gz_rc=0, size=2_000_000):
    monkeypatch.setattr(lab_snapshot, "BK", tmp_path / "backups")

    procs = []

    def _popen(cmd, **kw):
        # First call = pg_dump, second = gzip
        rc = dump_rc if not procs else gz_rc
        p = _FakeProc(rc)
        procs.append((cmd, p))
        return p

    monkeypatch.setattr(lab_snapshot.subprocess, "Popen", _popen)

    real_open = lab_snapshot.Path.open

    def _fake_open(self, mode="r", *a, **k):
        f = real_open(self, mode, *a, **k)
        # ensure stat() reports a non-zero size after write
        return f

    monkeypatch.setattr(lab_snapshot.Path, "open", _fake_open)
    return procs


@pytest.mark.asyncio
async def test_snapshot_reasoning_ok(monkeypatch, tmp_path, capsys):
    procs = _patch_snapshot(monkeypatch, tmp_path)
    # make the written file have a real size via stat by writing bytes through gzip stub
    rc = lab_snapshot._snapshot(full=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "snapshotting (reasoning)" in out
    assert "restore (CAREFUL" in out
    # reasoning mode passes -t for each table
    dump_cmd = procs[0][0]
    assert "-t" in dump_cmd
    assert "documents" in dump_cmd


@pytest.mark.asyncio
async def test_snapshot_full_ok(monkeypatch, tmp_path, capsys):
    procs = _patch_snapshot(monkeypatch, tmp_path)
    rc = lab_snapshot._snapshot(full=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "snapshotting (full)" in out
    # full mode does NOT restrict tables
    assert "-t" not in procs[0][0]


@pytest.mark.asyncio
async def test_snapshot_failure(monkeypatch, tmp_path, capsys):
    _patch_snapshot(monkeypatch, tmp_path, dump_rc=3)
    rc = lab_snapshot._snapshot(full=False)
    err = capsys.readouterr().err
    assert rc == 1
    assert "snapshot FAILED" in err
    # the partial file was unlinked
    assert not list((tmp_path / "backups").glob("*.sql.gz"))


def test_snapshot_list_none(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(lab_snapshot, "BK", tmp_path / "nope")
    rc = lab_snapshot._list()
    assert rc == 0
    assert "no backups/ yet" in capsys.readouterr().out


def test_snapshot_list_empty_dir(monkeypatch, tmp_path, capsys):
    bk = tmp_path / "backups"
    bk.mkdir()
    monkeypatch.setattr(lab_snapshot, "BK", bk)
    rc = lab_snapshot._list()
    assert rc == 0
    assert "no snapshots" in capsys.readouterr().out


def test_snapshot_list_with_files(monkeypatch, tmp_path, capsys):
    bk = tmp_path / "backups"
    bk.mkdir()
    (bk / "labfoundry_reasoning_20260101-000000.sql.gz").write_bytes(b"x" * 1000)
    monkeypatch.setattr(lab_snapshot, "BK", bk)
    rc = lab_snapshot._list()
    out = capsys.readouterr().out
    assert rc == 0
    assert "labfoundry_reasoning_20260101-000000.sql.gz" in out


def test_snapshot_main_list(monkeypatch):
    monkeypatch.setattr(lab_snapshot, "_list", lambda: 0)
    monkeypatch.setattr("sys.argv", ["ops.lab_snapshot", "--list"])
    assert lab_snapshot.main() == 0


def test_snapshot_main_snapshot(monkeypatch):
    seen = {}

    def _snap(full):
        seen["full"] = full
        return 0

    monkeypatch.setattr(lab_snapshot, "_snapshot", _snap)
    monkeypatch.setattr("sys.argv", ["ops.lab_snapshot", "--full"])
    assert lab_snapshot.main() == 0
    assert seen["full"] is True


# ─────────────────────────────────────────────────────────────────────────────
# ops.liveness_check
# ─────────────────────────────────────────────────────────────────────────────
def _liveness_row(**over):
    base = {
        "last_activity": None,
        "paused": False,
        "phase": "research",
        "deadline": None,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_liveness_healthy(monkeypatch, capsys):
    recent = datetime.now(UTC) - timedelta(seconds=10)
    pool = ScriptedPool([("MAX(started_at)", _liveness_row(last_activity=recent))])
    monkeypatch.setattr(liveness_check.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(liveness_check, "STALL_SECONDS", 1200)
    rc = await liveness_check.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "healthy" in out


@pytest.mark.asyncio
async def test_liveness_paused(monkeypatch, capsys):
    pool = ScriptedPool([("MAX(started_at)", _liveness_row(paused=True))])
    monkeypatch.setattr(liveness_check.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await liveness_check.main()
    assert rc == 0
    assert "company paused" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_liveness_past_deadline(monkeypatch, capsys):
    past = datetime.now(UTC) - timedelta(days=1)
    pool = ScriptedPool([("MAX(started_at)", _liveness_row(deadline=past))])
    monkeypatch.setattr(liveness_check.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await liveness_check.main()
    assert rc == 0
    assert "past deadline" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_liveness_stalled_restart_and_webhook(monkeypatch, capsys):
    old = datetime.now(UTC) - timedelta(seconds=5000)
    pool = ScriptedPool([("MAX(started_at)", _liveness_row(last_activity=old))])
    monkeypatch.setattr(liveness_check.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(liveness_check, "STALL_SECONDS", 1200)
    monkeypatch.setattr(liveness_check, "RESTART", True)
    monkeypatch.setattr(liveness_check, "WEBHOOK", "https://hook.example")

    restart_called = []
    monkeypatch.setattr(liveness_check, "_restart_harness", lambda: restart_called.append(True))

    posts = []

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None):
            posts.append((url, content))

    monkeypatch.setattr(liveness_check.httpx, "AsyncClient", _FakeClient)

    rc = await liveness_check.main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "STALLED" in err
    assert restart_called == [True]
    assert posts and posts[0][0] == "https://hook.example"


@pytest.mark.asyncio
async def test_liveness_stalled_no_restart_no_webhook(monkeypatch, capsys):
    old = datetime.now(UTC) - timedelta(seconds=5000)
    pool = ScriptedPool([("MAX(started_at)", _liveness_row(last_activity=old))])
    monkeypatch.setattr(liveness_check.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(liveness_check, "STALL_SECONDS", 1200)
    monkeypatch.setattr(liveness_check, "RESTART", False)
    monkeypatch.setattr(liveness_check, "WEBHOOK", None)
    restart_called = []
    monkeypatch.setattr(liveness_check, "_restart_harness", lambda: restart_called.append(True))
    rc = await liveness_check.main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "Restart disabled." in err
    assert restart_called == []  # restart disabled


@pytest.mark.asyncio
async def test_liveness_never_active(monkeypatch, capsys):
    """last_activity is None → 'ever' stall message, treated as stalled."""
    pool = ScriptedPool([("MAX(started_at)", _liveness_row(last_activity=None))])
    monkeypatch.setattr(liveness_check.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(liveness_check, "RESTART", False)
    monkeypatch.setattr(liveness_check, "WEBHOOK", None)
    rc = await liveness_check.main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "no activity for ever" in err


@pytest.mark.asyncio
async def test_liveness_webhook_failure_swallowed(monkeypatch, capsys):
    old = datetime.now(UTC) - timedelta(seconds=5000)
    pool = ScriptedPool([("MAX(started_at)", _liveness_row(last_activity=old))])
    monkeypatch.setattr(liveness_check.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(liveness_check, "RESTART", False)
    monkeypatch.setattr(liveness_check, "WEBHOOK", "https://hook.example")

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None):
            raise RuntimeError("network down")

    monkeypatch.setattr(liveness_check.httpx, "AsyncClient", _BoomClient)
    rc = await liveness_check.main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "webhook failed" in err


def test_liveness_restart_harness(monkeypatch):
    calls = []
    monkeypatch.setattr(liveness_check.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    liveness_check._restart_harness()
    assert calls
    args = calls[0][0][0]
    assert "systemctl" in args and "restart" in args


@pytest.mark.asyncio
async def test_liveness_fire_webhook_disabled(monkeypatch):
    """_fire_webhook is a no-op when no WEBHOOK configured."""
    monkeypatch.setattr(liveness_check, "WEBHOOK", None)
    await liveness_check._fire_webhook("msg")  # must not raise / not post
