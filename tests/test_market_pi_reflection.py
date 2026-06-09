"""Pytest coverage for the market-era PI phase handlers and the reflection loop.

Targets (all mocked — NO Postgres/Neo4j/Ollama/DeepSeek/network):
  - agents.pi.claim_invalidated     (claim.invalidated → spawn / no_action / pivot)
  - agents.pi.phase_adjudicator     (claim.confidence_changed → propose transition)
  - agents.pi.phase_budget_exceeded (watchdog forces a transition proposal)
  - agents.pi.phase_transition      (PI ratify / reject / defer; charter on commit)
  - agents.reflection.handler       (legacy + v2 batch reflection; lesson judging)

The LLM is reached only via dispatcher.router.invoke (an AsyncMock returning a
(decision, run_id) tuple of REAL pydantic objects). curator.build is an AsyncMock
returning a dummy prompt; state methods are AsyncMocks; the DB is a ScriptedPool;
memory/set_cooldown/lessons.* are AsyncMocks. No DATABASE_URL is required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.pi.phase_transition as phase_transition
from agents.pi.claim_invalidated import (
    ReplacementCategory,
    SpawnReplacementDecision,
    _build_spawn_replacement_task_data,
    handle_claim_invalidated,
)
from agents.pi.phase_adjudicator import (
    AdjudicatorVerdict,
    _build_adjudicator_task_data,
    handle_claim_confidence_changed,
)
from agents.pi.phase_budget_exceeded import handle_phase_budget_exceeded
from agents.pi.phase_transition import (
    CharterContent,
    PhaseTransitionDecision,
    _build_phase_transition_task_data,
    _request_pi_sources,
    handle_phase_transition_proposed,
)
from agents.reflection.handler import (
    ApplicationJudgements,
    BatchReflectionOutput,
    LessonCandidate,
    LessonJudgement,
    ReflectionOutput,
    _build_batch_reflection_task_data,
    _build_judge_applications_task_data,
    _build_reflection_task_data,
    _persist_or_credit_lesson,
    handle_reflection_requested,
    judge_pending_lesson_applications,
)
from tests._helpers import ScriptedPool

pytestmark = pytest.mark.asyncio

_BORN = datetime(2026, 1, 2, tzinfo=UTC)


# ── shared stubs ─────────────────────────────────────────────────────────────
def _claim(cid=10, *, claim="thesis claim", conf=0.6, kill_reason="too narrow"):
    return SimpleNamespace(id=cid, claim=claim, confidence=conf, created_at=_BORN, kill_reason=kill_reason)


def _company(phase="exploration", *, paused=False, days_ago=5):
    return SimpleNamespace(
        current_phase=phase,
        paused=paused,
        phase_started_at=datetime.now(UTC) - timedelta(days=days_ago),
    )


def _finding(fid=1, *, audit="pass", supports=True):
    return SimpleNamespace(id=fid, audit_verdict=audit, supports_thesis=supports)


def _category(claim="new niche idea", *, qs=None):
    return ReplacementCategory(
        claim=claim,
        rationale="this explores adjacent space distinctly" * 2,
        risks="might still be crowded",
        disambiguating_questions=qs or ["q1?", "q2?", "q3?"],
    )


def _charter():
    return CharterContent(
        claim="A self-hosted Postgres-tuning advisor for solo founders to ship faster.",
        niche="solo technical founders running their own Postgres",
        audience="indie hackers reachable on developer Twitter and HN",
        product="a CLI + dashboard that recommends index and config changes",
        gtm="content marketing on developer Twitter; $39/mo subscription",
        success_metric="100 paying subscribers within 90 days of launch",
    )


# =============================================================================
# claim_invalidated — _build_spawn_replacement_task_data
# =============================================================================
async def test_build_spawn_task_data_with_siblings():
    state = AsyncMock()
    state.get_claim = AsyncMock(return_value=_claim(10))
    state.get_active_claims = AsyncMock(return_value=[_claim(11, claim="sibling A", conf=0.5)])
    layer = await _build_spawn_replacement_task_data({"invalidated_claim_id": 10}, state, None)
    assert layer.name == "task_data"
    assert "A claim was just killed" in layer.content
    assert "T11: sibling A (conf 0.50)" in layer.content
    assert "too narrow" in layer.content


async def test_build_spawn_task_data_no_siblings_and_no_kill_reason():
    state = AsyncMock()
    state.get_claim = AsyncMock(return_value=_claim(10, kill_reason=None))
    state.get_active_claims = AsyncMock(return_value=[])
    layer = await _build_spawn_replacement_task_data({"invalidated_claim_id": 10}, state, None)
    assert "(no active siblings" in layer.content
    assert "(none recorded)" in layer.content


# =============================================================================
# claim_invalidated — handle_claim_invalidated
# =============================================================================
def _ci_dispatcher(decision, run_id=99, *, pool=None):
    d = AsyncMock()
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.router = AsyncMock()
    d.router.invoke = AsyncMock(return_value=(decision, run_id))
    d.state = AsyncMock()
    d.memory = AsyncMock()
    d.pool = pool if pool is not None else ScriptedPool()
    return d


async def test_claim_invalidated_spawn_creates_claims_and_tasks():
    decision = SpawnReplacementDecision(
        action="spawn",
        reasoning="two adjacent ideas worth probing right now",
        categories=[_category("idea one"), _category("idea two")],
    )
    d = _ci_dispatcher(decision, 42)
    d.state.create_claim = AsyncMock(side_effect=[SimpleNamespace(id=101), SimpleNamespace(id=102)])
    res = await handle_claim_invalidated({"id": 1, "target_id": 10}, d)
    assert res["action"] == "spawn"
    assert res["new_claim_ids"] == [101, 102]
    assert res["run_id"] == 42
    assert d.state.create_claim.await_count == 2
    # 2 categories × 3 disambiguating questions → 6 task inserts
    inserts = [c for c in d.pool.calls if c[0] == "execute" and "INSERT INTO tasks" in c[1]]
    assert len(inserts) == 6
    d.memory.write_message.assert_awaited_once()
    assert d.memory.write_message.await_args.kwargs["session_id"] == "pi-deliberations"


async def test_claim_invalidated_no_action():
    decision = SpawnReplacementDecision(action="no_action", reasoning="the slate is already well covered here")
    d = _ci_dispatcher(decision)
    res = await handle_claim_invalidated({"id": 2, "target_id": 10}, d)
    assert res["action"] == "no_action"
    assert "new_claim_ids" not in res and "pivoted_claim_id" not in res
    d.state.create_claim.assert_not_awaited()
    # only the deliberation memory write — no task inserts
    assert not any("INSERT INTO tasks" in c[1] for c in d.pool.calls)


async def test_claim_invalidated_pivot_raises_priority():
    decision = SpawnReplacementDecision(
        action="pivot",
        reasoning="raise energy on an existing sibling instead",
        pivot_claim_id=77,
    )
    d = _ci_dispatcher(decision)
    res = await handle_claim_invalidated({"id": 3, "target_id": 10}, d)
    assert res["pivoted_claim_id"] == 77
    updates = [c for c in d.pool.calls if c[0] == "execute" and "UPDATE tasks" in c[1]]
    assert len(updates) == 1
    assert updates[0][2] == (77,)


async def test_claim_invalidated_pivot_without_id_is_noop():
    # action=pivot but pivot_claim_id None → the elif guard fails, no UPDATE.
    decision = SpawnReplacementDecision(
        action="pivot",
        reasoning="ambiguous — model forgot to name a sibling claim id",
        pivot_claim_id=None,
    )
    d = _ci_dispatcher(decision)
    res = await handle_claim_invalidated({"id": 4, "target_id": 10}, d)
    assert "pivoted_claim_id" not in res
    assert not any("UPDATE tasks" in c[1] for c in d.pool.calls)


# =============================================================================
# phase_adjudicator — _build_adjudicator_task_data
# =============================================================================
async def test_build_adjudicator_task_data():
    layer = await _build_adjudicator_task_data(
        {"current_phase": "exploration", "days_in_phase": 4, "claims_summary": "- T1: ..."},
        None,
        None,
    )
    assert "Phase transition adjudication" in layer.content
    assert "**exploration**" in layer.content
    assert "Day in phase: 4" in layer.content


# =============================================================================
# phase_adjudicator — handle_claim_confidence_changed
# =============================================================================
def _adj_dispatcher(verdict=None, run_id=7, *, company=None, claims=None, pool=None):
    d = AsyncMock()
    d.state = AsyncMock()
    d.state.get_company_state = AsyncMock(return_value=company or _company())
    d.state.get_active_claims = AsyncMock(return_value=claims if claims is not None else [_claim(10)])
    d.state.get_recent_findings_for_claim = AsyncMock(return_value=[_finding(1)])
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.router = AsyncMock()
    if verdict is not None:
        d.router.invoke = AsyncMock(return_value=(verdict, run_id))
    d.session = object()
    d.pool = pool if pool is not None else ScriptedPool()
    return d


async def test_adjudicator_skips_in_execution():
    d = _adj_dispatcher(company=_company("execution"))
    res = await handle_claim_confidence_changed({"id": 1}, d)
    assert res == {"skipped": True, "reason": "already in execution"}
    d.router.invoke.assert_not_awaited()


async def test_adjudicator_skips_when_paused():
    d = _adj_dispatcher(company=_company("exploration", paused=True))
    res = await handle_claim_confidence_changed({"id": 1}, d)
    assert res == {"skipped": True, "reason": "company paused"}


async def test_adjudicator_skips_no_active_claims():
    d = _adj_dispatcher(claims=[])
    res = await handle_claim_confidence_changed({"id": 1}, d)
    assert res == {"skipped": True, "reason": "no active claims"}


async def test_adjudicator_no_transition_returns_false():
    verdict = AdjudicatorVerdict(transition=False, reasoning="criteria are not met yet here", confidence=0.6)
    d = _adj_dispatcher(verdict)
    res = await handle_claim_confidence_changed({"id": 9}, d)
    assert res == {"transition": False, "reasoning": verdict.reasoning, "run_id": 7}
    # no event emitted
    assert not any("phase.transition_proposed" in c[1] for c in d.pool.calls)


async def test_adjudicator_transition_true_but_no_target_returns_false():
    # transition=True but target_phase None → still treated as no-transition.
    verdict = AdjudicatorVerdict(transition=True, target_phase=None, reasoning="want to but unsure where", confidence=0.5)
    d = _adj_dispatcher(verdict)
    res = await handle_claim_confidence_changed({"id": 9}, d)
    assert res["transition"] is False


async def test_adjudicator_emits_transition_proposed():
    verdict = AdjudicatorVerdict(
        transition=True,
        target_phase="convergence",
        reasoning="three claims are converging on signal",
        cited_claim_ids=[10, 11],
        confidence=0.8,
    )
    d = _adj_dispatcher(verdict, run_id=55)
    res = await handle_claim_confidence_changed({"id": 9}, d)
    assert res == {
        "transition": True,
        "target_phase": "convergence",
        "reasoning": verdict.reasoning,
        "run_id": 55,
    }
    emits = [c for c in d.pool.calls if "phase.transition_proposed" in c[1]]
    assert len(emits) == 1
    # INSERT args: ($1 payload json, $2 run_id, $3 target_phase)
    assert emits[0][2][1] == 55
    assert emits[0][2][2] == "convergence"


# =============================================================================
# phase_budget_exceeded — handle_phase_budget_exceeded
# =============================================================================
def _budget_dispatcher(pool=None):
    d = AsyncMock()
    d.pool = pool if pool is not None else ScriptedPool()
    d.memory = AsyncMock()
    return d


async def test_budget_exceeded_missing_phase():
    d = _budget_dispatcher()
    res = await handle_phase_budget_exceeded({"payload": {}}, d)
    assert res == {"skipped": True, "reason": "no phase in payload"}
    d.memory.write_message.assert_not_awaited()


async def test_budget_exceeded_no_payload_key():
    # event with no payload at all → payload defaults {} → missing phase.
    d = _budget_dispatcher()
    res = await handle_phase_budget_exceeded({}, d)
    assert res["skipped"] is True


async def test_budget_exceeded_execution_has_no_next():
    d = _budget_dispatcher()
    res = await handle_phase_budget_exceeded({"payload": {"phase": "execution"}}, d)
    assert res == {"skipped": True, "reason": "no transition out of execution"}


async def test_budget_exceeded_unknown_phase():
    d = _budget_dispatcher()
    res = await handle_phase_budget_exceeded({"payload": {"phase": "nonsense"}}, d)
    assert res == {"skipped": True, "reason": "no transition out of nonsense"}


async def test_budget_exceeded_forces_transition():
    d = _budget_dispatcher()
    res = await handle_phase_budget_exceeded({"payload": {"phase": "exploration", "elapsed_days": 9}}, d)
    assert res == {
        "forced_transition_proposed": True,
        "from_phase": "exploration",
        "to_phase": "convergence",
    }
    emits = [c for c in d.pool.calls if "phase.transition_proposed" in c[1]]
    assert len(emits) == 1
    d.memory.write_message.assert_awaited_once()
    assert d.memory.write_message.await_args.kwargs["session_id"] == "phase-transitions"


# =============================================================================
# phase_transition — _build_phase_transition_task_data
# =============================================================================
async def test_build_transition_task_data_commitment_forced():
    state = AsyncMock()
    state.get_active_claims = AsyncMock(return_value=[_claim(10, claim="winner")])
    layer = await _build_phase_transition_task_data(
        {
            "from_phase": "convergence",
            "target_phase": "commitment",
            "adjudicator_reasoning": "top claim is strong",
            "forced": True,
        },
        state,
        None,
    )
    assert "transitioning toward execution" in layer.content
    assert "FORCED by the watchdog" in layer.content
    assert "T10: conf 0.60" in layer.content


async def test_build_transition_task_data_convergence_no_claims():
    state = AsyncMock()
    state.get_active_claims = AsyncMock(return_value=[])
    layer = await _build_phase_transition_task_data(
        {
            "from_phase": "exploration",
            "target_phase": "convergence",
            "adjudicator_reasoning": "narrowing the field",
        },
        state,
        None,
    )
    assert "transitioning to convergence" in layer.content
    assert "FORCED by the watchdog" not in layer.content
    assert "(none active)" in layer.content


# =============================================================================
# phase_transition — _request_pi_sources
# =============================================================================
async def test_request_pi_sources_off_by_default(monkeypatch):
    monkeypatch.delenv("MIMIR_LOOP", raising=False)
    called = AsyncMock()
    monkeypatch.setattr("agents.mimir.acquire.request_acquire", called)
    await _request_pi_sources(["topic a"], AsyncMock())
    called.assert_not_awaited()


async def test_request_pi_sources_on_dispatches(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    called = AsyncMock()
    monkeypatch.setattr("agents.mimir.acquire.request_acquire", called)
    # third entry is blank → skipped; only the first two acquire.
    await _request_pi_sources(["topic a", " topic b ", "  "], AsyncMock())
    assert called.await_count == 2


async def test_request_pi_sources_swallows_errors(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "v1")
    boom = AsyncMock(side_effect=RuntimeError("acquire down"))
    monkeypatch.setattr("agents.mimir.acquire.request_acquire", boom)
    # must not raise despite the failure
    await _request_pi_sources(["topic a"], AsyncMock())
    boom.assert_awaited_once()


# =============================================================================
# phase_transition — handle_phase_transition_proposed
# =============================================================================
def _pt_dispatcher(decision, run_id=99, *, company=None, pool=None):
    d = AsyncMock()
    d.state = AsyncMock()
    d.state.get_company_state = AsyncMock(return_value=company or _company("convergence"))
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.router = AsyncMock()
    d.router.invoke = AsyncMock(return_value=(decision, run_id))
    d.memory = AsyncMock()
    d.set_cooldown = AsyncMock()
    d.session = object()
    d.pool = pool if pool is not None else ScriptedPool()
    return d


def _event(from_phase="convergence", to_phase="commitment", *, forced=False, reasoning="ok"):
    return {
        "id": 1,
        "payload": {
            "from_phase": from_phase,
            "to_phase": to_phase,
            "reasoning": reasoning,
            "cited_claim_ids": [10],
            "forced": forced,
        },
    }


async def test_transition_skips_phase_mismatch():
    decision = PhaseTransitionDecision(action="ratify", reasoning="x" * 30)
    d = _pt_dispatcher(decision, company=_company("exploration"))
    res = await handle_phase_transition_proposed(_event("convergence", "commitment"), d)
    assert res["skipped"] is True
    assert "not convergence" in res["reason"]
    d.router.invoke.assert_not_awaited()


async def test_transition_reject():
    decision = PhaseTransitionDecision(action="reject", reasoning="not enough evidence to commit yet at all")
    d = _pt_dispatcher(decision, 33)
    res = await handle_phase_transition_proposed(_event(), d)
    assert res == {
        "action": "reject",
        "from_phase": "convergence",
        "target_phase": "commitment",
        "run_id": 33,
    }
    # no DB writes on a reject
    assert not any(c[0] == "execute" for c in d.pool.calls)
    d.memory.write_message.assert_awaited_once()
    d.set_cooldown.assert_not_awaited()


async def test_transition_defer_sets_cooldown():
    decision = PhaseTransitionDecision(action="defer", reasoning="this call is genuinely close — get more")
    d = _pt_dispatcher(decision)
    res = await handle_phase_transition_proposed(_event(), d)
    assert res["action"] == "defer"
    d.set_cooldown.assert_awaited_once()
    assert d.set_cooldown.await_args.kwargs["invocation_type"] == "phase_adjudicator.check"
    assert d.set_cooldown.await_args.kwargs["seconds"] == 12 * 3600
    d.memory.write_message.assert_awaited_once()


async def test_transition_soft_ratify_to_convergence():
    # ratify a convergence move (not commitment/execution) → no charter, plain
    # phase update + phase_transitions row.
    decision = PhaseTransitionDecision(action="ratify", reasoning="enter convergence; narrow and hunt contradictions")
    d = _pt_dispatcher(decision, 44, company=_company("exploration"))
    res = await handle_phase_transition_proposed(_event("exploration", "convergence"), d)
    assert res["effective_target_phase"] == "convergence"
    assert "charter_written" not in res
    execs = [c[1] for c in d.pool.calls if c[0] == "execute"]
    assert any("UPDATE company_state" in s for s in execs)
    assert any("INSERT INTO phase_transitions" in s for s in execs)
    assert not any("UPDATE claims" in s for s in execs)


async def test_transition_ratify_writes_charter_to_execution():
    decision = PhaseTransitionDecision(
        action="ratify",
        reasoning="commit to the winning claim and write the charter now",
        chosen_claim_id=10,
        charter=_charter(),
    )
    d = _pt_dispatcher(decision, 88)
    res = await handle_phase_transition_proposed(_event("convergence", "commitment"), d)
    # commitment with a charter collapses straight to execution
    assert res["effective_target_phase"] == "execution"
    assert res["charter_written"] is True
    assert res["chosen_claim_id"] == 10
    execs = [c[1] for c in d.pool.calls if c[0] == "execute"]
    assert any("SET current_phase = $1::phase" in s and "charter" in s for s in execs)
    # losing claims merged + the winner promoted
    assert any("status = 'merged'" in s for s in execs)
    assert any("status = 'promoted'" in s for s in execs)
    # two memory writes: phase-transitions narrative + charter snapshot
    sessions = [c.kwargs["session_id"] for c in d.memory.write_message.await_args_list]
    assert sessions == ["phase-transitions", "charter"]


async def test_transition_ratify_commitment_without_chosen_claim():
    # charter present but chosen_claim_id None → charter written, but the
    # claims merge/promote block is skipped.
    decision = PhaseTransitionDecision(
        action="ratify",
        reasoning="write the charter even though no single claim id was named",
        chosen_claim_id=None,
        charter=_charter(),
    )
    d = _pt_dispatcher(decision)
    res = await handle_phase_transition_proposed(_event("convergence", "commitment"), d)
    assert res["charter_written"] is True
    assert "chosen_claim_id" not in res
    execs = [c[1] for c in d.pool.calls if c[0] == "execute"]
    assert not any("status = 'merged'" in s for s in execs)


async def test_transition_ratify_commitment_without_charter_stays_plain():
    # target is commitment but the model omitted the charter → write_charter
    # False → plain phase update to the (non-collapsed) target.
    decision = PhaseTransitionDecision(
        action="ratify",
        reasoning="ratify the move but no charter content was provided here",
        charter=None,
    )
    d = _pt_dispatcher(decision)
    res = await handle_phase_transition_proposed(_event("convergence", "commitment"), d)
    assert res["effective_target_phase"] == "commitment"
    assert "charter_written" not in res
    execs = [c[1] for c in d.pool.calls if c[0] == "execute"]
    assert any("phase_started_at = NOW()" in s and "charter" not in s for s in execs)


async def test_transition_needed_sources_requested(monkeypatch):
    # needed_sources present → _request_pi_sources is invoked (mocked).
    req = AsyncMock()
    monkeypatch.setattr(phase_transition, "_request_pi_sources", req)
    decision = PhaseTransitionDecision(
        action="reject",
        reasoning="missing a couple of sources to decide this well",
        needed_sources=["a recent pricing teardown"],
    )
    d = _pt_dispatcher(decision)
    await handle_phase_transition_proposed(_event(), d)
    req.assert_awaited_once()
    assert req.await_args.args[0] == ["a recent pricing teardown"]


# =============================================================================
# reflection — _build_reflection_task_data / _build_batch_reflection_task_data
# =============================================================================
async def test_build_reflection_task_data():
    layer = await _build_reflection_task_data(
        {"invocation_type": "critic.kill_verdict", "run_summary": "killed T7"}, None, None
    )
    assert "Reflection on a dissenting run" in layer.content
    assert "critic.kill_verdict" in layer.content
    assert "killed T7" in layer.content


async def test_build_batch_reflection_task_data_with_runs():
    runs = [
        {"id": 1, "invocation_type": "critic.kill_verdict", "output_summary": "killed", "ago": "2h ago"},
        {"id": 2, "invocation_type": "evaluation.slop_score", "output_summary": None, "ago": "1d ago"},
    ]
    layer = await _build_batch_reflection_task_data({"runs": runs}, None, None)
    assert "Run #1 (critic.kill_verdict, 2h ago)" in layer.content
    assert "(no summary)" in layer.content
    assert "You see 2 runs" in layer.content


async def test_build_batch_reflection_task_data_empty():
    layer = await _build_batch_reflection_task_data({"runs": []}, None, None)
    assert "(no dissents in the window" in layer.content


# =============================================================================
# reflection — _persist_or_credit_lesson (3 branches)
# =============================================================================
def _cand(invocation="reflect.lesson_propose"):
    return LessonCandidate(
        lesson_text="discount AI-newsletter-cited findings by two points here",
        applies_to_invocation=invocation,
        applies_when={},
        rationale="this pattern recurs across multiple dissenting runs",
    )


async def test_persist_drops_unregistered_invocation():
    d = AsyncMock()
    d.lessons = AsyncMock()
    out = await _persist_or_credit_lesson(d, _cand("totally.unregistered_invocation"), 7)
    assert out is None
    d.lessons.find_near_duplicate.assert_not_awaited()
    d.lessons.insert_lesson_candidate.assert_not_awaited()


async def test_persist_credits_recurrence_on_dup():
    d = AsyncMock()
    d.lessons = AsyncMock()
    d.lessons.find_near_duplicate = AsyncMock(return_value=314)
    d.lessons.credit_recurrence = AsyncMock()
    out = await _persist_or_credit_lesson(d, _cand(), 7)
    assert out == 314
    d.lessons.credit_recurrence.assert_awaited_once_with(314, 7)
    d.lessons.insert_lesson_candidate.assert_not_awaited()


async def test_persist_inserts_new_lesson():
    d = AsyncMock()
    d.lessons = AsyncMock()
    d.lessons.find_near_duplicate = AsyncMock(return_value=None)
    d.lessons.insert_lesson_candidate = AsyncMock(return_value=900)
    out = await _persist_or_credit_lesson(d, _cand(), 7)
    assert out == 900
    d.lessons.insert_lesson_candidate.assert_awaited_once()
    assert d.lessons.insert_lesson_candidate.await_args.kwargs["derived_via"] == "reflection"


# =============================================================================
# reflection — handle_reflection_requested (legacy)
# =============================================================================
def _reflect_dispatcher(decision, run_id=12, *, pool=None):
    d = AsyncMock()
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.router = AsyncMock()
    d.router.invoke = AsyncMock(return_value=(decision, run_id))
    d.session = object()
    d.lessons = AsyncMock()
    d.pool = pool if pool is not None else ScriptedPool()
    return d


async def test_legacy_reflection_run_not_found(monkeypatch):
    monkeypatch.setenv("REFLECTION_LOOP", "legacy")
    d = _reflect_dispatcher(None, pool=ScriptedPool())  # fetchrow → None
    res = await handle_reflection_requested({"id": 1, "target_id": 5}, d)
    assert res == {"skipped": True, "reason": "run not found"}
    d.router.invoke.assert_not_awaited()


async def test_legacy_reflection_no_lesson(monkeypatch):
    monkeypatch.setenv("REFLECTION_LOOP", "legacy")
    run_row = {"invocation_type": "critic.kill_verdict", "output_summary": "s", "status": "completed"}
    pool = ScriptedPool(rules=[("FROM agent_runs", [run_row])])
    decision = ReflectionOutput(should_create_lesson=False, candidate=None, reasoning="one-off mistake here")
    d = _reflect_dispatcher(decision, 21, pool=pool)
    res = await handle_reflection_requested({"id": 1, "target_id": 5}, d)
    assert res == {"lesson_created": False, "reasoning": "one-off mistake here", "run_id": 21}
    d.lessons.insert_lesson_candidate.assert_not_awaited()


async def test_legacy_reflection_creates_lesson(monkeypatch):
    monkeypatch.setenv("REFLECTION_LOOP", "legacy")
    run_row = {"invocation_type": "critic.kill_verdict", "output_summary": None, "status": "completed"}
    pool = ScriptedPool(rules=[("FROM agent_runs", [run_row])])
    decision = ReflectionOutput(should_create_lesson=True, candidate=_cand(), reasoning="a real recurring pattern")
    d = _reflect_dispatcher(decision, 22, pool=pool)
    d.lessons.find_near_duplicate = AsyncMock(return_value=None)
    d.lessons.insert_lesson_candidate = AsyncMock(return_value=555)
    res = await handle_reflection_requested({"id": 1, "target_id": 5}, d)
    assert res["lesson_created"] is True
    assert res["lesson_id"] == 555
    assert res["run_id"] == 22


# =============================================================================
# reflection — handle_reflection_requested (v2 batch)
# =============================================================================
def _ar_row(rid, *, inv="critic.kill_verdict", summary="killed", days_ago=0, hours_ago=0, mins_ago=0):
    ts = datetime.now(UTC) - timedelta(days=days_ago, hours=hours_ago, minutes=mins_ago)
    return {"id": rid, "invocation_type": inv, "output_summary": summary, "started_at": ts}


async def test_batch_reflection_dedup_short_circuits(monkeypatch):
    monkeypatch.setenv("REFLECTION_LOOP", "v2")
    # recent batch ran 100s ago (< 6h gap) → skip.
    pool = ScriptedPool(rules=[("reflect.batch_propose_lessons", 100)])
    d = _reflect_dispatcher(None, pool=pool)
    res = await handle_reflection_requested({"id": 1, "target_id": 5}, d)
    assert res["skipped"] is True
    assert "recent batch reflection" in res["reason"]
    d.router.invoke.assert_not_awaited()


async def test_batch_reflection_no_rows(monkeypatch):
    monkeypatch.setenv("REFLECTION_LOOP", "v2")
    # dedup fetchval → None (no recent batch); rows fetch → [].
    pool = ScriptedPool(rules=[("reflect.batch_propose_lessons", None), ("FROM agent_runs", [])])
    d = _reflect_dispatcher(None, pool=pool)
    res = await handle_reflection_requested({"id": 1, "target_id": 5}, d)
    assert res["skipped"] is True
    assert "no dissenting runs" in res["reason"]


async def test_batch_reflection_creates_lessons(monkeypatch):
    monkeypatch.setenv("REFLECTION_LOOP", "v2")
    rows = [
        _ar_row(1, mins_ago=30),  # < 1h → "Xm ago"
        _ar_row(2, hours_ago=3),  # < 1d → "Xh ago"
        _ar_row(3, days_ago=2),  # ≥ 1d → "Xd ago"
    ]
    pool = ScriptedPool(rules=[("reflect.batch_propose_lessons", None), ("FROM agent_runs", rows)])
    output = BatchReflectionOutput(
        lessons=[_cand(), _cand("totally.dead_invocation")],
        reasoning="one recurring pattern across these runs",
    )
    d = _reflect_dispatcher(output, 70, pool=pool)
    d.lessons.find_near_duplicate = AsyncMock(return_value=None)
    d.lessons.insert_lesson_candidate = AsyncMock(return_value=601)
    res = await handle_reflection_requested({"id": 1, "target_id": 5}, d)
    assert res["batch_size"] == 3
    # one valid lesson inserted; the dead-invocation one is dropped
    assert res["lessons"] == 1
    assert res["lesson_ids"] == [601]
    assert res["run_id"] == 70


# =============================================================================
# reflection — _build_judge_applications_task_data
# =============================================================================
async def test_build_judge_applications_task_data():
    layer = await _build_judge_applications_task_data(
        {
            "invocation_type": "critic.kill_verdict",
            "run_status": "completed",
            "expectation": None,
            "outcome": None,
            "output_summary": "produced a kill",
            "lessons": [{"id": 5, "text": "discount newsletters"}],
        },
        None,
        None,
    )
    assert "Judge whether applied lessons helped" in layer.content
    assert "[5] discount newsletters" in layer.content
    assert "(none)" in layer.content  # expectation defaulted
    assert "produced a kill" in layer.content


# =============================================================================
# reflection — judge_pending_lesson_applications
# =============================================================================
def _judge_dispatcher(*, rows=None, out=None, judge_run_id=900):
    d = AsyncMock()
    d.lessons = AsyncMock()
    d.lessons.fetch_pending_applications = AsyncMock(return_value=rows or [])
    d.lessons.set_application_outcome = AsyncMock()
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.router = AsyncMock()
    if out is not None:
        d.router.invoke = AsyncMock(return_value=(out, judge_run_id))
    return d


def _app(lesson_id, *, run_id=1, status="completed", inv="critic.kill_verdict", text="t"):
    return {
        "agent_run_id": run_id,
        "lesson_id": lesson_id,
        "run_status": status,
        "invocation_type": inv,
        "lesson_text": text,
        "expectation": None,
        "outcome": None,
        "output_summary": "out",
    }


async def test_judge_pending_clients_unavailable():
    d = SimpleNamespace(lessons=None, curator=None, router=None)
    res = await judge_pending_lesson_applications(d)
    assert res == {"judged": 0, "skipped": "clients unavailable"}


async def test_judge_pending_no_rows():
    d = _judge_dispatcher(rows=[])
    res = await judge_pending_lesson_applications(d)
    assert res == {"judged": 0}


async def test_judge_pending_writes_outcomes():
    rows = [_app(5, run_id=1), _app(6, run_id=1)]
    out = ApplicationJudgements(
        judgements=[
            LessonJudgement(lesson_id=5, verdict="supportive"),
            LessonJudgement(lesson_id=6, verdict="contradicting"),
            LessonJudgement(lesson_id=999, verdict="supportive"),  # not applied → ignored
        ]
    )
    d = _judge_dispatcher(rows=rows, out=out, judge_run_id=900)
    res = await judge_pending_lesson_applications(d)
    assert res == {"judged": 2, "runs": 1}
    # the unapplied lesson 999 was never written
    written = {c.kwargs["lesson_id"]: c.kwargs["outcome"] for c in d.lessons.set_application_outcome.await_args_list}
    assert written == {5: "supportive", 6: "contradicting"}


async def test_judge_pending_supportive_demoted_when_run_failed():
    # run_status != completed → a 'supportive' verdict is downgraded to inconclusive.
    rows = [_app(5, run_id=2, status="failed")]
    out = ApplicationJudgements(judgements=[LessonJudgement(lesson_id=5, verdict="supportive")])
    d = _judge_dispatcher(rows=rows, out=out)
    res = await judge_pending_lesson_applications(d)
    assert res["judged"] == 1
    assert d.lessons.set_application_outcome.await_args.kwargs["outcome"] == "inconclusive"


async def test_judge_pending_swallows_invoke_failure():
    rows = [_app(5, run_id=1)]
    d = _judge_dispatcher(rows=rows)
    d.router.invoke = AsyncMock(side_effect=RuntimeError("judge boom"))
    res = await judge_pending_lesson_applications(d)
    # the failing run is skipped; nothing judged
    assert res == {"judged": 0, "runs": 1}
    d.lessons.set_application_outcome.assert_not_awaited()
