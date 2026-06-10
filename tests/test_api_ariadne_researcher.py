"""Tests for api/ariadne.py + api/researcher.py — FastAPI routers driven by a TestClient
over a minimal app with a mocked DB pool (ScriptedPool). No real Postgres / DATABASE_URL.

Covers every endpoint and its branches, plus direct unit tests of the pure helpers
(_queue_health, _loads_loose, _extract_question, _payload, _result, _task_row,
_deliberation_outcome, _reflection_outcome).
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import FastAPI
from starlette.testclient import TestClient

from agents.ariadne.scoring import DIMENSIONS
from api import ariadne, researcher
from tests._helpers import ScriptedPool

_NOW = datetime(2026, 6, 9, 12, 0, 0)


def make_client(rules):
    app = FastAPI()
    app.include_router(ariadne.router)
    app.include_router(researcher.router)
    app.state.pool = ScriptedPool(rules=rules)
    return TestClient(app)


def _direction_row(**over):
    """A direction row with all DIMENSIONS columns present (defaults to a scored direction)."""
    r = {
        "id": 10,
        "statement": "Title here: the body of the direction statement",
        "status": "proposed",
        "confidence": 0.5,
        "created_at": _NOW,
        "invalidation_reason": None,
        "composite": 0.82,
        "priority": "high",
        "rationale": "because",
        "gate": "pending",
        "n_goals": 2,
    }
    for d in DIMENSIONS:
        r[d] = 4
    r.update(over)
    return r


# ── /ariadne/overview ─────────────────────────────────────────────────────────
def _overview_rules(*, mission, directions, lessons, focus, modes):
    rules = [
        ("SELECT mode FROM agent_modes WHERE agent_name = 'ariadne'", [{"mode": "shadow"}]),
        ("c.claim_kind = 'direction' AND c.parent_id", directions),
        ("FROM lessons", lessons),
        ("SELECT count(*) FROM claims", [{"count": 42}]),
        ("acquire.requested", [{"count": 7}]),
        ("WHERE agent_name IN ('planner','researcher','experiments','quartermaster')", modes),
        ("FROM tasks WHERE department = 'research' AND status = 'pending'", [{"count": 1}]),
        ("FROM tasks WHERE department = 'research'", [{"count": 3}]),
        ("FROM experiment_runs WHERE status IN ('running','queued')", [{"count": 2}]),
        ("FROM experiment_runs WHERE code IS NOT NULL", [{"count": 9}]),
        ("FROM field_model WHERE trend_state IN ('hot','emerging')", focus),
    ]
    if mission:
        # Only script the mission fetchrow when present; absent -> rule omitted so the
        # ScriptedConn returns its default None (an empty list would IndexError on r[0]).
        rules.insert(1, ("claim_kind = 'mission'", [mission]))
    return rules


def test_overview_full_mission_scored_directions_lessons():
    mission = {"id": 5, "statement": "the mission", "status": "active", "created_at": _NOW}
    dirs = [
        _direction_row(id=10, gate="approved", status="proposed"),
        _direction_row(
            id=11,
            statement="Retired one: body",
            status="invalidated",
            invalidation_reason="killed",
            composite=None,
            priority=None,
            rationale=None,
            gate="rejected",
            n_goals=0,
        ),
    ]
    lessons = [
        {
            "lesson_text": "lesson A",
            "applies_when": {"when": "always"},
            "status": "active",
            "created_at": _NOW,
        }
    ]
    focus = [{"concept_name": "graphrag"}, {"concept_name": "rerank"}]
    modes = [
        {"agent_name": "planner", "mode": "shadow"},
        {"agent_name": "researcher", "mode": "off"},
        {"agent_name": "experiments", "mode": "active"},
        {"agent_name": "quartermaster", "mode": "active"},
    ]
    client = make_client(_overview_rules(mission=mission, directions=dirs, lessons=lessons, focus=focus, modes=modes))

    body = client.get("/ariadne/overview").json()

    assert body["mode"] == "shadow"
    g = body["at_a_glance"]
    assert g["active_directions"] == 1
    assert g["retired_directions"] == 1
    assert g["claim_goals"] == 2  # only the active direction has 2; retired has 0
    assert g["lessons"] == 1
    assert g["top_priority"] == "Title here"  # the scored, non-retired direction's title
    assert g["focus"] == ["graphrag", "rerank"]
    assert g["status"] == "Steering Research"  # lessons present
    assert g["approved"] == 1  # one gate == approved
    assert g["gate_budget"] == ariadne.GATE_BUDGET
    assert g["claims_total"] == 42
    assert g["acquire_requests_24h"] == 7
    assert g["planner_mode"] == "shadow"
    assert g["researcher_mode"] == "off"
    assert g["research_tasks"] == 3
    assert g["research_tasks_pending"] == 1
    assert g["experiments_mode"] == "active"
    assert g["quartermaster_mode"] == "active"
    assert g["experiments_running"] == 2
    assert g["experiments_total"] == 9
    assert body["mission"]["id"] == 5
    assert body["mission"]["framed_at"] == _NOW.isoformat()

    scored, retired = body["directions"]
    assert scored["title"] == "Title here"
    assert scored["statement"] == "the body of the direction statement"
    assert scored["composite"] == 0.82
    assert scored["scores"] == {d: 4 for d in DIMENSIONS}
    assert scored["retired"] is False
    assert retired["retired"] is True
    assert retired["scores"] is None  # composite None -> no scores dict
    assert retired["composite"] is None
    assert body["lessons"][0]["when"] == "always"
    assert body["lessons"][0]["created_at"] == _NOW.isoformat()


def test_overview_no_mission_dormant():
    client = make_client(_overview_rules(mission=None, directions=[], lessons=[], focus=[], modes=[]))
    body = client.get("/ariadne/overview").json()
    assert body["mission"] is None
    assert body["directions"] == []
    assert body["at_a_glance"]["status"] == "Dormant — no agenda framed"
    assert body["at_a_glance"]["top_priority"] is None
    # default modes when no rows
    assert body["at_a_glance"]["planner_mode"] == "off"
    assert body["at_a_glance"]["researcher_mode"] == "off"


def test_overview_mission_no_lessons_framing_and_unscored_top_priority():
    mission = {"id": 5, "statement": "m", "status": "active", "created_at": _NOW}
    # An unscored, non-retired direction with no "title: " separator -> title is truncated statement.
    dirs = [_direction_row(id=20, statement="just a plain statement no colon", composite=None, gate="pending")]
    client = make_client(_overview_rules(mission=mission, directions=dirs, lessons=[], focus=[], modes=[]))
    body = client.get("/ariadne/overview").json()
    assert body["at_a_glance"]["status"] == "Framing Directions"  # mission but no lessons
    # no scored direction -> top_priority falls back to first non-retired direction's title
    assert body["at_a_glance"]["top_priority"] == "just a plain statement no colon"
    assert body["directions"][0]["statement"] == ""  # no ": " -> empty body


def test_overview_lesson_applies_when_not_dict():
    mission = {"id": 5, "statement": "m", "status": "active", "created_at": _NOW}
    lessons = [{"lesson_text": "L", "applies_when": "not-a-dict", "status": "x", "created_at": None}]
    client = make_client(_overview_rules(mission=mission, directions=[], lessons=lessons, focus=[], modes=[]))
    body = client.get("/ariadne/overview").json()
    assert body["lessons"][0]["when"] is None  # non-dict applies_when -> None
    assert body["lessons"][0]["created_at"] is None


# ── /ariadne/field-model ──────────────────────────────────────────────────────
def test_field_model_groups_and_windows():
    rows = [
        {
            "concept_kind": "METHOD",
            "concept_name": "graphrag",
            "total_papers": 100,
            "recent_papers": 40,
            "prior_papers": 10,
            "velocity": 3.0,
            "trend_state": "hot",
            "recent_window": "2026",
            "prior_window": "2025",
        },
        {
            "concept_kind": "TASK",
            "concept_name": "rerank",
            "total_papers": 50,
            "recent_papers": 30,
            "prior_papers": 5,
            "velocity": 1.5,
            "trend_state": "emerging",
            "recent_window": "2026",
            "prior_window": "2025",
        },
    ]
    counts = [{"trend_state": "hot", "n": 1}, {"trend_state": "emerging", "n": 1}]
    client = make_client(
        [
            ("FROM field_model WHERE trend_state IN ('hot','emerging','saturated','declining')", rows),
            ("count(*) AS n FROM field_model GROUP BY trend_state", counts),
        ]
    )
    body = client.get("/ariadne/field-model").json()
    assert body["windows"] == {"recent": "2026", "prior": "2025"}
    assert body["counts"] == {"hot": 1, "emerging": 1}
    assert body["by_state"]["hot"][0]["name"] == "graphrag"
    assert body["by_state"]["hot"][0]["velocity"] == 3.0
    assert body["by_state"]["emerging"][0]["name"] == "rerank"


def test_field_model_per_state_cap():
    rows = [
        {
            "concept_kind": "M",
            "concept_name": f"c{i}",
            "total_papers": 10,
            "recent_papers": 5,
            "prior_papers": 2,
            "velocity": 1.0,
            "trend_state": "hot",
            "recent_window": "w",
            "prior_window": "p",
        }
        for i in range(5)
    ]
    client = make_client(
        [
            ("FROM field_model WHERE trend_state IN ('hot','emerging','saturated','declining')", rows),
            ("count(*) AS n FROM field_model GROUP BY trend_state", []),
        ]
    )
    body = client.get("/ariadne/field-model?per_state=2").json()
    assert len(body["by_state"]["hot"]) == 2  # capped at per_state


def test_field_model_empty():
    client = make_client(
        [
            ("FROM field_model WHERE trend_state IN ('hot','emerging','saturated','declining')", []),
            ("count(*) AS n FROM field_model GROUP BY trend_state", []),
        ]
    )
    body = client.get("/ariadne/field-model").json()
    assert body["windows"] == {"recent": None, "prior": None}
    assert body["by_state"] == {"hot": [], "emerging": [], "saturated": [], "declining": []}


# ── /ariadne/requests ─────────────────────────────────────────────────────────
def test_requests_with_reply_and_pending_and_health():
    rows = [
        {
            "target_id": 100,
            "payload": {"requester": "ariadne", "query": "graphrag survey", "why": "to learn"},
            "emitted_at": _NOW,
            "status": "open",
        },
        {
            "target_id": 200,
            "payload": '{"requester": "researcher", "arxiv_id": "2401.0001"}',  # json string payload
            "emitted_at": None,
            "status": "open",
        },
    ]
    reply = {"event_type": "acquire.fulfilled", "payload": {"status": "fulfilled", "document_id": 9}}
    health_row = {
        "pending": 2,
        "oldest_pending_age": 360,
        "requested_1h": 5,
        "resolved_1h": 3,
    }
    client = make_client(
        [
            ("FROM events WHERE event_type = 'acquire.requested' ORDER BY id DESC", rows),
            ("WHERE dedup_key = $1", [reply]),
            ("WITH req AS", [health_row]),
        ]
    )
    body = client.get("/ariadne/requests").json()
    reqs = body["requests"]
    assert reqs[0]["requester"] == "ariadne"
    assert reqs[0]["subject"] == "graphrag survey"
    assert reqs[0]["at"] == _NOW.isoformat()
    assert reqs[0]["outcome"] == "fulfilled"  # rp["status"]
    assert reqs[0]["document_id"] == 9
    assert reqs[1]["subject"] == "2401.0001"  # arxiv_id fallback, json-string payload parsed
    assert reqs[1]["at"] is None
    # counts tally outcomes
    assert body["counts"]["fulfilled"] == 2
    assert body["health"]["pending"] == 2
    assert body["health"]["oldest_pending_age_seconds"] == 360


def test_requests_no_reply_pending_outcome():
    rows = [{"target_id": 1, "payload": {"url": "http://x"}, "emitted_at": _NOW, "status": "open"}]
    health_row = {"pending": 0, "oldest_pending_age": None, "requested_1h": 0, "resolved_1h": 0}
    client = make_client(
        [
            ("FROM events WHERE event_type = 'acquire.requested' ORDER BY id DESC", rows),
            # no `WHERE dedup_key` rule -> fetchrow returns default None (no reply)
            ("WITH req AS", [health_row]),
        ]
    )
    body = client.get("/ariadne/requests").json()
    assert body["requests"][0]["subject"] == "http://x"  # url fallback
    assert body["requests"][0]["outcome"] == "pending"  # no reply -> pending
    assert body["counts"]["pending"] == 1
    assert body["health"]["oldest_pending_age_seconds"] is None


def test_requests_reply_without_status_uses_event_suffix():
    rows = [{"target_id": 1, "payload": {"doi": "10.1/x"}, "emitted_at": _NOW, "status": "open"}]
    reply = {"event_type": "acquire.rejected", "payload": {"reason": "spam"}}  # no status in payload
    health_row = {"pending": None, "oldest_pending_age": None, "requested_1h": None, "resolved_1h": None}
    client = make_client(
        [
            ("FROM events WHERE event_type = 'acquire.requested' ORDER BY id DESC", rows),
            ("WHERE dedup_key = $1", [reply]),
            ("WITH req AS", [health_row]),
        ]
    )
    body = client.get("/ariadne/requests").json()
    o = body["requests"][0]
    assert o["subject"] == "10.1/x"  # doi fallback
    assert o["outcome"] == "rejected"  # event_type suffix
    assert o["reason"] == "spam"
    # _queue_health "or 0" coercion of nulls
    assert body["health"]["pending"] == 0
    assert body["health"]["requested_1h"] == 0
    assert body["health"]["resolved_1h"] == 0


def test_requests_subject_em_dash_when_nothing():
    rows = [{"target_id": 1, "payload": {"requester": "x"}, "emitted_at": _NOW, "status": "open"}]
    health_row = {"pending": 0, "oldest_pending_age": None, "requested_1h": 0, "resolved_1h": 0}
    client = make_client(
        [
            ("FROM events WHERE event_type = 'acquire.requested' ORDER BY id DESC", rows),
            # no reply rule -> fetchrow default None
            ("WITH req AS", [health_row]),
        ]
    )
    body = client.get("/ariadne/requests").json()
    assert body["requests"][0]["subject"] == "—"  # no query/arxiv/url/doi


# ── /ariadne/conversations ────────────────────────────────────────────────────
def test_conversations_deliberation_and_reflection():
    ask_input = "## system foo\n\n## user\n# Question\nWhat is GraphRAG good for?\n\n## Retrieved passages\nlots of text"
    delib_rows = [
        {
            "session_id": 2,
            "at": _NOW,
            "ask_input": ask_input,
            "ask_output": '{"answer": "an answer", "citations": ["c1", 7, "c2"], "gaps": ["g1", null]}',
            "delib_output": '{"mission_frame": "frame it", "directions": [{"title": "D1"}, {"nope": 1}]}',
            "reflect_output": None,
        },
        {
            "session_id": 1,
            "at": None,
            "ask_input": "no question marker here",
            "ask_output": '```json\n{"answer": "reflect ans"}\n```',
            "delib_output": None,
            "reflect_output": (
                '{"reprioritized_focus": "refocus", '
                '"verdicts": [{"claim_id": 9, "assessment": "promising", "reason": "good"}, '
                '{"claim_id": 8, "assessment": "kill"}, {"no_assessment": true}]}'
            ),
        },
    ]
    client = make_client([("FROM agent_runs", delib_rows)])
    body = client.get("/ariadne/conversations").json()
    convs = body["conversations"]

    d = convs[0]
    assert d["kind"] == "deliberation"
    assert d["question"] == "What is GraphRAG good for?"
    assert d["answer"] == "an answer"
    assert d["citations"] == ["c1", "c2"]  # non-str 7 dropped
    assert d["gaps"] == ["g1"]  # non-str null dropped
    assert d["outcome"]["label"] == "Framed"
    assert d["outcome"]["summary"] == "frame it"
    assert d["outcome"]["items"] == ["D1"]  # only dict-with-title kept

    r = convs[1]
    assert r["kind"] == "reflection"
    assert r["question"] is None  # no "# Question" marker
    assert r["answer"] == "reflect ans"  # fenced json parsed
    assert r["outcome"]["label"] == "Steered"
    assert r["outcome"]["summary"] == "refocus"
    assert r["outcome"]["items"] == ["#9 promising — good", "#8 kill"]


# ── /ariadne/planner ──────────────────────────────────────────────────────────
def test_planner_panel_full():
    by_status = [{"status": "pending", "n": 2}, {"status": "done", "n": 1}]
    tasks = [
        {
            "id": 3,
            "task_type": "verify",
            "description": "check it",
            "status": "pending",
            "created_at": _NOW,
            "direction": "Some direction",
        },
        {
            "id": 2,
            "task_type": "verify",
            "description": "check2",
            "status": "done",
            "created_at": None,
            "direction": None,
        },
    ]
    client = make_client(
        [
            ("SELECT mode FROM agent_modes WHERE agent_name = 'planner'", [{"mode": "shadow"}]),
            ("count(*) AS n FROM tasks WHERE department = 'research' GROUP BY status", by_status),
            ("SELECT emitted_at FROM events WHERE event_type = 'planner.plan'", [{"emitted_at": _NOW}]),
            ("JOIN direction_gate g ON g.claim_id = c.id", [{"count": 4}]),
            ("FROM tasks t LEFT JOIN claims c ON c.id = t.claim_id", tasks),
        ]
    )
    body = client.get("/ariadne/planner").json()
    assert body["mode"] == "shadow"
    assert body["tasks_total"] == 3
    assert body["by_status"] == {"pending": 2, "done": 1}
    assert body["awaiting_plan"] == 4
    assert body["last_plan_at"] == _NOW.isoformat()
    assert body["tasks"][0]["direction"] == "Some direction"
    assert body["tasks"][0]["at"] == _NOW.isoformat()
    assert body["tasks"][1]["at"] is None


def test_planner_panel_defaults():
    client = make_client(
        [
            ("count(*) AS n FROM tasks WHERE department = 'research' GROUP BY status", []),
            ("FROM tasks t LEFT JOIN claims c ON c.id = t.claim_id", []),
        ]
    )
    body = client.get("/ariadne/planner").json()
    assert body["mode"] == "off"  # fetchval None -> "off"
    assert body["tasks_total"] == 0
    assert body["awaiting_plan"] == 0  # None -> 0
    assert body["last_plan_at"] is None
    assert body["tasks"] == []


# ── POST /ariadne/gate/{claim_id} ─────────────────────────────────────────────
def test_gate_invalid_decision():
    client = make_client([])
    body = client.post("/ariadne/gate/5", json={"decision": "nope"}).json()
    assert body["ok"] is False
    assert "decision must be one of" in body["error"]


def test_gate_not_a_direction():
    client = make_client([("SELECT claim_kind FROM claims WHERE id = $1", [{"claim_kind": "mission"}])])
    body = client.post("/ariadne/gate/5", json={"decision": "approved"}).json()
    assert body["ok"] is False
    assert "is not a direction" in body["error"]


def test_gate_approved_within_budget():
    client = make_client(
        [
            ("SELECT claim_kind FROM claims WHERE id = $1", [{"claim_kind": "direction"}]),
            ("FROM direction_gate dg JOIN claims c ON c.id = dg.claim_id", [{"count": 0}]),
            ("INSERT INTO direction_gate", "INSERT 0 1"),
        ]
    )
    body = client.post("/ariadne/gate/5", json={"decision": "approved", "note": "go"}).json()
    assert body == {"ok": True, "claim_id": 5, "decision": "approved"}


def test_gate_approved_budget_full():
    client = make_client(
        [
            ("SELECT claim_kind FROM claims WHERE id = $1", [{"claim_kind": "direction"}]),
            ("FROM direction_gate dg JOIN claims c ON c.id = dg.claim_id", [{"count": ariadne.GATE_BUDGET}]),
        ]
    )
    body = client.post("/ariadne/gate/5", json={"decision": "approved"}).json()
    assert body["ok"] is False
    assert body["budget_full"] is True
    assert f"{ariadne.GATE_BUDGET}/{ariadne.GATE_BUDGET}" in body["error"]


def test_gate_held_skips_budget_check():
    client = make_client(
        [
            ("SELECT claim_kind FROM claims WHERE id = $1", [{"claim_kind": "direction"}]),
            ("INSERT INTO direction_gate", "INSERT 0 1"),
        ]
    )
    body = client.post("/ariadne/gate/5", json={"decision": "held"}).json()
    assert body == {"ok": True, "claim_id": 5, "decision": "held"}


# ── researcher /researcher/overview ───────────────────────────────────────────
def test_researcher_overview_full():
    tasks = [
        {
            "id": 1,
            "task_type": "verify",
            "status": "done",
            "description": "task one",
            "claim_id": 30,
            "created_at": _NOW,
            "started_at": _NOW,
            "completed_at": _NOW,
            "direction": "Dir A",
            "result": {
                "verdict": "holds",
                "disposition": "supported",
                "grounded": True,
                "summary": "looks good",
                "key_evidence": ["e1"],
                "kill_condition_check": "ok",
                "gaps": ["g"],
                "acquire_queries": ["q"],
                "next_step": "more",
                "queries": ["sq"],
                "n_evidence": 3,
                "applied": {"confidence": 0.1, "acquires_fired": 2},
            },
        },
        {
            "id": 2,
            "task_type": "verify",
            "status": "pending",
            "description": "task two",
            "claim_id": None,
            "created_at": _NOW,
            "started_at": None,
            "completed_at": None,
            "direction": None,
            "result": '{"disposition": "contradicted", "verdict": "fails"}',  # json string result
        },
        {
            "id": 3,
            "task_type": "verify",
            "status": "pending",
            "description": "no result",
            "claim_id": None,
            "created_at": None,
            "started_at": None,
            "completed_at": None,
            "direction": None,
            "result": None,  # empty -> finding None
        },
    ]
    by_status = [{"status": "done", "n": 1}, {"status": "pending", "n": 2}]
    acq_rep = [{"status": "fulfilled"}, {"status": "fulfilled"}, {"status": "rejected"}]
    client = make_client(
        [
            ("SELECT mode FROM agent_modes WHERE agent_name = 'researcher'", [{"mode": "shadow"}]),
            ("count(*) AS n FROM tasks WHERE department = 'research' GROUP BY status", by_status),
            ("FROM tasks t LEFT JOIN claims c ON c.id = t.claim_id", tasks),
            ("event_type = 'acquire.requested' AND payload", [{"count": 5}]),  # acq_fired fetchval
            ("payload->>'status' AS status FROM events", acq_rep),  # acq_rep fetch
        ]
    )
    body = client.get("/researcher/overview").json()
    assert body["mode"] == "shadow"
    assert body["tasks_total"] == 3
    assert body["by_status"] == {"done": 1, "pending": 2}
    assert body["by_disposition"] == {"supported": 1, "contradicted": 1}
    assert body["acquire"]["fired_24h"] == 5
    assert body["acquire"]["replied"] == 3
    assert body["acquire"]["outcomes"] == {"fulfilled": 2, "rejected": 1}
    assert body["acquire"]["pending"] == 2  # 5 fired - 3 replied

    rows = body["tasks"]
    assert rows[0]["finding"]["disposition"] == "supported"
    assert rows[0]["finding"]["confidence_move"] == 0.1
    assert rows[0]["finding"]["acquires_fired"] == 2
    assert rows[0]["at"] == _NOW.isoformat()
    assert rows[1]["finding"]["verdict"] == "fails"  # json string parsed
    assert rows[1]["claim_id"] is None
    assert rows[2]["finding"] is None  # no result
    assert rows[2]["at"] is None


def test_researcher_overview_empty():
    client = make_client(
        [
            ("count(*) AS n FROM tasks WHERE department = 'research' GROUP BY status", []),
            ("FROM tasks t LEFT JOIN claims c ON c.id = t.claim_id", []),
            ("event_type = 'acquire.requested' AND payload", []),
            ("payload->>'status' AS status FROM events", []),
        ]
    )
    body = client.get("/researcher/overview").json()
    assert body["mode"] == "off"  # None -> off
    assert body["tasks_total"] == 0
    assert body["by_disposition"] == {}
    assert body["acquire"] == {"fired_24h": 0, "replied": 0, "outcomes": {}, "pending": 0}
    assert body["tasks"] == []


# ── direct unit tests of pure helpers ─────────────────────────────────────────
def test_payload_helper():
    assert ariadne._payload('{"a": 1}') == {"a": 1}
    assert ariadne._payload("not json") == {}
    assert ariadne._payload({"b": 2}) == {"b": 2}
    assert ariadne._payload(123) == {}  # non-str, non-dict


def test_loads_loose_variants():
    assert ariadne._loads_loose(None) == {}
    assert ariadne._loads_loose("") == {}
    assert ariadne._loads_loose('{"x": 1}') == {"x": 1}
    assert ariadne._loads_loose('```json\n{"y": 2}\n```') == {"y": 2}
    assert ariadne._loads_loose('```\n{"z": 3}\n```') == {"z": 3}
    assert ariadne._loads_loose('<think>musing</think>{"a": 4}') == {"a": 4}
    assert ariadne._loads_loose("garbage not json") == {}
    assert ariadne._loads_loose("[1, 2, 3]") == {}  # valid json but not a dict


def test_extract_question_variants():
    assert ariadne._extract_question(None) is None
    assert ariadne._extract_question("no marker") is None
    assert ariadne._extract_question("# Question\nWhat?\n\n## Retrieved") == "What?"
    assert ariadne._extract_question("# Question : Plain question") == "Plain question"
    assert ariadne._extract_question("# Question\nQ body\n\n# Task next") == "Q body"
    assert ariadne._extract_question("# Question\nQ2\n# Task here") == "Q2"
    # body is only the stripped chars (colon/whitespace) -> "" -> None
    assert ariadne._extract_question("# Question :") is None


def test_deliberation_outcome_helper():
    out = ariadne._deliberation_outcome(
        {"mission_frame": "MF", "directions": [{"title": "A"}, {"title": ""}, "bad", {"x": 1}]}
    )
    assert out["label"] == "Framed"
    assert out["summary"] == "MF"
    assert out["items"] == ["A"]  # empty title and non-dict skipped
    # directions not a list -> empty items
    assert ariadne._deliberation_outcome({"directions": "nope"})["items"] == []


def test_reflection_outcome_helper():
    out = ariadne._reflection_outcome(
        {
            "portfolio_assessment": "PA",
            "verdicts": [
                {"claim_id": 1, "assessment": "keep", "reason": "  trim me  "},
                {"claim_id": 2, "assessment": "drop"},
                {"claim_id": 3},  # no assessment -> skipped
                "not a dict",
            ],
        }
    )
    assert out["label"] == "Steered"
    assert out["summary"] == "PA"  # falls back to portfolio_assessment
    assert out["items"] == ["#1 keep — trim me", "#2 drop"]
    # verdicts not a list -> empty items
    assert ariadne._reflection_outcome({"verdicts": {}})["items"] == []


def test_result_helper():
    assert researcher._result('{"a": 1}') == {"a": 1}
    assert researcher._result("bad json") == {}
    assert researcher._result({"b": 2}) == {"b": 2}
    assert researcher._result([1, 2]) == {}  # non-dict after parse path
    assert researcher._result(None) == {}


def test_task_row_no_result_and_timestamp_fallback():
    # No result -> finding None, and 'at' falls back through completed/started/created.
    r = {
        "id": 1,
        "task_type": "t",
        "status": "pending",
        "description": "d",
        "claim_id": None,
        "direction": None,
        "result": None,
        "completed_at": None,
        "started_at": None,
        "created_at": _NOW,
    }
    row = researcher._task_row(r)
    assert row["finding"] is None
    assert row["at"] == _NOW.isoformat()

    r2 = {**r, "completed_at": None, "started_at": None, "created_at": None}
    assert researcher._task_row(r2)["at"] is None


def test_queue_health_direct():
    pool = ScriptedPool(
        rules=[("WITH req AS", [{"pending": 4, "oldest_pending_age": 99, "requested_1h": 2, "resolved_1h": 1}])]
    )
    health = asyncio.run(ariadne._queue_health(pool))
    assert health == {
        "pending": 4,
        "oldest_pending_age_seconds": 99,
        "requested_1h": 2,
        "resolved_1h": 1,
    }
