"""Unit tests for the research-loop engine substrate — the single guarded write path
(state.advance_direction) and, later, the transition registry + generic driver
(harness.loop_engine). NO real Postgres — everything is a ScriptedPool (tests._helpers).

advance_direction is the ONE place a direction's lifecycle status is written; these tests
pin its legality table (claims.status has no DB CHECK, so the guard lives in Python), the
monotone graduation guard, the audit-row write, and the lifecycle-event emission.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from harness import loop_engine
from state.client import _LEGAL_DIRECTION_TRANSITIONS, _STATUS_RANK, PostgresClient
from tests._helpers import ScriptedPool

aio = pytest.mark.asyncio


def _client(cur_status: str | None):
    """A PostgresClient whose only direction returns `cur_status` for the FOR UPDATE read."""
    return PostgresClient(ScriptedPool(rules=[("SELECT status::text FROM claims", cur_status)]))


def _ops(pool, kind: str, needle: str) -> list:
    return [c for c in pool.calls if c[0] == kind and needle in c[1]]


# ── pure legality data (cheap drift catch) ───────────────────────────────────────
def test_legality_table_is_monotone_and_terminal():
    # concluded + merged are dead ends; invalidated's only exit is reopen→proposed.
    assert _LEGAL_DIRECTION_TRANSITIONS["concluded"] == set()
    assert _LEGAL_DIRECTION_TRANSITIONS["merged"] == set()
    assert _LEGAL_DIRECTION_TRANSITIONS["invalidated"] == {"proposed"}
    # every active status may invalidate
    for s in ("proposed", "tested", "weakly_supported", "replicated"):
        assert "invalidated" in _LEGAL_DIRECTION_TRANSITIONS[s]
    # rank is strictly increasing along the graduation ladder
    ladder = ["proposed", "tested", "weakly_supported", "replicated", "concluded"]
    assert [_STATUS_RANK[s] for s in ladder] == sorted(_STATUS_RANK[s] for s in ladder)


# ── illegal / no-op edges write nothing ──────────────────────────────────────────
@aio
async def test_concluded_cannot_revert_to_proposed():
    pool = ScriptedPool(rules=[("SELECT status::text FROM claims", "concluded")])
    st = PostgresClient(pool)
    res = await st.advance_direction(5, "proposed", transition="reopen", decided_by="closure")
    assert res is None
    assert not _ops(pool, "execute", "UPDATE claims")
    assert not _ops(pool, "execute", "direction_transitions")


@aio
async def test_invalidated_cannot_jump_to_tested():
    pool = ScriptedPool(rules=[("SELECT status::text FROM claims", "invalidated")])
    res = await PostgresClient(pool).advance_direction(5, "tested", transition="graduate", decided_by="synthesis")
    assert res is None
    assert not _ops(pool, "execute", "UPDATE claims")


@aio
async def test_same_status_is_noop():
    pool = ScriptedPool(rules=[("SELECT status::text FROM claims", "tested")])
    res = await PostgresClient(pool).advance_direction(5, "tested", transition="graduate", decided_by="synthesis")
    assert res is None


@aio
async def test_not_a_direction_is_skipped():
    pool = ScriptedPool(rules=[("SELECT status::text FROM claims", None)])  # fetchval → None
    res = await PostgresClient(pool).advance_direction(5, "concluded", transition="conclude", decided_by="synthesis")
    assert res is None
    assert not _ops(pool, "execute", "UPDATE claims")


@aio
async def test_monotone_rejects_demotion():
    pool = ScriptedPool(rules=[("SELECT status::text FROM claims", "replicated")])
    res = await PostgresClient(pool).advance_direction(
        5, "tested", transition="graduate", decided_by="synthesis", monotone=True
    )
    assert res is None
    assert not _ops(pool, "execute", "UPDATE claims")


# ── legal edges write status + audit + the right event ────────────────────────────
@aio
async def test_conclude_emits_direction_concluded_and_audits():
    pool = ScriptedPool(rules=[("SELECT status::text FROM claims", "weakly_supported")])
    res = await PostgresClient(pool).advance_direction(
        7, "concluded", transition="conclude", decided_by="synthesis", monotone=True
    )
    assert res == {"claim_id": 7, "from": "weakly_supported", "to": "concluded", "transition": "conclude"}
    assert _ops(pool, "execute", "UPDATE claims")
    assert _ops(pool, "execute", "direction_transitions")
    ev = _ops(pool, "execute", "INSERT INTO events")
    assert ev and ev[0][2][0] == "direction.concluded"


@aio
async def test_gap_emits_claim_invalidated_and_marks_findings_stale():
    pool = ScriptedPool(rules=[("SELECT status::text FROM claims", "proposed")])
    res = await PostgresClient(pool).advance_direction(
        9, "invalidated", transition="gap", decided_by="closure", reason="research gap: corpus thin"
    )
    assert res["transition"] == "gap"
    assert _ops(pool, "execute", "invalidated_at = now()")
    assert _ops(pool, "execute", "UPDATE findings SET audit_verdict = 'stale'")
    ev = _ops(pool, "execute", "INSERT INTO events")
    assert ev and ev[0][2][0] == "claim.invalidated"


@aio
async def test_reopen_clears_invalidation_and_is_silent():
    pool = ScriptedPool(rules=[("SELECT status::text FROM claims", "invalidated")])
    res = await PostgresClient(pool).advance_direction(3, "proposed", transition="reopen", decided_by="closure")
    assert res["transition"] == "reopen"
    assert _ops(pool, "execute", "invalidated_at = NULL")
    assert _ops(pool, "execute", "direction_transitions")
    # reopen has no entry in _TRANSITION_EVENT → no lifecycle event from advance_direction
    assert not _ops(pool, "execute", "INSERT INTO events")


# ── the transition registry + generic driver ─────────────────────────────────────
def test_registry_shape_is_wellformed():
    names = {t.name for t in loop_engine.REGISTRY}
    # every legacy _maybe_* + the four re-armers are present as transitions
    assert {"adjudicate", "plan", "arc_review", "arc_propose", "arc_article", "experiment_coverage"} <= names
    assert {"rearm_interpret", "rearm_conclude", "rearm_audit", "rearm_attack"} <= names
    for t in loop_engine.REGISTRY:
        assert t.owner and t.emits and callable(t.from_guard) and callable(t.payload) and callable(t.dedup)
        # a system-target transition keys off the sentinel (target_key None), else off a row key
        assert (t.target_key is None) == (t.target_type == "system")
        # an SLA transition must carry the SQL that finds its stalled targets
        assert (t.stall_sla_min > 0) == bool(t.stall_since_sql)


@aio
async def test_drive_respects_mode_dial(monkeypatch):
    monkeypatch.setattr(loop_engine, "get_agent_mode", AsyncMock(return_value="off"))
    pool = ScriptedPool()
    t = next(t for t in loop_engine.REGISTRY if t.name == "plan")
    assert await loop_engine.drive(pool, t) == []
    assert pool.calls == []  # gated before any DB read


@aio
async def test_drive_pending_singleton_blocks_restack(monkeypatch):
    monkeypatch.setattr(loop_engine, "get_agent_mode", AsyncMock(return_value="active"))
    # a pending planner.plan already exists → the count query returns 1 → no emit
    pool = ScriptedPool(rules=[("count(*) FROM events WHERE event_type", 1)])
    t = next(t for t in loop_engine.REGISTRY if t.name == "plan")
    assert await loop_engine.drive(pool, t) == []
    assert not any(c[0] == "execute" and "INSERT INTO events" in c[1] for c in pool.calls)


@aio
async def test_drive_emits_for_eligible_rows(monkeypatch):
    monkeypatch.setattr(loop_engine, "get_agent_mode", AsyncMock(return_value="active"))
    # arc_review guard returns one eligible claim; no pending review; the INSERT reports 1 row.
    pool = ScriptedPool(
        rules=[
            ("count(*) FROM events WHERE event_type", 0),  # pending-singleton check → none pending
            ("FROM claims c JOIN direction_gate dg", [{"id": 77}]),  # arc_review guard
            ("to_char(now(), 'YYYY-MM-DD')", "2026-06-13"),
            ("INSERT INTO events", "INSERT 0 1"),
        ]
    )
    t = next(t for t in loop_engine.REGISTRY if t.name == "arc_review")
    fired = await loop_engine.drive(pool, t)
    assert len(fired) == 1
    assert fired[0]["event"] == "ariadne.review" and fired[0]["target_id"] == 77
    assert fired[0]["dedup"] == "arc-ariadne.review-77-2026-06-13"


@aio
async def test_drive_shadow_writes_nothing(monkeypatch):
    monkeypatch.setattr(loop_engine, "get_agent_mode", AsyncMock(return_value="active"))
    pool = ScriptedPool(
        rules=[
            ("count(*) FROM events WHERE event_type", 0),
            ("FROM claims c JOIN direction_gate dg", [{"id": 77}]),
            ("to_char(now(), 'YYYY-MM-DD')", "2026-06-13"),
        ]
    )
    t = next(t for t in loop_engine.REGISTRY if t.name == "arc_review")
    fired = await loop_engine.drive(pool, t, shadow=True)
    assert len(fired) == 1  # reports what it WOULD emit
    assert not any(c[0] == "execute" and "INSERT INTO events" in c[1] for c in pool.calls)  # but writes nothing


def test_confirm_real_data_transition_registered():
    t = next((t for t in loop_engine.REGISTRY if t.name == "confirm_real_data"), None)
    assert t is not None and t.owner == "experiments" and t.emits == "experiment.requested"
    # its payload flags a real-data confirmation run
    p = t.payload({"claim_id": 7, "task_id": 3})
    assert p["require_real_data"] is True and p["trigger"] == "confirm_real" and p["claim_id"] == 7
    assert t.dedup({"claim_id": 7}, "2026-06-13", 0) == "confirm-real-7-2026-06-13"
