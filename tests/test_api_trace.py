"""Tests for api/trace.py — the trace/session/journey router driven by a TestClient
over a minimal FastAPI app with a mocked DB pool (ScriptedPool). No real Postgres /
DATABASE_URL.

Covers /trace/sessions (+ min_steps HAVING filter + facets + filters), /trace/sessions/{id}
(per-session step DAG, summaries, parent/child, missing session), /trace/journeys (one row per
source, every outcome branch + facets + filters), /trace/journey/{ref} (full event chain keyed
on canonical_key, tolerant of sources with no document, rejected/blocked/certified branches),
and the Neo4j graph/stats + graph/claim endpoints (ok + unavailable). Plus direct unit tests of
the pure helpers (_latency_ms, _compact, _outcome) across every branch.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
from starlette.testclient import TestClient

from api import trace
from tests._helpers import FakeNeoDriver, ScriptedPool

_NOW = datetime(2026, 6, 9, 12, 0, 0)
_LATER = datetime(2026, 6, 9, 12, 0, 1, 500000)  # +1.5s


def make_client(rules):
    app = FastAPI()
    app.include_router(trace.router)
    app.state.pool = ScriptedPool(rules=rules)
    return TestClient(app)


# ── /trace/sessions ───────────────────────────────────────────────────────────
_SESSIONS_SQL = "FROM agent_sessions s"
_HANDLER_FACET_SQL = "SELECT handler_name, COUNT(*) AS n FROM agent_sessions"
_STATUS_FACET_SQL = "SELECT status, COUNT(*) AS n FROM agent_sessions"


def _session_row(**over):
    r = {
        "id": 7,
        "handler_name": "ariadne.deliberate",
        "status": "completed",
        "mode": "shadow",
        "started_at": _NOW,
        "completed_at": _LATER,
        "error": None,
        "triggered_by_event_id": 3,
        "trigger_event_type": "tick",
        "trigger_target_type": "agent",
        "trigger_target_id": 1,
        "step_count": 4,
        "failed_steps": 1,
        "input_tokens": 100,
        "output_tokens": 50,
    }
    r.update(over)
    return r


def test_list_sessions_full_and_facets():
    rows = [
        _session_row(),
        _session_row(
            id=6,
            started_at=None,
            completed_at=None,
            triggered_by_event_id=None,
            trigger_event_type=None,
            trigger_target_type=None,
            trigger_target_id=None,
        ),
    ]
    handlers = [{"handler_name": "mimir.ingest", "n": 10}, {"handler_name": "ariadne.deliberate", "n": 2}]
    statuses = [{"status": "completed", "n": 9}, {"status": "failed", "n": 1}]
    client = make_client(
        [
            (_SESSIONS_SQL, rows),
            (_HANDLER_FACET_SQL, handlers),
            (_STATUS_FACET_SQL, statuses),
        ]
    )
    body = client.get("/trace/sessions").json()
    s = body["sessions"][0]
    assert s["id"] == 7
    assert s["handler_name"] == "ariadne.deliberate"
    assert s["started_at"] == _NOW.isoformat()
    assert s["completed_at"] == _LATER.isoformat()
    assert s["latency_ms"] == 1500  # +1.5s
    assert s["trigger_event_id"] == 3
    assert s["trigger_event_type"] == "tick"
    assert s["step_count"] == 4
    assert s["failed_steps"] == 1
    assert s["input_tokens"] == 100
    assert s["output_tokens"] == 50
    # second row has no timestamps -> None / None latency
    s2 = body["sessions"][1]
    assert s2["started_at"] is None
    assert s2["completed_at"] is None
    assert s2["latency_ms"] is None
    assert s2["trigger_event_id"] is None
    assert body["facets"]["handlers"] == {"mimir.ingest": 10, "ariadne.deliberate": 2}
    assert body["facets"]["statuses"] == {"completed": 9, "failed": 1}


def test_list_sessions_empty():
    client = make_client(
        [
            (_SESSIONS_SQL, []),
            (_HANDLER_FACET_SQL, []),
            (_STATUS_FACET_SQL, []),
        ]
    )
    body = client.get("/trace/sessions").json()
    assert body["sessions"] == []
    assert body["facets"] == {"handlers": {}, "statuses": {}}


def test_list_sessions_filters_build_where_clause():
    """handler/status/mode filters add WHERE args; assert they reach the SQL as positional args."""
    client = make_client(
        [
            (_SESSIONS_SQL, []),
            (_HANDLER_FACET_SQL, []),
            (_STATUS_FACET_SQL, []),
        ]
    )
    r = client.get("/trace/sessions?handler_name=h1&status=completed&mode=shadow&limit=10")
    assert r.status_code == 200
    pool = client.app.state.pool
    main = next(c for c in pool.calls if c[0] == "fetch" and _SESSIONS_SQL in c[1])
    sql, args = main[1], main[2]
    assert "s.handler_name = $1" in sql
    assert "s.status = $2" in sql
    assert "s.mode = $3" in sql
    assert "WHERE " in sql
    assert args == ("h1", "completed", "shadow", 10)  # 3 filters + limit


def test_list_sessions_min_steps_adds_having():
    client = make_client(
        [
            (_SESSIONS_SQL, []),
            (_HANDLER_FACET_SQL, []),
            (_STATUS_FACET_SQL, []),
        ]
    )
    r = client.get("/trace/sessions?min_steps=2&limit=5")
    assert r.status_code == 200
    pool = client.app.state.pool
    main = next(c for c in pool.calls if c[0] == "fetch" and _SESSIONS_SQL in c[1])
    sql, args = main[1], main[2]
    assert "HAVING COUNT(r.id)" in sql
    assert ">= $1" in sql  # min_steps is the first (and only) arg before limit
    assert args == (2, 5)  # min_steps then limit


def test_list_sessions_limit_capped_at_200():
    client = make_client(
        [
            (_SESSIONS_SQL, []),
            (_HANDLER_FACET_SQL, []),
            (_STATUS_FACET_SQL, []),
        ]
    )
    client.get("/trace/sessions?limit=9999")
    pool = client.app.state.pool
    main = next(c for c in pool.calls if c[0] == "fetch" and _SESSIONS_SQL in c[1])
    assert main[2][-1] == 200  # limit clamped


# ── /trace/sessions/{id} ──────────────────────────────────────────────────────
_SESS_DETAIL_SQL = "e.payload     AS trigger_payload"
_RUNS_SQL = "FROM agent_runs"


def _detail_sess_row(**over):
    r = {
        "id": 7,
        "handler_name": "ariadne.deliberate",
        "status": "completed",
        "mode": "shadow",
        "started_at": _NOW,
        "completed_at": _LATER,
        "error": None,
        "triggered_by_event_id": 3,
        "trigger_event_type": "tick",
        "trigger_target_type": "agent",
        "trigger_target_id": 1,
        "trigger_payload": {"k": "v"},
    }
    r.update(over)
    return r


def _run_row(**over):
    r = {
        "id": 11,
        "invocation_type": "completion",
        "model_tier": "deep",
        "model_name": "deepseek",
        "status": "completed",
        "started_at": _NOW,
        "completed_at": _LATER,
        "input_token_count": 30,
        "output_token_count": 12,
        "input_summary": "in",
        "output_summary": "out",
        "error": None,
        "step_name": "ask",
        "parent_step_id": None,
        "step_order": 1,
        "fallback_attempts": ["modelA"],
        "langfuse_trace_id": "lf-1",
    }
    r.update(over)
    return r


def test_get_session_full_dag():
    sess = _detail_sess_row()
    runs = [
        _run_row(id=11, step_name="ask", parent_step_id=None, step_order=1),
        _run_row(
            id=12,
            step_name="synth",
            parent_step_id=11,
            step_order=2,
            started_at=None,
            completed_at=None,
            fallback_attempts=None,
        ),
    ]
    client = make_client([(_SESS_DETAIL_SQL, [sess]), (_RUNS_SQL, runs)])
    body = client.get("/trace/sessions/7").json()
    se = body["session"]
    assert se["id"] == 7
    assert se["latency_ms"] == 1500
    assert se["started_at"] == _NOW.isoformat()
    assert se["trigger_payload"] == {"k": "v"}
    rs = body["runs"]
    assert rs[0]["id"] == 11
    assert rs[0]["latency_ms"] == 1500
    assert rs[0]["input_summary"] == "in"
    assert rs[0]["output_summary"] == "out"
    assert rs[0]["fallback_attempts"] == ["modelA"]
    assert rs[0]["parent_step_id"] is None
    # child run, no timestamps -> latency None, falsy fallback_attempts -> []
    assert rs[1]["parent_step_id"] == 11
    assert rs[1]["latency_ms"] is None
    assert rs[1]["started_at"] is None
    assert rs[1]["fallback_attempts"] == []


def test_get_session_missing_returns_none():
    # No sess rule -> fetchrow returns default None.
    client = make_client([(_RUNS_SQL, [])])
    body = client.get("/trace/sessions/999").json()
    assert body == {"session": None, "runs": []}


def test_get_session_null_trigger_payload():
    sess = _detail_sess_row(trigger_payload=None, started_at=None, completed_at=None)
    client = make_client([(_SESS_DETAIL_SQL, [sess]), (_RUNS_SQL, [])])
    body = client.get("/trace/sessions/7").json()
    assert body["session"]["trigger_payload"] is None
    assert body["session"]["latency_ms"] is None
    assert body["runs"] == []


# ── /trace/graph/stats ────────────────────────────────────────────────────────
def test_graph_stats_ok(monkeypatch):
    from library.graph import tools as gtools

    # Each session.run returns records carrying a "count"; the fake driver replays them in order.
    counts = iter([7, 5, 3, 12, 4, 8])

    def on_run(query, params):
        return [{"count": next(counts)}]

    async def fake_get_driver():
        return FakeNeoDriver(on_run)

    monkeypatch.setattr(gtools, "_get_driver", fake_get_driver, raising=True)
    client = make_client([])
    body = client.get("/trace/graph/stats").json()
    assert body["status"] == "ok"
    assert body["nodes"] == {"claims": 7, "findings": 5, "verdicts": 3}
    assert body["edges"] == {"grounds": 12, "challenged": 4, "cited_by": 8}


def test_graph_stats_unavailable(monkeypatch):
    from library.graph import tools as gtools

    async def boom():
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(gtools, "_get_driver", boom, raising=True)
    client = make_client([])
    body = client.get("/trace/graph/stats").json()
    assert body["status"] == "unavailable"
    assert "neo4j down" in body["error"]


# ── /trace/graph/claim/{id} ───────────────────────────────────────────────────
def test_graph_claim_ok(monkeypatch):
    from library.graph import tools as gtools

    async def fake_evidence(claim_id, limit=20):
        return [{"finding": "f1"}]

    async def fake_critics(claim_id):
        return [{"verdict": "challenged"}]

    monkeypatch.setattr(gtools, "get_claim_evidence_chain", fake_evidence, raising=True)
    monkeypatch.setattr(gtools, "get_claim_critics", fake_critics, raising=True)
    client = make_client([])
    body = client.get("/trace/graph/claim/42").json()
    assert body["status"] == "ok"
    assert body["claim_id"] == 42
    assert body["evidence_chain"] == [{"finding": "f1"}]
    assert body["critic_verdicts"] == [{"verdict": "challenged"}]


def test_graph_claim_unavailable(monkeypatch):
    from library.graph import tools as gtools

    async def boom(*a, **k):
        raise RuntimeError("graph error")

    monkeypatch.setattr(gtools, "get_claim_evidence_chain", boom, raising=True)
    client = make_client([])
    body = client.get("/trace/graph/claim/9").json()
    assert body["status"] == "unavailable"
    assert body["claim_id"] == 9
    assert "graph error" in body["error"]


# ── /trace/journeys ───────────────────────────────────────────────────────────
_JOURNEYS_SQL = "WITH recent AS"
_REJ_SQL = "event_type = 'library.ingest_rejected'"
_BLOCKED_SQL = "event_type = 'mimir.ingest_blocked' AND target_id = ANY"


def _journey_row(**over):
    r = {
        "canonical_key": "arxiv:2401.0001",
        "source_kind": "arxiv",
        "title": "Discovered title",
        "event_id": 100,
        "started_at": _NOW,
        "doc_id": None,
        "doc_status": None,
        "queryable": None,
        "trust_tier": None,
        "doc_title": None,
        "ingested_at": None,
    }
    r.update(over)
    return r


def test_journeys_ingested_outcome():
    rows = [
        _journey_row(
            canonical_key="k-ing",
            doc_id=5,
            queryable=True,
            trust_tier="trusted",
            doc_status="ingested",
            doc_title="Doc Title",
            ingested_at=_LATER,
        ),
    ]
    client = make_client([(_JOURNEYS_SQL, rows), (_REJ_SQL, []), (_BLOCKED_SQL, [])])
    body = client.get("/trace/journeys").json()
    it = body["journeys"][0]
    assert it["outcome"] == "ingested"
    assert it["outcome_reason"] == "trusted · in the Library"
    assert it["title"] == "Doc Title"  # doc_title preferred
    assert it["doc_id"] == 5
    assert it["started_at"] == _NOW.isoformat()
    assert it["ended_at"] == _LATER.isoformat()  # ingested_at
    assert body["facets"] == {"ingested": 1}
    assert body["total"] == 1


def test_journeys_rejected_outcome():
    rows = [_journey_row(canonical_key="k-rej", title="Rej title")]
    rej = [{"ck": "k-rej", "reason": "low quality", "stage": "filter", "emitted_at": _LATER}]
    client = make_client([(_JOURNEYS_SQL, rows), (_REJ_SQL, rej), (_BLOCKED_SQL, [])])
    body = client.get("/trace/journeys").json()
    it = body["journeys"][0]
    assert it["outcome"] == "rejected"
    assert it["outcome_reason"] == "low quality"
    assert it["title"] == "Rej title"  # falls back to discovery title
    assert it["ended_at"] == _LATER.isoformat()  # rej emitted_at


def test_journeys_rejected_reason_blank_fallback():
    rows = [_journey_row(canonical_key="k-rej2")]
    rej = [{"ck": "k-rej2", "reason": None, "stage": "x", "emitted_at": _NOW}]
    client = make_client([(_JOURNEYS_SQL, rows), (_REJ_SQL, rej), (_BLOCKED_SQL, [])])
    body = client.get("/trace/journeys").json()
    assert body["journeys"][0]["outcome_reason"] == "rejected before ingest"


def test_journeys_blocked_outcome():
    rows = [_journey_row(canonical_key="k-blk", doc_id=8, queryable=False, doc_status="blocked")]
    blocked = [{"target_id": 8}]
    client = make_client([(_JOURNEYS_SQL, rows), (_REJ_SQL, []), (_BLOCKED_SQL, blocked)])
    body = client.get("/trace/journeys").json()
    it = body["journeys"][0]
    assert it["outcome"] == "blocked"
    assert it["outcome_reason"] == "blocked by Mimir before ingest"


def test_journeys_in_library_outcome():
    # doc exists, not queryable, not rejected, not blocked -> in_library(doc_status).
    rows = [_journey_row(canonical_key="k-lib", doc_id=9, queryable=False, doc_status="parsed")]
    client = make_client([(_JOURNEYS_SQL, rows), (_REJ_SQL, []), (_BLOCKED_SQL, [])])
    body = client.get("/trace/journeys").json()
    it = body["journeys"][0]
    assert it["outcome"] == "in_library"
    assert it["outcome_reason"] == "parsed"


def test_journeys_pending_outcome_and_title_fallback_to_key():
    # no doc, no rejection, no title -> pending, title falls back to canonical_key.
    rows = [_journey_row(canonical_key="k-pend", title=None, doc_title=None)]
    client = make_client([(_JOURNEYS_SQL, rows), (_REJ_SQL, []), (_BLOCKED_SQL, [])])
    body = client.get("/trace/journeys").json()
    it = body["journeys"][0]
    assert it["outcome"] == "pending"
    assert it["outcome_reason"] == "discovered, not yet resolved"
    assert it["title"] == "k-pend"  # falls all the way back to the key
    assert it["ended_at"] is None  # no ingested_at, no rejection


def test_journeys_empty_no_keys_skips_rej_and_blocked():
    # Empty rows -> keys empty -> rej/blocked sub-queries not issued at all.
    client = make_client([(_JOURNEYS_SQL, [])])
    body = client.get("/trace/journeys").json()
    assert body["journeys"] == []
    assert body["facets"] == {}
    assert body["total"] == 0
    pool = client.app.state.pool
    assert not any(_REJ_SQL in c[1] for c in pool.calls)
    assert not any(_BLOCKED_SQL in c[1] for c in pool.calls)


def test_journeys_filters_outcome_kind_q_and_limit():
    rows = [
        _journey_row(
            canonical_key="k-ing",
            source_kind="arxiv",
            doc_id=1,
            queryable=True,
            trust_tier="t",
            doc_title="GraphRAG paper",
            ingested_at=_NOW,
        ),
        _journey_row(canonical_key="k-pend", source_kind="web", title="Other thing"),
    ]
    client = make_client([(_JOURNEYS_SQL, rows), (_REJ_SQL, []), (_BLOCKED_SQL, [])])
    # outcome filter
    b = client.get("/trace/journeys?outcome=ingested").json()
    assert [i["canonical_key"] for i in b["journeys"]] == ["k-ing"]
    # kind filter
    b = client.get("/trace/journeys?kind=web").json()
    assert [i["canonical_key"] for i in b["journeys"]] == ["k-pend"]
    # q search (case-insensitive, matches title OR canonical_key)
    b = client.get("/trace/journeys?q=graphrag").json()
    assert [i["canonical_key"] for i in b["journeys"]] == ["k-ing"]
    b = client.get("/trace/journeys?q=k-pend").json()
    assert [i["canonical_key"] for i in b["journeys"]] == ["k-pend"]
    # limit caps the returned slice (total still counts all matched)
    b = client.get("/trace/journeys?limit=1").json()
    assert len(b["journeys"]) == 1
    assert b["total"] == 2


# ── /trace/journey/{ref} ──────────────────────────────────────────────────────
_DOC_BY_ID_SQL = "FROM documents WHERE id = $1"
_DOC_BY_KEY_SQL = "WHERE canonical_key = $1 OR arxiv_id = $1"
_DISC_META_SQL = "payload->'source'->>'kind' AS kind"
_SEEN_SQL = "FROM discovery_seen WHERE source_kind"
_DISCOVERED_EVENTS_SQL = "WHERE event_type = 'source.discovered' AND payload->'source'->>'canonical_key' = $1"
_REJECTED_EVENTS_SQL = "WHERE event_type = 'library.ingest_rejected' "
_CERTS_SQL = "FROM certifications WHERE document_id = $1"
_DOC_EVENTS_SQL = "WHERE target_type = 'document' AND target_id = $1"
_RUN_SESSION_SQL = "SELECT session_id FROM agent_runs WHERE id = $1"


def _doc_row(**over):
    r = {
        "id": 5,
        "title": "Doc Title",
        "source_kind": "arxiv",
        "canonical_key": "arxiv:2401.0001",
        "trust_tier": "trusted",
        "trust_state": "certified",
        "status": "ingested",
        "queryable": True,
        "ingested_at": _LATER,
    }
    r.update(over)
    return r


def test_journey_by_doc_id_full_chain():
    doc = _doc_row()
    seen = {"first_seen_at": _NOW, "attempts": 2}
    disc_events = [
        {
            "id": 200,
            "emitted_at": _NOW,
            "status": "ok",
            "payload": {"source": {"why": "looks relevant", "url": "http://x"}},
            "session_id": 9,
        },
    ]
    certs = [
        {
            "id": 1,
            "decision": "approve",
            "to_tier": "trusted",
            "used_llm": True,
            "reasons": "clean",
            "decided_by_run_id": 77,
            "created_at": _NOW,
        },
    ]
    doc_events = [
        {
            "id": 300,
            "event_type": "document.parsed",
            "emitted_at": _NOW,
            "status": "ok",
            "payload": {"foo": "bar"},
            "session_id": 9,
        },
        {
            "id": 301,
            "event_type": "document.ingested",
            "emitted_at": _LATER,
            "status": "ok",
            "payload": {"n_chunks": 12, "embedded": 12, "trust_tier": "trusted"},
            "session_id": 9,
        },
    ]
    client = make_client(
        [
            (_DOC_BY_ID_SQL, [doc]),
            (_SEEN_SQL, [seen]),
            (_DISCOVERED_EVENTS_SQL, disc_events),
            (_REJECTED_EVENTS_SQL, []),
            (_CERTS_SQL, certs),
            (_RUN_SESSION_SQL, [{"session_id": 9}]),
            (_DOC_EVENTS_SQL, doc_events),
        ]
    )
    body = client.get("/trace/journey/5").json()
    subj = body["subject"]
    assert subj["canonical_key"] == "arxiv:2401.0001"
    assert subj["doc_id"] == 5
    assert subj["trust_tier"] == "trusted"
    assert subj["queryable"] is True
    assert subj["ingested_at"] == _LATER.isoformat()
    assert subj["outcome"] == "ingested"  # queryable + ingest step
    assert subj["outcome_reason"] == "in the Library, retrievable"
    kinds = [s["kind"] for s in body["steps"]]
    assert "scout" in kinds
    assert "discovered" in kinds
    assert "certify" in kinds
    assert "parse" in kinds
    assert "ingest" in kinds
    # scout step detail mentions attempts
    scout = next(s for s in body["steps"] if s["kind"] == "scout")
    assert "attempts=2" in scout["detail"]
    # discovered detail uses 'why'
    disc = next(s for s in body["steps"] if s["kind"] == "discovered")
    assert disc["detail"] == "looks relevant"
    assert disc["session_id"] == 9
    # certify step resolved session_id via agent_runs lookup
    cert = next(s for s in body["steps"] if s["kind"] == "certify")
    assert cert["session_id"] == 9
    assert "used_llm=True" in cert["detail"]
    # ingest detail summarizes chunks
    ing = next(s for s in body["steps"] if s["kind"] == "ingest")
    assert "12 chunks" in ing["detail"]
    # steps sorted by 'at' then isoformatted
    assert all(isinstance(s["at"], str) for s in body["steps"])


def test_journey_by_canonical_key_no_doc_pending():
    # ref isn't a digit and not a doc -> learn source from discovery event; never became a doc.
    disc_meta = {"kind": "web", "title": "A web page", "url": "http://w"}
    disc_events = [
        {"id": 210, "emitted_at": _NOW, "status": "ok", "payload": {"source": {"url": "http://w"}}, "session_id": None},
    ]
    seen = {"first_seen_at": _NOW, "attempts": 1}
    client = make_client(
        [
            (_DOC_BY_KEY_SQL, []),  # no doc by key
            (_DISC_META_SQL, [disc_meta]),
            (_SEEN_SQL, [seen]),
            (_DISCOVERED_EVENTS_SQL, disc_events),
            (_REJECTED_EVENTS_SQL, []),
        ]
    )
    body = client.get("/trace/journey/some-web-key").json()
    subj = body["subject"]
    assert subj["doc_id"] is None
    assert subj["source_kind"] == "web"  # learned from discovery meta
    assert subj["title"] == "A web page"  # disc_meta title
    assert subj["queryable"] is False
    assert subj["outcome"] == "pending"
    assert subj["outcome_reason"] == "discovered, not yet resolved"
    # discovered detail falls back to url (no 'why')
    disc = next(s for s in body["steps"] if s["kind"] == "discovered")
    assert disc["detail"] == "http://w"


def test_journey_no_doc_no_disc_meta_no_source_kind():
    # ref not digit, no doc, no discovery meta -> source_kind None -> no scout step, title=key.
    client = make_client(
        [
            (_DOC_BY_KEY_SQL, []),
            (_DISC_META_SQL, []),  # fetchrow None
            (_DISCOVERED_EVENTS_SQL, []),
            (_REJECTED_EVENTS_SQL, []),
        ]
    )
    body = client.get("/trace/journey/orphan-key").json()
    subj = body["subject"]
    assert subj["source_kind"] is None
    assert subj["title"] == "orphan-key"
    assert subj["outcome"] == "pending"
    assert body["steps"] == []
    # the discovery_seen lookup is skipped when source_kind is None
    pool = client.app.state.pool
    assert not any(_SEEN_SQL in c[1] for c in pool.calls)


def test_journey_rejected_source_no_doc():
    # A source rejected before it ever became a document -> rejected step, rejected outcome.
    disc_meta = {"kind": "arxiv", "title": "Rejected paper", "url": "http://r"}
    rej_events = [
        {"id": 400, "emitted_at": _NOW, "status": "rejected", "payload": {"reason": "duplicate", "stage": "dedup"}},
    ]
    client = make_client(
        [
            (_DOC_BY_KEY_SQL, []),
            (_DISC_META_SQL, [disc_meta]),
            (_SEEN_SQL, []),  # fetchrow None -> no scout step
            (_DISCOVERED_EVENTS_SQL, []),
            (_REJECTED_EVENTS_SQL, rej_events),
        ]
    )
    body = client.get("/trace/journey/rej-key").json()
    subj = body["subject"]
    assert subj["doc_id"] is None
    assert subj["outcome"] == "rejected"
    rej = next(s for s in body["steps"] if s["kind"] == "rejected")
    assert rej["detail"] == "duplicate (stage: dedup)"
    assert rej["label"] == "library.ingest_rejected"


def test_journey_blocked_certification():
    # A document whose certification was NOT approve -> blocked step + blocked outcome.
    doc = _doc_row(id=6, queryable=False, status="parsed", trust_state="suspect")
    certs = [
        {
            "id": 2,
            "decision": "reject",
            "to_tier": "quarantine",
            "used_llm": False,
            "reasons": "retracted source",
            "decided_by_run_id": None,
            "created_at": _NOW,
        },
    ]
    blocked_events = [
        {
            "id": 500,
            "event_type": "mimir.ingest_blocked",
            "emitted_at": _LATER,
            "status": "blocked",
            "payload": {"reasons": "retracted"},
            "session_id": 9,
        },
    ]
    client = make_client(
        [
            (_DOC_BY_ID_SQL, [doc]),
            (_SEEN_SQL, []),
            (_DISCOVERED_EVENTS_SQL, []),
            (_REJECTED_EVENTS_SQL, []),
            (_CERTS_SQL, certs),
            (_DOC_EVENTS_SQL, blocked_events),
        ]
    )
    body = client.get("/trace/journey/6").json()
    subj = body["subject"]
    assert subj["outcome"] == "blocked"
    # the first 'blocked' step is the certification (sorted earlier), so its detail is the reason
    assert "used_llm=False" in subj["outcome_reason"]
    assert "retracted source" in subj["outcome_reason"]
    # certification with decision != approve -> kind "blocked", label "Mimir blocked"
    cert = next(s for s in body["steps"] if s["label"] == "Mimir blocked")
    assert cert["kind"] == "blocked"
    assert cert["session_id"] is None  # decided_by_run_id None -> no lookup
    assert "used_llm=False" in cert["detail"]
    # mimir.ingest_blocked event -> kind blocked, detail from reasons
    evt = next(s for s in body["steps"] if s["event_id"] == 500)
    assert evt["kind"] == "blocked"
    assert evt["detail"] == "retracted"


def test_journey_in_library_outcome_doc_not_queryable_no_block():
    # doc exists, not queryable, no rejected/blocked steps -> in_library outcome.
    doc = _doc_row(id=7, queryable=False, status="parsed")
    parsed_events = [
        {
            "id": 600,
            "event_type": "document.parsed",
            "emitted_at": _NOW,
            "status": "ok",
            "payload": {"pages": 10},
            "session_id": None,
        },
    ]
    client = make_client(
        [
            (_DOC_BY_ID_SQL, [doc]),
            (_SEEN_SQL, []),
            (_DISCOVERED_EVENTS_SQL, []),
            (_REJECTED_EVENTS_SQL, []),
            (_CERTS_SQL, []),
            (_DOC_EVENTS_SQL, parsed_events),
        ]
    )
    body = client.get("/trace/journey/7").json()
    assert body["subject"]["outcome"] == "in_library"
    assert body["subject"]["outcome_reason"] == "document created"
    # an unknown-mapping event still becomes kind "parse" here; a non-mapped type -> "event"
    parse = next(s for s in body["steps"] if s["event_id"] == 600)
    assert parse["kind"] == "parse"
    assert parse["detail"] == ""  # parse kind has no detail synthesis


def test_journey_generic_event_kind():
    # A document event whose type isn't in the kind map -> kind "event".
    doc = _doc_row(id=8, queryable=True, status="ingested")
    generic = [
        {
            "id": 700,
            "event_type": "document.reindexed",
            "emitted_at": _NOW,
            "status": "ok",
            "payload": "not-a-dict",
            "session_id": None,
        },
    ]
    client = make_client(
        [
            (_DOC_BY_ID_SQL, [doc]),
            (_SEEN_SQL, []),
            (_DISCOVERED_EVENTS_SQL, []),
            (_REJECTED_EVENTS_SQL, []),
            (_CERTS_SQL, []),
            (_DOC_EVENTS_SQL, generic),
        ]
    )
    body = client.get("/trace/journey/8").json()
    evt = next(s for s in body["steps"] if s["event_id"] == 700)
    assert evt["kind"] == "event"
    assert evt["detail"] == ""
    assert evt["payload"] is None  # non-dict payload -> _compact None


# ── unit tests: _latency_ms ───────────────────────────────────────────────────
def test_latency_ms():
    assert trace._latency_ms(_NOW, _LATER) == 1500
    assert trace._latency_ms(None, _LATER) is None
    assert trace._latency_ms(_NOW, None) is None
    assert trace._latency_ms(None, None) is None
    assert trace._latency_ms(_NOW, _NOW) == 0


# ── unit tests: _compact ──────────────────────────────────────────────────────
def test_compact_non_dict_returns_none():
    assert trace._compact(None) is None
    assert trace._compact("str") is None
    assert trace._compact([1, 2]) is None


def test_compact_empty_dict_returns_none():
    assert trace._compact({}) is None


def test_compact_trims_long_top_level_string():
    long = "x" * 400
    out = trace._compact({"k": long, "short": "ok", "n": 7})
    assert out["k"] == "x" * 300 + "…"
    assert out["short"] == "ok"
    assert out["n"] == 7


def test_compact_trims_nested_dict_strings():
    nested = {"deep": "y" * 250, "ok": "fine", "num": 3}
    out = trace._compact({"meta": nested})
    assert out["meta"]["deep"] == "y" * 200 + "…"
    assert out["meta"]["ok"] == "fine"
    assert out["meta"]["num"] == 3


def test_compact_preserves_non_string_values():
    out = trace._compact({"flag": True, "lst": [1, 2], "none": None})
    assert out == {"flag": True, "lst": [1, 2], "none": None}


# ── unit tests: _outcome ──────────────────────────────────────────────────────
def _step(kind, **over):
    s = {"kind": kind, "detail": over.get("detail", "")}
    s.update(over)
    return s


def test_outcome_ingested():
    steps = [_step("scout"), _step("ingest")]
    assert trace._outcome(steps, has_doc=True, queryable=True) == (
        "ingested",
        "in the Library, retrievable",
    )


def test_outcome_ingest_without_queryable_is_not_ingested():
    # ingest present but not queryable -> falls through to has_doc -> in_library.
    steps = [_step("ingest")]
    assert trace._outcome(steps, has_doc=True, queryable=False) == (
        "in_library",
        "document created",
    )


def test_outcome_rejected_uses_detail():
    steps = [_step("rejected", detail="bad source")]
    assert trace._outcome(steps, has_doc=False, queryable=False) == ("rejected", "bad source")


def test_outcome_rejected_default_detail():
    steps = [{"kind": "rejected"}]  # no detail key
    assert trace._outcome(steps, has_doc=False, queryable=False) == (
        "rejected",
        "rejected before ingest",
    )


def test_outcome_blocked_uses_detail():
    steps = [_step("blocked", detail="mimir says no")]
    assert trace._outcome(steps, has_doc=True, queryable=False) == ("blocked", "mimir says no")


def test_outcome_blocked_default_detail():
    steps = [{"kind": "blocked"}]
    assert trace._outcome(steps, has_doc=True, queryable=False) == ("blocked", "blocked by Mimir")


def test_outcome_in_library_has_doc():
    steps = [_step("parse")]
    assert trace._outcome(steps, has_doc=True, queryable=False) == (
        "in_library",
        "document created",
    )


def test_outcome_pending():
    steps = [_step("discovered")]
    assert trace._outcome(steps, has_doc=False, queryable=False) == (
        "pending",
        "discovered, not yet resolved",
    )
