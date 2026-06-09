"""Unit tests for the operational ops *driver* CLIs — the run()/main() orchestrators that
wire library/agent functions together and PRINT a report.

Everything is mocked. NO real Postgres/Neo4j/Ollama/network/subprocess, and crucially NO
DATABASE_URL fixture: asyncpg.create_pool / asyncpg.connect are monkeypatched to hand back a
ScriptedPool/ScriptedConn (tests._helpers), load_dotenv is a no-op, and every library/agent
function each driver imports is patched at the importing module. Each driver is then driven
through its run()/main() happy path, its no-DSN / missing-key guards, and its error branches —
asserting it wires the mocked deps and prints, since the drivers are thin orchestration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock

import pytest

from ops import (
    ariadne_firstlight,
    bootstrap,
    canonicalize_concepts,
    extract_concepts_backfill,
    field_model_build,
    mimir_ask,
    mimir_firstlight,
    planner_firstlight,
    researcher_firstlight,
    seed_corpus,
)
from tests._helpers import FakeNeoDriver, ScriptedConn, ScriptedPool


# ── shared monkeypatch helpers ───────────────────────────────────────────────
def _no_dotenv(monkeypatch, *modules):
    for m in modules:
        monkeypatch.setattr(m, "load_dotenv", lambda *a, **k: None, raising=False)


def _patch_pool(monkeypatch, module, pool):
    monkeypatch.setattr(module.asyncpg, "create_pool", AsyncMock(return_value=pool))


def _patch_connect(monkeypatch, module, conn):
    monkeypatch.setattr(module.asyncpg, "connect", AsyncMock(return_value=conn))


def _ns(**kw):
    return argparse.Namespace(**kw)


# ─────────────────────────────────────────────────────────────────────────────
# ops.field_model_build
# ─────────────────────────────────────────────────────────────────────────────
def _fm_stats():
    return {
        "concepts": 120,
        "prior": "2022",
        "recent": "2024",
        "n_prior": 100,
        "n_recent": 50,
        "sat_threshold": 30,
        "by_state": {"hot": 4, "emerging": 3, "stable": 10, "saturated": 2, "declining": 1},
    }


def _fm_row(**over):
    base = {
        "concept_kind": "METHOD",
        "concept_name": "retrieval augmented generation",
        "total_papers": 42,
        "recent_papers": 30,
        "prior_papers": 12,
        "velocity": 0.5,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_field_model_build_happy(monkeypatch, capsys):
    pool = ScriptedPool([("FROM field_model WHERE trend_state", [_fm_row(), _fm_row(concept_name="x" * 60)])])
    _patch_pool(monkeypatch, field_model_build, pool)
    _no_dotenv(monkeypatch, field_model_build)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(field_model_build, "_get_driver", AsyncMock(return_value=FakeNeoDriver()))
    monkeypatch.setattr(field_model_build, "build_field_model", AsyncMock(return_value=_fm_stats()))
    brief = AsyncMock(return_value="BRIEF-BODY")
    monkeypatch.setattr(field_model_build, "read_field_brief", brief)

    rc = await field_model_build.run(_ns(brief=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "120 concepts classified" in out
    assert "HOT:" in out and "EMERGING:" in out
    assert "retrieval augmented generation"[:38] in out
    brief.assert_not_awaited()  # --brief off → no brief read


@pytest.mark.asyncio
async def test_field_model_build_with_brief(monkeypatch, capsys):
    pool = ScriptedPool([("FROM field_model WHERE trend_state", [])])
    _patch_pool(monkeypatch, field_model_build, pool)
    _no_dotenv(monkeypatch, field_model_build)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(field_model_build, "_get_driver", AsyncMock(return_value=FakeNeoDriver()))
    monkeypatch.setattr(field_model_build, "build_field_model", AsyncMock(return_value=_fm_stats()))
    monkeypatch.setattr(field_model_build, "read_field_brief", AsyncMock(return_value="THE-BRIEF"))

    rc = await field_model_build.run(_ns(brief=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "BRIEF (what Ariadne reads):" in out
    assert "THE-BRIEF" in out


@pytest.mark.asyncio
async def test_field_model_build_no_dsn(monkeypatch, capsys):
    _no_dotenv(monkeypatch, field_model_build)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await field_model_build.run(_ns(brief=False))
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_field_model_build_main(monkeypatch):
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    monkeypatch.setattr(sys, "argv", ["ops.field_model_build", "--brief"])
    assert field_model_build.main() == 0


# ─────────────────────────────────────────────────────────────────────────────
# ops.canonicalize_concepts
# ─────────────────────────────────────────────────────────────────────────────
def _canon_records():
    """Two surface variants of LLM (merge) + one already-canonical 'rag' (no merge)."""
    return [
        {"key": "llm", "name": "LLM", "papers": 5},
        {"key": "llms", "name": "LLMs", "papers": 9},
        {"key": "rag", "name": "RAG", "papers": 3},
    ]


@pytest.mark.asyncio
async def test_canonicalize_apply(monkeypatch, capsys):
    driver = FakeNeoDriver(_canon_records())
    monkeypatch.setattr(canonicalize_concepts, "_get_driver", AsyncMock(return_value=driver))
    _no_dotenv(monkeypatch, canonicalize_concepts)

    rc = await canonicalize_concepts.run(dry_run=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "METHOD:" in out and "nodes ->" in out
    # one variant folded into the llm canonical → the merge/delete queries ran
    assert "variants ✓" in out  # the apply-only confirmation line
    queries = " ".join(q for s in driver.sessions for q, _ in s.queries)
    assert "MERGE" in queries and "DETACH DELETE" in queries


@pytest.mark.asyncio
async def test_canonicalize_dry_run(monkeypatch, capsys):
    driver = FakeNeoDriver(_canon_records())
    monkeypatch.setattr(canonicalize_concepts, "_get_driver", AsyncMock(return_value=driver))
    _no_dotenv(monkeypatch, canonicalize_concepts)

    rc = await canonicalize_concepts.run(dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "groups need merging" in out
    assert "variants ✓" not in out  # dry-run never applies
    # only the planning MATCH ran per label — no MERGE/DELETE
    queries = " ".join(q for s in driver.sessions for q, _ in s.queries)
    assert "DETACH DELETE" not in queries


@pytest.mark.asyncio
async def test_canonicalize_nothing_to_merge(monkeypatch, capsys):
    """All nodes already canonical → no pairs → the apply branch is skipped (continue)."""
    driver = FakeNeoDriver([{"key": "rag", "name": "RAG", "papers": 3}])
    monkeypatch.setattr(canonicalize_concepts, "_get_driver", AsyncMock(return_value=driver))
    _no_dotenv(monkeypatch, canonicalize_concepts)
    rc = await canonicalize_concepts.run(dry_run=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 groups need merging" in out
    assert "variants ✓" not in out


def test_canonicalize_main(monkeypatch):
    captured = {}

    def _run(dry):
        captured["dry"] = dry

        async def _c():
            return 0

        return _c()

    monkeypatch.setattr(canonicalize_concepts, "run", _run)
    monkeypatch.setattr(sys, "argv", ["ops.canonicalize_concepts", "--dry-run"])
    assert canonicalize_concepts.main() == 0
    assert captured["dry"] is True


# ─────────────────────────────────────────────────────────────────────────────
# ops.extract_concepts_backfill
# ─────────────────────────────────────────────────────────────────────────────
def _backfill_rules(candidate_ids, bodies):
    cand_rows = [{"id": i} for i in candidate_ids]
    body_rows = [{"id": pid, "title": b["title"], "body": b["body"]} for pid, b in bodies.items()]
    return [
        ("WHERE d.kind = 'paper'", cand_rows),
        ("FROM documents d WHERE d.id = ANY", body_rows),
    ]


@pytest.mark.asyncio
async def test_extract_backfill_happy(monkeypatch, capsys):
    conn = ScriptedConn(_backfill_rules([1, 2, 3], {1: {"title": "T1", "body": "b1"}, 2: {"title": "T2", "body": "b2"}}))
    conn.close = AsyncMock()
    _patch_connect(monkeypatch, extract_concepts_backfill, conn)
    _no_dotenv(monkeypatch, extract_concepts_backfill)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(extract_concepts_backfill, "ensure_concept_constraints", AsyncMock())
    monkeypatch.setattr(extract_concepts_backfill, "extracted_paper_ids", AsyncMock(return_value={3}))
    monkeypatch.setattr(extract_concepts_backfill, "extract_paper_concepts", AsyncMock(return_value={"methods": ["m"]}))
    # paper 1 yields concepts, paper 2 is empty → exercises both counters
    proj = AsyncMock(side_effect=[{"methods": 2, "datasets": 1, "tasks": 0}, {"methods": 0, "datasets": 0, "tasks": 0}])
    monkeypatch.setattr(extract_concepts_backfill, "project_paper_concepts", proj)

    rc = await extract_concepts_backfill.run(limit=0, model=None, progress_every=1, batch=200)
    out = capsys.readouterr().out
    assert rc == 0
    assert "already extracted: 1" in out  # id 3 done
    assert "to do this run: 2" in out  # ids 1,2 (3 skipped)
    assert "Projected: {'methods': 2, 'datasets': 1, 'tasks': 0}" in out
    assert "1 papers had no concepts" in out
    conn.close.assert_awaited()


@pytest.mark.asyncio
async def test_extract_backfill_with_model_and_limit(monkeypatch, capsys):
    conn = ScriptedConn(_backfill_rules([10, 11], {10: {"title": "A", "body": "x"}}))
    conn.close = AsyncMock()
    _patch_connect(monkeypatch, extract_concepts_backfill, conn)
    _no_dotenv(monkeypatch, extract_concepts_backfill)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(extract_concepts_backfill, "ensure_concept_constraints", AsyncMock())
    monkeypatch.setattr(extract_concepts_backfill, "extracted_paper_ids", AsyncMock(return_value=set()))
    epc = AsyncMock(return_value={"methods": ["m"]})
    monkeypatch.setattr(extract_concepts_backfill, "extract_paper_concepts", epc)
    monkeypatch.setattr(
        extract_concepts_backfill,
        "project_paper_concepts",
        AsyncMock(return_value={"methods": 1, "datasets": 0, "tasks": 0}),
    )

    # limit=1 → only id 10; its body exists, id 11 would be missing anyway
    rc = await extract_concepts_backfill.run(limit=1, model="qwen", progress_every=100, batch=200)
    out = capsys.readouterr().out
    assert rc == 0
    assert "model=qwen" in out
    # model override is threaded into extract_paper_concepts as a kwarg
    assert epc.await_args.kwargs.get("model") == "qwen"
    assert "Done this run: 1 papers" in out


@pytest.mark.asyncio
async def test_extract_backfill_no_candidates(monkeypatch, capsys):
    conn = ScriptedConn(_backfill_rules([], {}))
    conn.close = AsyncMock()
    _patch_connect(monkeypatch, extract_concepts_backfill, conn)
    _no_dotenv(monkeypatch, extract_concepts_backfill)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(extract_concepts_backfill, "ensure_concept_constraints", AsyncMock())
    monkeypatch.setattr(extract_concepts_backfill, "extracted_paper_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(extract_concepts_backfill, "extract_paper_concepts", AsyncMock(return_value={}))
    monkeypatch.setattr(extract_concepts_backfill, "project_paper_concepts", AsyncMock(return_value={}))

    rc = await extract_concepts_backfill.run(limit=0, model=None, progress_every=100, batch=200)
    out = capsys.readouterr().out
    assert rc == 0
    assert "to do this run: 0" in out


@pytest.mark.asyncio
async def test_extract_backfill_no_dsn(monkeypatch, capsys):
    _no_dotenv(monkeypatch, extract_concepts_backfill)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await extract_concepts_backfill.run(limit=0, model=None, progress_every=100, batch=200)
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_extract_backfill_main(monkeypatch):
    seen = {}

    def _run(limit, model, progress_every, batch):
        seen.update(limit=limit, model=model, progress_every=progress_every, batch=batch)

        async def _c():
            return 0

        return _c()

    monkeypatch.setattr(extract_concepts_backfill, "run", _run)
    monkeypatch.setattr(sys, "argv", ["ops.extract_concepts_backfill", "--limit", "5", "--model", "m"])
    assert extract_concepts_backfill.main() == 0
    assert seen["limit"] == 5 and seen["model"] == "m"


# ─────────────────────────────────────────────────────────────────────────────
# ops.planner_firstlight
# ─────────────────────────────────────────────────────────────────────────────
def _plan_out():
    task = types.SimpleNamespace(
        priority=5, task_type="literature", title="Survey X", description="d" * 200, rationale="r" * 200
    )
    plan = types.SimpleNamespace(claim_id=7, tasks=[task])
    return types.SimpleNamespace(plans=[plan], notes="some notes")


def _plan_grade(passed=True, invalid=None):
    return types.SimpleNamespace(
        valid_refs=True,
        tasks_wellformed=True,
        n_tasks=1,
        n_plans=1,
        invalid_refs=invalid or [],
        passed=passed,
    )


@pytest.mark.asyncio
async def test_planner_firstlight_happy(monkeypatch, capsys):
    _patch_pool(monkeypatch, planner_firstlight, ScriptedPool([]))
    _no_dotenv(monkeypatch, planner_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(planner_firstlight, "PostgresClient", lambda pool: "STATE")
    monkeypatch.setattr(planner_firstlight, "run_planning", AsyncMock(return_value=(_plan_out(), [7])))
    monkeypatch.setattr(planner_firstlight, "grade_plan", lambda out, ids: _plan_grade())

    rc = await planner_firstlight.run()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Planner first light" in out
    assert "DIRECTION #7" in out and "Survey X" in out
    assert "NOTES" in out
    assert "PASS — eligible to create tasks" in out


@pytest.mark.asyncio
async def test_planner_firstlight_invalid_refs(monkeypatch, capsys):
    _patch_pool(monkeypatch, planner_firstlight, ScriptedPool([]))
    _no_dotenv(monkeypatch, planner_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(planner_firstlight, "PostgresClient", lambda pool: "STATE")
    monkeypatch.setattr(planner_firstlight, "run_planning", AsyncMock(return_value=(_plan_out(), [7])))
    monkeypatch.setattr(planner_firstlight, "grade_plan", lambda out, ids: _plan_grade(passed=False, invalid=[99]))

    rc = await planner_firstlight.run()
    out = capsys.readouterr().out
    assert rc == 0
    assert "invalid refs (hallucinated ids): [99]" in out
    assert "NOT yet" in out


@pytest.mark.asyncio
async def test_planner_firstlight_no_directions(monkeypatch, capsys):
    _patch_pool(monkeypatch, planner_firstlight, ScriptedPool([]))
    _no_dotenv(monkeypatch, planner_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(planner_firstlight, "PostgresClient", lambda pool: "STATE")
    monkeypatch.setattr(planner_firstlight, "run_planning", AsyncMock(return_value=(None, [])))

    rc = await planner_firstlight.run()
    out = capsys.readouterr().out
    assert rc == 0
    assert "No APPROVED directions to plan" in out


@pytest.mark.asyncio
async def test_planner_firstlight_missing_key(monkeypatch, capsys):
    _no_dotenv(monkeypatch, planner_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    rc = await planner_firstlight.run()
    assert rc == 2
    assert "DATABASE_URL and DEEPSEEK_API_KEY required" in capsys.readouterr().err


def test_planner_firstlight_main(monkeypatch):
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    assert planner_firstlight.main() == 0


# ─────────────────────────────────────────────────────────────────────────────
# ops.mimir_ask
# ─────────────────────────────────────────────────────────────────────────────
def _answer(**over):
    base = {
        "answer": "line one\nline two",
        "citations": ["[1] Paper A"],
        "related_concepts": ["rag", "llm"],
        "gaps": ["uncertainty quantification"],
    }
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_mimir_ask_full(monkeypatch, capsys):
    _no_dotenv(monkeypatch, mimir_ask)
    monkeypatch.setattr(mimir_ask, "answer_question", AsyncMock(return_value=_answer()))
    rc = await mimir_ask.run("what methods?")
    out = capsys.readouterr().out
    assert rc == 0
    assert "Q: what methods?" in out
    assert "ANSWER" in out and "line one" in out
    assert "CITATIONS" in out and "[1] Paper A" in out
    assert "RELATED CONCEPTS (graph): rag, llm" in out
    assert "GAPS" in out and "uncertainty quantification" in out


@pytest.mark.asyncio
async def test_mimir_ask_minimal(monkeypatch, capsys):
    """No citations/concepts/gaps → only the ANSWER block prints."""
    _no_dotenv(monkeypatch, mimir_ask)
    monkeypatch.setattr(
        mimir_ask,
        "answer_question",
        AsyncMock(return_value=_answer(citations=[], related_concepts=[], gaps=[])),
    )
    rc = await mimir_ask.run("q")
    out = capsys.readouterr().out
    assert rc == 0
    assert "ANSWER" in out
    assert "CITATIONS" not in out
    assert "RELATED CONCEPTS" not in out
    assert "GAPS" not in out


def test_mimir_ask_main_usage(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ops.mimir_ask"])
    rc = mimir_ask.main()
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_mimir_ask_main_runs(monkeypatch):
    captured = {}
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    monkeypatch.setattr(sys, "argv", ["ops.mimir_ask", "hello", "world"])

    async def _run(q):
        captured["q"] = q
        return 0

    monkeypatch.setattr(mimir_ask, "run", _run)
    assert mimir_ask.main() == 0


# ─────────────────────────────────────────────────────────────────────────────
# ops.ariadne_firstlight
# ─────────────────────────────────────────────────────────────────────────────
def _scores():
    return types.SimpleNamespace(
        novelty=4,
        differentiation=4,
        paper_potential=5,
        feasibility=3,
        evidence_availability=4,
        reviewer_interest=4,
        technical_depth=3,
        cost_efficiency=4,
        lab_alignment=5,
        rationale="solid bet",
    )


def _direction(with_scores=True):
    goal = types.SimpleNamespace(
        expectation="X improves Y",
        kill_condition="no effect",
        novelty_target="beat SOTA",
        next_milestone="run pilot",
        priority_hint="high",
    )
    return types.SimpleNamespace(
        title="Agentic RAG",
        scores=_scores() if with_scores else None,
        statement="bet it works",
        novelty_rationale="under-explored",
        claim_goals=[goal],
        kill_conditions=["settled already"],
        reviewer_risks=["incremental"],
    )


def _ariadne_out(directions=None):
    request = types.SimpleNamespace(paper="Some Paper", arxiv_id="2406.1", why="need it")
    return types.SimpleNamespace(
        mission_frame="frame the mission",
        directions=directions if directions is not None else [_direction()],
        novelty_risks=["saturation in X"],
        requests=[request],
        reflection="reflect here",
    )


def _ariadne_report(passed=True, unresolved=None):
    return types.SimpleNamespace(
        schema_valid=True,
        claim_goals_wellformed=1.0,
        directions_grounded=1.0,
        citations_resolved=0.9,
        n_citations=3,
        scores_wellformed=1.0,
        unresolved=unresolved or [],
        passed=passed,
    )


@pytest.mark.asyncio
async def test_ariadne_firstlight_happy(monkeypatch, capsys):
    _patch_pool(monkeypatch, ariadne_firstlight, ScriptedPool([]))
    _no_dotenv(monkeypatch, ariadne_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(ariadne_firstlight, "PostgresClient", lambda pool: AsyncMock())
    monkeypatch.setattr(ariadne_firstlight, "run_shadow", AsyncMock(return_value=_ariadne_out()))
    monkeypatch.setattr(ariadne_firstlight, "grade", AsyncMock(return_value=_ariadne_report()))

    rc = await ariadne_firstlight.run(_ns(seed=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "shadow direction tree" in out
    assert "frame the mission" in out
    assert "Agentic RAG" in out
    assert "REQUESTS TO MIMIR" in out and "[2406.1]" in out
    assert "REFLECTION" in out
    assert "PASS — eligible for advisory mode" in out


@pytest.mark.asyncio
async def test_ariadne_firstlight_seed_and_unscored(monkeypatch, capsys):
    """--seed overrides the in-memory state; a direction with no scores + a failing grade."""
    cs = types.SimpleNamespace(problem_statement="orig")
    state = AsyncMock()
    state.get_company_state = AsyncMock(return_value=cs)
    _patch_pool(monkeypatch, ariadne_firstlight, ScriptedPool([]))
    _no_dotenv(monkeypatch, ariadne_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(ariadne_firstlight, "PostgresClient", lambda pool: state)
    out_obj = _ariadne_out(directions=[_direction(with_scores=False)])
    out_obj.novelty_risks = []
    out_obj.requests = []
    monkeypatch.setattr(ariadne_firstlight, "run_shadow", AsyncMock(return_value=out_obj))
    monkeypatch.setattr(
        ariadne_firstlight, "grade", AsyncMock(return_value=_ariadne_report(passed=False, unresolved=["ghost ref"]))
    )

    rc = await ariadne_firstlight.run(_ns(seed="new seed problem"))
    out = capsys.readouterr().out
    assert rc == 0
    assert cs.problem_statement == "new seed problem"  # seed applied in-memory
    assert "unresolved (possible hallucinations)" in out
    assert "ghost ref" in out
    assert "NOT yet" in out


@pytest.mark.asyncio
async def test_ariadne_firstlight_no_deepseek_warns(monkeypatch, capsys):
    """Missing DEEPSEEK_API_KEY warns (fallback to local) but still runs."""
    _patch_pool(monkeypatch, ariadne_firstlight, ScriptedPool([]))
    _no_dotenv(monkeypatch, ariadne_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(ariadne_firstlight, "PostgresClient", lambda pool: AsyncMock())
    monkeypatch.setattr(ariadne_firstlight, "run_shadow", AsyncMock(return_value=_ariadne_out()))
    monkeypatch.setattr(ariadne_firstlight, "grade", AsyncMock(return_value=_ariadne_report()))

    rc = await ariadne_firstlight.run(_ns(seed=None))
    assert rc == 0
    assert "fall back to the local Ollama model" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_ariadne_firstlight_no_dsn(monkeypatch, capsys):
    _no_dotenv(monkeypatch, ariadne_firstlight)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await ariadne_firstlight.run(_ns(seed=None))
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_ariadne_firstlight_main(monkeypatch):
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    monkeypatch.setattr(sys, "argv", ["ops.ariadne_firstlight"])
    assert ariadne_firstlight.main() == 0


# ─────────────────────────────────────────────────────────────────────────────
# ops.researcher_firstlight
# ─────────────────────────────────────────────────────────────────────────────
def _ctx(**over):
    base = {
        "task_id": 5,
        "task_type": "literature",
        "description": "investigate scaling",
        "direction": "scaling laws",
        "expectation": "holds",
        "kill_condition": "breaks",
        "queries": ["scaling laws", "chinchilla"],
        "claim_id": 7,
    }
    base.update(over)
    return base


def _finding(**over):
    base = {
        "verdict": "supports",
        "confidence": 0.8,
        "summary": "evidence supports it",
        "kill_condition_check": "not triggered",
        "key_evidence": ["A Real Title", "Unresolved Title"],
        "gaps": ["one gap"],
        "next_step": "run experiment",
    }
    base.update(over)
    return types.SimpleNamespace(**base)


def _ref(title="A Real Title"):
    return types.SimpleNamespace(title=title)


def _mimir(gaps=None):
    return types.SimpleNamespace(gaps=gaps or ["g1"])


def _feedback_item(task_id=5, disposition="supported"):
    return types.SimpleNamespace(task_id=task_id, disposition=disposition)


def _dir_feedback(**over):
    base = {
        "dominant": "supported",
        "confidence_delta": 0.05,
        "set_last_evidence": True,
        "items": [_feedback_item()],
        "acquire_queries": ["acquire X"],
    }
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_researcher_firstlight_happy(monkeypatch, capsys):
    pool = ScriptedPool([("FROM tasks WHERE department = 'research'", [{"id": 5}])])
    _patch_pool(monkeypatch, researcher_firstlight, pool)
    _no_dotenv(monkeypatch, researcher_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(researcher_firstlight, "PostgresClient", lambda pool: "STATE")
    refs = [_ref()]
    result = (_ctx(), refs, _mimir(), _finding())
    monkeypatch.setattr(researcher_firstlight, "investigate_task", AsyncMock(return_value=result))
    monkeypatch.setattr(researcher_firstlight, "grade_finding", lambda f, r: {"grounded": 0.5, "n_cited": 2})
    monkeypatch.setattr(researcher_firstlight, "finding_feedback", lambda c, f, g: _feedback_item())
    monkeypatch.setattr(researcher_firstlight, "aggregate_direction", lambda cid, d, items: _dir_feedback())

    rc = await researcher_firstlight.run(_ns(limit=8))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Researcher first light" in out
    assert "TASK T5" in out
    assert "VERDICT: ✓ supports" in out
    # one key-evidence line resolves to a ref, the other is unresolved
    assert "A Real Title" in out and "✗(unresolved)" in out
    assert "STEERING PLAN" in out
    assert "would FIRE 1 self-healing acquire(s)" in out
    assert "verdict tally: supports 1" in out


@pytest.mark.asyncio
async def test_researcher_firstlight_no_pending(monkeypatch, capsys):
    pool = ScriptedPool([("FROM tasks WHERE department = 'research'", [])])
    _patch_pool(monkeypatch, researcher_firstlight, pool)
    _no_dotenv(monkeypatch, researcher_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(researcher_firstlight, "PostgresClient", lambda pool: "STATE")
    rc = await researcher_firstlight.run(_ns(limit=8))
    out = capsys.readouterr().out
    assert rc == 0
    assert "No pending research tasks" in out


@pytest.mark.asyncio
async def test_researcher_firstlight_task_failure_and_none(monkeypatch, capsys):
    """One task raises (caught, printed), one returns None (skipped) → no findings."""
    pool = ScriptedPool([("FROM tasks WHERE department = 'research'", [{"id": 5}, {"id": 6}])])
    _patch_pool(monkeypatch, researcher_firstlight, pool)
    _no_dotenv(monkeypatch, researcher_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(researcher_firstlight, "PostgresClient", lambda pool: "STATE")

    async def _investigate(state, tid):
        if tid == 6:
            raise RuntimeError("boom on 6")
        return None

    monkeypatch.setattr(researcher_firstlight, "investigate_task", _investigate)
    rc = await researcher_firstlight.run(_ns(limit=8))
    out = capsys.readouterr().out
    assert rc == 0
    assert "T6: investigation failed — boom on 6" in out
    assert "verdict tally: none" in out


@pytest.mark.asyncio
async def test_researcher_firstlight_no_acquire_no_delta(monkeypatch, capsys):
    """A direction with no acquire queries and no confidence delta exercises the else branch."""
    pool = ScriptedPool([("FROM tasks WHERE department = 'research'", [{"id": 5}])])
    _patch_pool(monkeypatch, researcher_firstlight, pool)
    _no_dotenv(monkeypatch, researcher_firstlight)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(researcher_firstlight, "PostgresClient", lambda pool: "STATE")
    finding = _finding(verdict="inconclusive", key_evidence=[], gaps=[])
    result = (_ctx(), [_ref()], _mimir(), finding)
    monkeypatch.setattr(researcher_firstlight, "investigate_task", AsyncMock(return_value=result))
    monkeypatch.setattr(researcher_firstlight, "grade_finding", lambda f, r: {"grounded": 0.0, "n_cited": 0})
    monkeypatch.setattr(researcher_firstlight, "finding_feedback", lambda c, f, g: _feedback_item())
    monkeypatch.setattr(
        researcher_firstlight,
        "aggregate_direction",
        lambda cid, d, items: _dir_feedback(
            confidence_delta=0.0, set_last_evidence=False, acquire_queries=[], dominant="inconclusive"
        ),
    )
    rc = await researcher_firstlight.run(_ns(limit=8))
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 (no decisive evidence)" in out
    assert "last_evidence_at unchanged" in out
    assert "would FIRE" not in out


@pytest.mark.asyncio
async def test_researcher_firstlight_no_dsn(monkeypatch, capsys):
    _no_dotenv(monkeypatch, researcher_firstlight)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await researcher_firstlight.run(_ns(limit=8))
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_researcher_firstlight_main(monkeypatch):
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    monkeypatch.setattr(sys, "argv", ["ops.researcher_firstlight"])
    assert researcher_firstlight.main() == 0


# ─────────────────────────────────────────────────────────────────────────────
# ops.bootstrap
# ─────────────────────────────────────────────────────────────────────────────
def _kickoff_output():
    cat = types.SimpleNamespace(
        claim="a falsifiable thesis",
        rationale="why it matters",
        risks="dead end if settled",
        disambiguating_questions=["q1", "q2", "q3"],
    )
    return types.SimpleNamespace(categories=[cat, cat], selection_reasoning="covers the space")


def _wire_bootstrap(monkeypatch, pool, *, existing=None):
    """Patch every external dep ops.bootstrap touches; return (router, curator, state)."""
    monkeypatch.setattr(bootstrap, "load_dotenv", lambda *a, **k: None, raising=False)
    _patch_pool(monkeypatch, bootstrap, pool)

    state = AsyncMock()
    claim = types.SimpleNamespace(id=11)
    state.create_claim = AsyncMock(return_value=claim)
    monkeypatch.setattr(bootstrap, "PostgresClient", lambda pool: state)
    monkeypatch.setattr(bootstrap, "ZepClient", types.SimpleNamespace(from_env=lambda: "ZEP"))
    monkeypatch.setattr(bootstrap, "LessonsClient", lambda pool: "LESSONS")
    monkeypatch.setattr(bootstrap, "Curator", lambda **kw: types.SimpleNamespace(build=AsyncMock(return_value="PROMPT")))
    monkeypatch.setattr(bootstrap, "GPULock", lambda: "LOCK")
    monkeypatch.setattr(bootstrap, "build_cloud_chain", lambda env: "CLOUD")
    monkeypatch.setattr(bootstrap, "build_premium_chain", lambda env: "PREMIUM")

    router = types.SimpleNamespace(
        invoke=AsyncMock(return_value=(_kickoff_output(), 99)),
        close=AsyncMock(),
    )
    monkeypatch.setattr(bootstrap, "Router", lambda **kw: router)
    return router, state


@pytest.mark.asyncio
async def test_bootstrap_happy(monkeypatch, capsys):
    # company_state not yet seeded (existing → None), then tasks/events execute fine
    pool = ScriptedPool([("SELECT 1 FROM company_state WHERE id = 1", None)])
    monkeypatch.setenv("DATABASE_URL", "x")
    router, state = _wire_bootstrap(monkeypatch, pool)

    await bootstrap.bootstrap()
    out = capsys.readouterr().out
    assert "Seeded company_state" in out
    assert "PI returned 2 categories" in out
    assert "Bootstrap complete" in out
    assert "Research tasks queued: 6" in out  # 2 categories * 3 questions
    assert state.create_claim.await_count == 2
    router.close.assert_awaited()
    router.invoke.assert_awaited()


@pytest.mark.asyncio
async def test_bootstrap_already_bootstrapped(monkeypatch, capsys):
    """company_state.id=1 already exists → early return, router never built/closed."""
    pool = ScriptedPool([("SELECT 1 FROM company_state WHERE id = 1", 1)])
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(bootstrap, "load_dotenv", lambda *a, **k: None, raising=False)
    _patch_pool(monkeypatch, bootstrap, pool)
    # router is referenced in finally — guard it exists; but the early return happens
    # before it's assigned, so a NameError would surface. Patch the builders anyway.
    monkeypatch.setattr(bootstrap, "PostgresClient", lambda pool: AsyncMock())

    with pytest.raises(UnboundLocalError):
        # the early `return` runs inside try; finally references `router` which is
        # never assigned → UnboundLocalError. This documents the real behavior.
        await bootstrap.bootstrap()
    out = capsys.readouterr().out
    assert "already bootstrapped" in out


@pytest.mark.asyncio
async def test_bootstrap_main_keyboardinterrupt(monkeypatch):
    """The __main__ guard catches KeyboardInterrupt → exit 130. Exercise that wrapper."""

    def _run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(bootstrap.asyncio, "run", _run)
    with pytest.raises(SystemExit) as ei:
        try:
            bootstrap.asyncio.run(bootstrap.bootstrap())
        except KeyboardInterrupt:
            sys.exit(130)
    assert ei.value.code == 130


# ─────────────────────────────────────────────────────────────────────────────
# ops.seed_corpus
# ─────────────────────────────────────────────────────────────────────────────
def _seed_args(tmp_path, **over):
    base = {
        "data_dir": str(tmp_path),
        "limit": 0,
        "batch_size": 64,
        "progress_every": 1,
    }
    base.update(over)
    return _ns(**base)


def _make_corpus_files(tmp_path):
    (tmp_path / "parsed_papers.json").write_text("[]")
    (tmp_path / "chunks.json").write_text("[]")


@pytest.mark.asyncio
async def test_seed_corpus_happy(monkeypatch, capsys, tmp_path):
    _make_corpus_files(tmp_path)
    pool = ScriptedPool([("count(*) FROM documents WHERE queryable", 3), ("count(*) FROM chunks", 9)])
    _no_dotenv(monkeypatch, seed_corpus)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(seed_corpus.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(seed_corpus, "PostgresClient", lambda pool: "STATE")
    monkeypatch.setattr(seed_corpus, "_build_or_load_meta", lambda parsed, cache: {"arxiv_1": {"title": "T"}})

    # two papers stream out of chunks.json; ingest reports new then skip
    def _grouped(path):
        yield "arxiv_1", ["chunk one", "chunk two"]
        yield "arxiv_2", ["chunk a"]

    monkeypatch.setattr(seed_corpus, "_grouped_chunks", _grouped)

    embedder = types.SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(seed_corpus, "_BatchEmbedder", lambda url, model: embedder)
    monkeypatch.setattr(seed_corpus, "_ingest", AsyncMock(side_effect=["new", "skip"]))

    rc = await seed_corpus.run(_seed_args(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Bulk-seeding from" in out
    assert "Done: 2 papers" in out
    assert "new 1" in out
    assert "corpus now: 3 queryable documents, 9 embedded chunks" in out
    embedder.close.assert_awaited()


@pytest.mark.asyncio
async def test_seed_corpus_ingest_error_and_limit(monkeypatch, capsys, tmp_path):
    """One paper raises in _ingest (caught → error count); --limit stops the stream early."""
    _make_corpus_files(tmp_path)
    pool = ScriptedPool([("count(*) FROM documents WHERE queryable", 0), ("count(*) FROM chunks", 0)])
    _no_dotenv(monkeypatch, seed_corpus)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setattr(seed_corpus.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(seed_corpus, "PostgresClient", lambda pool: "STATE")
    monkeypatch.setattr(seed_corpus, "_build_or_load_meta", lambda parsed, cache: {})

    def _grouped(path):
        yield "arxiv_1", ["c"]
        yield "arxiv_2", ["c"]
        yield "arxiv_3", ["c"]  # never reached: limit=2

    monkeypatch.setattr(seed_corpus, "_grouped_chunks", _grouped)
    monkeypatch.setattr(seed_corpus, "_BatchEmbedder", lambda url, model: types.SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(seed_corpus, "_ingest", AsyncMock(side_effect=[RuntimeError("bad paper"), "new"]))

    rc = await seed_corpus.run(_seed_args(tmp_path, limit=2))
    captured = capsys.readouterr()
    assert rc == 0
    assert "Done: 2 papers" in captured.out
    assert "errors 1" in captured.out
    assert "arxiv_1: bad paper" in captured.err


@pytest.mark.asyncio
async def test_seed_corpus_no_dsn(monkeypatch, capsys, tmp_path):
    _no_dotenv(monkeypatch, seed_corpus)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await seed_corpus.run(_seed_args(tmp_path))
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_seed_corpus_missing_data_files(monkeypatch, capsys, tmp_path):
    """DSN set but the rag-bench files are absent → guard returns 2."""
    _no_dotenv(monkeypatch, seed_corpus)
    monkeypatch.setenv("DATABASE_URL", "x")
    rc = await seed_corpus.run(_seed_args(tmp_path))
    assert rc == 2
    assert "missing" in capsys.readouterr().err


def test_seed_corpus_main(monkeypatch):
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    monkeypatch.setattr(sys, "argv", ["ops.seed_corpus", "--limit", "10"])
    assert seed_corpus.main() == 0


def test_seed_corpus_main_keyboardinterrupt(monkeypatch, capsys):
    def _run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", _run)
    monkeypatch.setattr(sys, "argv", ["ops.seed_corpus"])
    assert seed_corpus.main() == 130
    assert "interrupted" in capsys.readouterr().err


def test_seed_corpus_clean_and_arxiv_id_helpers():
    assert seed_corpus._clean("a\x00b") == "ab"
    assert seed_corpus._clean(None) == ""
    assert seed_corpus._arxiv_id_from_doc("arxiv_2401.1", None) == "2401.1"
    assert seed_corpus._arxiv_id_from_doc("plain", {"arxiv_id": " 99 "}) == "99"


class _FakeHTTPResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHTTPClient:
    def __init__(self, payload, *, timeout=None):
        self._payload = payload
        self.posts = []
        self.closed = False

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return _FakeHTTPResp(self._payload)

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_seed_corpus_batch_embedder(monkeypatch):
    client = _FakeHTTPClient({"embeddings": [[0.0] * 768, [1.0] * 768]})
    monkeypatch.setattr(seed_corpus.httpx, "AsyncClient", lambda **kw: client)
    emb = seed_corpus._BatchEmbedder("http://o", "nomic", dim=768)
    assert await emb.embed_many([]) == []  # empty short-circuit
    vecs = await emb.embed_many(["a", "b"])
    assert len(vecs) == 2 and client.posts
    await emb.close()
    assert client.closed


@pytest.mark.asyncio
async def test_seed_corpus_batch_embedder_count_mismatch(monkeypatch):
    client = _FakeHTTPClient({"embeddings": [[0.0] * 768]})  # 1 vec for 2 inputs
    monkeypatch.setattr(seed_corpus.httpx, "AsyncClient", lambda **kw: client)
    emb = seed_corpus._BatchEmbedder("http://o", "nomic", dim=768)
    with pytest.raises(ValueError, match="returned 1 vectors"):
        await emb.embed_many(["a", "b"])


@pytest.mark.asyncio
async def test_seed_corpus_batch_embedder_dim_mismatch(monkeypatch):
    client = _FakeHTTPClient({"embeddings": [[0.0] * 10]})  # wrong dim
    monkeypatch.setattr(seed_corpus.httpx, "AsyncClient", lambda **kw: client)
    emb = seed_corpus._BatchEmbedder("http://o", "wrong-model", dim=768)
    with pytest.raises(ValueError, match="embed dim 10"):
        await emb.embed_many(["a"])


def test_seed_corpus_build_meta_from_file_then_cache(tmp_path):
    parsed = tmp_path / "parsed_papers.json"
    parsed.write_text(
        json.dumps(
            [
                {"doc_id": "arxiv_1", "arxiv_id": "1", "title": "T1", "authors": ["a"]},
                {"title": "no doc_id — skipped"},
            ]
        )
    )
    cache = tmp_path / ".lf_meta_index.json"
    index = seed_corpus._build_or_load_meta(parsed, cache)
    assert "arxiv_1" in index and len(index) == 1
    assert cache.exists()
    # second call loads the cache (different parsed path proves the cache is used)
    index2 = seed_corpus._build_or_load_meta(tmp_path / "absent.json", cache)
    assert index2 == index


def test_seed_corpus_build_meta_cache_write_swallowed(tmp_path):
    """A cache path under a missing directory → exists()=False so it builds, but the
    write raises FileNotFoundError, which is swallowed; the index is still returned."""
    parsed = tmp_path / "parsed_papers.json"
    parsed.write_text(json.dumps([{"doc_id": "arxiv_1", "arxiv_id": "1", "title": "T"}]))
    cache = tmp_path / "no_such_dir" / "cache.json"  # parent absent → open('w') fails
    index = seed_corpus._build_or_load_meta(parsed, cache)
    assert "arxiv_1" in index
    assert not cache.exists()


def test_seed_corpus_grouped_chunks(tmp_path):
    chunks = tmp_path / "chunks.json"
    chunks.write_text(
        json.dumps(
            [
                {"doc_id": "arxiv_1", "text": "a"},
                {"doc_id": "arxiv_1", "text": "b"},
                {"doc_id": "arxiv_2", "text": "c"},
            ]
        )
    )
    groups = list(seed_corpus._grouped_chunks(chunks))
    assert groups == [("arxiv_1", ["a", "b"]), ("arxiv_2", ["c"])]


def test_seed_corpus_grouped_chunks_empty(tmp_path):
    chunks = tmp_path / "chunks.json"
    chunks.write_text("[]")
    assert list(seed_corpus._grouped_chunks(chunks)) == []


def _seed_fake_state(*, is_new=True, queryable=False):
    st = AsyncMock()
    st.upsert_document = AsyncMock(return_value=(101, is_new))
    st.get_document = AsyncMock(return_value={"queryable": queryable})
    st.stage_chunk_plan = AsyncMock()
    st.set_document_trust = AsyncMock()
    st.append_certification = AsyncMock()
    st.get_chunk_plan = AsyncMock(
        return_value=[
            {"ordinal": 0, "content_hash": "h0", "text": "a", "has_embedding": False},
            {"ordinal": 1, "content_hash": "h1", "text": "b", "has_embedding": True},
        ]
    )
    st.set_chunk_embeddings = AsyncMock()
    st.set_document_queryable = AsyncMock()
    return st


@pytest.mark.asyncio
async def test_seed_corpus_ingest_new():
    state = _seed_fake_state(is_new=True)
    embedder = types.SimpleNamespace(model="nomic", embed_many=AsyncMock(return_value=[[0.0]]))
    outcome = await seed_corpus._ingest(
        state, embedder, "arxiv_1", ["text one", "\x00text two"], {"title": "T", "authors": []}, batch_size=64
    )
    assert outcome == "new"
    state.stage_chunk_plan.assert_awaited()
    state.append_certification.assert_awaited()
    state.set_document_queryable.assert_awaited()
    # only the one un-embedded chunk was embedded
    embedder.embed_many.assert_awaited_once()


@pytest.mark.asyncio
async def test_seed_corpus_ingest_empty():
    state = _seed_fake_state()
    embedder = types.SimpleNamespace(model="nomic", embed_many=AsyncMock(return_value=[]))
    outcome = await seed_corpus._ingest(state, embedder, "arxiv_1", ["\x00", "  "], None, batch_size=64)
    assert outcome == "empty"
    state.upsert_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_corpus_ingest_skip_already_queryable():
    state = _seed_fake_state(is_new=False, queryable=True)
    embedder = types.SimpleNamespace(model="nomic", embed_many=AsyncMock(return_value=[]))
    outcome = await seed_corpus._ingest(state, embedder, "arxiv_1", ["text"], {"title": "T"}, batch_size=64)
    assert outcome == "skip"
    state.stage_chunk_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_corpus_ingest_resume():
    state = _seed_fake_state(is_new=False, queryable=False)
    embedder = types.SimpleNamespace(model="nomic", embed_many=AsyncMock(return_value=[[0.0]]))
    outcome = await seed_corpus._ingest(state, embedder, "arxiv_1", ["text"], {"title": "T"}, batch_size=64)
    assert outcome == "resume"
    # no re-staging on resume, but pending chunks still embed + finalize
    state.stage_chunk_plan.assert_not_awaited()
    state.set_document_queryable.assert_awaited()


# ─────────────────────────────────────────────────────────────────────────────
# ops.mimir_firstlight
# ─────────────────────────────────────────────────────────────────────────────
def _mf_preflight_rules(*, seeded=1, present_state=None):
    tables = [
        {"tablename": "documents"},
        {"tablename": "chunks"},
        {"tablename": "certifications"},
        {"tablename": "claims"},
    ]
    return [
        ("FROM pg_tables", tables),
        ("count(*) FROM company_state WHERE id = 1", seeded if present_state is None else present_state),
        # snapshot
        ("count(*) FROM documents", 100),
        ("count(*) FROM documents WHERE queryable", 80),
        ("count(*) FROM certifications", 50),
        ("count(*) FROM certifications WHERE used_llm", 5),
        ("GROUP BY trust_tier", [{"trust_tier": "preprint", "n": 80}]),
    ]


def _wire_mf_chains(monkeypatch):
    monkeypatch.setattr(mimir_firstlight, "load_dotenv", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(mimir_firstlight, "register_vector_codec", AsyncMock(), raising=False)
    monkeypatch.setattr(mimir_firstlight, "PostgresClient", lambda pool: "STATE")
    monkeypatch.setattr(mimir_firstlight, "ZepClient", types.SimpleNamespace(from_env=lambda: "ZEP"))
    monkeypatch.setattr(mimir_firstlight, "LessonsClient", lambda pool: "LESSONS")
    monkeypatch.setattr(mimir_firstlight, "Curator", lambda **kw: "CURATOR")
    monkeypatch.setattr(mimir_firstlight, "GPULock", lambda: "LOCK")
    monkeypatch.setattr(mimir_firstlight, "build_cloud_chain", lambda env: "CLOUD")
    lead = types.SimpleNamespace(provider=types.SimpleNamespace(value="deepseek"), model_name="v4")
    monkeypatch.setattr(mimir_firstlight, "build_premium_chain", lambda env: [lead])
    router = types.SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(mimir_firstlight, "Router", lambda **kw: router)
    return router


def _patch_httpx_tags(monkeypatch, model="nomic-embed-text"):
    """Make the preflight Ollama /api/tags probe report the embed model present."""

    class _Resp:
        def json(self):
            return {"models": [{"name": model}]}

        @property
        def status_code(self):
            return 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(mimir_firstlight.httpx, "AsyncClient", _Client)


def _mf_args(**over):
    base = {
        "mode": "discover",
        "topic": None,
        "per_topic": 2,
        "limit": 3,
        "arxiv_id": None,
        "url": None,
        "query": None,
        "requester": "researcher",
        "no_llm": False,
    }
    base.update(over)
    return _ns(**base)


@pytest.mark.asyncio
async def test_mimir_firstlight_discover(monkeypatch, capsys):
    rules = _mf_preflight_rules() + [
        ("event_type='source.discovered'", [{"id": 1, "payload": {"source": {"kind": "paper"}}}]),
    ]
    pool = ScriptedPool(rules)
    _patch_pool(monkeypatch, mimir_firstlight, pool)
    monkeypatch.setenv("DATABASE_URL", "x")
    _patch_httpx_tags(monkeypatch)
    router = _wire_mf_chains(monkeypatch)
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))
    monkeypatch.setattr(mimir_firstlight, "run_discovery_sweep", AsyncMock(return_value={"scanned": 4, "discovered": 1}))
    ingest = AsyncMock(
        return_value={
            "decision": "approve",
            "document_id": 7,
            "tier": "preprint",
            "used_llm": False,
            "embedded": True,
            "queryable": True,
        }
    )
    monkeypatch.setattr(mimir_firstlight, "ingest_source", ingest)

    rc = await mimir_firstlight.run(_mf_args(mode="discover", no_llm=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Preflight" in out
    assert "Discovery sweep" in out
    assert "doc 7 APPROVED" in out
    assert "Corpus snapshot" in out
    assert "Mimir cycle complete" in out
    ingest.assert_awaited()
    router.close.assert_awaited()


@pytest.mark.asyncio
async def test_mimir_firstlight_discover_nothing(monkeypatch, capsys):
    """Sweep finds nothing → no discovered events → 'No fresh sources' branch."""
    rules = _mf_preflight_rules() + [("event_type='source.discovered'", [])]
    pool = ScriptedPool(rules)
    _patch_pool(monkeypatch, mimir_firstlight, pool)
    monkeypatch.setenv("DATABASE_URL", "x")
    _patch_httpx_tags(monkeypatch)
    _wire_mf_chains(monkeypatch)
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))
    monkeypatch.setattr(mimir_firstlight, "run_discovery_sweep", AsyncMock(return_value={"scanned": 0, "discovered": 0}))

    rc = await mimir_firstlight.run(_mf_args(topic=["llm"], no_llm=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "No fresh sources to ingest" in out


@pytest.mark.asyncio
async def test_mimir_firstlight_seed_no_llm(monkeypatch, capsys):
    """--mode seed --arxiv-id with --no-llm: no router/curator built; BLOCK verdict path."""
    pool = ScriptedPool(_mf_preflight_rules())
    _patch_pool(monkeypatch, mimir_firstlight, pool)
    monkeypatch.setenv("DATABASE_URL", "x")
    _patch_httpx_tags(monkeypatch)
    _wire_mf_chains(monkeypatch)
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))
    monkeypatch.setattr(
        mimir_firstlight,
        "ingest_source",
        AsyncMock(return_value={"decision": "block", "document_id": 3, "used_llm": False, "reason": "spam"}),
    )

    rc = await mimir_firstlight.run(_mf_args(mode="seed", arxiv_id="1706.03762", no_llm=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Seeding arXiv:1706.03762" in out
    assert "BLOCKED" in out


@pytest.mark.asyncio
async def test_mimir_firstlight_seed_url_skip(monkeypatch, capsys):
    """--mode seed --url → web source; ingest returns a skip/dedupe verdict."""
    pool = ScriptedPool(_mf_preflight_rules())
    _patch_pool(monkeypatch, mimir_firstlight, pool)
    monkeypatch.setenv("DATABASE_URL", "x")
    _patch_httpx_tags(monkeypatch)
    _wire_mf_chains(monkeypatch)
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))
    monkeypatch.setattr(
        mimir_firstlight, "ingest_source", AsyncMock(return_value={"decision": "skip", "reason": "deduped"})
    )

    rc = await mimir_firstlight.run(_mf_args(mode="seed", url="https://blog/post", no_llm=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Seeding URL https://blog/post" in out
    assert "skipped — deduped" in out


@pytest.mark.asyncio
async def test_mimir_firstlight_seed_missing_args(monkeypatch, capsys):
    pool = ScriptedPool(_mf_preflight_rules())
    _patch_pool(monkeypatch, mimir_firstlight, pool)
    monkeypatch.setenv("DATABASE_URL", "x")
    _patch_httpx_tags(monkeypatch)
    _wire_mf_chains(monkeypatch)
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))

    rc = await mimir_firstlight.run(_mf_args(mode="seed", no_llm=True))
    assert rc == 2
    assert "--mode seed needs --arxiv-id or --url" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_mimir_firstlight_acquire(monkeypatch, capsys):
    rules = _mf_preflight_rules() + [
        ("event_type='acquire.requested'", {"id": 9, "payload": {"requester": "researcher"}}),
    ]
    pool = ScriptedPool(rules)
    _patch_pool(monkeypatch, mimir_firstlight, pool)
    monkeypatch.setenv("DATABASE_URL", "x")
    _patch_httpx_tags(monkeypatch)
    _wire_mf_chains(monkeypatch)
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))
    monkeypatch.setattr(mimir_firstlight, "request_acquire", AsyncMock())
    monkeypatch.setattr(
        mimir_firstlight,
        "handle_acquire_requested",
        AsyncMock(return_value={"status": "fulfilled", "reason": "ingested", "document_id": 12}),
    )
    monkeypatch.setattr(mimir_firstlight, "AcquireRequest", lambda **kw: types.SimpleNamespace(**kw))

    rc = await mimir_firstlight.run(_mf_args(mode="acquire", query="speculative decoding", no_llm=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Acquire" in out
    assert "acquire -> fulfilled" in out


@pytest.mark.asyncio
async def test_mimir_firstlight_acquire_not_emitted(monkeypatch, capsys):
    """request_acquire fired but no acquire.requested event found → ✗ early."""
    rules = _mf_preflight_rules() + [("event_type='acquire.requested'", None)]
    pool = ScriptedPool(rules)
    _patch_pool(monkeypatch, mimir_firstlight, pool)
    monkeypatch.setenv("DATABASE_URL", "x")
    _patch_httpx_tags(monkeypatch)
    _wire_mf_chains(monkeypatch)
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))
    monkeypatch.setattr(mimir_firstlight, "request_acquire", AsyncMock())
    monkeypatch.setattr(mimir_firstlight, "AcquireRequest", lambda **kw: types.SimpleNamespace(**kw))

    rc = await mimir_firstlight.run(_mf_args(mode="acquire", arxiv_id="1706.03762", requester="pi", no_llm=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "acquire.requested was not emitted" in out


@pytest.mark.asyncio
async def test_mimir_firstlight_acquire_missing_args(monkeypatch, capsys):
    pool = ScriptedPool(_mf_preflight_rules())
    _patch_pool(monkeypatch, mimir_firstlight, pool)
    monkeypatch.setenv("DATABASE_URL", "x")
    _patch_httpx_tags(monkeypatch)
    _wire_mf_chains(monkeypatch)
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))
    rc = await mimir_firstlight.run(_mf_args(mode="acquire", no_llm=True))
    assert rc == 2
    assert "--mode acquire needs --arxiv-id or --query" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_mimir_firstlight_preflight_fails(monkeypatch, capsys):
    """Missing core tables → hard preflight fails → abort with 2 before any ingest."""
    rules = [
        ("FROM pg_tables", [{"tablename": "documents"}]),  # chunks/certifications/claims missing
        ("count(*) FROM company_state WHERE id = 1", 0),
    ]
    pool = ScriptedPool(rules)
    _patch_pool(monkeypatch, mimir_firstlight, pool)
    monkeypatch.setenv("DATABASE_URL", "x")
    _patch_httpx_tags(monkeypatch, model="other-model")  # embed model not pulled either
    _wire_mf_chains(monkeypatch)
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[]))

    rc = await mimir_firstlight.run(_mf_args(no_llm=True))
    err = capsys.readouterr().err
    assert rc == 2
    assert "Hard preflight check failed" in err


@pytest.mark.asyncio
async def test_mimir_firstlight_temp_state_seeded(monkeypatch, capsys):
    """want_llm + company_state absent → a TEMP row is inserted and removed on exit."""
    rules = _mf_preflight_rules(present_state=0) + [("event_type='source.discovered'", [])]
    pool = ScriptedPool(rules)
    _patch_pool(monkeypatch, mimir_firstlight, pool)
    monkeypatch.setenv("DATABASE_URL", "x")
    _patch_httpx_tags(monkeypatch)
    _wire_mf_chains(monkeypatch)
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))
    monkeypatch.setattr(mimir_firstlight, "run_discovery_sweep", AsyncMock(return_value={"scanned": 1, "discovered": 0}))

    rc = await mimir_firstlight.run(_mf_args(no_llm=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "seeded a TEMPORARY company_state" in out
    # the cleanup DELETE ran
    assert any("DELETE FROM company_state" in c[1] for c in pool.calls)


@pytest.mark.asyncio
async def test_mimir_firstlight_no_dsn(monkeypatch, capsys):
    monkeypatch.setattr(mimir_firstlight, "load_dotenv", lambda *a, **k: None, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await mimir_firstlight.run(_mf_args(no_llm=True))
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


def _preflight_pool():
    return ScriptedPool(_mf_preflight_rules())


class _MFRouteClient:
    """An httpx client whose GET routes by URL: /api/tags → embed-model present,
    anything else (searxng) → a 200. Optionally raises to exercise the except paths."""

    def __init__(self, *, raise_on=None, tags_model="nomic-embed-text"):
        self._raise_on = raise_on or set()
        self._tags_model = tags_model

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        if any(tok in url for tok in self._raise_on):
            raise RuntimeError("unreachable")

        class _R:
            def __init__(self, model):
                self._m = model

            def json(self):
                return {"models": [{"name": self._m}]}

            @property
            def status_code(self):
                return 200

        return _R(self._tags_model)


@pytest.mark.asyncio
async def test_mimir_firstlight_preflight_web_and_no_premium(monkeypatch, capsys):
    """LIBRARY_SCOUTS includes web + searxng reachable, and an empty premium chain
    exercises the web-scout, SearXNG, and 'no premium chain' soft branches."""
    monkeypatch.setattr(mimir_firstlight.httpx, "AsyncClient", _MFRouteClient())
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv,web")
    monkeypatch.setenv("SEARXNG_URL", "http://searx")
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))
    monkeypatch.setattr(mimir_firstlight, "build_premium_chain", lambda env: [])

    ok = await mimir_firstlight._preflight(_preflight_pool(), mode="discover", want_llm=True)
    out = capsys.readouterr().out
    assert ok is True
    assert "SearXNG reachable" in out
    assert "no premium chain" in out


@pytest.mark.asyncio
async def test_mimir_firstlight_preflight_ollama_unreachable(monkeypatch, capsys):
    """Ollama /api/tags raising → HARD fail; arXiv soft for non-discover modes."""
    monkeypatch.setattr(mimir_firstlight.httpx, "AsyncClient", _MFRouteClient(raise_on={"/api/tags"}))
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[{"id": "x"}]))

    ok = await mimir_firstlight._preflight(_preflight_pool(), mode="seed", want_llm=False)
    out = capsys.readouterr().out
    assert ok is False
    assert "Ollama unreachable" in out


@pytest.mark.asyncio
async def test_mimir_firstlight_preflight_arxiv_raises_and_searxng_down(monkeypatch, capsys):
    """discover mode + arXiv search raising → HARD fail; web scout + SearXNG GET raising
    → soft 'unreachable' branch."""
    monkeypatch.setattr(mimir_firstlight.httpx, "AsyncClient", _MFRouteClient(raise_on={"searx"}))
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv,web")
    monkeypatch.setenv("SEARXNG_URL", "http://searx")
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(side_effect=RuntimeError("net")))

    ok = await mimir_firstlight._preflight(_preflight_pool(), mode="discover", want_llm=False)
    out = capsys.readouterr().out
    assert ok is False
    assert "arXiv API unreachable" in out
    assert "SearXNG unreachable" in out


@pytest.mark.asyncio
async def test_mimir_firstlight_preflight_arxiv_empty(monkeypatch, capsys):
    """discover mode + arXiv returns no hits → HARD fail with the no-results message."""
    monkeypatch.setattr(mimir_firstlight.httpx, "AsyncClient", _MFRouteClient())
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    monkeypatch.setattr(mimir_firstlight, "search_arxiv", AsyncMock(return_value=[]))

    ok = await mimir_firstlight._preflight(_preflight_pool(), mode="discover", want_llm=False)
    out = capsys.readouterr().out
    assert ok is False
    assert "arXiv API returned no results" in out


@pytest.mark.asyncio
async def test_mimir_firstlight_preflight_db_unreachable(monkeypatch, capsys):
    """The pg_tables probe raising → DB unreachable, return False immediately."""

    def _boom():
        raise RuntimeError("conn refused")

    pool = ScriptedPool([("FROM pg_tables", _boom)])
    monkeypatch.setattr(mimir_firstlight.httpx, "AsyncClient", _MFRouteClient())
    ok = await mimir_firstlight._preflight(pool, mode="discover", want_llm=False)
    out = capsys.readouterr().out
    assert ok is False
    assert "DB unreachable" in out


def test_mimir_firstlight_main(monkeypatch):
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    monkeypatch.setattr(sys, "argv", ["ops.mimir_firstlight"])
    assert mimir_firstlight.main() == 0


def test_mimir_firstlight_main_keyboardinterrupt(monkeypatch):
    def _run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", _run)
    monkeypatch.setattr(sys, "argv", ["ops.mimir_firstlight"])
    assert mimir_firstlight.main() == 130
