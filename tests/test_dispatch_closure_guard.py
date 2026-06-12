"""Closure guard + auto-close research ladder for harness/dispatch.py — fully mocked, NO Postgres.

Two halves:
  * DETECTION — _detect_unclosed_events (non-exempt event types landing no_handler → loop.unclosed),
    _detect_stuck_directions (approved+active direction, work all terminal, nothing open).
  * AUTO-CLOSE — _advance_research_closure walks the bounded ladder one step per eligible tick:
        thin_corpus → acquire delivered new corpus? → re-queue (acquire_retry)
                    → else no scout yet            → ONE targeted scout sweep
                    → else scout settled           → re-queue (scouted_retry)
                    → else STILL thin after scout  → genuine gap → invalidate
    Idempotency comes from the candidate filter (a re-queue creates an OPEN task), so each branch
    is tested in isolation with a tailored ScriptedPool.

DB is scripted (tests._helpers.ScriptedPool, substring-keyed rules); get_agent_mode is monkeypatched
so the researcher gate is controllable; module constants are read at call time so monkeypatching
them on the module sets thresholds without a real clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import harness.dispatch as dispatch_mod
from harness.dispatch import Dispatcher
from tests._helpers import ScriptedPool

_NOW = datetime(2026, 6, 9, 18, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_mode_cache():
    from harness import agent_modes

    agent_modes._cache.clear()
    yield
    agent_modes._cache.clear()


def _indicator(pool, event_type):
    """loop.unclosed/etc emitted via _emit_indicator: execute args = (event_type, payload_json, dedup)."""
    out = []
    for kind, sql, args in pool.calls:
        if kind == "execute" and "INSERT INTO events" in sql and args and args[0] == event_type:
            out.append({"payload": json.loads(args[1]), "dedup": args[2]})
    return out


def _task_inserts(pool):
    out = []
    for kind, sql, args in pool.calls:
        if kind == "execute" and "INSERT INTO tasks" in sql:
            out.append({"description": args[0], "payload": json.loads(args[1]), "claim_id": args[2]})
    return out


def _sweep_inserts(pool):
    out = []
    for kind, sql, args in pool.calls:
        if kind == "execute" and "INSERT INTO events" in sql and "library.sweep_requested" in sql:
            out.append({"claim_id": args[0], "payload": json.loads(args[1]), "dedup": args[2]})
    return out


# ===========================================================================
# Detection — _detect_unclosed_events
# ===========================================================================


@pytest.mark.asyncio
async def test_detect_unclosed_events_flags_non_exempt_only():
    rows = [
        {"event_type": "task.completed", "n": 5, "last": _NOW},  # non-exempt → flagged
        {"event_type": "session.started", "n": 999, "last": _NOW},  # telemetry → ignored
        {"event_type": "acquire.fulfilled", "n": 9, "last": _NOW},  # poll-consumed → ignored
    ]
    pool = ScriptedPool([("suppression_reason = 'no_handler'", rows)])
    disp = Dispatcher(pool=pool)
    await disp._detect_unclosed_events(pool.conn)
    flagged = _indicator(pool, "loop.unclosed")
    assert len(flagged) == 1
    assert flagged[0]["payload"] == {
        "kind": "unhandled_event",
        "event_type": "task.completed",
        "count": 5,
        "window_days": dispatch_mod.CLOSURE_LOOKBACK_DAYS,
    }


@pytest.mark.asyncio
async def test_detect_unclosed_events_quiet_when_all_exempt():
    rows = [{"event_type": "step.completed", "n": 100, "last": _NOW}]
    pool = ScriptedPool([("suppression_reason = 'no_handler'", rows)])
    disp = Dispatcher(pool=pool)
    await disp._detect_unclosed_events(pool.conn)
    assert _indicator(pool, "loop.unclosed") == []


@pytest.mark.asyncio
async def test_detect_stuck_directions_emits_per_direction():
    pool = ScriptedPool([("JOIN direction_gate dg", [{"id": 43, "status": "proposed"}])])
    disp = Dispatcher(pool=pool)
    await disp._detect_stuck_directions(pool.conn)
    flagged = _indicator(pool, "loop.unclosed")
    assert flagged and flagged[0]["payload"]["kind"] == "direction_stalled"
    assert flagged[0]["payload"]["claim_id"] == 43


# ===========================================================================
# Auto-close ladder — _advance_research_closure
# ===========================================================================


def _ladder_pool(latest, *, pending_acq=0, fulfilled_new=0, scout_at=None, topics=None, now=_NOW, candidates=None):
    """Scripted DB for one ladder candidate (#43). `latest` is the most-recent task row."""
    if candidates is None:
        candidates = [{"id": 43, "statement": "Frontier mapping for Gaussian processes"}]
    rules = [
        ("c.id, c.statement", candidates),
        ("result->>'disposition' AS disp", latest),
        ("status='pending' AND emitted_at", pending_acq),  # the ladder's RECENT-pending-acquire wait
        ("'status' = 'fulfilled'", fulfilled_new),
        ("max(emitted_at) FROM events WHERE event_type='library.sweep_requested'", scout_at),
        ("DISTINCT payload->>'query'", topics or [{"q": "deep kernel GP"}, {"q": "spectral mixture kernel"}]),
        ("SELECT now()", now),
    ]
    return ScriptedPool(rules)


def _researcher_active(monkeypatch, mode="active"):
    monkeypatch.setattr(dispatch_mod, "get_agent_mode", AsyncMock(return_value=mode))


@pytest.mark.asyncio
async def test_ladder_requeues_on_acquire_delivered_corpus(monkeypatch):
    _researcher_active(monkeypatch)
    latest = {"completed_at": _NOW - timedelta(hours=2), "disp": "thin_corpus", "stage": None}
    pool = _ladder_pool(latest, fulfilled_new=2)
    disp = Dispatcher(pool=pool)
    await disp._advance_research_closure(pool.conn)
    tasks = _task_inserts(pool)
    assert len(tasks) == 1
    assert tasks[0]["payload"]["closure"]["stage"] == "acquire_retry"
    assert tasks[0]["claim_id"] == 43
    assert _sweep_inserts(pool) == []  # no scout yet — acquire corpus came first


@pytest.mark.asyncio
async def test_ladder_fires_one_scout_when_acquire_exhausted(monkeypatch):
    _researcher_active(monkeypatch)
    latest = {"completed_at": _NOW - timedelta(hours=2), "disp": "thin_corpus", "stage": "acquire_retry"}
    pool = _ladder_pool(latest, fulfilled_new=0, scout_at=None)
    disp = Dispatcher(pool=pool)
    await disp._advance_research_closure(pool.conn)
    sweeps = _sweep_inserts(pool)
    assert len(sweeps) == 1
    assert sweeps[0]["claim_id"] == 43
    assert sweeps[0]["payload"]["claim_id"] == 43
    assert sweeps[0]["payload"]["topics"] == ["deep kernel GP", "spectral mixture kernel"]
    assert _task_inserts(pool) == []  # don't re-queue until the scout settles


@pytest.mark.asyncio
async def test_ladder_requeues_scouted_after_settle(monkeypatch):
    _researcher_active(monkeypatch)
    latest = {"completed_at": _NOW - timedelta(hours=3), "disp": "thin_corpus", "stage": "acquire_retry"}
    pool = _ladder_pool(latest, scout_at=_NOW - timedelta(hours=1))  # scouted 1h ago > 20m settle
    disp = Dispatcher(pool=pool)
    await disp._advance_research_closure(pool.conn)
    tasks = _task_inserts(pool)
    assert len(tasks) == 1 and tasks[0]["payload"]["closure"]["stage"] == "scouted_retry"


@pytest.mark.asyncio
async def test_ladder_declares_gap_after_scouted_retry_still_thin(monkeypatch):
    _researcher_active(monkeypatch)
    latest = {"completed_at": _NOW - timedelta(hours=3), "disp": "thin_corpus", "stage": "scouted_retry"}
    pool = _ladder_pool(latest, scout_at=_NOW - timedelta(hours=1))
    disp = Dispatcher(pool=pool)
    disp.state = AsyncMock()
    await disp._advance_research_closure(pool.conn)
    disp.state.invalidate_claim.assert_awaited_once()
    _args, kwargs = disp.state.invalidate_claim.call_args
    assert _args[0] == 43
    assert kwargs["verdict_id"] is None
    assert "gap" in kwargs["reason"].lower()
    assert _task_inserts(pool) == []  # gap declared, not re-queued


@pytest.mark.asyncio
async def test_ladder_retires_corpus_exhausted_direction(monkeypatch):
    """The researcher escalates a scouted-but-still-thin result to corpus_exhausted; the ladder
    must RETIRE it (free the 1-direction budget slot), not skip it."""
    _researcher_active(monkeypatch)
    latest = {"completed_at": _NOW - timedelta(hours=1), "disp": "corpus_exhausted", "stage": "scouted_retry"}
    pool = _ladder_pool(latest)
    disp = Dispatcher(pool=pool)
    disp.state = AsyncMock()
    await disp._advance_research_closure(pool.conn)
    disp.state.invalidate_claim.assert_awaited_once()
    args, kwargs = disp.state.invalidate_claim.call_args
    assert args[0] == 43 and kwargs["verdict_id"] is None and "exhausted" in kwargs["reason"]
    # short-circuited before the acquire/scout queries
    assert not any("'status' = 'fulfilled'" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_ladder_gap_swallows_invalidate_failure(monkeypatch):
    """A gap-declaration that raises must NOT kill the watchdog sweep."""
    _researcher_active(monkeypatch)
    latest = {"completed_at": _NOW - timedelta(hours=3), "disp": "thin_corpus", "stage": "scouted_retry"}
    pool = _ladder_pool(latest, scout_at=_NOW - timedelta(hours=1))
    disp = Dispatcher(pool=pool)
    disp.state = AsyncMock()
    disp.state.invalidate_claim.side_effect = RuntimeError("claim vanished")
    await disp._advance_research_closure(pool.conn)  # must not raise
    disp.state.invalidate_claim.assert_awaited_once()


@pytest.mark.asyncio
async def test_ladder_gap_without_state_emits_indicator(monkeypatch):
    _researcher_active(monkeypatch)
    latest = {"completed_at": _NOW - timedelta(hours=3), "disp": "thin_corpus", "stage": "scouted_retry"}
    pool = _ladder_pool(latest, scout_at=_NOW - timedelta(hours=1))
    disp = Dispatcher(pool=pool)  # no .state attached
    await disp._advance_research_closure(pool.conn)
    flagged = _indicator(pool, "loop.unclosed")
    assert flagged and flagged[0]["payload"]["kind"] == "research_gap"


@pytest.mark.asyncio
async def test_ladder_waits_while_acquire_pending(monkeypatch):
    _researcher_active(monkeypatch)
    latest = {"completed_at": _NOW - timedelta(hours=2), "disp": "thin_corpus", "stage": None}
    pool = _ladder_pool(latest, pending_acq=1, fulfilled_new=2)  # acquire still in flight → wait
    disp = Dispatcher(pool=pool)
    await disp._advance_research_closure(pool.conn)
    assert _task_inserts(pool) == [] and _sweep_inserts(pool) == []


@pytest.mark.asyncio
async def test_ladder_waits_while_scout_not_settled(monkeypatch):
    _researcher_active(monkeypatch)
    latest = {"completed_at": _NOW - timedelta(hours=2), "disp": "thin_corpus", "stage": "acquire_retry"}
    pool = _ladder_pool(latest, scout_at=_NOW - timedelta(minutes=5))  # < 20m settle
    disp = Dispatcher(pool=pool)
    await disp._advance_research_closure(pool.conn)
    assert _task_inserts(pool) == []  # neither re-queue nor gap until the scout has settled


@pytest.mark.asyncio
async def test_ladder_skips_non_thin_disposition(monkeypatch):
    _researcher_active(monkeypatch)
    latest = {"completed_at": _NOW - timedelta(hours=2), "disp": "supported", "stage": None}
    pool = _ladder_pool(latest)
    disp = Dispatcher(pool=pool)
    await disp._advance_research_closure(pool.conn)
    assert _task_inserts(pool) == [] and _sweep_inserts(pool) == []


@pytest.mark.asyncio
async def test_ladder_noop_when_researcher_paused(monkeypatch):
    _researcher_active(monkeypatch, mode="off")
    pool = _ladder_pool({"completed_at": _NOW, "disp": "thin_corpus", "stage": None})
    disp = Dispatcher(pool=pool)
    await disp._advance_research_closure(pool.conn)
    # returned before even reading candidates
    assert not any("c.id, c.statement" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_emit_targeted_sweep_carries_claim_and_topics():
    pool = ScriptedPool()
    disp = Dispatcher(pool=pool)
    await disp._emit_targeted_sweep(pool.conn, 43, ["gp kernels"], "closure-scout-43")
    s = _sweep_inserts(pool)
    assert s and s[0]["claim_id"] == 43 and s[0]["payload"]["topics"] == ["gp kernels"]
    assert s[0]["dedup"] == "closure-scout-43"
    # targeted niche search → relevance ranking, not newest arXiv-wide
    assert s[0]["payload"]["sort"] == "relevance"


# ===========================================================================
# Reopen — _reopen_gapped_directions (gap ≠ dead end)
# ===========================================================================


def _reopen_pool(*, gapped=None, topics=None, matches=0, update="UPDATE 1"):
    """Scripted DB for the reopen rung. `gapped` rows come from the invalidated-direction
    scan (keyed on its distinctive c.invalidated_at column list); `matches` is the count of
    new docs whose chunks FTS-match the direction's thin topics."""
    if gapped is None:
        gapped = [
            {
                "id": 43,
                "statement": "Frontier mapping for Gaussian processes",
                "invalidated_at": _NOW - timedelta(days=2),
            }
        ]
    rules = [
        ("c.invalidated_at FROM claims", gapped),
        ("DISTINCT payload->>'query'", topics or [{"q": "deep kernel GP"}]),
        ("count(DISTINCT ch.document_id)", matches),
        ("UPDATE claims SET status = 'proposed'", update),
    ]
    return ScriptedPool(rules)


def _reopen_events(pool):
    out = []
    for kind, sql, args in pool.calls:
        if kind == "execute" and "direction.reopened" in sql:
            out.append({"claim_id": args[0], "payload": json.loads(args[1]), "dedup": args[2]})
    return out


@pytest.mark.asyncio
async def test_reopen_fires_on_new_matching_evidence(monkeypatch):
    """Enough new on-topic docs since the gap → claim back to 'proposed', breadcrumb event,
    and a fresh research task at the 'reopened' closure stage."""
    _researcher_active(monkeypatch)
    monkeypatch.setattr(dispatch_mod, "CLOSURE_REOPEN_MATCH_MIN", 5)
    pool = _reopen_pool(matches=7)
    disp = Dispatcher(pool=pool)
    await disp._reopen_gapped_directions(pool.conn)
    assert any(c[0] == "execute" and "UPDATE claims SET status = 'proposed'" in c[1] for c in pool.calls)
    ev = _reopen_events(pool)
    assert len(ev) == 1 and ev[0]["claim_id"] == 43
    assert ev[0]["payload"]["new_matching_docs"] == 7
    assert ev[0]["dedup"].startswith("reopen-43-")
    tasks = _task_inserts(pool)
    assert len(tasks) == 1
    assert tasks[0]["payload"]["closure"]["stage"] == "reopened"
    assert tasks[0]["claim_id"] == 43


@pytest.mark.asyncio
async def test_reopen_skips_below_match_threshold(monkeypatch):
    """A trickle of new docs that don't reach the threshold leaves the gap standing."""
    _researcher_active(monkeypatch)
    monkeypatch.setattr(dispatch_mod, "CLOSURE_REOPEN_MATCH_MIN", 5)
    pool = _reopen_pool(matches=4)
    disp = Dispatcher(pool=pool)
    await disp._reopen_gapped_directions(pool.conn)
    assert not any(c[0] == "execute" for c in pool.calls)
    assert _task_inserts(pool) == []


@pytest.mark.asyncio
async def test_reopen_trickles_one_per_tick(monkeypatch):
    """Two reopenable gaps, cap 1 → only the OLDEST gap reopens this tick (no gate flood)."""
    _researcher_active(monkeypatch)
    monkeypatch.setattr(dispatch_mod, "CLOSURE_REOPEN_MATCH_MIN", 1)
    monkeypatch.setattr(dispatch_mod, "CLOSURE_REOPEN_MAX_PER_TICK", 1)
    gapped = [
        {"id": 43, "statement": "GP frontier", "invalidated_at": _NOW - timedelta(days=2)},
        {"id": 44, "statement": "LoRA edge cases", "invalidated_at": _NOW - timedelta(days=1)},
    ]
    pool = _reopen_pool(gapped=gapped, matches=9)
    disp = Dispatcher(pool=pool)
    await disp._reopen_gapped_directions(pool.conn)
    ev = _reopen_events(pool)
    assert len(ev) == 1 and ev[0]["claim_id"] == 43
    assert len(_task_inserts(pool)) == 1


@pytest.mark.asyncio
async def test_reopen_raced_update_skips_followups(monkeypatch):
    """If the UPDATE matched no row (claim moved concurrently), neither the breadcrumb event
    nor the task re-queue may fire."""
    _researcher_active(monkeypatch)
    monkeypatch.setattr(dispatch_mod, "CLOSURE_REOPEN_MATCH_MIN", 1)
    pool = _reopen_pool(matches=9, update="UPDATE 0")
    disp = Dispatcher(pool=pool)
    await disp._reopen_gapped_directions(pool.conn)
    assert _reopen_events(pool) == []
    assert _task_inserts(pool) == []


@pytest.mark.asyncio
async def test_reopen_noop_when_researcher_paused(monkeypatch):
    _researcher_active(monkeypatch, mode="off")
    pool = _reopen_pool(matches=9)
    disp = Dispatcher(pool=pool)
    await disp._reopen_gapped_directions(pool.conn)
    assert pool.calls == []  # gated before any DB read


# ===========================================================================
# Re-arm — _rearm_research_spines (one-shot spine events re-derive from state)
# ===========================================================================


def _rearm_pool(*, exp_rows=None, synth_rows=None, audit_rows=None, critic_rows=None):
    """Scripted DB for the spine re-armer. Each spine's scan is keyed on a substring unique
    to its SQL; unsupplied spines scan empty (the fetch default)."""
    rules = [("to_char(now()", "2026-06-09")]
    if exp_rows is not None:
        rules.append(("COALESCE(e.interpretation, e.researcher_notes)", exp_rows))
    if synth_rows is not None:
        rules.append(("max(rf.n_experiments)", synth_rows))
    if audit_rows is not None:
        rules.append(("f.audit_verdict IS NULL", audit_rows))
    if critic_rows is not None:
        rules.append(("f.audit_verdict = 'pass'", critic_rows))
    return ScriptedPool(rules)


def _rearm_emits(pool, event_type):
    out = []
    for kind, sql, args in pool.calls:
        if kind == "execute" and "INSERT INTO events" in sql and args and args[0] == event_type:
            out.append({"target_id": args[2], "payload": json.loads(args[3]), "dedup": args[4]})
    return out


@pytest.mark.asyncio
async def test_rearm_uninterpreted_experiments(monkeypatch):
    """Terminal runs nothing interpreted → experiment.completed for completed, .failed for
    killed/failed, fresh day-bucketed dedup, payload marked rearmed."""
    _researcher_active(monkeypatch)  # all dials read 'active'
    exp_rows = [
        {"id": 7, "status": "completed", "task_id": 70, "claim_id": 43},
        {"id": 8, "status": "killed", "task_id": 71, "claim_id": 44},
    ]
    pool = _rearm_pool(exp_rows=exp_rows)
    disp = Dispatcher(pool=pool)
    await disp._rearm_research_spines(pool.conn)
    done = _rearm_emits(pool, "experiment.completed")
    failed = _rearm_emits(pool, "experiment.failed")
    assert len(done) == 1 and done[0]["payload"]["experiment_id"] == 7 and done[0]["payload"]["rearmed"]
    assert done[0]["dedup"] == "rearm-exp-7-2026-06-09"
    assert len(failed) == 1 and failed[0]["payload"]["experiment_id"] == 8


@pytest.mark.asyncio
async def test_rearm_starving_synthesis(monkeypatch):
    _researcher_active(monkeypatch)
    pool = _rearm_pool(synth_rows=[{"id": 43, "done": 4}])
    disp = Dispatcher(pool=pool)
    await disp._rearm_research_spines(pool.conn)
    synth = _rearm_emits(pool, "finding.synthesize")
    assert len(synth) == 1
    assert synth[0]["target_id"] == 43
    assert synth[0]["payload"] == {"claim_id": 43, "experiment_count": 4, "rearmed": True}
    assert synth[0]["dedup"] == "rearm-synth-43-2026-06-09"


@pytest.mark.asyncio
async def test_rearm_unaudited_findings(monkeypatch):
    _researcher_active(monkeypatch)
    pool = _rearm_pool(audit_rows=[{"id": 501}, {"id": 502}])
    disp = Dispatcher(pool=pool)
    await disp._rearm_research_spines(pool.conn)
    audits = _rearm_emits(pool, "task.completed")
    assert [a["target_id"] for a in audits] == [501, 502]
    assert all(a["payload"] == {"rearmed": True} for a in audits)


@pytest.mark.asyncio
async def test_rearm_unattacked_high_signal_one_per_claim(monkeypatch):
    """Two unattacked high-signal findings on the SAME claim → one re-emit (the critic
    attacks the claim; stacking per-finding events would hammer it)."""
    _researcher_active(monkeypatch)
    critic_rows = [
        {"id": 901, "claim_id": 43, "relevance_score": 9},
        {"id": 902, "claim_id": 43, "relevance_score": 8},
        {"id": 903, "claim_id": 44, "relevance_score": 8},
    ]
    pool = _rearm_pool(critic_rows=critic_rows)
    disp = Dispatcher(pool=pool)
    await disp._rearm_research_spines(pool.conn)
    attacks = _rearm_emits(pool, "finding.high_signal")
    assert [(a["target_id"], a["payload"]["finding_id"]) for a in attacks] == [(43, 901), (44, 903)]


@pytest.mark.asyncio
async def test_rearm_spines_gated_per_agent_dial(monkeypatch):
    """Each spine honours ITS agent's dial: with only 'evaluation' active, the experiment /
    synthesis / critic scans never run — no churn for paused agents, work kept in state."""

    async def _mode(pool, agent):
        return "active" if agent == "evaluation" else "off"

    monkeypatch.setattr(dispatch_mod, "get_agent_mode", _mode)
    pool = _rearm_pool(
        exp_rows=[{"id": 7, "status": "completed", "task_id": 70, "claim_id": 43}],
        synth_rows=[{"id": 43, "done": 4}],
        audit_rows=[{"id": 501}],
        critic_rows=[{"id": 901, "claim_id": 43, "relevance_score": 9}],
    )
    disp = Dispatcher(pool=pool)
    await disp._rearm_research_spines(pool.conn)
    assert _rearm_emits(pool, "experiment.completed") == []
    assert _rearm_emits(pool, "finding.synthesize") == []
    assert _rearm_emits(pool, "finding.high_signal") == []
    assert [a["target_id"] for a in _rearm_emits(pool, "task.completed")] == [501]
