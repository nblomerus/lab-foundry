"""Pytest coverage for the Planner — both the Stage-2 direction→tasks path
(plan/persist/decompose) and the market-era queue.empty planner (handler/loop).

Everything external is mocked: NO Postgres/Neo4j/Ollama/DeepSeek/network. The LLM
`_chain_complete` is patched via tests._helpers.patch_chain; the DB is a ScriptedPool;
state/dispatcher are AsyncMocks; get_agent_mode is monkeypatched per test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.planner.decompose as decompose_mod
import agents.planner.loop as loop_mod
import agents.planner.plan as plan_mod
from agents.planner.decompose import handle_planner_decompose
from agents.planner.handler import (
    PlannedTask,
    PlannedTasks,
    _build_planner_task_data,
    handle_queue_empty,
)
from agents.planner.loop import (
    _build_assess_state,
    _build_critique,
    _build_propose_tasks,
    run_planner_loop,
)
from agents.planner.persist import persist_plan
from agents.planner.plan import (
    MAX_TASKS_PER_DIRECTION,
    _approved_agenda,
    grade_plan,
    run_planning,
)
from agents.planner.schemas import (
    DirectionPlan,
    PlanOutput,
    ResearchTask,
    StateAssessment,
    ThesisGap,
)
from tests._helpers import ScriptedPool, make_state, patch_chain

pytestmark = pytest.mark.asyncio


# ── builders ──────────────────────────────────────────────────────────────────
def _task(claim_id=10, *, ttype="analyze", desc="do a thing", pri="high", title="T", rat="r"):
    return ResearchTask(title=title, description=desc, task_type=ttype, rationale=rat, priority=pri)


def _plan(claim_id=10, tasks=None):
    return DirectionPlan(claim_id=claim_id, tasks=tasks if tasks is not None else [_task(claim_id)])


def _plan_json(claim_id=10, n_tasks=1, ttype="analyze", desc="do a thing"):
    tasks = [
        {
            "title": f"task {i}",
            "description": desc,
            "task_type": ttype,
            "rationale": "advances the goal",
            "priority": "high",
        }
        for i in range(n_tasks)
    ]
    return json.dumps({"plans": [{"claim_id": claim_id, "tasks": tasks}], "notes": "ok"})


_AGENDA_RULES = [
    ("claim_kind = 'mission'", "Our mission is to advance X"),
    (
        "dg.status = 'approved'",
        [
            {"id": 10, "statement": "Direction A statement", "composite": 4.2, "priority": "high"},
            {"id": 11, "statement": "Direction B statement", "composite": 3.1, "priority": None},
        ],
    ),
    (
        "FROM claim_goals",
        [
            {
                "claim_id": 10,
                "expectation": "we expect X",
                "kill_condition": "kill if Y",
                "next_milestone": "do Z next",
            },
        ],
    ),
    ("FROM lessons", []),
]


def _agenda_pool(rules=None):
    return ScriptedPool(rules=_AGENDA_RULES if rules is None else rules)


# ── plan.py: _approved_agenda ───────────────────────────────────────────────────
async def test_approved_agenda_happy():
    pool = _agenda_pool()
    mission, ids, agenda = await _approved_agenda(pool)
    assert mission == "Our mission is to advance X"
    assert ids == [10, 11]
    assert "#10 [high]" in agenda
    assert "#11 [—]" in agenda  # priority None → em-dash
    assert "goal: expect=we expect X" in agenda
    assert "next: do Z next" in agenda


async def test_approved_agenda_goal_without_next_milestone():
    rules = [
        ("claim_kind = 'mission'", None),
        ("dg.status = 'approved'", [{"id": 10, "statement": "Dir", "composite": 1.0, "priority": "low"}]),
        (
            "FROM claim_goals",
            [{"claim_id": 10, "expectation": "e", "kill_condition": "k", "next_milestone": None}],
        ),
    ]
    mission, ids, agenda = await _approved_agenda(_agenda_pool(rules))
    assert mission is None
    assert ids == [10]
    assert "next:" not in agenda  # no next_milestone branch
    assert "kill=k" in agenda


async def test_approved_agenda_empty():
    rules = [
        ("claim_kind = 'mission'", None),
        ("dg.status = 'approved'", []),  # no approved directions
    ]
    mission, ids, agenda = await _approved_agenda(_agenda_pool(rules))
    assert mission is None
    assert ids == []
    assert agenda == "(no approved directions)"


# ── plan.py: run_planning ───────────────────────────────────────────────────────
async def test_run_planning_no_approved_returns_none(monkeypatch):
    patch_chain(monkeypatch, plan_mod, content=_plan_json())
    rules = [("claim_kind = 'mission'", None), ("dg.status = 'approved'", [])]
    state = make_state(_agenda_pool(rules))
    out, ids = await run_planning(state)
    assert out is None and ids == []


async def test_run_planning_happy(monkeypatch):
    calls = patch_chain(monkeypatch, plan_mod, content=_plan_json(claim_id=10, n_tasks=2))
    state = make_state(_agenda_pool())
    out, ids = await run_planning(state, model="deepseek-x")
    assert isinstance(out, PlanOutput)
    assert ids == [10, 11]
    assert out.plans[0].claim_id == 10
    assert len(out.plans[0].tasks) == 2
    # the model was given a system + user message and our model override
    (messages, kw) = calls[0]
    assert kw["primary_model"] == "deepseek-x"
    assert messages[0]["role"] == "system"


async def test_run_planning_strips_fences(monkeypatch):
    fenced = "```json\n" + _plan_json() + "\n```"
    patch_chain(monkeypatch, plan_mod, content=fenced)
    state = make_state(_agenda_pool())
    out, ids = await run_planning(state)
    assert isinstance(out, PlanOutput)
    assert out.plans[0].claim_id == 10


# ── plan.py: grade_plan ─────────────────────────────────────────────────────────
async def test_grade_plan_pass():
    out = PlanOutput(plans=[_plan(10), _plan(11)], notes="")
    g = grade_plan(out, [10, 11])
    assert g.passed and g.valid_refs and g.tasks_wellformed
    assert g.n_plans == 2 and g.n_tasks == 2 and g.invalid_refs == []


async def test_grade_plan_invalid_refs():
    out = PlanOutput(plans=[_plan(10), _plan(999)], notes="")
    g = grade_plan(out, [10])
    assert not g.passed and not g.valid_refs
    assert g.invalid_refs == [999]


async def test_grade_plan_empty_plans():
    g = grade_plan(PlanOutput(plans=[], notes=""), [10])
    assert not g.passed and not g.valid_refs
    assert g.n_plans == 0 and g.n_tasks == 0


async def test_grade_plan_bad_task_type():
    out = PlanOutput(plans=[_plan(10, tasks=[_task(10, ttype="bogus")])], notes="")
    g = grade_plan(out, [10])
    assert g.valid_refs and not g.tasks_wellformed and not g.passed


async def test_grade_plan_empty_description():
    out = PlanOutput(plans=[_plan(10, tasks=[_task(10, desc="   ")])], notes="")
    g = grade_plan(out, [10])
    assert not g.tasks_wellformed and not g.passed


async def test_grade_plan_no_tasks_is_wellformed_false():
    out = PlanOutput(plans=[_plan(10, tasks=[])], notes="")
    g = grade_plan(out, [10])
    assert g.valid_refs and g.n_tasks == 0 and not g.tasks_wellformed


async def test_grade_plan_invalid_refs_capped_at_8():
    plans = [_plan(900 + i) for i in range(10)]
    g = grade_plan(PlanOutput(plans=plans, notes=""), [10])
    assert len(g.invalid_refs) == 8


# ── persist.py: persist_plan ────────────────────────────────────────────────────
def _inserts(pool):
    return [c for c in pool.calls if c[0] == "execute" and "INSERT INTO tasks" in c[1]]


async def test_persist_plan_writes_research_tasks():
    pool = ScriptedPool()
    state = make_state(pool)
    out = PlanOutput(plans=[_plan(10, tasks=[_task(10, ttype="survey", pri="high")])], notes="")
    counts = await persist_plan(state, out, [10], run_id=7)
    assert counts == {"tasks": 1, "directions_planned": 1}
    ins = _inserts(pool)
    assert len(ins) == 1
    args = ins[0][2]
    assert args[0] == "survey"  # task_type
    assert args[3] == 8  # high → priority 8
    assert args[4] == 10  # claim_id
    payload = json.loads(args[2])
    assert payload == {
        "title": "T",
        "rationale": "r",
        "from": "planner",
        "direction_id": 10,
        "run_id": 7,
    }


async def test_persist_plan_skips_invalid_ids():
    pool = ScriptedPool()
    state = make_state(pool)
    out = PlanOutput(plans=[_plan(10), _plan(999)], notes="")
    counts = await persist_plan(state, out, [10])
    assert counts["tasks"] == 1  # only the valid direction wrote
    assert counts["directions_planned"] == 2  # n_plans counts all plans


async def test_persist_plan_skips_bad_tasks():
    pool = ScriptedPool()
    state = make_state(pool)
    bad = [_task(10, desc="  "), _task(10, ttype="nope")]
    out = PlanOutput(plans=[_plan(10, tasks=bad)], notes="")
    counts = await persist_plan(state, out, [10])
    assert counts["tasks"] == 0
    assert _inserts(pool) == []


async def test_persist_plan_caps_tasks_per_direction():
    pool = ScriptedPool()
    state = make_state(pool)
    many = [_task(10, title=f"t{i}") for i in range(MAX_TASKS_PER_DIRECTION + 3)]
    out = PlanOutput(plans=[_plan(10, tasks=many)], notes="")
    counts = await persist_plan(state, out, [10])
    assert counts["tasks"] == MAX_TASKS_PER_DIRECTION
    assert len(_inserts(pool)) == MAX_TASKS_PER_DIRECTION


async def test_persist_plan_skips_direction_already_at_pending_cap():
    # Direction 10 already has MAX pending tasks -> add NONE. This is the per-direction
    # PENDING cap (not per-invocation), so repeated planner.plan calls can't pile up.
    pool = ScriptedPool(rules=[("FROM tasks WHERE claim_id", [{"count": MAX_TASKS_PER_DIRECTION}])])
    state = make_state(pool)
    out = PlanOutput(plans=[_plan(10, tasks=[_task(10), _task(10)])], notes="")
    counts = await persist_plan(state, out, [10])
    assert counts["tasks"] == 0
    assert _inserts(pool) == []


async def test_persist_plan_only_fills_remaining_pending_room():
    # Direction 10 has 1 pending already -> only (MAX - 1) new tasks created.
    pool = ScriptedPool(rules=[("FROM tasks WHERE claim_id", [{"count": 1}])])
    state = make_state(pool)
    many = [_task(10, title=f"t{i}") for i in range(MAX_TASKS_PER_DIRECTION + 2)]
    out = PlanOutput(plans=[_plan(10, tasks=many)], notes="")
    counts = await persist_plan(state, out, [10])
    assert counts["tasks"] == MAX_TASKS_PER_DIRECTION - 1


async def test_persist_plan_priority_default_for_unknown_label():
    pool = ScriptedPool()
    state = make_state(pool)
    t = ResearchTask(title="T", description="d", task_type="analyze", rationale="r", priority="weird")
    out = PlanOutput(plans=[_plan(10, tasks=[t])], notes="")
    await persist_plan(state, out, [10])
    assert _inserts(pool)[0][2][3] == 5  # unknown priority → default 5


async def test_persist_plan_truncates_long_description():
    pool = ScriptedPool()
    state = make_state(pool)
    t = ResearchTask(title="T", description="x" * 5000, task_type="analyze", rationale="r", priority="low")
    out = PlanOutput(plans=[_plan(10, tasks=[t])], notes="")
    await persist_plan(state, out, [10])
    assert len(_inserts(pool)[0][2][1]) == 4000  # description[:4000]


# ── decompose.py: handle_planner_decompose ──────────────────────────────────────
def _mode(monkeypatch, mode):
    monkeypatch.setattr(decompose_mod, "get_agent_mode", AsyncMock(return_value=mode))


def _no_persist_pool():
    # run_planning reads its agenda from state.pool; persist would also use it
    return _agenda_pool()


async def test_decompose_nothing_to_plan(monkeypatch):
    _mode(monkeypatch, "active")
    patch_chain(monkeypatch, plan_mod, content=_plan_json())
    rules = [("claim_kind = 'mission'", None), ("dg.status = 'approved'", [])]
    dispatcher = SimpleNamespace(state=make_state(_agenda_pool(rules)))
    res = await handle_planner_decompose({}, dispatcher)
    assert res == {"mode": "active", "planned": False, "reason": "nothing_to_plan"}


async def test_decompose_shadow_writes_nothing(monkeypatch):
    _mode(monkeypatch, "shadow")
    patch_chain(monkeypatch, plan_mod, content=_plan_json(claim_id=10, n_tasks=2))
    pool = _agenda_pool()
    dispatcher = SimpleNamespace(state=make_state(pool))
    res = await handle_planner_decompose({}, dispatcher)
    assert res["mode"] == "shadow" and res["persisted"] is False
    assert res["plans"] == 1 and res["tasks"] == 2
    assert _inserts(pool) == []  # shadow never persists


async def test_decompose_off_writes_nothing(monkeypatch):
    _mode(monkeypatch, "off")
    patch_chain(monkeypatch, plan_mod, content=_plan_json())
    pool = _agenda_pool()
    dispatcher = SimpleNamespace(state=make_state(pool))
    res = await handle_planner_decompose({}, dispatcher)
    assert res["persisted"] is False
    assert _inserts(pool) == []


async def test_decompose_advisory_persists(monkeypatch):
    _mode(monkeypatch, "advisory")
    patch_chain(monkeypatch, plan_mod, content=_plan_json(claim_id=10, n_tasks=1))
    pool = _agenda_pool()
    dispatcher = SimpleNamespace(state=make_state(pool))
    res = await handle_planner_decompose({}, dispatcher)
    assert res["persisted"] is True
    assert res["tasks"] == 1 and res["directions_planned"] == 1
    assert len(_inserts(pool)) == 1


async def test_decompose_active_persists(monkeypatch):
    _mode(monkeypatch, "active")
    patch_chain(monkeypatch, plan_mod, content=_plan_json(claim_id=10, n_tasks=2))
    pool = _agenda_pool()
    dispatcher = SimpleNamespace(state=make_state(pool))
    res = await handle_planner_decompose({}, dispatcher)
    assert res["persisted"] is True
    assert res["tasks"] == 2  # MAX_TASKS_PER_DIRECTION defaults to 2
    assert len(_inserts(pool)) == 2


async def test_decompose_grade_fail_no_persist(monkeypatch):
    _mode(monkeypatch, "active")
    # claim_id 999 isn't in the approved set {10, 11} → grade fails on invalid refs
    patch_chain(monkeypatch, plan_mod, content=_plan_json(claim_id=999, n_tasks=1))
    pool = _agenda_pool()
    dispatcher = SimpleNamespace(state=make_state(pool))
    res = await handle_planner_decompose({}, dispatcher)
    assert res["persisted"] is False
    assert res["reason"] == "failed_grading"
    assert res["graded_pass"] is False
    assert _inserts(pool) == []


# ── handler.py: handle_queue_empty (market-era) ─────────────────────────────────
async def test_queue_empty_skips_non_research():
    res = await handle_queue_empty({"payload": {"department": "ops"}}, AsyncMock())
    assert res["skipped"] is True and "ops" in res["reason"]


async def test_queue_empty_skips_missing_payload():
    res = await handle_queue_empty({}, AsyncMock())
    assert res["skipped"] is True


def _planned_task(claim_id=1):
    return PlannedTask(
        claim_id=claim_id,
        task_type="deepen",
        description="probe the gap",
        query="is X true?",
        sources=["web"],
        priority=6,
    )


async def test_queue_empty_v2_with_tasks(monkeypatch):
    monkeypatch.setenv("PLANNER_LOOP", "v2")
    tasks = [_planned_task(1), _planned_task(2)]
    loop_stub = AsyncMock(return_value=(tasks, 42, "summary", 0.8))
    monkeypatch.setattr(loop_mod, "run_planner_loop", loop_stub)

    pool = ScriptedPool()
    dispatcher = AsyncMock()
    dispatcher.pool = pool
    event = {"id": 99, "payload": {"department": "research"}}
    res = await handle_queue_empty(event, dispatcher)

    assert res == {
        "tasks_created": 2,
        "run_id": 42,
        "reasoning": "summary",
        "critique_confidence": 0.8,
    }
    dispatcher.set_cooldown.assert_awaited_once()
    ins = [c for c in pool.calls if c[0] == "execute" and "INSERT INTO tasks" in c[1]]
    assert len(ins) == 2


async def test_queue_empty_v2_empty(monkeypatch):
    monkeypatch.setenv("PLANNER_LOOP", "v2")
    loop_stub = AsyncMock(return_value=([], 7, "nothing to do", 1.0))
    monkeypatch.setattr(loop_mod, "run_planner_loop", loop_stub)
    dispatcher = AsyncMock()
    dispatcher.pool = ScriptedPool()
    res = await handle_queue_empty({"id": 1, "payload": {"department": "research"}}, dispatcher)
    assert res == {
        "tasks_created": 0,
        "run_id": 7,
        "reasoning": "nothing to do",
        "critique_confidence": 1.0,
    }


async def test_queue_empty_legacy_with_tasks(monkeypatch):
    monkeypatch.setenv("PLANNER_LOOP", "legacy")
    planned = PlannedTasks(tasks=[_planned_task(1)], reasoning="covers the under-explored claim space")
    pool = ScriptedPool()
    dispatcher = AsyncMock()
    dispatcher.pool = pool
    dispatcher.curator.build = AsyncMock(return_value="PROMPT")
    dispatcher.router.invoke = AsyncMock(return_value=(planned, 55))
    res = await handle_queue_empty({"id": 3, "payload": {"department": "research"}}, dispatcher)
    assert res == {"tasks_created": 1, "run_id": 55, "reasoning": planned.reasoning}
    ins = [c for c in pool.calls if c[0] == "execute" and "INSERT INTO tasks" in c[1]]
    assert len(ins) == 1


async def test_queue_empty_legacy_empty(monkeypatch):
    monkeypatch.setenv("PLANNER_LOOP", "legacy")
    planned = PlannedTasks(tasks=[], reasoning="no active claims to plan against right now")
    dispatcher = AsyncMock()
    dispatcher.pool = ScriptedPool()
    dispatcher.curator.build = AsyncMock(return_value="PROMPT")
    dispatcher.router.invoke = AsyncMock(return_value=(planned, 5))
    res = await handle_queue_empty({"id": 4, "payload": {"department": "research"}}, dispatcher)
    assert res == {"tasks_created": 0, "run_id": 5, "reasoning": planned.reasoning}


# ── handler.py: _build_planner_task_data ────────────────────────────────────────
def _claim(cid=1, claim="claim text", conf=0.5):
    return SimpleNamespace(id=cid, statement=claim, claim=claim, confidence=conf)


def _finding(fid=1):
    return SimpleNamespace(id=fid, relevance_score=0.9, audit_verdict="ok", title="finding title")


def _company_state():
    return SimpleNamespace(
        current_phase="exploration",
        bootstrap_at=datetime.now(UTC),
    )


async def test_build_planner_task_data_no_claims():
    state = AsyncMock()
    state.get_active_claims = AsyncMock(return_value=[])
    state.get_company_state = AsyncMock(return_value=_company_state())
    layer = await _build_planner_task_data({}, state, None)
    assert "no active claims" in layer.content
    assert layer.name == "task_data"


async def test_build_planner_task_data_with_claims_and_findings():
    state = AsyncMock()
    state.get_active_claims = AsyncMock(return_value=[_claim(1), _claim(2)])
    state.get_company_state = AsyncMock(return_value=_company_state())
    state.get_recent_findings_for_claim = AsyncMock(side_effect=[[_finding(1)], []])
    layer = await _build_planner_task_data({}, state, None)
    assert "research queue is empty" in layer.content
    assert "F1 [rel 0.9" in layer.content  # claim 1 has a finding
    assert "(no findings yet)" in layer.content  # claim 2 has none


# ── loop.py: prompt builders ────────────────────────────────────────────────────
def _thesis(tid=1, claim="thesis claim", conf=0.7):
    return SimpleNamespace(id=tid, statement=claim, claim=claim, confidence=conf)


def _tfinding(fid=1):
    return SimpleNamespace(id=fid, relevance_score=0.8, audit_verdict="ok", supports_thesis=True, title="t title")


async def test_build_assess_state_with_theses():
    state = AsyncMock()
    state.get_active_theses = AsyncMock(return_value=[_thesis(1), _thesis(2)])
    state.get_company_state = AsyncMock(return_value=_company_state())
    state.get_recent_findings_for_thesis = AsyncMock(side_effect=[[_tfinding(1)], []])
    layer = await _build_assess_state({}, state, None)
    assert "Assess the research portfolio" in layer.content
    assert "F1 [rel 0.8" in layer.content
    assert "(no findings yet)" in layer.content


async def test_build_assess_state_no_theses():
    state = AsyncMock()
    state.get_active_theses = AsyncMock(return_value=[])
    state.get_company_state = AsyncMock(return_value=_company_state())
    layer = await _build_assess_state({}, state, None)
    assert "(no active theses)" in layer.content


async def test_build_propose_tasks():
    gap = ThesisGap(thesis_id=1, evidence_gap="no comparison", suggested_task_type="deepen", priority_score=0.7)
    assessment = StateAssessment(thesis_gaps=[gap], portfolio_notes="thin", target_task_count=6)
    ctx = {
        "state": _company_state(),
        "assessment": assessment,
        "theses_by_id": {1: _thesis(1)},
    }
    layer = await _build_propose_tasks(ctx, AsyncMock(), None)
    assert "Propose research tasks" in layer.content
    assert "T1 (priority 0.70" in layer.content
    assert "Claim: thesis claim" in layer.content


async def test_build_propose_tasks_unknown_thesis():
    gap = ThesisGap(thesis_id=999, evidence_gap="g", suggested_task_type="compare", priority_score=0.1)
    assessment = StateAssessment(thesis_gaps=[gap], portfolio_notes="n", target_task_count=4)
    ctx = {"state": _company_state(), "assessment": assessment, "theses_by_id": {}}
    layer = await _build_propose_tasks(ctx, AsyncMock(), None)
    assert "Claim: (unknown)" in layer.content


async def test_build_propose_tasks_no_gaps():
    assessment = StateAssessment(
        thesis_gaps=[ThesisGap(thesis_id=1, evidence_gap="g", suggested_task_type="deepen", priority_score=0.0)],
        portfolio_notes="n",
        target_task_count=4,
    )
    # empty gap_lines branch: build ctx with a gap but render path still exercised;
    # to hit the "(no gaps...)" line, pass an assessment-like object with no gaps.
    empty = SimpleNamespace(thesis_gaps=[], portfolio_notes="n", target_task_count=4)
    ctx = {"state": _company_state(), "assessment": empty, "theses_by_id": {}}
    layer = await _build_propose_tasks(ctx, AsyncMock(), None)
    assert "(no gaps — emit empty list)" in layer.content
    assert assessment.target_task_count == 4


def _loop_task(thesis_id=1):
    # PlannedTask exposes claim_id; loop builders/orchestrator read t.claim_id, so use a stub.
    return SimpleNamespace(
        claim_id=thesis_id,
        task_type="deepen",
        description="probe",
        query="q?",
        sources=["web"],
        priority=5,
    )


async def test_build_critique_with_proposal():
    gap = ThesisGap(thesis_id=1, evidence_gap="g", suggested_task_type="deepen", priority_score=0.5)
    assessment = StateAssessment(thesis_gaps=[gap], portfolio_notes="n", target_task_count=4)
    proposal = SimpleNamespace(tasks=[_loop_task(1), _loop_task(999)], reasoning="why")
    ctx = {"assessment": assessment, "proposal": proposal, "theses_by_id": {1: _thesis(1)}}
    layer = await _build_critique(ctx, AsyncMock(), None)
    assert "Critique the proposed task batch" in layer.content
    assert "thesis:  thesis claim" in layer.content  # known thesis
    assert "(unknown)" in layer.content  # task 999 has no thesis
    assert "Proposed tasks (2)" in layer.content


async def test_build_critique_empty_proposal():
    gap = ThesisGap(thesis_id=1, evidence_gap="g", suggested_task_type="deepen", priority_score=0.5)
    assessment = StateAssessment(thesis_gaps=[gap], portfolio_notes="n", target_task_count=4)
    proposal = SimpleNamespace(tasks=[], reasoning="empty")
    ctx = {"assessment": assessment, "proposal": proposal, "theses_by_id": {}}
    layer = await _build_critique(ctx, AsyncMock(), None)
    assert "(empty)" in layer.content


# ── loop.py: run_planner_loop ───────────────────────────────────────────────────
def _dispatcher_for_loop(*, invoke_side_effect=None, invoke_return=None):
    d = AsyncMock()
    d.router = AsyncMock()
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    if invoke_side_effect is not None:
        d.router.invoke = AsyncMock(side_effect=invoke_side_effect)
    else:
        d.router.invoke = AsyncMock(return_value=invoke_return)
    d.state = AsyncMock()
    d.session = object()
    return d


async def test_run_planner_loop_no_thesis_gaps():
    assessment = SimpleNamespace(thesis_gaps=[])
    d = _dispatcher_for_loop(invoke_return=(assessment, 11))
    final, run_id, summary, conf = await run_planner_loop(dispatcher=d, triggered_by_event_id=1)
    assert final == [] and run_id == 11 and summary == "no active theses" and conf == 1.0


async def test_run_planner_loop_empty_proposal():
    assessment = SimpleNamespace(thesis_gaps=[SimpleNamespace(thesis_id=1)])
    proposal = SimpleNamespace(tasks=[], reasoning="nothing useful")
    d = _dispatcher_for_loop(invoke_side_effect=[(assessment, 11), (proposal, 22)])
    d.state.get_active_theses = AsyncMock(return_value=[_thesis(1)])
    d.state.get_company_state = AsyncMock(return_value=_company_state())
    final, run_id, summary, conf = await run_planner_loop(dispatcher=d, triggered_by_event_id=1)
    assert final == [] and run_id == 22 and summary == "nothing useful" and conf == 1.0


async def test_run_planner_loop_full_filters_unknown_theses():
    assessment = SimpleNamespace(thesis_gaps=[SimpleNamespace(thesis_id=1)])
    proposal = SimpleNamespace(tasks=[_loop_task(1)], reasoning="r")
    critiqued = SimpleNamespace(
        final_tasks=[_loop_task(1), _loop_task(999)],  # 999 not active → filtered
        changes_summary="kept one",
        confidence=0.6,
    )
    d = _dispatcher_for_loop(invoke_side_effect=[(assessment, 11), (proposal, 22), (critiqued, 33)])
    d.state.get_active_theses = AsyncMock(return_value=[_thesis(1)])
    d.state.get_company_state = AsyncMock(return_value=_company_state())
    final, run_id, summary, conf = await run_planner_loop(dispatcher=d, triggered_by_event_id=7)
    assert [t.claim_id for t in final] == [1]
    assert run_id == 33 and summary == "kept one" and conf == 0.6


async def test_run_planner_loop_full_no_filtering():
    assessment = SimpleNamespace(thesis_gaps=[SimpleNamespace(thesis_id=1)])
    proposal = SimpleNamespace(tasks=[_loop_task(1)], reasoning="r")
    critiqued = SimpleNamespace(final_tasks=[_loop_task(1)], changes_summary="ok", confidence=0.9)
    d = _dispatcher_for_loop(invoke_side_effect=[(assessment, 11), (proposal, 22), (critiqued, 33)])
    d.state.get_active_theses = AsyncMock(return_value=[_thesis(1)])
    d.state.get_company_state = AsyncMock(return_value=_company_state())
    final, run_id, summary, conf = await run_planner_loop(dispatcher=d, triggered_by_event_id=7)
    assert len(final) == 1 and run_id == 33 and conf == 0.9
