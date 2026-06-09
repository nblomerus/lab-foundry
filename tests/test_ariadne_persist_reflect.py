"""Unit tests for Ariadne's persist / handler / reflect modules.

Everything external is mocked — NO real Postgres/Neo4j/Ollama/DeepSeek/network. The DB is a
ScriptedPool (fake asyncpg pool+conn), the LLM chain is patched via patch_chain, and Mimir /
acquire / grade / run_shadow / run_reflection / get_agent_mode are monkeypatched on the module
under test where it simplifies. No `db` fixture / DATABASE_URL.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agents.ariadne import handler, persist, reflect
from tests._helpers import ScriptedPool, make_state, patch_chain

# ════════════════════════════════════════════════════════════════════════════════
# canned outputs / builders
# ════════════════════════════════════════════════════════════════════════════════
_SCORES = {
    "novelty": 4,
    "feasibility": 3,
    "evidence_availability": 4,
    "paper_potential": 3,
    "reviewer_interest": 4,
    "technical_depth": 3,
    "differentiation": 4,
    "cost_efficiency": 5,
    "lab_alignment": 4,
    "rationale": "emerging gap, light to run",
}


def _scores(**over):
    return SimpleNamespace(**{**_SCORES, **over})


def _goal(
    expectation="R@20 improves",
    kill_condition="no lift",
    novelty_target="time-decay",
    next_milestone="ablation",
    priority_hint="high",
):
    return SimpleNamespace(
        expectation=expectation,
        kill_condition=kill_condition,
        novelty_target=novelty_target,
        next_milestone=next_milestone,
        priority_hint=priority_hint,
    )


def _direction(title="Trust-weighted RRF", statement="Attack stale retrieval.", scores=None, goals=None):
    return SimpleNamespace(
        title=title,
        statement=statement,
        scores=_scores() if scores is None else scores,
        claim_goals=[_goal()] if goals is None else goals,
    )


def _ariadne_out(mission="Make retrieval trustworthy.", directions=None, requests=None):
    return SimpleNamespace(
        mission_frame=mission,
        directions=[_direction()] if directions is None else directions,
        requests=[] if requests is None else requests,
    )


def _request(paper="Original RRF paper", arxiv_id="2009.12345", why="a long-enough why " * 3):
    return SimpleNamespace(paper=paper, arxiv_id=arxiv_id, why=why)


def _verdict(claim_id=1, assessment="advance", reason="still sharp", new_priority=None):
    return SimpleNamespace(
        claim_id=claim_id,
        assessment=assessment,
        reason=reason,
        new_priority=new_priority,
    )


def _lesson(lesson="Use strong baselines", rationale="weak eval recurs", applies_when="weak eval"):
    return SimpleNamespace(lesson=lesson, rationale=rationale, applies_when=applies_when)


def _reflection_out(verdicts=None, lessons=None):
    return SimpleNamespace(
        verdicts=[_verdict()] if verdicts is None else verdicts,
        lessons=[_lesson()] if lessons is None else lessons,
    )


def _persist_pool(mission_id=10, dir_id=20):
    """A ScriptedPool that returns ids for the mission/direction INSERT...RETURNING."""
    return ScriptedPool(
        rules=[
            ("'mission', 'proposed'", [{"id": mission_id}]),
            ("'direction', $2", [{"id": dir_id}]),
        ]
    )


# ════════════════════════════════════════════════════════════════════════════════
# persist.persist_directions
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_persist_directions_happy_path_full_tree():
    pool = _persist_pool()
    state = make_state(pool=pool)
    out = _ariadne_out()
    res = await persist.persist_directions(state, out, run_id=7)
    assert res["mission_id"] == 10
    assert res["directions"] == 1
    assert res["claim_goals"] == 1
    assert res["scored"] == 1
    # default conn.execute status "OK" doesn't start with UPDATE → superseded 0
    assert res["superseded"] == 0
    sqls = " || ".join(c[1] for c in pool.calls)
    assert "INSERT INTO direction_scores" in sqls
    assert "INSERT INTO claim_goals" in sqls
    # composite 3.65 → priority "medium" persisted into direction_scores
    score_args = next(c[2] for c in pool.calls if "INSERT INTO direction_scores" in c[1])
    assert "medium" in score_args


@pytest.mark.asyncio
async def test_persist_directions_supersede_count_parsed():
    pool = ScriptedPool(
        rules=[
            ("UPDATE claims SET status='invalidated'", "UPDATE 3"),
            ("'mission', 'proposed'", [{"id": 10}]),
            ("'direction', $2", [{"id": 20}]),
        ]
    )
    state = make_state(pool=pool)
    res = await persist.persist_directions(state, _ariadne_out())
    assert res["superseded"] == 3


@pytest.mark.asyncio
async def test_persist_directions_skips_scores_when_not_wellformed():
    # a malformed score (out of 1..5) → is_wellformed False → no direction_scores row
    bad = _direction(scores=_scores(novelty=9))
    pool = _persist_pool()
    state = make_state(pool=pool)
    res = await persist.persist_directions(state, _ariadne_out(directions=[bad]))
    assert res["scored"] == 0
    assert res["directions"] == 1
    sqls = " || ".join(c[1] for c in pool.calls)
    assert "INSERT INTO direction_scores" not in sqls


@pytest.mark.asyncio
async def test_persist_directions_multiple_directions_and_goals():
    d1 = _direction(title="A", goals=[_goal(), _goal(expectation="second")])
    d2 = _direction(title="B", scores=_scores(novelty=0), goals=[])  # bad score, no goals
    pool = _persist_pool()
    state = make_state(pool=pool)
    res = await persist.persist_directions(state, _ariadne_out(directions=[d1, d2]))
    assert res["directions"] == 2
    assert res["claim_goals"] == 2  # 2 from d1, 0 from d2
    assert res["scored"] == 1  # only d1 well-formed


# ════════════════════════════════════════════════════════════════════════════════
# persist.persist_reflection
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_persist_reflection_all_assessment_branches():
    verdicts = [
        _verdict(claim_id=1, assessment="retire", reason="saturated"),
        _verdict(claim_id=2, assessment="reprioritize", new_priority="low"),
        _verdict(claim_id=3, assessment="pivot", new_priority="high"),
        _verdict(claim_id=4, assessment="advance"),
    ]
    pool = ScriptedPool()
    state = make_state(pool=pool)
    out = _reflection_out(verdicts=verdicts, lessons=[_lesson()])
    res = await persist.persist_reflection(state, out, [1, 2, 3, 4], run_id=5)
    assert res == {"retired": 1, "reprioritized": 2, "advanced": 1, "lessons": 1}
    sqls = " || ".join(c[1] for c in pool.calls)
    assert "UPDATE claims SET status='invalidated'" in sqls
    assert "UPDATE direction_scores SET priority" in sqls
    assert "INSERT INTO lessons" in sqls


@pytest.mark.asyncio
async def test_persist_reflection_skips_invalid_ids():
    verdicts = [_verdict(claim_id=99, assessment="retire")]  # id not in valid set
    pool = ScriptedPool()
    state = make_state(pool=pool)
    res = await persist.persist_reflection(state, _reflection_out(verdicts=verdicts, lessons=[]), [1, 2])
    assert res == {"retired": 0, "reprioritized": 0, "advanced": 0, "lessons": 0}


@pytest.mark.asyncio
async def test_persist_reflection_reprioritize_without_priority_is_noop():
    # reprioritize/pivot only count when new_priority is truthy → falls through, no count
    verdicts = [_verdict(claim_id=1, assessment="reprioritize", new_priority=None)]
    pool = ScriptedPool()
    state = make_state(pool=pool)
    res = await persist.persist_reflection(state, _reflection_out(verdicts=verdicts, lessons=[]), [1])
    assert res["reprioritized"] == 0
    assert "UPDATE direction_scores" not in " || ".join(c[1] for c in pool.calls)


@pytest.mark.asyncio
async def test_persist_reflection_lesson_with_when_and_blank_skipped():
    lessons = [
        _lesson(lesson="Keep baselines strong", applies_when="weak eval"),
        _lesson(lesson="   ", applies_when=None),  # blank → skipped
        _lesson(lesson="No condition lesson", applies_when=None),  # empty applies_when → {}
    ]
    pool = ScriptedPool()
    state = make_state(pool=pool)
    res = await persist.persist_reflection(state, _reflection_out(verdicts=[], lessons=lessons), [])
    assert res["lessons"] == 2
    lesson_calls = [c for c in pool.calls if "INSERT INTO lessons" in c[1]]
    assert len(lesson_calls) == 2
    # first lesson's applies_when serialized to {"when": ...}; third to {}
    assert json.loads(lesson_calls[0][2][0]) == {"when": "weak eval"}
    assert json.loads(lesson_calls[1][2][0]) == {}


# ════════════════════════════════════════════════════════════════════════════════
# persist.request_evidence
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_request_evidence_prefers_arxiv_id(monkeypatch):
    seen = []

    async def _acq(state, mreq):
        seen.append(mreq)

    monkeypatch.setattr(persist, "request_acquire", _acq)
    state = make_state()
    n = await persist.request_evidence(state, [_request(arxiv_id="2406.00001")])
    assert n == 1
    assert seen[0].arxiv_id == "2406.00001"
    assert seen[0].kind == "paper"
    assert seen[0].requester == "pi"


@pytest.mark.asyncio
async def test_request_evidence_falls_back_to_title_query(monkeypatch):
    seen = []

    async def _acq(state, mreq):
        seen.append(mreq)

    monkeypatch.setattr(persist, "request_acquire", _acq)
    state = make_state()
    n = await persist.request_evidence(state, [_request(arxiv_id=None, paper="Some Exact Title")])
    assert n == 1
    assert seen[0].arxiv_id is None
    assert seen[0].query == "Some Exact Title"


@pytest.mark.asyncio
async def test_request_evidence_pads_short_why(monkeypatch):
    seen = []

    async def _acq(state, mreq):
        seen.append(mreq)

    monkeypatch.setattr(persist, "request_acquire", _acq)
    state = make_state()
    # short why (<30 chars) → padded so AcquireRequest's min_length=30 validation passes
    await persist.request_evidence(state, [_request(why="too short")])
    assert "Ariadne wants this specific paper" in seen[0].why
    assert len(seen[0].why) >= 30


@pytest.mark.asyncio
async def test_request_evidence_short_why_none_uses_paper(monkeypatch):
    seen = []

    async def _acq(state, mreq):
        seen.append(mreq)

    monkeypatch.setattr(persist, "request_acquire", _acq)
    state = make_state()
    # why=None → fallback uses r.paper in the padded message
    await persist.request_evidence(state, [_request(why=None, paper="Foundational Paper")])
    assert "Foundational Paper" in seen[0].why


@pytest.mark.asyncio
async def test_request_evidence_skips_bad_request(monkeypatch):
    async def _acq(state, mreq):
        raise RuntimeError("acquire blew up")

    monkeypatch.setattr(persist, "request_acquire", _acq)
    state = make_state()
    # the exception is swallowed (best-effort) → 0 emitted, no raise
    n = await persist.request_evidence(state, [_request()])
    assert n == 0


@pytest.mark.asyncio
async def test_request_evidence_empty_and_none(monkeypatch):
    monkeypatch.setattr(persist, "request_acquire", AsyncMock())
    state = make_state()
    assert await persist.request_evidence(state, []) == 0
    assert await persist.request_evidence(state, None) == 0


# ════════════════════════════════════════════════════════════════════════════════
# handler.handle_ariadne_deliberate
# ════════════════════════════════════════════════════════════════════════════════
def _grade_report(passed=True, citations_resolved=1.0):
    return SimpleNamespace(passed=passed, citations_resolved=citations_resolved)


def _dispatcher(pool=None):
    return SimpleNamespace(state=make_state(pool=pool))


def _wire_deliberate(monkeypatch, *, mode="advisory", passed=True):
    out = _ariadne_out(requests=[_request()])
    monkeypatch.setattr(handler, "get_agent_mode", AsyncMock(return_value=mode))
    monkeypatch.setattr(handler, "run_shadow", AsyncMock(return_value=out))
    monkeypatch.setattr(handler, "grade", AsyncMock(return_value=_grade_report(passed=passed)))
    persist_directions = AsyncMock(
        return_value={"mission_id": 10, "directions": 1, "claim_goals": 1, "scored": 1, "superseded": 0}
    )
    request_evidence = AsyncMock(return_value=2)
    monkeypatch.setattr(handler, "persist_directions", persist_directions)
    monkeypatch.setattr(handler, "request_evidence", request_evidence)
    return out, persist_directions, request_evidence


@pytest.mark.asyncio
async def test_deliberate_advisory_persists(monkeypatch):
    out, pd, re_ = _wire_deliberate(monkeypatch, mode="advisory", passed=True)
    res = await handler.handle_ariadne_deliberate({"payload": {}}, _dispatcher())
    assert res["persisted"] is True
    assert res["mode"] == "advisory"
    assert res["evidence_requests"] == 2
    assert res["directions"] == 1
    pd.assert_awaited_once()
    re_.assert_awaited_once()


@pytest.mark.asyncio
async def test_deliberate_active_persists(monkeypatch):
    _out, pd, re_ = _wire_deliberate(monkeypatch, mode="active", passed=True)
    res = await handler.handle_ariadne_deliberate({}, _dispatcher())
    assert res["persisted"] is True
    assert res["mode"] == "active"
    pd.assert_awaited_once()


@pytest.mark.asyncio
async def test_deliberate_shadow_writes_nothing(monkeypatch):
    _out, pd, re_ = _wire_deliberate(monkeypatch, mode="shadow", passed=True)
    res = await handler.handle_ariadne_deliberate({"payload": {}}, _dispatcher())
    assert res["persisted"] is False
    pd.assert_not_awaited()
    re_.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliberate_off_writes_nothing(monkeypatch):
    _out, pd, _re = _wire_deliberate(monkeypatch, mode="off", passed=True)
    res = await handler.handle_ariadne_deliberate({"payload": {}}, _dispatcher())
    assert res["persisted"] is False
    pd.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliberate_grade_fail_no_persist(monkeypatch):
    _out, pd, re_ = _wire_deliberate(monkeypatch, mode="advisory", passed=False)
    res = await handler.handle_ariadne_deliberate({"payload": {}}, _dispatcher())
    assert res["persisted"] is False
    assert res["reason"] == "failed_grading"
    pd.assert_not_awaited()
    re_.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliberate_focus_threaded_to_run_shadow(monkeypatch):
    _out, _pd, _re = _wire_deliberate(monkeypatch, mode="advisory", passed=True)
    await handler.handle_ariadne_deliberate({"payload": {"focus": "latency tail"}}, _dispatcher())
    _args, kwargs = handler.run_shadow.call_args
    assert kwargs["focus"] == "latency tail"
    assert kwargs["emit_conversation"] is True


# ════════════════════════════════════════════════════════════════════════════════
# handler.handle_ariadne_reflect
# ════════════════════════════════════════════════════════════════════════════════
def _refl_grade(passed=True, n_verdicts=1, n_lessons=1, invalid_refs=None):
    return SimpleNamespace(passed=passed, n_verdicts=n_verdicts, n_lessons=n_lessons, invalid_refs=invalid_refs or [])


def _wire_reflect(monkeypatch, *, mode="advisory", passed=True, out=None, valid_ids=None):
    refl_out = _reflection_out() if out is None else out
    monkeypatch.setattr(handler, "get_agent_mode", AsyncMock(return_value=mode))
    monkeypatch.setattr(
        handler, "run_reflection", AsyncMock(return_value=(refl_out, valid_ids if valid_ids is not None else [1]))
    )
    monkeypatch.setattr(handler, "grade_reflection", lambda o, v: _refl_grade(passed=passed))
    persist_reflection = AsyncMock(return_value={"retired": 1, "reprioritized": 0, "advanced": 0, "lessons": 1})
    monkeypatch.setattr(handler, "persist_reflection", persist_reflection)
    return persist_reflection


@pytest.mark.asyncio
async def test_reflect_advisory_persists(monkeypatch):
    pr = _wire_reflect(monkeypatch, mode="advisory", passed=True)
    res = await handler.handle_ariadne_reflect({}, _dispatcher())
    assert res["persisted"] is True
    assert res["mode"] == "advisory"
    assert res["retired"] == 1
    pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_reflect_active_persists(monkeypatch):
    pr = _wire_reflect(monkeypatch, mode="active", passed=True)
    res = await handler.handle_ariadne_reflect({}, _dispatcher())
    assert res["persisted"] is True
    pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_reflect_empty_agenda(monkeypatch):
    # run_reflection returns (None, []) → nothing to steer
    monkeypatch.setattr(handler, "get_agent_mode", AsyncMock(return_value="advisory"))
    monkeypatch.setattr(handler, "run_reflection", AsyncMock(return_value=(None, [])))
    pr = AsyncMock()
    monkeypatch.setattr(handler, "persist_reflection", pr)
    res = await handler.handle_ariadne_reflect({}, _dispatcher())
    assert res["reflected"] is False
    assert res["reason"] == "empty_agenda"
    pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_reflect_shadow_writes_nothing(monkeypatch):
    pr = _wire_reflect(monkeypatch, mode="shadow", passed=True)
    res = await handler.handle_ariadne_reflect({}, _dispatcher())
    assert res["persisted"] is False
    pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_reflect_grade_fail_no_persist(monkeypatch):
    pr = _wire_reflect(monkeypatch, mode="advisory", passed=False)
    res = await handler.handle_ariadne_reflect({}, _dispatcher())
    assert res["persisted"] is False
    assert res["reason"] == "failed_grading"
    pr.assert_not_awaited()


# ════════════════════════════════════════════════════════════════════════════════
# reflect.run_reflection
# ════════════════════════════════════════════════════════════════════════════════
def _reflection_json():
    return json.dumps(
        {
            "portfolio_assessment": "agenda still mostly sharp",
            "verdicts": [{"claim_id": 1, "assessment": "advance", "reason": "still thin area", "new_priority": None}],
            "lessons": [{"lesson": "Use strong baselines", "rationale": "recurs", "applies_when": "weak eval"}],
            "reprioritized_focus": "emphasize trust-decay",
        }
    )


@pytest.mark.asyncio
async def test_run_reflection_no_standing_dirs_returns_none(monkeypatch):
    # _standing_agenda → no rows → ids empty → (None, [])
    pool = ScriptedPool(
        rules=[
            ("claim_kind = 'mission'", []),
            ("c.claim_kind = 'direction'", []),
        ]
    )
    state = make_state(pool=pool)
    # guard: field-brief / mimir / chain must NOT be reached on the empty path
    monkeypatch.setattr(reflect, "read_field_brief", AsyncMock(side_effect=AssertionError("unreached")))
    out, ids = await reflect.run_reflection(state)
    assert out is None
    assert ids == []


@pytest.mark.asyncio
async def test_run_reflection_happy_path(monkeypatch):
    rows = [
        {
            "id": 1,
            "statement": "Trust-decayed RRF",
            "status": "proposed",
            "confidence": 0.5,
            "last_evidence_at": None,
            "created_at": None,
            "priority": "high",
            "composite": 3.65,
        },
    ]
    pool = ScriptedPool(
        rules=[
            ("claim_kind = 'mission'", [{"statement": "Trustworthy retrieval"}]),
            ("c.claim_kind = 'direction'", rows),
            ("SELECT now()", "2026-06-09"),
            (
                "FROM claim_goals",
                [{"claim_id": 1, "expectation": "R@20 up", "kill_condition": "no lift", "status": "open"}],
            ),
            ("field_model", [{"concept_name": "agentic RAG"}]),
            ("FROM lessons", []),
        ]
    )
    state = make_state(pool=pool)
    monkeypatch.setattr(reflect, "read_field_brief", AsyncMock(return_value="## FIELD MODEL\nhot: RAG"))
    monkeypatch.setattr(
        reflect,
        "answer_question",
        AsyncMock(return_value=SimpleNamespace(answer="landscape shifted", gaps=["trust decay"])),
    )
    calls = patch_chain(monkeypatch, reflect, content=_reflection_json())
    out, ids = await reflect.run_reflection(state, emit_conversation=True)
    assert ids == [1]
    assert out.verdicts[0].claim_id == 1
    assert out.reprioritized_focus == "emphasize trust-decay"
    # the deliberation user prompt carried the mission, agenda and the mimir block
    user = calls[0][0][1]["content"]
    assert "Trustworthy retrieval" in user
    assert "Trust-decayed RRF" in user
    assert "Mimir's reflection synthesis" in user
    assert "## FIELD MODEL" in user


# ════════════════════════════════════════════════════════════════════════════════
# reflect._standing_agenda — formatting
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_standing_agenda_formats_rows_and_goals():
    rows = [
        {
            "id": 1,
            "statement": "Direction one",
            "status": "proposed",
            "confidence": 0.5,
            "last_evidence_at": None,
            "created_at": None,
            "priority": "high",
            "composite": 3.65,
        },
        {
            "id": 2,
            "statement": "Direction two",
            "status": "tested",
            "confidence": 0.8,
            "last_evidence_at": "yes",
            "created_at": None,
            "priority": None,
            "composite": None,
        },
    ]
    pool = ScriptedPool(
        rules=[
            ("claim_kind = 'mission'", "the mission"),
            ("c.claim_kind = 'direction'", rows),
            ("SELECT now()", "now"),
            (
                "FROM claim_goals",
                [{"claim_id": 1, "expectation": "expect A", "kill_condition": "kill A", "status": "open"}],
            ),
        ]
    )
    mission, ids, agenda = await reflect._standing_agenda(pool)
    assert mission == "the mission"
    assert ids == [1, 2]
    # #1: scored (priority high, composite 3.65), no-evidence; goal line rendered
    assert "#1 [proposed · conf=0.50 · priority=high (composite 3.65)" in agenda
    assert "no-evidence" in agenda
    assert "goal[open]: expect=expect A || kill=kill A" in agenda
    # #2: unscored priority + composite "—" + has-evidence
    assert "#2 [tested · conf=0.80 · priority=unscored (composite —)" in agenda
    assert "has-evidence" in agenda


@pytest.mark.asyncio
async def test_standing_agenda_no_rows_placeholder():
    pool = ScriptedPool(
        rules=[
            ("claim_kind = 'mission'", None),
            ("c.claim_kind = 'direction'", []),
            ("SELECT now()", "now"),
        ]
    )
    mission, ids, agenda = await reflect._standing_agenda(pool)
    assert mission is None
    assert ids == []
    assert agenda == "(no standing directions)"


# ════════════════════════════════════════════════════════════════════════════════
# reflect._age_days
# ════════════════════════════════════════════════════════════════════════════════
def test_age_days_bad_inputs_returns_zero():
    # subtracting None raises inside → caught → 0
    assert reflect._age_days(None, None) == 0


def test_age_days_valid_delta():
    from datetime import datetime, timedelta

    created = datetime(2026, 6, 1)
    now = created + timedelta(days=5)
    assert reflect._age_days(created, now) == 5


# ════════════════════════════════════════════════════════════════════════════════
# reflect._deliberate_reflection
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_deliberate_reflection_parses_canned_json(monkeypatch):
    calls = patch_chain(monkeypatch, reflect, content=_reflection_json())
    out = await reflect._deliberate_reflection(
        "the mission",
        "#1 ...",
        "## FIELD MODEL",
        "## Standing lessons\n- x",
        "## Mimir's reflection synthesis\nshifted",
        model="m",
    )
    assert out.portfolio_assessment == "agenda still mostly sharp"
    assert out.verdicts[0].assessment == "advance"
    messages, kwargs = calls[0]
    assert messages[0]["role"] == "system"
    user = messages[1]["content"]
    assert "the mission" in user
    assert "Mimir's reflection synthesis" in user  # mimir_block injected
    assert kwargs["invocation_type"] == "ariadne.reflect"
    assert kwargs["primary_model"] == "m"


@pytest.mark.asyncio
async def test_deliberate_reflection_empty_mission_and_no_mimir(monkeypatch):
    calls = patch_chain(monkeypatch, reflect, content=_reflection_json())
    await reflect._deliberate_reflection("", "#1 ...", "", "", "", model="m")
    user = calls[0][0][1]["content"]
    assert "(none set)" in user  # empty mission → placeholder
    assert "(field model not built)" in user  # empty field brief
    assert "No standing lessons yet." in user  # empty lessons


@pytest.mark.asyncio
async def test_deliberate_reflection_malformed_json_raises(monkeypatch):
    patch_chain(monkeypatch, reflect, content="not json at all")
    with pytest.raises(ValidationError):
        await reflect._deliberate_reflection("m", "a", "f", "l", "", model="x")


# ════════════════════════════════════════════════════════════════════════════════
# reflect._mimir_reflect_brief
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_mimir_reflect_brief_emit_on_with_anchors_and_gaps(monkeypatch):
    pool = ScriptedPool(
        rules=[
            ("FROM field_model", [{"concept_name": "agentic RAG"}, {"concept_name": "tool use"}]),
        ]
    )
    state = SimpleNamespace(pool=pool)
    captured = {}

    async def _aq(question, k, state, asker):
        captured.update(question=question, k=k, state=state, asker=asker)
        return SimpleNamespace(answer="landscape shifted", gaps=["trust decay", "stale docs"])

    monkeypatch.setattr(reflect, "answer_question", _aq)
    block = await reflect._mimir_reflect_brief(state, "the mission", "#1 dir", emit=True)
    assert "Mimir's reflection synthesis" in block
    assert "GAPS Mimir flags now" in block
    assert "- trust decay" in block
    # anchors injected, state threaded when emit=True, asker tagged
    assert "agentic RAG, tool use" in captured["question"]
    assert captured["state"] is state
    assert captured["asker"] == "ariadne" and captured["k"] == 8


@pytest.mark.asyncio
async def test_mimir_reflect_brief_emit_off_state_none_no_gaps(monkeypatch):
    pool = ScriptedPool(rules=[("FROM field_model", [{"concept_name": "x"}])])
    state = SimpleNamespace(pool=pool)
    captured = {}

    async def _aq(question, k, state, asker):
        captured["state"] = state
        return SimpleNamespace(answer="ans", gaps=[])

    monkeypatch.setattr(reflect, "answer_question", _aq)
    block = await reflect._mimir_reflect_brief(state, None, "agenda", emit=False)
    assert "Mimir's reflection synthesis" in block
    assert "GAPS Mimir flags now" not in block  # no gaps section
    assert captured["state"] is None  # emit=False → state not threaded


@pytest.mark.asyncio
async def test_mimir_reflect_brief_pool_none_returns_empty(monkeypatch):
    called = AsyncMock()
    monkeypatch.setattr(reflect, "answer_question", called)
    block = await reflect._mimir_reflect_brief(SimpleNamespace(pool=None), "m", "a", emit=True)
    assert block == ""
    called.assert_not_called()


@pytest.mark.asyncio
async def test_mimir_reflect_brief_anchor_query_failure_still_runs(monkeypatch):
    class _BoomFetchPool:
        async def fetch(self, *a):
            raise RuntimeError("no field_model")

    captured = {}

    async def _aq(question, k, state, asker):
        captured["question"] = question
        return SimpleNamespace(answer="ans", gaps=[])

    monkeypatch.setattr(reflect, "answer_question", _aq)
    block = await reflect._mimir_reflect_brief(SimpleNamespace(pool=_BoomFetchPool()), "m", "a", emit=True)
    # anchors empty → that clause omitted, but mimir still answers
    assert "Active/emerging areas right now:" not in captured["question"]
    assert "Mimir's reflection synthesis" in block


@pytest.mark.asyncio
async def test_mimir_reflect_brief_answer_failure_returns_empty(monkeypatch):
    pool = ScriptedPool(rules=[("FROM field_model", [{"concept_name": "x"}])])

    async def _boom(*a, **k):
        raise RuntimeError("mimir offline")

    monkeypatch.setattr(reflect, "answer_question", _boom)
    block = await reflect._mimir_reflect_brief(SimpleNamespace(pool=pool), "m", "a", emit=True)
    assert block == ""
