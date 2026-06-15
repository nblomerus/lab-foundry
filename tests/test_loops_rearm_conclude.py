"""Re-arm + conclude-wall loop steps — DB-backed against the repointed SQL.

Two structural loop-closers from the agent consolidation, verified against a real (disposable) Postgres:

  * `loop_engine._guard_rearm_audit` — the Phase 3 repoint: re-arms the audit of unaudited
    *research_findings* (Aletheia via finding.composed), not the dead market-era findings/task path.
  * `persist_directions` supersession — the Phase 4 conclude-wall fix: a re-frame only supersedes
    UN-WORKED directions, so a direction with a completed experiment or a research_finding survives
    and can reach `concluded`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.ariadne import persist
from harness import loop_engine
from tests.test_ariadne_persist_reflect import _ariadne_out


async def _insert_direction(db, statement, *, status="tested") -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO claims (statement, claim_kind, status) VALUES ($1, 'direction', $2::claim_status) RETURNING id",
            statement,
            status,
        )


async def _insert_finding(db, claim_id, *, verdict=None, age_days=7) -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO research_findings (direction_claim_id, headline, claim_text, supported, "
            "confidence, audit_verdict, created_at) "
            "VALUES ($1, 'H', 'C', 'supported', 0.8, $2, now() - make_interval(days => $3)) RETURNING id",
            claim_id,
            verdict,
            age_days,
        )


# ---------------------------------------------------------------------------
# rearm_audit guard — re-issues finding.composed for stale unaudited findings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rearm_audit_returns_stale_unaudited_finding_on_active_direction(db):
    cid = await _insert_direction(db, "active dir")
    fid = await _insert_finding(db, cid, verdict=None)  # unaudited, 7d old, on an active direction
    async with db.pool.acquire() as conn:
        rows = await loop_engine._guard_rearm_audit(conn)
    assert {"claim_id": cid, "finding_id": fid} in rows


@pytest.mark.asyncio
async def test_rearm_audit_skips_already_audited_finding(db):
    cid = await _insert_direction(db, "audited dir")
    await _insert_finding(db, cid, verdict="pass")
    async with db.pool.acquire() as conn:
        rows = await loop_engine._guard_rearm_audit(conn)
    assert rows == []


@pytest.mark.asyncio
async def test_rearm_audit_skips_finding_on_terminal_direction(db):
    cid = await _insert_direction(db, "concluded dir", status="concluded")  # not an ACTIVE status
    await _insert_finding(db, cid, verdict=None)
    async with db.pool.acquire() as conn:
        rows = await loop_engine._guard_rearm_audit(conn)
    assert rows == []


@pytest.mark.asyncio
async def test_rearm_audit_skips_when_finding_composed_already_pending(db):
    cid = await _insert_direction(db, "in-flight dir")
    await _insert_finding(db, cid, verdict=None)
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO events (event_type, target_type, target_id, payload, status, dedup_key) "
            "VALUES ('finding.composed', 'claim', $1, '{}'::jsonb, 'pending', $2)",
            cid,
            f"fc-{cid}",
        )
        rows = await loop_engine._guard_rearm_audit(conn)
    assert rows == []  # the audit is already in flight — don't double-arm


@pytest.mark.asyncio
async def test_rearm_audit_skips_fresh_finding_inside_grace(db):
    cid = await _insert_direction(db, "fresh dir")
    await _insert_finding(db, cid, verdict=None, age_days=0)  # just composed — inside the grace window
    async with db.pool.acquire() as conn:
        rows = await loop_engine._guard_rearm_audit(conn)
    assert rows == []


# ---------------------------------------------------------------------------
# conclude-wall — a re-frame supersedes only UN-WORKED directions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_spares_worked_directions_and_invalidates_idle_ones(db, monkeypatch):
    # _coverage_score hits the corpus; the disposable DB has none — stub it.
    monkeypatch.setattr(persist, "_coverage_score", AsyncMock(return_value=None))

    worked_finding = await _insert_direction(db, "worked via finding")
    await _insert_finding(db, worked_finding, verdict=None)

    worked_experiment = await _insert_direction(db, "worked via experiment")
    async with db.pool.acquire() as conn:
        task_id = await conn.fetchval(
            "INSERT INTO tasks (department, task_type, description, claim_id) "
            "VALUES ('research', 'research', 'd', $1) RETURNING id",
            worked_experiment,
        )
        await conn.execute(
            "INSERT INTO experiment_runs (task_id, kind, params, status) "
            "VALUES ($1, 'benchmark', '{}'::jsonb, 'completed')",
            task_id,
        )

    idle = await _insert_direction(db, "idle direction")  # no experiments, no findings

    # Re-frame: persist a fresh deliberation. Only UN-WORKED actives should be superseded.
    await persist.persist_directions(db, _ariadne_out())

    async with db.pool.acquire() as conn:
        statuses = {
            r["id"]: r["status"]
            for r in await conn.fetch(
                "SELECT id, status::text AS status FROM claims WHERE id = ANY($1)",
                [worked_finding, worked_experiment, idle],
            )
        }
    assert statuses[worked_finding] != "invalidated"  # has a finding → survives
    assert statuses[worked_experiment] != "invalidated"  # has a completed experiment → survives
    assert statuses[idle] == "invalidated"  # un-worked → superseded by the re-frame
