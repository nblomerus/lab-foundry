"""Verification loop (Phase 3) — step by step, DB-backed.

    finding.composed → Aletheia (evaluation) audits the research_finding → research_findings.audit_verdict
    → (confident pass) finding.high_signal targeting the DIRECTION → arms Momus (critic).

Each step is asserted against a real (disposable) Postgres via the `db` fixture; only the LLM
(curator.build + router.invoke) is mocked. This is the research-era reconnect that replaced the dead
market-era task.completed→findings path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.evaluation.handler import AuditScore, handle_finding_composed
from tests._helpers import make_dispatcher


async def _direction(db, statement="Quantized GPs vs XGBoost") -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO claims (statement, claim_kind, status) VALUES ($1, 'direction', 'tested') RETURNING id",
            statement,
        )


async def _finding(db, claim_id: int, *, confidence: float, supported="supported") -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO research_findings (direction_claim_id, headline, claim_text, supported, confidence) "
            "VALUES ($1, 'H', 'C', $2, $3) RETURNING id",
            claim_id,
            supported,
            confidence,
        )


def _dispatcher(db, *, audit_score: float):
    """A dispatcher whose LLM returns a fixed AuditScore; state is the real DB."""
    router = AsyncMock()
    router.invoke.return_value = (
        # finding_id is unused by the handler (it keys off the event payload) — the verdict is
        # re-derived from audit_score, so "pass" here is just a placeholder.
        AuditScore(finding_id=0, audit_score=audit_score, verdict="pass", reasoning="r"),
        None,  # run_id — router is mocked, so no agent_runs row exists to FK against
    )
    curator = AsyncMock()
    return make_dispatcher(state=db, router=router, curator=curator, session=None)


async def _high_signal_rows(db, claim_id: int):
    async with db.pool.acquire() as conn:
        return await conn.fetch(
            "SELECT payload, emitted_by_run_id FROM events "
            "WHERE event_type='finding.high_signal' AND target_type='claim' AND target_id=$1",
            claim_id,
        )


@pytest.mark.asyncio
async def test_confident_pass_audits_and_arms_critic(db):
    cid = await _direction(db)
    fid = await _finding(db, cid, confidence=0.8)  # confident finding
    disp = _dispatcher(db, audit_score=0.85)  # score ≥ 0.7 → verdict pass

    out = await handle_finding_composed({"id": 1, "payload": {"finding_id": fid, "claim_id": cid}}, disp)

    # step: handler returns the verdict it derived from the score
    assert out["verdict"] == "pass"
    assert out["finding_id"] == fid

    # step: the research_finding now carries the audit verdict + score
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT audit_verdict, audit_score FROM research_findings WHERE id=$1", fid)
    assert row["audit_verdict"] == "pass"
    assert abs(float(row["audit_score"]) - 0.85) < 1e-6

    # step: a confident pass arms Momus — finding.high_signal targets the DIRECTION claim
    hs = await _high_signal_rows(db, cid)
    assert len(hs) == 1
    payload = hs[0]["payload"]
    payload = payload if isinstance(payload, dict) else __import__("json").loads(payload)
    assert payload["finding_id"] == fid
    assert payload["research"] is True
    assert payload["score"] == pytest.approx(8.0)  # confidence 0.8 × 10


@pytest.mark.asyncio
async def test_slop_audit_records_verdict_but_does_not_arm_critic(db):
    cid = await _direction(db)
    fid = await _finding(db, cid, confidence=0.8)
    disp = _dispatcher(db, audit_score=0.1)  # score < 0.3 → verdict slop

    out = await handle_finding_composed({"id": 2, "payload": {"finding_id": fid, "claim_id": cid}}, disp)
    assert out["verdict"] == "slop"

    async with db.pool.acquire() as conn:
        verdict = await conn.fetchval("SELECT audit_verdict FROM research_findings WHERE id=$1", fid)
    assert verdict == "slop"
    assert await _high_signal_rows(db, cid) == []  # slop never challenges


@pytest.mark.asyncio
async def test_pass_but_low_confidence_finding_is_not_challenged(db):
    cid = await _direction(db)
    fid = await _finding(db, cid, confidence=0.5)  # below the 0.7 challenge floor
    disp = _dispatcher(db, audit_score=0.9)  # passes the audit…

    out = await handle_finding_composed({"id": 3, "payload": {"finding_id": fid, "claim_id": cid}}, disp)
    assert out["verdict"] == "pass"
    # …but the lab isn't confident enough to spend Momus on it
    assert await _high_signal_rows(db, cid) == []


@pytest.mark.asyncio
async def test_already_audited_finding_is_a_noop(db):
    cid = await _direction(db)
    fid = await _finding(db, cid, confidence=0.8)
    disp = _dispatcher(db, audit_score=0.85)
    await handle_finding_composed({"id": 4, "payload": {"finding_id": fid, "claim_id": cid}}, disp)

    # second delivery (re-arm / duplicate) must not double-audit or double-arm
    out2 = await handle_finding_composed({"id": 5, "payload": {"finding_id": fid, "claim_id": cid}}, disp)
    assert out2["skipped"] is True
    assert len(await _high_signal_rows(db, cid)) == 1  # still exactly one challenge


@pytest.mark.asyncio
async def test_missing_finding_id_is_skipped(db):
    disp = _dispatcher(db, audit_score=0.9)
    out = await handle_finding_composed({"id": 6, "payload": {}}, disp)
    assert out["skipped"] is True
    disp.router.invoke.assert_not_awaited()  # never reached the LLM
