"""DB-backed tests for state.client.PostgresClient — the Postgres substrate.

Run against a migrated pgvector DB (the `db` fixture in conftest skips otherwise;
CI provides one). Covers the core lab loop: company state, claim lifecycle, the
task queue, findings + evaluation/slop, and read helpers.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def _make_task(db, department: str = "research") -> int:
    """Insert a pending task directly (tasks are created by the planner, which
    has no PostgresClient method) and return its id."""
    return await db.pool.fetchval(
        "INSERT INTO tasks (department, task_type, description) VALUES ($1, 'execute', 't') RETURNING id",
        department,
    )


# ---- company state -------------------------------------------------------


async def test_get_company_state(db):
    cs = await db.get_company_state()
    assert cs.problem_statement == "test problem"
    assert cs.current_phase == "frame"
    assert cs.paused is False


# ---- claim lifecycle -----------------------------------------------------


async def test_create_and_get_claim(db):
    c = await db.create_claim("hypothesis X", initial_confidence=0.6)
    assert c.id > 0
    assert c.status == "proposed"
    assert c.confidence == 0.6
    got = await db.get_claim(c.id)
    assert got.statement == "hypothesis X"
    assert got.id == c.id


async def test_create_claim_rejects_bad_confidence(db):
    with pytest.raises(ValueError):
        await db.create_claim("bad", initial_confidence=1.5)


async def test_get_claim_missing_raises(db):
    with pytest.raises(ValueError):
        await db.get_claim(999_999)


async def test_create_claim_emits_event(db):
    c = await db.create_claim("with event")
    n = await db.pool.fetchval("SELECT count(*) FROM events WHERE event_type='claim.created' AND target_id=$1", c.id)
    assert n == 1


async def test_active_claims_sorted_and_counted(db):
    high = await db.create_claim("high", 0.9)
    low = await db.create_claim("low", 0.2)
    active = await db.get_active_claims()
    assert {c.id for c in active} == {high.id, low.id}
    assert active[0].id == high.id  # sort_by confidence DESC by default
    assert await db.count_active_claims() == 2


async def test_update_claim_confidence(db):
    c = await db.create_claim("c", 0.5)
    updated = await db.update_claim_confidence(c.id, 0.8, reason="new evidence")
    assert updated.confidence == 0.8
    assert updated.confidence_prev == 0.5


async def test_update_confidence_rejects_out_of_range(db):
    c = await db.create_claim("c", 0.5)
    with pytest.raises(ValueError):
        await db.update_claim_confidence(c.id, 2.0, reason="x")


async def test_invalidate_claim_idempotent(db):
    c = await db.create_claim("doomed", 0.5)
    vid = await db.pool.fetchval(
        "INSERT INTO critic_verdicts (thesis_id, verdict, confidence, reasoning, cited_finding_ids) "
        "VALUES ($1, 'kill', 0.9, 'refuted', '{}') RETURNING id",
        c.id,
    )
    inv = await db.invalidate_claim(c.id, reason="refuted by critic", verdict_id=vid)
    assert inv.status == "invalidated"
    assert inv.invalidation_reason == "refuted by critic"
    # idempotent: invalidating again returns the claim, still invalidated
    again = await db.invalidate_claim(c.id, reason="again", verdict_id=vid)
    assert again.status == "invalidated"
    # no longer counted active
    assert await db.count_active_claims() == 0


# ---- task queue ----------------------------------------------------------


async def test_claim_complete_task(db):
    tid = await _make_task(db)
    t = await db.claim_task("worker-1", "research")
    assert t.id == tid
    assert t.status == "running"
    # queue now empty for that department
    assert await db.claim_task("worker-1", "research") is None
    await db.complete_task(tid, {"ok": True})
    assert (await db.get_task(tid)).status == "completed"
    # completing a non-running task raises
    with pytest.raises(ValueError):
        await db.complete_task(tid, {})


async def test_claim_task_respects_department(db):
    await _make_task(db, department="research")
    assert await db.claim_task("w", "writing") is None  # wrong department


async def test_complete_task_emits_event(db):
    tid = await _make_task(db)
    await db.claim_task("w", "research")
    await db.complete_task(tid, {"r": 1})
    n = await db.pool.fetchval("SELECT count(*) FROM events WHERE event_type='task.completed' AND target_id=$1", tid)
    assert n == 1


async def test_fail_task(db):
    tid = await _make_task(db)
    await db.claim_task("w", "research")
    await db.fail_task(tid, "boom")
    assert (await db.get_task(tid)).status == "failed"


async def test_get_task_missing_raises(db):
    with pytest.raises(ValueError):
        await db.get_task(999_999)


# ---- findings + evaluation ----------------------------------------------


async def test_record_and_read_findings(db):
    c = await db.create_claim("c", 0.5)
    tid = await _make_task(db)
    fid = await db.record_finding(
        task_id=tid,
        source="web",
        title="T",
        summary="S",
        relevance_score=9,
        why_it_matters="W",
        claim_id=c.id,
        url="http://x",
    )
    f = await db.get_finding(fid)
    assert f.summary == "S"
    assert f.relevance_score == 9
    assert f.claim_id == c.id
    assert [x.id for x in await db.get_findings([fid])] == [fid]
    assert [x.id for x in await db.get_unaudited_findings_for_task(tid)] == [fid]
    assert [x.id for x in await db.get_recent_findings_for_claim(c.id)] == [fid]


async def test_record_finding_rejects_bad_relevance(db):
    tid = await _make_task(db)
    with pytest.raises(ValueError):
        await db.record_finding(
            task_id=tid,
            source="w",
            title="t",
            summary="s",
            relevance_score=11,
            why_it_matters="w",
        )


async def test_get_findings_empty(db):
    assert await db.get_findings([]) == []


async def test_update_finding_audit_pass_emits_high_signal(db):
    c = await db.create_claim("c", 0.5)
    tid = await _make_task(db)
    fid = await db.record_finding(
        task_id=tid,
        source="w",
        title="t",
        summary="s",
        relevance_score=9,
        why_it_matters="w",
        claim_id=c.id,
    )
    await db.update_finding_audit(fid, audit_score=0.9, audit_verdict="pass")
    f = await db.get_finding(fid)
    assert f.audit_verdict == "pass"
    assert float(f.audit_score) == 0.9
    n = await db.pool.fetchval(
        "SELECT count(*) FROM events WHERE event_type='finding.high_signal' AND target_id=$1", c.id
    )
    assert n == 1
    # re-auditing is a no-op (guarded by audit_verdict IS NULL)
    await db.update_finding_audit(fid, audit_score=0.1, audit_verdict="slop")
    assert (await db.get_finding(fid)).audit_verdict == "pass"


async def test_detect_slop_breaker(db):
    c = await db.create_claim("c", 0.5)
    tid = await _make_task(db)
    # 5 audited findings, 3 slop = 60% >= 40% threshold -> True
    for i in range(5):
        fid = await db.record_finding(
            task_id=tid,
            source="w",
            title="t",
            summary="s",
            relevance_score=5,
            why_it_matters="w",
            claim_id=c.id,
        )
        await db.update_finding_audit(fid, 0.1 if i < 3 else 0.9, "slop" if i < 3 else "pass")
    assert await db.detect_slop_breaker(c.id) is True


async def test_detect_slop_breaker_below_threshold(db):
    c = await db.create_claim("c", 0.5)
    tid = await _make_task(db)
    for _ in range(5):
        fid = await db.record_finding(
            task_id=tid,
            source="w",
            title="t",
            summary="s",
            relevance_score=5,
            why_it_matters="w",
            claim_id=c.id,
        )
        await db.update_finding_audit(fid, 0.9, "pass")
    assert await db.detect_slop_breaker(c.id) is False


# ---- known schema/code mismatch (documented, not yet fixed) --------------


@pytest.mark.xfail(
    strict=True,
    reason="create_critic_verdict inserts `claim_id`, but migration 008 renamed the "
    "adversary_verdicts table to critic_verdicts WITHOUT renaming its thesis_id "
    "column. Fix is a follow-up (rename the column to claim_id + match the code).",
)
async def test_create_critic_verdict_schema_mismatch(db):
    c = await db.create_claim("c", 0.5)
    await db.create_critic_verdict(claim_id=c.id, verdict="kill", confidence=0.9, reasoning="r", cited_finding_ids=[])
