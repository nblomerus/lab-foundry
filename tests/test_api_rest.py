"""REST + stream + app-factory tests for the command-center API.

Every GET endpoint in api.knowledge / api.debug / api.ops / api.snapshot is driven
through a starlette TestClient over a minimal FastAPI app whose app.state.pool is a
ScriptedPool — no real Postgres, no DATABASE_URL, no network. api.ops host stats and
api.debug costs monkeypatch psutil / nvidia-smi / the DeepSeek balance to canned values.
api.stream is unit-tested directly (StreamHub enrichment, fanout, the notify callback,
the listener start/stop) with a fake websocket and a ScriptedPool. api.main is built via
its lifespan with asyncpg.create_pool + the stream hub mocked. api.models is exercised by
instantiating each pydantic model with valid + edge data.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from api import debug, knowledge, main, ops, snapshot, stream
from api.models import (
    AgentRunOut,
    CompanyStateOut,
    CostTrackingOut,
    DissentItem,
    EdgeActivity,
    EventOut,
    FindingOut,
    LessonOut,
    OrgRoleOut,
    PhaseTransitionOut,
    SnapshotOut,
    StatsOut,
    TaskCount,
    TelemetryDay,
    ThesisOut,
)
from library.corpus.tools import RetrievedChunk
from tests._helpers import ScriptedPool

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def make_client(rules, *, router):
    app = FastAPI()
    app.include_router(router)
    app.state.pool = ScriptedPool(rules=rules)
    return TestClient(app)


class _RaisingPool:
    """A pool whose acquire()/method raises the given error on first DB access."""

    def __init__(self, err, *, method="fetch"):
        self._err = err
        self._method = method

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def _raise(self, *_a):
        raise self._err

    def __getattr__(self, name):
        if name in ("fetch", "fetchrow", "fetchval", "execute"):
            return self._raise
        raise AttributeError(name)


def _raising_client(err, *, router=knowledge.router):
    app = FastAPI()
    app.include_router(router)
    app.state.pool = _RaisingPool(err)
    return TestClient(app)


# ════════════════════════════════════════════════════════════════════════════
# api.knowledge
# ════════════════════════════════════════════════════════════════════════════
def _knowledge_client(rules):
    return make_client(rules, router=knowledge.router)


def test_knowledge_stats_full(monkeypatch):
    # Pin the graph block deterministically (a live Neo4j may or may not be up).
    async def _graph():
        return {"status": "unavailable", "error": "down"}

    monkeypatch.setattr(knowledge, "_graph_stats", _graph)
    rules = [
        ("FROM documents GROUP BY kind", [{"k": "paper", "c": 3}, {"k": "web", "c": 2}]),
        ("GROUP BY trust_tier", [{"t": "high", "c": 4}]),
        ("FROM documents GROUP BY status", [{"s": "certified", "c": 5}]),
        ("FILTER (WHERE embedding IS NOT NULL)", {"total": 100, "embedded": 80}),
        ("SELECT COUNT(*) FROM datasets", 7),
        ("COALESCE(certified_at, ingested_at) >= date_trunc('day', now())", 2),
        ("SELECT COUNT(*) FROM claims", 9),
        ("FROM experiment_runs", 4),
    ]
    body = _knowledge_client(rules).get("/knowledge/stats").json()
    assert body["corpus"]["status"] == "ok"
    assert body["corpus"]["documents_by_kind"] == {"paper": 3, "web": 2}
    assert body["corpus"]["docs_by_trust_tier"] == {"high": 4}
    assert body["corpus"]["chunks"] == 100
    assert body["corpus"]["chunks_embedded"] == 80
    assert body["corpus"]["datasets"] == 7
    assert body["graph"]["status"] == "unavailable"
    assert body["memory"] == {"claims": 9, "experiments": 4}


def test_knowledge_stats_planned_when_table_missing(monkeypatch):
    async def _raise(_pool):
        raise asyncpg.UndefinedTableError("no documents")

    monkeypatch.setattr(knowledge, "_corpus_stats", _raise)
    body = _knowledge_client([]).get("/knowledge/stats").json()
    assert body["corpus"]["status"] == "planned"
    assert body["corpus"]["chunks"] == 0


def test_knowledge_stats_corpus_error(monkeypatch):
    async def _boom(_pool):
        raise ValueError("kaboom")

    monkeypatch.setattr(knowledge, "_corpus_stats", _boom)
    body = _knowledge_client([]).get("/knowledge/stats").json()
    assert body["corpus"]["status"] == "error"
    assert "kaboom" in body["corpus"]["error"]


def test_knowledge_graph_stats_ok(monkeypatch):
    class _Res:
        def __init__(self, n):
            self._n = n

        async def data(self):
            return [{"count": self._n}]

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def run(self, query):
            if "COUNT(n)" in query:
                return _Res(10)
            if "(p:Paper)" in query:
                return _Res(4)
            if "(d:Dataset)" in query:
                return _Res(2)
            return _Res(1)  # citations (CITES)

    class _Driver:
        def session(self):
            return _Sess()

    async def _get_driver():
        return _Driver()

    import library.graph.tools as gt

    monkeypatch.setattr(gt, "_get_driver", _get_driver)
    out = asyncio.run(knowledge._graph_stats())
    assert out["status"] == "ok"
    assert out["nodes"] == 10
    assert out["papers"] == 4
    assert out["datasets"] == 2
    assert out["citations"] == 1


def test_memory_counts_pool_fails():
    class _BadPool:
        def acquire(self):
            raise RuntimeError("pool gone")

    out = asyncio.run(knowledge._memory_counts(_BadPool()))
    assert out == {"claims": 0, "experiments": 0}


def test_memory_counts_experiments_table_missing():
    # claims ok, experiment_runs raises → experiments coerced to 0.
    pool = ScriptedPool(
        rules=[
            ("SELECT COUNT(*) FROM claims", 5),
            ("FROM experiment_runs", lambda: (_ for _ in ()).throw(RuntimeError("no table"))),
        ]
    )
    out = asyncio.run(knowledge._memory_counts(pool))
    assert out == {"claims": 5, "experiments": 0}


def test_knowledge_recent_ok():
    rows = [
        {
            "id": 1,
            "title": "Paper A",
            "source_kind": "arxiv",
            "arxiv_id": "2401.1",
            "source_url": "http://x",
            "status": "certified",
            "at": _NOW,
        },
        {
            "id": 2,
            "title": "Paper B",
            "source_kind": "web",
            "arxiv_id": None,
            "source_url": None,
            "status": "pending",
            "at": None,
        },
    ]
    rules = [
        ("FROM documents ORDER BY", rows),
        (">= date_trunc('day', now())", 1),
    ]
    body = _knowledge_client(rules).get("/knowledge/recent?limit=50").json()
    assert body["status"] == "ok"
    assert body["today"] == 1
    assert body["items"][0]["at"] == _NOW.isoformat()
    assert body["items"][1]["at"] is None


def test_knowledge_recent_error():
    body = _raising_client(RuntimeError("down")).get("/knowledge/recent").json()
    assert body["status"] == "error"
    assert body["items"] == []


def test_mimir_panel_full():
    rules = [
        ("status::text s, COUNT(*) c FROM documents", [{"s": "certified", "c": 10}, {"s": "blocked", "c": 1}]),
        ("trust_tier::text t", [{"t": "high", "c": 8}]),
        ("source_kind, COUNT(*) c FROM documents GROUP BY source_kind", [{"source_kind": "arxiv", "c": 9}]),
        (
            "event_type e, COUNT(*) c FROM events",
            [{"e": "document.ingested", "c": 3}, {"e": "source.discovered", "c": 5}],
        ),
        ("event_type = 'document.ingested' ", 2),  # ingested_yday fetchval
        ("status NOT IN ('certified', 'quarantined', 'blocked')", 4),
        (
            "WHERE status = 'certified' ",
            [{"title": "C1", "source_kind": "arxiv", "arxiv_id": "1", "canonical_key": "k", "at": _NOW}],
        ),
        (
            "WHERE event_type IN ('acquire.requested'",
            [
                {
                    "event_type": "acquire.requested",
                    "payload": {"query": "graphrag", "requested_by": "ariadne"},
                    "emitted_at": _NOW,
                }
            ],
        ),
        ("event_type = 'library.trends'", {"payload": {"topics": ["t1", "t2"]}}),
    ]
    body = _knowledge_client(rules).get("/knowledge/mimir").json()
    assert body["status"] == "ok"
    assert body["at_a_glance"]["certified"] == 10
    assert body["at_a_glance"]["quarantined"] == 1  # blocked counts toward quarantined
    assert body["at_a_glance"]["ingested_today"] == 3
    assert body["at_a_glance"]["ingested_yesterday"] == 2
    assert body["at_a_glance"]["pending"] == 4
    assert body["trust_ladder"] == {"high": 8}
    assert body["pipeline_today"]["discovered"] == 5
    assert body["source_mix"][0] == {"kind": "arxiv", "count": 9, "pct": 100}
    assert body["focus_topics"] == ["t1", "t2"]
    assert body["recent_certifications"][0]["title"] == "C1"
    assert body["requests"][0]["requester"] == "ariadne"
    assert body["requests"][0]["ask"] == "graphrag"
    assert body["requests"][0]["status"] == "requested"


def test_mimir_panel_no_trends_no_mix():
    # Empty mix → total_mix falls to 1; no trends row → focus_topics [].
    rules = [
        ("status::text s, COUNT(*) c FROM documents", []),
        ("trust_tier::text t", []),
        ("source_kind, COUNT(*) c FROM documents GROUP BY source_kind", []),
        ("event_type e, COUNT(*) c FROM events", []),
        ("event_type = 'document.ingested' ", None),
        ("status NOT IN ('certified', 'quarantined', 'blocked')", None),
        ("WHERE status = 'certified' ", []),
        ("WHERE event_type IN ('acquire.requested'", []),
        # no library.trends rule → fetchrow None
    ]
    body = _knowledge_client(rules).get("/knowledge/mimir").json()
    assert body["status"] == "ok"
    assert body["source_mix"] == []
    assert body["focus_topics"] == []
    assert body["requests"] == []


def test_mimir_panel_planned():
    body = _raising_client(asyncpg.UndefinedTableError("nope")).get("/knowledge/mimir").json()
    assert body == {"status": "planned"}


def test_mimir_panel_error():
    body = _raising_client(RuntimeError("boom")).get("/knowledge/mimir").json()
    assert body["status"] == "error"
    assert "boom" in body["error"]


def test_acquire_row_string_payload_and_fallbacks():
    r = {
        "event_type": "acquire.fulfilled",
        "payload": '{"topic": "rerank", "agent": "researcher"}',
        "emitted_at": None,
    }
    row = knowledge._acquire_row(r)
    assert row["status"] == "fulfilled"
    assert row["ask"] == "rerank"  # topic fallback
    assert row["requester"] == "researcher"  # agent fallback
    assert row["at"] is None
    # Unknown event_type echoes through; no ask/requester → em dash + None ask.
    r2 = {"event_type": "acquire.weird", "payload": {}, "emitted_at": _NOW}
    row2 = knowledge._acquire_row(r2)
    assert row2["status"] == "acquire.weird"
    assert row2["ask"] is None
    assert row2["requester"] == "—"


def test_parse_topic_variants():
    assert knowledge._parse_topic(None) is None
    assert knowledge._parse_topic("no topic marker") is None
    assert knowledge._parse_topic("arxiv topic: continual learning") == "continual learning"
    assert knowledge._parse_topic("dataset topic: vision (HF downloads=9)") == "vision"
    assert knowledge._parse_topic("topic:   ") is None  # empty after strip
    assert knowledge._parse_topic("topic: " + "x" * 60) is None  # too long


def test_payload_helper():
    assert knowledge._payload('{"a": 1}') == {"a": 1}
    assert knowledge._payload("") == {}
    assert knowledge._payload({"b": 2}) == {"b": 2}
    assert knowledge._payload(None) == {}


def test_scout_panel_unknown_kind():
    body = _knowledge_client([]).get("/knowledge/scout?kind=pinterest").json()
    assert body["status"] == "error"
    assert "pinterest" in body["error"]


def test_scout_panel_full():
    recent = [
        {
            "title": "P1",
            "source_url": "http://p1",
            "arxiv_id": "1",
            "canonical_key": "k1",
            "status": "certified",
            "at": _NOW,
            "snippet": "  hello world  ",
        },
        {
            "title": "P2",
            "source_url": None,
            "arxiv_id": None,
            "canonical_key": None,
            "status": "pending",
            "at": None,
            "snippet": None,
        },
    ]
    searched = [
        {"why": "arxiv topic: graphrag", "emitted_at": _NOW},
        {"why": "arxiv topic: graphrag", "emitted_at": _NOW},  # dup → deduped
        {"why": "arxiv topic: rerank", "emitted_at": _NOW},
        {"why": None, "emitted_at": _NOW},
    ]
    rules = [
        # added_today is more specific (has AND COALESCE) → list it before in_corpus.
        ("WHERE source_kind = $1 AND COALESCE(certified_at, ingested_at) >= date_trunc('day', now())", 3),
        ("SELECT COUNT(*) FROM documents WHERE source_kind = $1", 12),
        ("FROM documents d\n", recent),
        ("event_type = 'source.discovered' ", searched),
    ]
    body = _knowledge_client(rules).get("/knowledge/scout?kind=arxiv").json()
    assert body["status"] == "ok"
    assert body["in_corpus"] == 12
    assert body["added_today"] == 3
    assert body["last_searched"]["topics"] == ["graphrag", "rerank"]
    assert body["last_searched"]["at"] == _NOW.isoformat()
    assert body["recent"][0]["snippet"] == "hello world"
    assert body["recent"][1]["snippet"] is None
    assert body["recent"][1]["at"] is None


def test_scout_panel_no_searched():
    rules = [
        ("WHERE source_kind = $1 AND COALESCE(certified_at, ingested_at) >= date_trunc('day', now())", None),
        ("SELECT COUNT(*) FROM documents WHERE source_kind = $1", None),
        ("FROM documents d\n", []),
        ("event_type = 'source.discovered' ", []),
    ]
    body = _knowledge_client(rules).get("/knowledge/scout?kind=web").json()
    assert body["in_corpus"] == 0
    assert body["last_searched"] == {"topics": [], "at": None}


def test_scout_panel_topic_cap():
    searched = [{"why": f"arxiv topic: topic{i}", "emitted_at": _NOW} for i in range(12)]
    rules = [
        ("WHERE source_kind = $1 AND COALESCE(certified_at, ingested_at) >= date_trunc('day', now())", 0),
        ("SELECT COUNT(*) FROM documents WHERE source_kind = $1", 0),
        ("FROM documents d\n", []),
        ("event_type = 'source.discovered' ", searched),
    ]
    body = _knowledge_client(rules).get("/knowledge/scout?kind=arxiv").json()
    assert len(body["last_searched"]["topics"]) == 8  # capped at 8


def test_scout_panel_planned():
    body = _raising_client(asyncpg.UndefinedTableError("nope")).get("/knowledge/scout?kind=arxiv").json()
    assert body == {"status": "planned", "source_kind": "arxiv"}


def test_scout_panel_error():
    body = _raising_client(RuntimeError("kaboom")).get("/knowledge/scout?kind=arxiv").json()
    assert body["status"] == "error"
    assert body["source_kind"] == "arxiv"


def test_gate_panel_unknown_kind():
    body = _knowledge_client([]).get("/knowledge/gate?kind=pinterest").json()
    assert body["status"] == "error"
    assert "pinterest" in body["error"]


def test_gate_panel_full_scoped():
    blocked = [
        {
            "emitted_at": _NOW,
            "payload": {"reasons": ["spam", "low-trust"]},
            "title": "Bad",
            "source_kind": "web",
            "source_url": "http://bad",
        }
    ]
    rejected = [
        {
            "emitted_at": None,
            "payload": {"title": "Junk", "url": "http://junk", "source_kind": "web", "reason": "duplicate"},
        }
    ]
    admitted = [
        {
            "title": "Good",
            "source_kind": "web",
            "arxiv_id": None,
            "canonical_key": "ck",
            "trust_tier": "high",
            "at": _NOW,
        }
    ]
    rules = [
        ("WHERE d.status='certified'", 5),
        ("mimir.ingest_blocked' AND d.source_kind = $1 AND e.emitted_at >= date_trunc", 2),
        ("library.ingest_rejected' AND payload->>'source_kind' = $1 AND emitted_at >= date_trunc", 1),
        ("source.discovered'", 9),
        ("WHERE TRUE", 50),
        ("WHERE status='blocked'", 3),
        ("mimir.ingest_blocked' AND d.source_kind = $1 ORDER BY e.emitted_at DESC", blocked),
        ("library.ingest_rejected' AND payload->>'source_kind' = $1 ORDER BY emitted_at DESC", rejected),
        ("FROM documents d WHERE status='certified'", admitted),
    ]
    body = _knowledge_client(rules).get("/knowledge/gate?kind=web").json()
    assert body["status"] == "ok"
    assert body["scope"] == "web"
    assert body["in_corpus"] == 50
    assert body["quarantined"] == 3
    assert body["today"]["admitted"] == 5
    assert body["today"]["discovered"] == 9
    ta = body["turned_away"]
    assert any(x["gate"] == "trust" for x in ta)
    assert any(x["gate"] == "quality" for x in ta)
    trust = next(x for x in ta if x["gate"] == "trust")
    assert trust["reason"] == ["spam", "low-trust"]
    quality = next(x for x in ta if x["gate"] == "quality")
    assert quality["reason"] == "duplicate"
    assert quality["at"] is None
    assert body["admitted"][0]["title"] == "Good"


def test_gate_panel_unscoped_default_reasons():
    blocked = [{"emitted_at": _NOW, "payload": {}, "title": "B", "source_kind": "web", "source_url": "u"}]
    rejected = [{"emitted_at": _NOW, "payload": {}}]
    rules = [
        ("WHERE d.status='certified'", None),
        ("mimir.ingest_blocked' AND e.emitted_at >= date_trunc", None),
        ("library.ingest_rejected' AND emitted_at >= date_trunc", None),
        ("source.discovered'", None),
        ("WHERE TRUE", None),
        ("WHERE status='blocked'", None),
        ("mimir.ingest_blocked' ORDER BY e.emitted_at DESC", blocked),
        ("library.ingest_rejected' ORDER BY emitted_at DESC", rejected),
        ("FROM documents d WHERE status='certified'", []),
    ]
    body = _knowledge_client(rules).get("/knowledge/gate").json()
    assert body["scope"] == "all"
    trust = next(x for x in body["turned_away"] if x["gate"] == "trust")
    assert trust["reason"] == "blocked by trust gate"  # default
    quality = next(x for x in body["turned_away"] if x["gate"] == "quality")
    assert quality["reason"] == "failed quality gate"  # default


def test_gate_panel_planned():
    body = _raising_client(asyncpg.UndefinedTableError("nope")).get("/knowledge/gate").json()
    assert body == {"status": "planned"}


def test_gate_panel_error():
    body = _raising_client(RuntimeError("oops")).get("/knowledge/gate").json()
    assert body["status"] == "error"


def _chunk(**over):
    base = dict(
        chunk_id=1,
        document_id=5,
        ordinal=0,
        text="some snippet text",
        kind="paper",
        title="T",
        source_url="http://u",
        trust_tier="high",
        distance=0.1,
        sim=0.9,
        trust_w=1.0,
        recency=0.5,
        score=0.876543,
    )
    base.update(over)
    return RetrievedChunk(**base)


def test_knowledge_search_empty_query():
    body = _knowledge_client([]).get("/knowledge/search?q=  ").json()
    assert body == {"status": "ok", "query": "", "hits": []}


def test_knowledge_search_ok(monkeypatch):
    async def _search(q, k):
        assert q == "graphrag"
        return [_chunk()]

    monkeypatch.setattr(knowledge, "corpus_search", _search)
    body = _knowledge_client([]).get("/knowledge/search?q=graphrag&k=3").json()
    assert body["status"] == "ok"
    assert body["hits"][0]["document_id"] == 5
    assert body["hits"][0]["score"] == 0.8765
    assert body["hits"][0]["snippet"] == "some snippet text"


def test_knowledge_search_error(monkeypatch):
    async def _boom(q, k):
        raise RuntimeError("embed down")

    monkeypatch.setattr(knowledge, "corpus_search", _boom)
    body = _knowledge_client([]).get("/knowledge/search?q=x").json()
    assert body["status"] == "error"
    assert body["hits"] == []


def test_knowledge_timeseries_ok_with_kind_join():
    rows = [{"t": _NOW, "value": 2}, {"t": _NOW + timedelta(hours=1), "value": 0}]
    rules = [("WITH buckets AS", rows)]
    body = _knowledge_client(rules).get("/knowledge/timeseries?metric=ingested&kind=arxiv&bucket=hour").json()
    assert body["status"] == "ok"
    assert body["metric"] == "ingested"
    assert body["kind"] == "arxiv"
    assert body["points"][0]["value"] == 2
    # discovered uses the payload-kind clause path (no documents join)
    body2 = _knowledge_client(rules).get("/knowledge/timeseries?metric=discovered&kind=web&bucket=day").json()
    assert body2["status"] == "ok"


def test_knowledge_timeseries_invalid_inputs():
    c = _knowledge_client([])
    assert c.get("/knowledge/timeseries?metric=bogus").json()["status"] == "error"
    assert c.get("/knowledge/timeseries?metric=ingested&bucket=week").json()["status"] == "error"
    assert c.get("/knowledge/timeseries?metric=ingested&kind=pinterest").json()["status"] == "error"


def test_knowledge_timeseries_planned():
    body = _raising_client(asyncpg.UndefinedTableError("nope")).get("/knowledge/timeseries").json()
    assert body["status"] == "planned"


def test_knowledge_timeseries_error():
    body = _raising_client(RuntimeError("bad sql")).get("/knowledge/timeseries").json()
    assert body["status"] == "error"


# ════════════════════════════════════════════════════════════════════════════
# api.debug
# ════════════════════════════════════════════════════════════════════════════
def _debug_client(rules):
    return make_client(rules, router=debug.router)


def test_agent_runs_full():
    rows = [
        {
            "id": 9,
            "started_at": _NOW,
            "completed_at": _NOW + timedelta(seconds=2),
            "agent_name": "researcher",
            "invocation_type": "verify",
            "model_tier": "workhorse",
            "model_name": "deepseek-v4",
            "status": "completed",
            "error": None,
            "output_summary": "done",
            "input_token_count": 100,
            "output_token_count": 50,
            "trigger_target_type": "task",
            "trigger_target_id": 42,
        },
        {
            "id": 8,
            "started_at": None,
            "completed_at": None,
            "agent_name": "pi",
            "invocation_type": "plan",
            "model_tier": "reasoning",
            "model_name": "deepseek-v4",
            "status": "failed",
            "error": "boom",
            "output_summary": None,
            "input_token_count": None,
            "output_token_count": None,
            "trigger_target_type": "thesis",
            "trigger_target_id": 7,
        },
    ]
    rules = [
        ("FROM agent_runs r", rows),
        ("GROUP BY status ORDER BY n DESC", [{"status": "completed", "n": 3}]),
        ("SELECT DISTINCT invocation_type", [{"invocation_type": "verify"}, {"invocation_type": "plan"}]),
    ]
    body = _debug_client(rules).get("/debug/agent-runs?status=completed&invocation_type=verify&limit=10").json()
    assert body["runs"][0]["latency_ms"] == 2000
    assert body["runs"][0]["task_id"] == 42  # target_type == task
    assert body["runs"][1]["latency_ms"] is None
    assert body["runs"][1]["task_id"] is None  # thesis, not task
    assert body["facets"]["statuses"] == {"completed": 3}
    assert body["facets"]["invocation_types"] == ["verify", "plan"]


def test_agent_runs_no_filters():
    rules = [
        ("FROM agent_runs r", []),
        ("GROUP BY status ORDER BY n DESC", []),
        ("SELECT DISTINCT invocation_type", []),
    ]
    body = _debug_client(rules).get("/debug/agent-runs").json()
    assert body["runs"] == []
    assert body["facets"]["statuses"] == {}


def test_research_tree(monkeypatch):
    from state import client as state_client

    async def _tree(self, task_id):
        return {"task": {"id": task_id}, "inquiries": []}

    monkeypatch.setattr(state_client.PostgresClient, "get_research_tree", _tree)
    body = _debug_client([]).get("/debug/research-tree/77").json()
    assert body["task"]["id"] == 77


def test_latency_ms_helper():
    assert debug._latency_ms(None, _NOW) is None
    assert debug._latency_ms(_NOW, None) is None
    assert debug._latency_ms(_NOW, _NOW + timedelta(seconds=1)) == 1000


def test_costs_full(monkeypatch):
    rows = [
        {"day": _NOW.date(), "in_tok": 1_000_000, "out_tok": 500_000, "n": 4},
        {"day": (_NOW - timedelta(days=1)).date(), "in_tok": 0, "out_tok": 0, "n": 1},
    ]
    first = {"recorded_at": _NOW - timedelta(days=2), "total_balance": 10.0}
    rules = [
        ("FROM agent_runs", rows),
        ("ORDER BY recorded_at DESC LIMIT 1", {"recorded_at": _NOW}),  # last → recent, no insert
        # first_today (date_trunc filter) is more specific → before the generic ASC rule.
        ("recorded_at >= date_trunc('day', NOW())", {"total_balance": 9.0}),
        ("ORDER BY recorded_at ASC LIMIT 1", first),
    ]

    async def _power():
        return [{"index": 0, "name": "RTX", "watts": 200.0, "util": 50.0}]

    async def _balance():
        return {"total": 8.0, "topped_up": 0, "granted": 0, "currency": "USD", "available": True}

    monkeypatch.setattr(debug, "_gpu_power", _power)
    monkeypatch.setattr(debug, "_deepseek_balance", _balance)
    body = _debug_client(rules).get("/debug/costs").json()
    expected_today = round(1_000_000 * debug.DS_INPUT_PER_TOK + 500_000 * debug.DS_OUTPUT_PER_TOK, 4)
    assert body["deepseek"]["today_cost_usd"] == expected_today
    assert body["deepseek"]["balance"]["total"] == 8.0
    assert body["deepseek"]["spent"]["spent_tracked_usd"] == 2.0  # 10 - 8
    assert body["deepseek"]["spent"]["spent_today_usd"] == 1.0  # 9 - 8
    assert body["power"]["total_watts"] == 200.0
    assert body["power"]["gpus"][0]["name"] == "RTX"


def test_costs_no_balance_no_gpu(monkeypatch):
    rules = [("FROM agent_runs", [])]

    async def _power():
        return []

    async def _balance():
        return None

    monkeypatch.setattr(debug, "_gpu_power", _power)
    monkeypatch.setattr(debug, "_deepseek_balance", _balance)
    body = _debug_client(rules).get("/debug/costs").json()
    assert body["deepseek"]["today_cost_usd"] == 0.0
    assert body["deepseek"]["days"] == []
    assert body["deepseek"]["spent"]["spent_tracked_usd"] is None
    assert body["power"]["total_watts"] == 0


def test_costs_balance_inserts_when_stale(monkeypatch):
    # No prior log row → insert path runs; first/first_today None → spend stays None.
    rules = [
        ("FROM agent_runs", []),
        ("ORDER BY recorded_at DESC LIMIT 1", None),  # last is None → insert
        ("INSERT INTO deepseek_balance_log", "INSERT 0 1"),
        ("recorded_at >= date_trunc('day', NOW())", None),
        ("ORDER BY recorded_at ASC LIMIT 1", None),
    ]
    client = _debug_client(rules)

    async def _power():
        return []

    async def _balance():
        return {"total": 5.0, "topped_up": 0, "granted": 0, "currency": "USD", "available": True}

    monkeypatch.setattr(debug, "_gpu_power", _power)
    monkeypatch.setattr(debug, "_deepseek_balance", _balance)
    body = client.get("/debug/costs").json()
    assert body["deepseek"]["spent"]["tracked_since"] is None
    assert any("INSERT INTO deepseek_balance_log" in c[1] for c in client.app.state.pool.calls)


def test_gpu_power_subprocess(monkeypatch):
    class _Proc:
        async def communicate(self):
            return (b"0, RTX 4090, 210.5, 77\n1, RTX 4090, 0.0, 0\n", b"")

    async def _exec(*_a, **_k):
        return _Proc()

    async def _wait_for(coro, timeout):
        return await coro

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(asyncio, "wait_for", _wait_for)
    gpus = asyncio.run(debug._gpu_power())
    assert gpus[0] == {"index": 0, "name": "RTX 4090", "watts": 210.5, "util": 77.0}
    assert len(gpus) == 2


def test_gpu_power_failure(monkeypatch):
    async def _exec(*_a, **_k):
        raise FileNotFoundError("no nvidia-smi")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    assert asyncio.run(debug._gpu_power()) == []


def test_deepseek_balance_cached():
    debug._balance_cache.update(ts=debug.time.time(), data={"total": 1.0})
    out = asyncio.run(debug._deepseek_balance())
    assert out == {"total": 1.0}
    debug._balance_cache.update(ts=0.0, data=None)  # reset


def test_deepseek_balance_no_key(monkeypatch):
    debug._balance_cache.update(ts=0.0, data=None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert asyncio.run(debug._deepseek_balance()) is None


def test_deepseek_balance_http(monkeypatch):
    debug._balance_cache.update(ts=0.0, data=None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    class _Resp:
        def json(self):
            return {
                "is_available": True,
                "balance_infos": [
                    {"total_balance": "12.34", "topped_up_balance": "10", "granted_balance": "2.34", "currency": "USD"}
                ],
            }

    class _Client:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, url, headers):
            return _Resp()

    monkeypatch.setattr(debug.httpx, "AsyncClient", _Client)
    out = asyncio.run(debug._deepseek_balance())
    assert out["total"] == 12.34
    assert out["available"] is True
    debug._balance_cache.update(ts=0.0, data=None)  # reset


def test_deepseek_balance_http_error(monkeypatch):
    debug._balance_cache.update(ts=0.0, data="cached-fallback")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    class _Client:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, url, headers):
            raise RuntimeError("network down")

    monkeypatch.setattr(debug.httpx, "AsyncClient", _Client)
    out = asyncio.run(debug._deepseek_balance())
    assert out == "cached-fallback"  # falls back to cached data
    debug._balance_cache.update(ts=0.0, data=None)  # reset


# ════════════════════════════════════════════════════════════════════════════
# api.ops
# ════════════════════════════════════════════════════════════════════════════
def test_ops_host_ok(monkeypatch):
    class _VM:
        percent = 42.0
        used = 8e9
        total = 16e9

    class _DU:
        percent = 55.5
        used = 100e9
        total = 500e9

    monkeypatch.setattr(ops.psutil, "cpu_percent", lambda interval=None: 12.345)
    monkeypatch.setattr(ops.psutil, "virtual_memory", lambda: _VM())
    monkeypatch.setattr(ops.psutil, "disk_usage", lambda p: _DU())
    monkeypatch.setattr(ops.psutil, "cpu_count", lambda logical=True: 8)
    monkeypatch.setattr(ops.psutil, "getloadavg", lambda: (1.0, 2.0, 3.0))
    app = FastAPI()
    app.include_router(ops.router)
    body = TestClient(app).get("/ops/host").json()
    assert body["status"] == "ok"
    assert body["cpu_percent"] == 12.3
    assert body["cpu_count"] == 8
    assert body["load_avg"] == [1.0, 2.0, 3.0]
    assert body["memory_percent"] == 42.0
    assert body["memory_total_gb"] == 16.0
    assert body["disk_percent"] == 55.5


def test_ops_host_no_loadavg(monkeypatch):
    class _VM:
        percent = 10.0
        used = 1e9
        total = 2e9

    class _DU:
        percent = 20.0
        used = 1e9
        total = 2e9

    monkeypatch.setattr(ops.psutil, "cpu_percent", lambda interval=None: 1.0)
    monkeypatch.setattr(ops.psutil, "virtual_memory", lambda: _VM())
    monkeypatch.setattr(ops.psutil, "disk_usage", lambda p: _DU())
    monkeypatch.setattr(ops.psutil, "cpu_count", lambda logical=True: 4)
    monkeypatch.delattr(ops.psutil, "getloadavg", raising=False)
    app = FastAPI()
    app.include_router(ops.router)
    body = TestClient(app).get("/ops/host").json()
    assert body["load_avg"] == [0.0, 0.0, 0.0]  # fallback when getloadavg missing


def test_ops_host_unavailable(monkeypatch):
    def _boom(interval=None):
        raise RuntimeError("psutil broken")

    monkeypatch.setattr(ops.psutil, "cpu_percent", _boom)
    app = FastAPI()
    app.include_router(ops.router)
    body = TestClient(app).get("/ops/host").json()
    assert body["status"] == "unavailable"
    assert "psutil broken" in body["error"]


# ════════════════════════════════════════════════════════════════════════════
# api.snapshot
# ════════════════════════════════════════════════════════════════════════════
def _company_row(**over):
    base = {
        "current_phase": "discovery",
        "phase_started_at": _NOW - timedelta(days=3),
        "bootstrap_at": _NOW - timedelta(days=10),
        "problem_statement": "p",
        "stance": "s",
        "success_criterion": "sc",
        "thesis": "t",
        "niche": "n",
        "audience": "a",
        "charter": "c",
        "paused": False,
        "paused_reason": None,
    }
    base.update(over)
    return base


def _thesis_row(**over):
    base = {
        "id": 1,
        "statement": "claim text",
        "status": "proposed",
        "confidence": 0.7,
        "confidence_prev": 0.5,
        "parent_id": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "invalidated_at": None,
        "invalidation_reason": None,
        "finding_count": 2,
        "supporting_count": 1,
        "contradicting_count": 1,
    }
    base.update(over)
    return base


def _finding_row(**over):
    base = {
        "id": 1,
        "task_id": 3,
        "claim_id": 1,
        "source": "web",
        "url": "http://u",
        "title": "T",
        "summary": "sum",
        "relevance_score": 8.0,
        "why_it_matters": "matters",
        "audit_score": 9.0,
        "audit_verdict": "pass",
        "supports_thesis": True,
        "created_at": _NOW,
    }
    base.update(over)
    return base


def _run_row(**over):
    base = {
        "id": 1,
        "department": "research",
        "invocation_type": "verify",
        "model_tier": "workhorse",
        "model_name": "deepseek-v4",
        "started_at": _NOW,
        "completed_at": _NOW,
        "status": "completed",
        "input_token_count": 10,
        "output_token_count": 5,
        "output_summary": "ok",
        "error": None,
        "langfuse_trace_id": "tr-1",
    }
    base.update(over)
    return base


def _stats_row():
    return {
        "pending_tasks": 1,
        "running_tasks": 2,
        "findings_today": 3,
        "high_signal_today": 1,
        "slop_today": 0,
        "failed_runs_today": 0,
        "schema_failures_today": 0,
        "source_hn_in_flight": 0,
        "source_reddit_in_flight": 0,
        "source_web_in_flight": 1,
        "last_activity_at": _NOW,
    }


def _critic_row():
    return {
        "kind": "critic",
        "id": 1,
        "claim_id": 1,
        "detail": "reject",
        "confidence": 0.8,
        "reasoning": "bad",
        "created_at": _NOW,
    }


def _phase_row():
    return {"id": 1, "from_phase": "a", "to_phase": "b", "reason": "r", "forced": False, "decided_at": _NOW}


def _org_row(**over):
    base = {"role": "pi", "running_count": 1, "last_run_at": _NOW, "runs_today": 2, "avg_duration_s": 3.0}
    base.update(over)
    return base


def _cost_row():
    return {
        "day": _NOW.date(),
        "reasoning_calls": 1,
        "workhorse_calls": 2,
        "fast_calls": 3,
        "code_calls": 0,
        "total_cost_usd": 1.5,
        "cap_reached": False,
    }


def _edge_row():
    return {"event_type": "task.created", "count_last_minute": 1, "count_today": 5, "last_fired_at": _NOW}


def _snapshot_rules():
    # `FROM claims t\n` must precede the COUNT(*) status rules: the theses SQL
    # contains the active status clause too, and first-match wins.
    return [
        ("SELECT * FROM company_state WHERE id = 1", _company_row()),
        ("FROM claims t\n", [_thesis_row()]),
        ("SELECT COUNT(*) FROM claims WHERE status IN ('proposed'", 4),
        ("SELECT COUNT(*) FROM claims WHERE status IN ('invalidated'", 1),
        ("FROM findings WHERE COALESCE(audit_verdict, '') != 'stale' ORDER BY", [_finding_row()]),
        ("FROM agent_runs ORDER BY started_at DESC", [_run_row()]),
        ("FROM critic_verdicts av", [_critic_row()]),
        ("FROM phase_transitions ORDER BY", [_phase_row()]),
        ("GROUP BY department\n", [_org_row()]),
        ("FROM cost_tracking WHERE day = CURRENT_DATE", _cost_row()),
        ("FROM lessons GROUP BY status", [{"status": "active", "n": 5}]),
        ("date_trunc('day', started_at)::date AS day", [{"day": _NOW.date(), "runs": 4, "tokens": 12000}]),
        ("date_trunc('day', created_at)::date AS day", [{"day": _NOW.date(), "findings": 2}]),
        ("FROM tasks GROUP BY status", [{"status": "pending", "n": 3}, {"status": "running", "n": 1}]),
        ("AS pending_tasks", _stats_row()),
        ("FROM events\n", [_edge_row()]),
    ]


def test_snapshot_full():
    body = make_client(_snapshot_rules(), router=snapshot.router).get("/snapshot").json()
    assert body["state"]["current_phase"] == "discovery"
    assert body["state"]["active_claims_count"] == 4
    assert body["state"]["invalidated_claims_count"] == 1
    assert body["active_claims"][0]["claim"] == "claim text"
    assert body["recent_findings"][0]["title"] == "T"
    assert body["recent_runs"][0]["langfuse_trace_id"] == "tr-1"
    assert body["dissent"][0]["kind"] == "critic"
    assert body["phase_transitions"][0]["to_phase"] == "b"
    pi = next(r for r in body["org_roles"] if r["role"] == "pi")
    assert pi["running_count"] == 1
    planner = next(r for r in body["org_roles"] if r["role"] == "planner")
    assert planner["running_count"] == 0  # absent role → zeroed
    assert body["cost"]["total_cost_usd"] == 1.5
    assert body["lesson_counts"] == {"active": 5}
    assert len(body["telemetry"]) == 7  # always 7 days
    tc = {t["label"]: t["value"] for t in body["task_counts"]}
    assert tc["pending"] == 3
    assert body["stats"]["pending_tasks"] == 1
    assert body["edge_activity"][0]["event_type"] == "task.created"


def test_snapshot_state_not_seeded():
    rules = [("SELECT * FROM company_state WHERE id = 1", None)]
    app = FastAPI()
    app.include_router(snapshot.router)
    app.state.pool = ScriptedPool(rules=rules)
    resp = TestClient(app, raise_server_exceptions=False).get("/snapshot")
    assert resp.status_code == 500


def test_snapshot_cost_none_and_empty_aggregates():
    rules = [
        ("SELECT * FROM company_state WHERE id = 1", _company_row(paused=True, paused_reason="manual", stance=None)),
        ("FROM claims t\n", []),
        ("SELECT COUNT(*) FROM claims WHERE status IN ('proposed'", None),
        ("SELECT COUNT(*) FROM claims WHERE status IN ('invalidated'", None),
        ("FROM findings WHERE COALESCE(audit_verdict, '') != 'stale' ORDER BY", []),
        ("FROM agent_runs ORDER BY started_at DESC", []),
        ("FROM critic_verdicts av", []),
        ("FROM phase_transitions ORDER BY", []),
        ("GROUP BY department\n", []),
        ("FROM cost_tracking WHERE day = CURRENT_DATE", None),  # → defaults
        ("FROM lessons GROUP BY status", []),
        ("date_trunc('day', started_at)::date AS day", []),
        ("date_trunc('day', created_at)::date AS day", []),
        ("FROM tasks GROUP BY status", []),
        ("AS pending_tasks", _stats_row()),
        ("FROM events\n", []),
    ]
    body = make_client(rules, router=snapshot.router).get("/snapshot").json()
    assert body["state"]["paused"] is True
    assert body["state"]["stance"] is None
    assert body["state"]["active_claims_count"] == 0  # None → 0
    assert body["cost"]["day"] is None
    assert body["cost"]["total_cost_usd"] == 0.0
    assert body["active_claims"] == []
    assert all(r["running_count"] == 0 for r in body["org_roles"])
    assert all(t["value"] == 0 for t in body["task_counts"])


def test_events_endpoint():
    rows = [
        {
            "id": 1,
            "event_type": "task.created",
            "target_type": "task",
            "target_id": 5,
            "payload": {"k": "v"},
            "status": "ok",
            "suppression_reason": None,
            "emitted_at": _NOW,
            "consumed_at": None,
            "consumed_by_handler": None,
        },
        {
            "id": 2,
            "event_type": "x",
            "target_type": None,
            "target_id": None,
            "payload": "not-a-dict",  # non-dict → coerced to {}
            "status": "ok",
            "suppression_reason": "dup",
            "emitted_at": _NOW,
            "consumed_at": _NOW,
            "consumed_by_handler": "h",
        },
    ]
    body = make_client([("FROM events ORDER BY emitted_at DESC", rows)], router=snapshot.router).get("/events").json()
    assert body[0]["payload"] == {"k": "v"}
    assert body[1]["payload"] == {}  # non-dict payload coerced


def test_thesis_findings_endpoint():
    rows = [_finding_row(audit_score=None, supports_thesis=None)]
    body = (
        make_client([("FROM findings WHERE claim_id = $1", rows)], router=snapshot.router)
        .get("/theses/1/findings")
        .json()
    )
    assert body[0]["audit_score"] is None
    assert body[0]["supports_thesis"] is None


def test_snapshot_org_avg_duration_none():
    # _org with avg_duration_s None → stays None on the populated role.
    org = _org_row(running_count=0, last_run_at=None, avg_duration_s=None)
    pool = ScriptedPool(rules=[("GROUP BY department\n", [org])])
    roles = asyncio.run(snapshot._org(pool))
    pi = next(r for r in roles if r.role == "pi")
    assert pi.avg_duration_s is None


def test_snapshot_langfuse_host(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://lf")
    body = make_client(_snapshot_rules(), router=snapshot.router).get("/snapshot").json()
    assert body["langfuse_host"] == "http://lf"


# ════════════════════════════════════════════════════════════════════════════
# api.stream — StreamHub unit tests
# ════════════════════════════════════════════════════════════════════════════
class FakeWS:
    def __init__(self, fail=False):
        self.sent: list[dict] = []
        self.fail = fail

    async def send_json(self, msg):
        if self.fail:
            raise RuntimeError("ws closed")
        self.sent.append(msg)


def test_serialize_handles_types():
    row = {"a": 1, "dt": _NOW, "obj": object(), "nested": {"x": 1}}
    out = stream._serialize(row)
    assert out["a"] == 1
    assert out["dt"] == _NOW.isoformat()
    assert isinstance(out["obj"], str)  # non-JSON → str()
    assert out["nested"] == {"x": 1}


def test_enrich_step_session_skips():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool()
    row = {"target_type": "task", "target_id": 1, "event_type": "step.start", "session_id": 9}
    out = asyncio.run(hub._enrich(row))
    assert out == {"session_id": 9}


def test_enrich_no_pool_returns_empty():
    hub = stream.StreamHub()
    hub._pool = None
    row = {"target_type": "thesis", "target_id": 1, "event_type": "thesis.created", "session_id": None}
    assert asyncio.run(hub._enrich(row)) == {}


def test_enrich_thesis():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool(rules=[("FROM claims WHERE id = $1", {"id": 1, "statement": "c", "created_at": _NOW})])
    row = {"target_type": "thesis", "target_id": 1, "event_type": "thesis.created"}
    out = asyncio.run(hub._enrich(row))
    assert out["thesis"]["statement"] == "c"
    assert out["thesis"]["created_at"] == _NOW.isoformat()


def test_enrich_task_completed():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool(rules=[("FROM tasks WHERE id = $1", {"id": 2, "status": "completed"})])
    row = {"target_type": "task", "target_id": 2, "event_type": "task.completed"}
    out = asyncio.run(hub._enrich(row))
    assert out["task"]["status"] == "completed"


def test_enrich_task_completed_missing_row():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool(rules=[("FROM tasks WHERE id = $1", [])])  # no row
    row = {"target_type": "task", "target_id": 2, "event_type": "task.completed"}
    assert asyncio.run(hub._enrich(row)) == {}


def test_enrich_finding_high_signal():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool(rules=[("FROM findings WHERE id = $1", {"id": 5, "summary": "s"})])
    row = {
        "target_type": "finding",
        "target_id": 1,
        "event_type": "finding.high_signal",
        "payload": {"finding_id": 5},
    }
    out = asyncio.run(hub._enrich(row))
    assert out["finding"]["summary"] == "s"


def test_enrich_finding_high_signal_no_fid():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool()
    row = {"target_type": "finding", "target_id": 1, "event_type": "finding.high_signal", "payload": {}}
    assert asyncio.run(hub._enrich(row)) == {}


def test_enrich_finding_high_signal_missing_row():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool(rules=[("FROM findings WHERE id = $1", [])])  # fid present, no row
    row = {
        "target_type": "finding",
        "target_id": 1,
        "event_type": "finding.high_signal",
        "payload": {"finding_id": 5},
    }
    assert asyncio.run(hub._enrich(row)) == {}


def test_enrich_phase():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool(rules=[("FROM company_state WHERE id = 1", {"id": 1, "current_phase": "build"})])
    row = {"target_type": "phase", "target_id": None, "event_type": "phase.transition_proposed"}
    out = asyncio.run(hub._enrich(row))
    assert out["company_state"]["current_phase"] == "build"


def test_enrich_thesis_missing_row():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool(rules=[("FROM claims WHERE id = $1", [])])  # no row
    row = {"target_type": "thesis", "target_id": 1, "event_type": "thesis.created"}
    assert asyncio.run(hub._enrich(row)) == {}


def test_enrich_phase_missing_row():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool(rules=[("FROM company_state WHERE id = 1", [])])
    row = {"target_type": "phase", "target_id": None, "event_type": "phase.transition_proposed"}
    assert asyncio.run(hub._enrich(row)) == {}


def test_enrich_unhandled_target():
    # A target_type that matches none of thesis/task/finding/phase → falls through
    # past the phase elif (the 132->140 branch) to an empty enrichment.
    hub = stream.StreamHub()
    hub._pool = ScriptedPool()
    row = {"target_type": "company", "target_id": 1, "event_type": "company.updated"}
    assert asyncio.run(hub._enrich(row)) == {}
    # A task event that is NOT task.completed → inner if False, falls through (117->140).
    task_row = {"target_type": "task", "target_id": 1, "event_type": "task.created"}
    assert asyncio.run(hub._enrich(task_row)) == {}


def test_enrich_exception_swallowed():
    class _BadPool:
        def acquire(self):
            raise RuntimeError("acquire failed")

    hub = stream.StreamHub()
    hub._pool = _BadPool()
    row = {"id": 1, "target_type": "thesis", "target_id": 1, "event_type": "thesis.created"}
    assert asyncio.run(hub._enrich(row)) == {}


def test_fanout_drops_dead_clients():
    hub = stream.StreamHub()
    good, bad = FakeWS(), FakeWS(fail=True)
    hub.clients = {good, bad}
    asyncio.run(hub._fanout({"type": "event"}))
    assert good.sent == [{"type": "event"}]
    assert bad not in hub.clients  # dead client discarded
    assert good in hub.clients


def test_broadcast_event_full():
    hub = stream.StreamHub()
    ev_row = {
        "id": 7,
        "event_type": "thesis.created",
        "target_type": "thesis",
        "target_id": 3,
        "session_id": None,
        "payload": {"a": 1},
        "status": "ok",
        "emitted_at": _NOW,
    }
    hub._pool = ScriptedPool(
        rules=[
            ("FROM events WHERE id = $1", ev_row),
            ("FROM claims WHERE id = $1", {"id": 3, "statement": "c"}),
        ]
    )
    ws = FakeWS()
    hub.clients = {ws}
    asyncio.run(hub._broadcast_event(7))
    msg = ws.sent[0]
    assert msg["type"] == "event"
    assert msg["event"]["id"] == 7
    assert msg["event"]["emitted_at"] == _NOW.isoformat()
    assert msg["thesis"]["statement"] == "c"


def test_broadcast_event_string_payload():
    hub = stream.StreamHub()
    ev_row = {
        "id": 8,
        "event_type": "step.start",
        "target_type": "task",
        "target_id": 1,
        "session_id": 4,
        "payload": '{"k": "v"}',  # JSON string payload
        "status": "ok",
        "emitted_at": _NOW,
    }
    hub._pool = ScriptedPool(rules=[("FROM events WHERE id = $1", ev_row)])
    ws = FakeWS()
    hub.clients = {ws}
    asyncio.run(hub._broadcast_event(8))
    assert ws.sent[0]["event"]["payload"] == {"k": "v"}
    assert ws.sent[0]["session_id"] == 4  # step.* enrichment


def test_broadcast_event_bad_string_payload():
    hub = stream.StreamHub()
    ev_row = {
        "id": 9,
        "event_type": "session.start",
        "target_type": None,
        "target_id": None,
        "session_id": 1,
        "payload": "not json{",
        "status": "ok",
        "emitted_at": _NOW,
    }
    hub._pool = ScriptedPool(rules=[("FROM events WHERE id = $1", ev_row)])
    ws = FakeWS()
    hub.clients = {ws}
    asyncio.run(hub._broadcast_event(9))
    assert ws.sent[0]["event"]["payload"] == {}  # unparseable → {}


def test_broadcast_event_no_clients():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool()
    hub.clients = set()
    asyncio.run(hub._broadcast_event(1))  # returns early, no error


def test_broadcast_event_missing_row():
    hub = stream.StreamHub()
    hub._pool = ScriptedPool(rules=[("FROM events WHERE id = $1", [])])
    hub.clients = {FakeWS()}
    asyncio.run(hub._broadcast_event(999))  # row None → returns


def test_on_notify_schedules_broadcast():
    hub = stream.StreamHub()
    scheduled = []

    class _Loop:
        def create_task(self, coro):
            scheduled.append(coro)
            coro.close()  # avoid 'never awaited' warning

    hub._loop = _Loop()
    hub._on_notify(None, 1, "events", json.dumps({"id": 12}))
    assert len(scheduled) == 1
    hub._on_notify(None, 1, "events", "not-json")  # bad payload → ignored
    assert len(scheduled) == 1


def test_start_and_stop():
    class _Conn:
        def __init__(self):
            self.listeners = []

        async def add_listener(self, ch, cb):
            self.listeners.append((ch, cb))

        async def remove_listener(self, ch, cb):
            self.listeners.remove((ch, cb))

    class _Pool:
        def __init__(self):
            self.conn = _Conn()
            self.released = []

        async def acquire(self):
            return self.conn

        async def release(self, c):
            self.released.append(c)

    async def _run():
        hub = stream.StreamHub()
        pool = _Pool()
        await hub.start(pool)
        assert hub.listener_conn is pool.conn
        assert pool.conn.listeners  # listener registered
        await hub.stop()
        assert pool.conn.listeners == []  # listener removed
        assert pool.released == [pool.conn]
        assert hub.listener_conn is None

    asyncio.run(_run())


def test_stop_noop_without_conn():
    async def _run():
        hub = stream.StreamHub()
        await hub.stop()  # no listener_conn → no-op

    asyncio.run(_run())


def test_on_notify_no_loop():
    # _loop is None → branch falls through without scheduling.
    hub = stream.StreamHub()
    hub._loop = None
    hub._on_notify(None, 1, "events", json.dumps({"id": 1}))  # no error, nothing scheduled


def test_ws_events_endpoint():
    app = FastAPI()
    app.include_router(stream.router)
    hub = stream.StreamHub()
    app.state.stream_hub = hub
    with TestClient(app).websocket_connect("/ws/events") as ws:
        hello = ws.receive_json()
        assert hello == {"type": "hello"}
        assert len(hub.clients) == 1
    assert len(hub.clients) == 0  # discarded after disconnect


def test_ws_events_generic_error():
    from types import SimpleNamespace

    hub = stream.StreamHub()
    ws_app = SimpleNamespace(state=SimpleNamespace(stream_hub=hub))
    sent = []

    class _WS:
        app = ws_app

        async def accept(self):
            pass

        async def send_json(self, msg):
            sent.append(msg)

        async def receive_text(self):
            raise RuntimeError("boom")  # non-disconnect → generic except branch

    fake = _WS()
    asyncio.run(stream.ws_events(fake))
    assert sent == [{"type": "hello"}]
    assert fake not in hub.clients  # cleaned up in finally


# ════════════════════════════════════════════════════════════════════════════
# api.main — lifespan + app factory
# ════════════════════════════════════════════════════════════════════════════
def test_init_conn_registers_codec():
    calls = []

    class _Conn:
        async def set_type_codec(self, name, **kw):
            calls.append((name, kw))

    asyncio.run(main._init_conn(_Conn()))
    assert calls[0][0] == "jsonb"
    assert calls[0][1]["schema"] == "pg_catalog"


def test_app_lifespan(monkeypatch):
    created = {}

    class _Pool:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    fake_pool = _Pool()

    async def _create_pool(db_url, **kw):
        created["url"] = db_url
        created["init"] = kw.get("init")
        return fake_pool

    started, stopped = [], []

    async def _start(self, pool):
        started.append(pool)

    async def _stop(self):
        stopped.append(True)

    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    monkeypatch.setattr(main.asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(main.stream.StreamHub, "start", _start)
    monkeypatch.setattr(main.stream.StreamHub, "stop", _stop)

    with TestClient(main.app) as client:
        assert main.app.state.pool is fake_pool
        assert started == [fake_pool]
        assert client.get("/health").json() == {"ok": True}
    assert stopped == [True]
    assert fake_pool.closed is True
    assert created["url"] == "postgres://fake/db"
    assert created["init"] is main._init_conn


def test_app_routes_registered():
    paths = {r.path for r in main.app.routes}
    assert "/snapshot" in paths
    assert "/knowledge/stats" in paths
    assert "/ops/host" in paths
    assert "/debug/agent-runs" in paths
    assert "/health" in paths


# ════════════════════════════════════════════════════════════════════════════
# api.models — instantiate every model with valid + edge data
# ════════════════════════════════════════════════════════════════════════════
def test_models_company_state():
    m = CompanyStateOut(
        current_phase="x",
        phase_started_at=_NOW,
        bootstrap_at=_NOW,
        days_in_phase=1,
        days_since_start=2,
        problem_statement="p",
        stance=None,
        success_criterion=None,
        thesis=None,
        niche=None,
        audience=None,
        charter=None,
        paused=False,
        paused_reason=None,
        active_claims_count=0,
        invalidated_claims_count=0,
    )
    assert m.current_phase == "x"


def test_models_thesis_finding_run():
    t = ThesisOut(
        id=1,
        claim="c",
        status="proposed",
        confidence=0.5,
        confidence_prev=None,
        parent_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        invalidated_at=None,
        kill_reason=None,
        finding_count=0,
        supporting_count=0,
        contradicting_count=0,
    )
    assert t.confidence == 0.5
    f = FindingOut(
        id=1,
        task_id=2,
        claim_id=None,
        source=None,
        url=None,
        title=None,
        summary="s",
        relevance_score=1.0,
        why_it_matters=None,
        audit_score=None,
        audit_verdict=None,
        supports_thesis=None,
        created_at=_NOW,
    )
    assert f.claim_id is None
    r = AgentRunOut(
        id=1,
        department="d",
        invocation_type="i",
        model_tier="t",
        model_name="m",
        started_at=_NOW,
        completed_at=None,
        status="ok",
        input_token_count=None,
        output_token_count=None,
        output_summary=None,
        error=None,
    )
    assert r.langfuse_trace_id is None  # default


def test_models_dissent_phase_event_lesson():
    d = DissentItem(kind="critic", id=1, claim_id=2, detail="d", confidence=None, reasoning=None, created_at=_NOW)
    assert d.confidence is None
    p = PhaseTransitionOut(id=1, from_phase="a", to_phase="b", reason="r", forced=True, decided_at=_NOW)
    assert p.forced is True
    e = EventOut(
        id=1,
        event_type="t",
        target_type=None,
        target_id=None,
        payload={"a": 1},
        status="ok",
        suppression_reason=None,
        emitted_at=_NOW,
        consumed_at=None,
        consumed_by_handler=None,
    )
    assert e.payload == {"a": 1}
    le = LessonOut(
        id=1,
        applies_to_invocation="x",
        lesson_text="lt",
        confidence=0.9,
        status="active",
        promotion_run_count=1,
        contradiction_run_count=0,
        created_at=_NOW,
    )
    assert le.lesson_text == "lt"


def test_models_cost_org_telemetry_taskcount_stats_edge():
    c = CostTrackingOut(
        day=None,
        reasoning_calls=0,
        workhorse_calls=0,
        fast_calls=0,
        code_calls=0,
        total_cost_usd=0.0,
        cap_reached=False,
    )
    assert c.day is None
    o = OrgRoleOut(role="pi", running_count=0, last_run_at=None, runs_today=0, avg_duration_s=None)
    assert o.role == "pi"
    td = TelemetryDay(day="2026-06-09", label="Tue", runs=1, findings=2, tokens=3)
    assert td.label == "Tue"
    tc = TaskCount(label="pending", value=4)
    assert tc.value == 4
    s = StatsOut(
        pending_tasks=1,
        running_tasks=0,
        findings_today=0,
        high_signal_today=0,
        slop_today=0,
        failed_runs_today=0,
        schema_failures_today=0,
        source_hn_in_flight=0,
        source_reddit_in_flight=0,
        source_web_in_flight=0,
    )
    assert s.last_activity_at is None  # default
    ea = EdgeActivity(event_type="t", count_last_minute=0, count_today=0, last_fired_at=None)
    assert ea.event_type == "t"


def test_models_snapshot_out():
    state = CompanyStateOut(
        current_phase="x",
        phase_started_at=_NOW,
        bootstrap_at=_NOW,
        days_in_phase=0,
        days_since_start=0,
        problem_statement="p",
        stance=None,
        success_criterion=None,
        thesis=None,
        niche=None,
        audience=None,
        charter=None,
        paused=False,
        paused_reason=None,
        active_claims_count=0,
        invalidated_claims_count=0,
    )
    cost = CostTrackingOut(
        day=None,
        reasoning_calls=0,
        workhorse_calls=0,
        fast_calls=0,
        code_calls=0,
        total_cost_usd=0.0,
        cap_reached=False,
    )
    stats = StatsOut(
        pending_tasks=0,
        running_tasks=0,
        findings_today=0,
        high_signal_today=0,
        slop_today=0,
        failed_runs_today=0,
        schema_failures_today=0,
        source_hn_in_flight=0,
        source_reddit_in_flight=0,
        source_web_in_flight=0,
    )
    snap = SnapshotOut(
        state=state,
        active_claims=[],
        invalidated_claims=[],
        recent_findings=[],
        recent_runs=[],
        dissent=[],
        phase_transitions=[],
        org_roles=[],
        cost=cost,
        lesson_counts={"active": 1},
        telemetry=[],
        task_counts=[],
        stats=stats,
    )
    assert snap.edge_activity == []  # default
    assert snap.langfuse_host is None  # default


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
