"""DB-backed tests for the sandboxed-experiment + Quartermaster lifecycle on
state.client.PostgresClient.

These exercise the real SQL added in migration 011 (queued/running/killed
status set, code/budgets/provenance columns, heartbeats, resource_usage,
interpretation + ingested-doc persistence) against a migrated pgvector DB. The
`db` fixture (conftest) skips when no DATABASE_URL is reachable — exactly like
tests/test_state_client.py — and truncates per test, so each starts empty with
predictable ids.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def _make_claim_and_task(db, department: str = "research") -> tuple[int, int]:
    """Create a claim, then a task FK'd to it (the planner inserts tasks directly;
    there's no PostgresClient method). Returns (claim_id, task_id)."""
    c = await db.create_claim("hypothesis under test", initial_confidence=0.5)
    task_id = await db.pool.fetchval(
        "INSERT INTO tasks (department, task_type, description, claim_id) VALUES ($1, 'execute', 't', $2) RETURNING id",
        department,
        c.id,
    )
    return c.id, task_id


async def _make_document(db) -> int:
    """A real documents row so set_experiment_ingested_doc's FK is satisfied."""
    doc_id, _is_new = await db.upsert_document(
        kind="note",
        source_kind="experiment",
        canonical_key="exp-doc-1",
        title="experiment writeup",
    )
    return doc_id


# ---- queue + read back ---------------------------------------------------


async def test_queue_experiment_persists_all_fields_and_is_queued(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(
        task_id=task_id,
        inquiry_id=None,
        kind="code",
        params={"seed": 7},
        code="print('hi')",
        wall_clock_budget_s=120,
        mem_budget_mb=512,
        requires_gpu=True,
        gpu_mem_mb=8000,
        priority=9,
        provenance={"image": "sha256:abc", "code_hash": "deadbeef"},
        dataset_refs=[{"hash": "h1"}],
    )
    assert xid > 0

    exp = await db.get_experiment(xid)
    assert exp["status"] == "queued"
    assert exp["code"] == "print('hi')"
    assert exp["wall_clock_budget_s"] == 120
    assert exp["mem_budget_mb"] == 512
    assert exp["requires_gpu"] is True
    assert exp["gpu_mem_mb"] == 8000
    assert exp["priority"] == 9
    # jsonb columns round-trip to dicts/lists via _parse_experiment_row
    assert exp["params"] == {"seed": 7}
    assert exp["provenance"] == {"image": "sha256:abc", "code_hash": "deadbeef"}
    assert exp["dataset_refs"] == [{"hash": "h1"}]


async def test_queue_experiment_defaults_and_null_optionals(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={})
    exp = await db.get_experiment(xid)
    # migration 011 column defaults
    assert exp["status"] == "queued"
    assert exp["code"] is None
    assert exp["wall_clock_budget_s"] == 600
    assert exp["mem_budget_mb"] == 2048
    assert exp["requires_gpu"] is False
    assert exp["gpu_mem_mb"] is None
    assert exp["priority"] == 5
    assert exp["provenance"] is None
    assert exp["dataset_refs"] is None
    assert exp["params"] == {}


async def test_get_queued_experiments_ordered_by_priority_desc(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    low = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={}, priority=1)
    high = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={}, priority=9)
    mid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={}, priority=5)

    queued = await db.get_queued_experiments()
    assert [e["id"] for e in queued] == [high, mid, low]
    assert all(e["status"] == "queued" for e in queued)


async def test_get_experiment_missing_returns_none(db):
    assert await db.get_experiment(999_999) is None


# ---- claim the slot ------------------------------------------------------


async def test_mark_experiment_running_claims_once(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={})

    assert await db.mark_experiment_running(xid, "lf-exp-1") is True
    exp = await db.get_experiment(xid)
    assert exp["status"] == "running"
    assert exp["worker"] == "lf-exp-1"

    running = await db.get_running_experiments()
    assert [e["id"] for e in running] == [xid]

    # second claim loses the race (already 'running', not 'queued')
    assert await db.mark_experiment_running(xid, "lf-exp-2") is False
    assert (await db.get_experiment(xid))["worker"] == "lf-exp-1"


async def test_get_running_experiments_empty(db):
    assert await db.get_running_experiments() == []


# ---- heartbeat + code rewrite -------------------------------------------


async def test_heartbeat_experiment_sets_heartbeat_at(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={})
    assert (await db.get_experiment(xid))["heartbeat_at"] is None
    await db.heartbeat_experiment(xid)
    assert (await db.get_experiment(xid))["heartbeat_at"] is not None


async def test_update_experiment_code_overwrites(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={}, code="v1")
    await db.update_experiment_code(xid, "v2", provenance={"code_hash": "new"})
    exp = await db.get_experiment(xid)
    assert exp["code"] == "v2"
    assert exp["provenance"] == {"code_hash": "new"}


async def test_update_experiment_code_keeps_provenance_when_none(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(
        task_id=task_id, inquiry_id=None, kind="code", params={}, code="v1", provenance={"orig": True}
    )
    # COALESCE($3, provenance) keeps the existing provenance when None is passed
    await db.update_experiment_code(xid, "v2")
    exp = await db.get_experiment(xid)
    assert exp["code"] == "v2"
    assert exp["provenance"] == {"orig": True}


# ---- record result -------------------------------------------------------


async def test_record_experiment_result_completed(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={})
    await db.mark_experiment_running(xid, "lf-exp-1")
    await db.record_experiment_result(
        xid,
        status="completed",
        result={"accuracy": 0.91},
        resource_usage={"peak_mem_mb": 480, "exit_code": 0},
    )
    exp = await db.get_experiment(xid)
    assert exp["status"] == "completed"
    assert exp["result"] == {"accuracy": 0.91}
    assert exp["resource_usage"] == {"peak_mem_mb": 480, "exit_code": 0}
    assert exp["completed_at"] is not None
    # error stays NULL (COALESCE leaves it untouched when None)
    assert exp["error"] is None


async def test_record_experiment_result_failed_with_error(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={})
    await db.record_experiment_result(xid, status="failed", error="boom traceback")
    exp = await db.get_experiment(xid)
    assert exp["status"] == "failed"
    assert exp["error"] == "boom traceback"
    assert exp["result"] is None


# ---- kill ----------------------------------------------------------------


async def test_kill_experiment_sets_killed_and_reason(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={})
    await db.mark_experiment_running(xid, "lf-exp-1")
    await db.kill_experiment(xid, "wall-clock budget exceeded")
    exp = await db.get_experiment(xid)
    assert exp["status"] == "killed"
    assert exp["kill_reason"] == "wall-clock budget exceeded"
    assert exp["killed_at"] is not None


async def test_kill_experiment_noop_when_terminal(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={})
    await db.record_experiment_result(xid, status="completed", result={"ok": True})
    # guard: only kills rows in ('running', 'queued') — a completed row is untouched
    await db.kill_experiment(xid, "too late")
    exp = await db.get_experiment(xid)
    assert exp["status"] == "completed"
    assert exp["kill_reason"] is None


# ---- interpretation + ingested doc --------------------------------------


async def test_set_experiment_interpretation_persists(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={})
    await db.set_experiment_interpretation(
        xid,
        interpretation="the model overfits",
        interpret_run_id=None,
        researcher_notes="tried 3 seeds, all diverged",
    )
    exp = await db.get_experiment(xid)
    assert exp["interpretation"] == "the model overfits"
    assert exp["researcher_notes"] == "tried 3 seeds, all diverged"


async def test_set_experiment_ingested_doc_sets_fk(db):
    _claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={})
    doc_id = await _make_document(db)
    await db.set_experiment_ingested_doc(xid, doc_id)
    assert (await db.get_experiment(xid))["ingested_doc_id"] == doc_id


# ---- recent notes for Ariadne -------------------------------------------


async def test_get_recent_experiment_notes_for_claims(db):
    claim_id, task_id = await _make_claim_and_task(db)
    xid = await db.queue_experiment(task_id=task_id, inquiry_id=None, kind="code", params={})
    # no notes yet → the claim's experiment is filtered out
    assert await db.get_recent_experiment_notes_for_claims([claim_id]) == []

    await db.set_experiment_interpretation(xid, interpretation="signal found", researcher_notes="held up across seeds")
    notes = await db.get_recent_experiment_notes_for_claims([claim_id])
    assert len(notes) == 1
    row = notes[0]
    assert row["claim_id"] == claim_id
    assert row["experiment_id"] == xid
    assert row["kind"] == "code"
    assert row["researcher_notes"] == "held up across seeds"
    assert row["interpretation"] == "signal found"


async def test_get_recent_experiment_notes_for_empty_list(db):
    # the empty-input fast path: returns [] without touching the DB
    assert await db.get_recent_experiment_notes_for_claims([]) == []
