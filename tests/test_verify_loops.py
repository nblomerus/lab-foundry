"""ops.verify_loops — the per-loop live verifier.

`classify` is a pure function (every branch unit-tested); the audit / emit / poll / dry-run paths run
against a real (disposable) Postgres via the `db` fixture. No running harness, so injected triggers stay
pending — exactly the ⚠ the tool reports off a live lab with a dead consumer.
"""

from __future__ import annotations

import pytest

from ops import verify_loops as vl


async def _emit_event(db, event_type, *, status="consumed", reason=None, target_id=1, dedup=None):
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO events (event_type, target_type, target_id, payload, status, suppression_reason, dedup_key) "
            "VALUES ($1, 'claim', $2, '{}'::jsonb, $3::event_status, $4, $5)",
            event_type,
            target_id,
            status,
            reason,
            dedup or f"{event_type}-{target_id}",
        )


# ---------------------------------------------------------------------------
# classify — pure, every branch
# ---------------------------------------------------------------------------


def test_classify_idle_when_no_rows():
    assert vl.classify(None, None, "finding.composed", 0)[0] == vl._DOT


def test_classify_consumed_is_ok():
    assert vl.classify("consumed", None, "finding.composed", 3)[0] == vl._OK


def test_classify_failed_is_bad():
    assert vl.classify("failed", None, "finding.composed", 1)[0] == vl._BAD


def test_classify_no_handler_on_real_event_is_bad():
    assert vl.classify("suppressed", "no_handler", "finding.composed", 1)[0] == vl._BAD


def test_classify_no_handler_on_exempt_telemetry_is_warn_not_bad():
    # document.parsed is on CLOSURE_EXEMPT_EVENTS — never flagged broken
    mark, _ = vl.classify("suppressed", "no_handler", "document.parsed", 5)
    assert mark == vl._WARN


def test_classify_gate_suppression_is_warn():
    assert vl.classify("suppressed", "cost_cap", "planner.plan", 1)[0] == vl._WARN


def test_classify_pending_is_warn():
    assert vl.classify("pending", None, "planner.plan", 1)[0] == vl._WARN


# ---------------------------------------------------------------------------
# _step_status / audit — DB-backed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_status_reads_latest_status(db):
    await _emit_event(db, "finding.high_signal", status="consumed")
    async with db.pool.acquire() as conn:
        s = await vl._step_status(conn, "finding.high_signal", since_min=60)
    assert s["mark"] == vl._OK
    assert s["n"] == 1


@pytest.mark.asyncio
async def test_audit_returns_a_record_per_loop_step(db):
    await _emit_event(db, "finding.composed", status="consumed")
    async with db.pool.acquire() as conn:
        records = await vl.audit(conn, since_min=60)
    total_steps = sum(len(steps) for _, steps in vl.LOOPS)
    assert len(records) == total_steps
    composed = [r for r in records if r["event"] == "finding.composed"]
    assert composed and all(r["mark"] == vl._OK for r in composed)


# ---------------------------------------------------------------------------
# emit + poll — the --dry primitives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_consumed_times_out_when_nothing_consumes(db):
    async with db.pool.acquire() as conn:
        eid = await vl._emit(conn, "finding.composed", {"x": 1}, "verify-poll-1")
        assert eid is not None
        # No harness running → stays pending → timeout returns None
        assert await vl._poll_consumed(conn, eid, timeout_s=0) is None


@pytest.mark.asyncio
async def test_poll_consumed_detects_terminal_status(db):
    async with db.pool.acquire() as conn:
        eid = await vl._emit(conn, "finding.composed", {"x": 1}, "verify-poll-2")
        await conn.execute("UPDATE events SET status='consumed' WHERE id=$1", eid)
        assert await vl._poll_consumed(conn, eid, timeout_s=0) == "consumed"


@pytest.mark.asyncio
async def test_emit_dedup_returns_none_on_conflict(db):
    async with db.pool.acquire() as conn:
        first = await vl._emit(conn, "planner.plan", {}, "verify-dup")
        second = await vl._emit(conn, "planner.plan", {}, "verify-dup")
    assert first is not None
    assert second is None  # ON CONFLICT DO NOTHING


@pytest.mark.asyncio
async def test_dry_run_reports_warn_without_a_consumer(db):
    async with db.pool.acquire() as conn:
        records = await vl.dry_run(conn, timeout_s=0)
    # every trigger stays pending (no harness) → ⚠, none ✗
    assert records
    assert all(r["mark"] in (vl._WARN, vl._DOT) for r in records)


# ---------------------------------------------------------------------------
# run() — the verdict / exit code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_clean_returns_zero(db):
    await _emit_event(db, "finding.composed", status="consumed")
    rc = await vl.run(since_min=60, dry=False, timeout_s=0)
    assert rc == 0


@pytest.mark.asyncio
async def test_run_flags_broken_step_returns_one(db):
    # a real (non-exempt) loop event dropped no_handler is a broken step
    await _emit_event(db, "finding.high_signal", status="suppressed", reason="no_handler")
    rc = await vl.run(since_min=60, dry=False, timeout_s=0)
    assert rc == 1
