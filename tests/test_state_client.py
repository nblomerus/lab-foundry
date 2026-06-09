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
        "INSERT INTO critic_verdicts (claim_id, verdict, confidence, reasoning, cited_finding_ids) "
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


# ---- critic verdicts -----------------------------------------------------


async def test_create_and_get_critic_verdict_round_trip(db):
    # Was xfail: critic_verdicts physically kept the legacy `thesis_id` column
    # while the code expected `claim_id`, so create+get both raised. Migration
    # 010 renamed the column; this now exercises the full write→read round-trip.
    c = await db.create_claim("c", 0.5)
    vid = await db.create_critic_verdict(
        claim_id=c.id,
        verdict="kill",
        confidence=0.9,
        reasoning="refuted by adversary",
        cited_finding_ids=[],
    )
    assert isinstance(vid, int)

    v = await db.get_critic_verdict(vid)
    assert v.id == vid
    assert v.claim_id == c.id
    assert v.verdict == "kill"
    assert float(v.confidence) == pytest.approx(0.9)
    assert v.reasoning == "refuted by adversary"
    assert v.cited_finding_ids == []


# ---- fetch cache ---------------------------------------------------------


async def test_fetch_cache_roundtrip(db):
    assert await db.fetch_cache_get("http://x/a") is None
    await db.fetch_cache_put(
        "http://x/a",
        content="hello",
        extractor="trafilatura",
        status_code=200,
        bytes_fetched=5,
        ttl_seconds=3600,
    )
    hit = await db.fetch_cache_get("http://x/a")
    assert hit is not None
    assert hit["content"] == "hello"
    assert hit["extractor"] == "trafilatura"


async def test_fetch_cache_upsert_overwrites(db):
    await db.fetch_cache_put("http://x/b", "v1", "e1", 200, 2, 3600)
    await db.fetch_cache_put("http://x/b", "v2", "e2", 500, 2, 3600)
    hit = await db.fetch_cache_get("http://x/b")
    assert hit["content"] == "v2"
    assert hit["status_code"] == 500


async def test_fetch_cache_expired_is_miss(db):
    # ttl 0 -> expires_at = now(), so it's already non-future -> a miss
    await db.fetch_cache_put("http://x/c", "stale", "e", 200, 5, 0)
    assert await db.fetch_cache_get("http://x/c") is None


# ---- research loop: inquiries / evidence / experiments -------------------


async def test_record_inquiry(db):
    tid = await _make_task(db)
    iid = await db.record_inquiry(
        task_id=tid,
        iteration=0,
        question="why?",
        sub_questions=[{"q": "sub1"}],
        proposed_experiments=[{"kind": "fetch_pricing"}],
    )
    assert iid > 0
    tree = await db.get_research_tree(tid)
    assert len(tree["inquiries"]) == 1
    assert tree["inquiries"][0]["question"] == "why?"
    assert tree["inquiries"][0]["sub_questions"] == [{"q": "sub1"}]


async def test_record_evidence_and_read(db):
    tid = await _make_task(db)
    eid = await db.record_evidence(
        task_id=tid,
        inquiry_id=None,
        sub_question_idx=0,
        url="http://e",
        quote="q",
        claim="c",
        stance="supports",
        confidence=0.7,
        title="T",
    )
    assert eid > 0
    rows = await db.get_evidence_for_task(tid)
    assert len(rows) == 1
    assert rows[0]["stance"] == "supports"
    assert rows[0]["url"] == "http://e"


async def test_record_evidence_rejects_bad_confidence(db):
    tid = await _make_task(db)
    with pytest.raises(ValueError):
        await db.record_evidence(
            task_id=tid,
            inquiry_id=None,
            sub_question_idx=0,
            url="u",
            quote="q",
            claim="c",
            stance="neutral",
            confidence=1.5,
        )


async def test_experiment_lifecycle(db):
    tid = await _make_task(db)
    xid = await db.start_experiment(task_id=tid, inquiry_id=None, kind="fetch_pricing", params={"url": "http://p"})
    running = await db.get_experiment_runs_for_task(tid)
    assert running[0]["status"] == "running"
    assert running[0]["params"] == {"url": "http://p"}
    await db.complete_experiment(xid, result={"price": 9.99}, interpretation="cheap")
    done = await db.get_experiment_runs_for_task(tid)
    assert done[0]["status"] == "completed"
    assert done[0]["result"] == {"price": 9.99}
    assert done[0]["interpretation"] == "cheap"


async def test_fail_experiment(db):
    tid = await _make_task(db)
    xid = await db.start_experiment(task_id=tid, inquiry_id=None, kind="k", params={})
    await db.fail_experiment(xid, "exploded")
    rows = await db.get_experiment_runs_for_task(tid)
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "exploded"


async def test_get_research_tree_assembles_everything(db):
    c = await db.create_claim("c", 0.5)
    tid = await db.pool.fetchval(
        "INSERT INTO tasks (department, task_type, description, claim_id) "
        "VALUES ('research', 'execute', 't', $1) RETURNING id",
        c.id,
    )
    await db.record_inquiry(task_id=tid, iteration=0, question="q", sub_questions=[], proposed_experiments=[])
    await db.record_evidence(
        task_id=tid,
        inquiry_id=None,
        sub_question_idx=0,
        url="u",
        quote="q",
        claim="c",
        stance="neutral",
        confidence=0.5,
    )
    await db.start_experiment(task_id=tid, inquiry_id=None, kind="k", params={})
    await db.record_finding(
        task_id=tid,
        source="w",
        title="t",
        summary="s",
        relevance_score=7,
        why_it_matters="w",
        claim_id=c.id,
    )
    tree = await db.get_research_tree(tid)
    assert tree["task"]["id"] == tid
    assert len(tree["inquiries"]) == 1
    assert len(tree["evidence"]) == 1
    assert len(tree["experiments"]) == 1
    assert len(tree["findings"]) == 1
    assert tree["agent_runs"] == []  # none recorded
