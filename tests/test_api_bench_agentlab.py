"""Tests for api/bench.py + api/agentlab.py — FastAPI routers driven by a starlette
TestClient over a minimal app with a mocked DB pool (ScriptedPool) and a fake
BenchEngine injected onto app.state. No real Postgres / DATABASE_URL, no Zep, no
Ollama, no live models: router.run_single and the agentlab runner functions
(ingest_source, run_discovery_sweep, scouts, handle_acquire_requested, _classify,
_mimir_state) are all monkeypatched to canned results.

Covers every GET + POST endpoint and its branches:
  bench    — /options, /run (+worker persist + error branches), /jobs/{id},
             /runs, /runs/{id}, /replay-step (frozen + override + error), helpers.
  agentlab — /agents, /run (llm/mimir/collectors dispatch + every sub-branch),
             /suite, /suite/run.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.agentlab as agentlab
import api.bench as bench
from harness.curator import BuiltPrompt, PromptLayer
from harness.router import Provider
from library.ingest.scouts import SourceDescriptor
from tests._helpers import ScriptedPool

_NOW = datetime(2026, 6, 9, 12, 0, 0)
_RUNNABLE = "researcher.parse_pricing"  # recipe whose schema validates against "{}"
_RUNNABLE_SCHEMA = "ParsedPricing"


# ── fakes ─────────────────────────────────────────────────────────────────────
@dataclass
class _ChainProvider:
    provider: Provider
    model_name: str


class FakeCurator:
    """Stand-in for harness.curator.Curator: build() returns a canned BuiltPrompt."""

    def __init__(self, *, total_tokens=42, content="SYSTEM PROMPT TEXT", raises=None):
        self.total_tokens = total_tokens
        self.content = content
        self.raises = raises
        self.calls: list[tuple] = []

    async def build(self, invocation_type, ctx):
        self.calls.append((invocation_type, ctx))
        if self.raises is not None:
            raise self.raises
        return BuiltPrompt(
            layers=[PromptLayer(name="frozen", content=self.content, priority=1)],
            tool_names=[],
            output_schema="X",
            lesson_ids=[],
            total_tokens=self.total_tokens,
            invocation_type=invocation_type,
        )


class FakeRouter:
    """Stand-in for harness.router.Router. run_single returns canned (text, tokens)
    or raises. premium_chain/cloud_chain feed /bench/options + _resolve_model."""

    def __init__(self, *, result=("{}", 11), raises=None, premium=None, cloud=None):
        self.result = result
        self.raises = raises
        self.premium_chain = premium if premium is not None else []
        self.cloud_chain = cloud if cloud is not None else []
        self.calls: list[tuple] = []

    async def run_single(self, prompt, schema_cls, provider, model_name, **kw):
        self.calls.append((provider, model_name, kw))
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeState:
    def __init__(self, claims=None):
        self._claims = claims or []

    async def get_active_claims(self, limit=25):
        return self._claims[:limit]


@dataclass
class _Claim:
    id: int
    statement: str


def make_engine(*, pool=None, curator=None, router=None, claims=None, schemas=None):
    eng = bench.BenchEngine.__new__(bench.BenchEngine)
    eng.pool = pool if pool is not None else ScriptedPool()
    eng.state = FakeState(claims=claims)
    eng.memory = bench._NullMemory()
    eng.lessons = None
    eng.curator = curator if curator is not None else FakeCurator()
    eng.router = router if router is not None else FakeRouter()
    eng.schemas = schemas if schemas is not None else bench._build_schema_registry()
    return eng


def make_app(*, pool=None, engine=None, **eng_kw):
    """Minimal app with both routers + a fake engine pre-installed on app.state so
    get_engine() never calls BenchEngine.create (no DB / Zep / Ollama)."""
    app = FastAPI()
    app.include_router(bench.router)
    app.include_router(agentlab.router)
    app.state.pool = pool if pool is not None else ScriptedPool()
    eng = engine if engine is not None else make_engine(pool=app.state.pool, **eng_kw)
    app.state.bench_engine = eng
    app.state._fake_engine = eng  # handle for tests
    return app


# ============================================================================
# BENCH
# ============================================================================
def test_bench_options_tasks_models_theses(monkeypatch):
    # Make Ollama listing fail fast (no network) → models come only from chains.
    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise RuntimeError("no ollama")

    monkeypatch.setattr(bench.httpx, "AsyncClient", _BoomClient)
    router = FakeRouter(
        premium=[_ChainProvider(Provider.DEEPSEEK, "deepseek-chat")],
        cloud=[
            _ChainProvider(Provider.DEEPSEEK, "deepseek-chat"),  # dup → deduped
            _ChainProvider(Provider.OLLAMA, "qwen3:14b"),
        ],
    )
    app = make_app(router=router, claims=[_Claim(1, "claim one")])
    body = TestClient(app).get("/bench/options").json()

    assert {t["invocation_type"] for t in body["tasks"]}  # non-empty task list
    kickoff = next(t for t in body["tasks"] if t["invocation_type"] == "pi.exploration_kickoff")
    assert kickoff["agent"] == "pi"
    assert kickoff["runnable"] is True  # schema resolves
    crit = next(t for t in body["tasks"] if t["invocation_type"] == "critic.kill_verdict")
    assert crit["accepts_thesis"] is True
    # models deduped: deepseek once, ollama once
    ids = [m["id"] for m in body["models"]]
    assert ids.count("deepseek:deepseek-chat") == 1
    assert "ollama:qwen3:14b" in ids
    assert body["theses"] == [{"id": 1, "claim": "claim one"}]


def test_bench_options_lists_ollama_models(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "llama3:8b"}, {}]}  # second has no name → skipped

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(bench.httpx, "AsyncClient", _Client)
    app = make_app(router=FakeRouter())
    body = TestClient(app).get("/bench/options").json()
    assert {"id": "ollama:llama3:8b", "provider": "ollama", "model_name": "llama3:8b", "location": "local"} in body[
        "models"
    ]


def _drain_jobs(app):
    """Run the event loop until every background bench job is done (or give up)."""
    for _ in range(200):
        jobs = getattr(app.state, "bench_jobs", {})
        if jobs and all(j["status"] == "done" for j in jobs.values()):
            return jobs
        time.sleep(0.01)
    return getattr(app.state, "bench_jobs", {})


def test_bench_run_unknown_task():
    app = make_app()
    body = TestClient(app).post("/bench/run", json={"invocation_type": "no.such_task", "models": []}).json()
    assert "Unknown task" in body["error"]


def test_bench_run_schema_not_resolvable():
    # A real recipe whose output_schema is NOT in our (deliberately tiny) registry.
    app = make_app(schemas={})
    body = (
        TestClient(app)
        .post(
            "/bench/run",
            json={
                "invocation_type": "pi.exploration_kickoff",
                "models": [{"provider": "deepseek", "model_name": "deepseek-chat"}],
            },
        )
        .json()
    )
    assert "no resolvable output schema" in body["error"]


def test_bench_run_context_error():
    # critic.kill_verdict with no active claim + no findings → BenchContextError.
    pool = ScriptedPool(rules=[("FROM critic_verdicts", [])])
    app = make_app(pool=pool, claims=[])
    body = (
        TestClient(app)
        .post(
            "/bench/run",
            json={
                "invocation_type": "critic.kill_verdict",
                "models": [{"provider": "deepseek", "model_name": "deepseek-chat"}],
            },
        )
        .json()
    )
    assert "Can't ground this task" in body["error"]


def test_bench_run_prompt_build_failure():
    app = make_app(curator=FakeCurator(raises=ValueError("boom")))
    body = (
        TestClient(app)
        .post(
            "/bench/run",
            json={
                "invocation_type": "pi.exploration_kickoff",
                "models": [{"provider": "deepseek", "model_name": "deepseek-chat"}],
            },
        )
        .json()
    )
    assert body["error"].startswith("Prompt build failed: ValueError")


def test_bench_run_ok_persists_and_validates(monkeypatch):
    # run_single returns valid JSON for ParsedPricing ("{}" validates) → worker
    # marks job done and INSERTs into bench_runs.
    pool = ScriptedPool(rules=[("INSERT INTO bench_runs", "INSERT 0 1")])
    router = FakeRouter(result=("{}", 17))
    app = make_app(pool=pool, router=router)
    resp = (
        TestClient(app)
        .post(
            "/bench/run",
            json={
                "invocation_type": _RUNNABLE,
                "models": [{"provider": "deepseek", "model_name": "deepseek-chat"}],
            },
        )
        .json()
    )
    assert resp["status"] == "running"
    assert resp["output_schema"] == _RUNNABLE_SCHEMA
    assert resp["prompt_tokens"] == 42
    job_id = resp["job_id"]

    jobs = _drain_jobs(app)
    job = jobs[job_id]
    assert job["status"] == "done"
    res = job["results"]["deepseek:deepseek-chat"]
    assert res["status"] == "ok"
    assert res["valid"] is True  # ParsedPricing validates "{}"
    assert res["parsed"] == {}
    assert res["validated"] is not None
    # the INSERT INTO bench_runs ran
    assert any("INSERT INTO bench_runs" in c[1] for c in pool.calls)


def test_bench_run_unknown_provider():
    router = FakeRouter()
    app = make_app(router=router)
    resp = (
        TestClient(app)
        .post(
            "/bench/run",
            json={
                "invocation_type": _RUNNABLE,
                "models": [{"provider": "not-a-provider", "model_name": "x"}],
            },
        )
        .json()
    )
    jobs = _drain_jobs(app)
    res = jobs[resp["job_id"]]["results"]["not-a-provider:x"]
    assert res["status"] == "error"
    assert "Unknown provider" in res["error"]
    assert router.calls == []  # never reached run_single


def test_bench_run_model_call_raises():
    router = FakeRouter(raises=RuntimeError("model down"))
    app = make_app(router=router)
    resp = (
        TestClient(app)
        .post(
            "/bench/run",
            json={
                "invocation_type": _RUNNABLE,
                "models": [{"provider": "deepseek", "model_name": "deepseek-chat"}],
            },
        )
        .json()
    )
    jobs = _drain_jobs(app)
    res = jobs[resp["job_id"]]["results"]["deepseek:deepseek-chat"]
    assert res["status"] == "error"
    assert "RuntimeError: model down" in res["error"]


def test_bench_run_invalid_output_records_validation_error():
    # Non-JSON text → parsed stays None, raw set, valid False, validation_error set.
    router = FakeRouter(result=("not json at all", 3))
    app = make_app(router=router)
    resp = (
        TestClient(app)
        .post(
            "/bench/run",
            json={
                "invocation_type": _RUNNABLE,
                "models": [{"provider": "deepseek", "model_name": "deepseek-chat"}],
            },
        )
        .json()
    )
    jobs = _drain_jobs(app)
    res = jobs[resp["job_id"]]["results"]["deepseek:deepseek-chat"]
    assert res["status"] == "ok"
    assert res["parsed"] is None
    assert res["raw"] == "not json at all"
    assert res["valid"] is False
    assert res["validation_error"]


def test_bench_run_persist_failure_swallowed():
    # INSERT raises → worker logs + still marks done (no exception escapes).
    def _raise():
        raise RuntimeError("db gone")

    pool = ScriptedPool(rules=[("INSERT INTO bench_runs", _raise)])
    app = make_app(pool=pool, router=FakeRouter(result=("{}", 1)))
    resp = (
        TestClient(app)
        .post(
            "/bench/run",
            json={
                "invocation_type": _RUNNABLE,
                "models": [{"provider": "deepseek", "model_name": "deepseek-chat"}],
            },
        )
        .json()
    )
    jobs = _drain_jobs(app)
    assert jobs[resp["job_id"]]["status"] == "done"


def test_bench_jobs_status_and_gone():
    app = make_app(router=FakeRouter(result=("{}", 1)))
    client = TestClient(app)
    resp = client.post(
        "/bench/run",
        json={
            "invocation_type": _RUNNABLE,
            "models": [{"provider": "deepseek", "model_name": "deepseek-chat"}],
        },
    ).json()
    _drain_jobs(app)
    got = client.get(f"/bench/jobs/{resp['job_id']}").json()
    assert got["status"] == "done"
    assert isinstance(got["results"], list)
    gone = client.get("/bench/jobs/deadbeef").json()
    assert gone["status"] == "gone"


def test_bench_prune_drops_oldest():
    jobs = {f"j{i}": {"status": "done"} for i in range(22)}
    bench._prune(jobs, keep=20)
    assert len(jobs) == 20
    assert "j0" not in jobs and "j1" not in jobs
    assert "j21" in jobs


def test_bench_list_runs_populated_and_empty():
    rows = [
        {
            "id": 7,
            "created_at": _NOW,
            "invocation_type": "x.y",
            "tier": "workhorse",
            "context_note": "note",
            "status": "done",
            "results": [
                {"provider": "deepseek", "model_name": "deepseek-chat", "status": "ok", "latency_ms": 120, "valid": True},
            ],
        },
        {
            "id": 8,
            "created_at": _NOW,
            "invocation_type": "a.b",
            "tier": "fast",
            "context_note": None,
            "status": "done",
            "results": None,  # results None → []
        },
    ]
    app = make_app(pool=ScriptedPool(rules=[("FROM bench_runs ORDER BY created_at DESC", rows)]))
    body = TestClient(app).get("/bench/runs?limit=5").json()
    assert body["runs"][0]["id"] == 7
    assert body["runs"][0]["models"][0]["model_name"] == "deepseek-chat"
    assert body["runs"][1]["models"] == []  # None results

    empty = make_app(pool=ScriptedPool(rules=[("FROM bench_runs ORDER BY created_at DESC", [])]))
    assert TestClient(empty).get("/bench/runs").json() == {"runs": []}


def test_bench_get_run_found_and_missing():
    row = {
        "id": 9,
        "created_at": _NOW,
        "invocation_type": "x.y",
        "tier": "fast",
        "context_note": "n",
        "prompt_tokens": 50,
        "prompt_preview": "P",
        "status": "done",
        "results": [{"status": "ok"}],
    }
    app = make_app(pool=ScriptedPool(rules=[("SELECT * FROM bench_runs WHERE id", [row])]))
    body = TestClient(app).get("/bench/runs/9").json()
    assert body["id"] == 9
    assert body["prompt_preview"] == "P"
    assert body["results"] == [{"status": "ok"}]

    missing = make_app(pool=ScriptedPool(rules=[("SELECT * FROM bench_runs WHERE id", [])]))
    assert TestClient(missing).get("/bench/runs/99").json()["error"] == "Run not found."


def test_bench_get_run_null_results_coerced():
    row = {
        "id": 1,
        "created_at": _NOW,
        "invocation_type": "x",
        "tier": "fast",
        "context_note": None,
        "prompt_tokens": 0,
        "prompt_preview": "",
        "status": "done",
        "results": None,
    }
    app = make_app(pool=ScriptedPool(rules=[("SELECT * FROM bench_runs WHERE id", [row])]))
    assert TestClient(app).get("/bench/runs/1").json()["results"] == []


# ── /bench/replay-step ──────────────────────────────────────────────────────
def _agent_run_row(**over):
    r = {
        "id": 50,
        "invocation_type": _RUNNABLE,
        "model_tier": "code",
        "model_name": "qwen3:14b",
        "step_name": "step1",
        "input_summary": "the frozen system prompt the model saw",
        "output_summary": '{"orig": 1}',
        "started_at": _NOW,
        "completed_at": _NOW,
        "input_token_count": 100,
        "output_token_count": 20,
        "status": "completed",
        "error": None,
    }
    r.update(over)
    return r


def test_replay_step_not_found():
    app = make_app(pool=ScriptedPool(rules=[("FROM agent_runs WHERE id", [])]))
    body = (
        TestClient(app)
        .post("/bench/replay-step", json={"run_id": 1, "model": {"provider": "deepseek", "model_name": "x"}})
        .json()
    )
    assert "not found" in body["error"]


def test_replay_step_no_input_summary():
    row = _agent_run_row(input_summary=None)
    app = make_app(pool=ScriptedPool(rules=[("FROM agent_runs WHERE id", [row])]))
    body = (
        TestClient(app)
        .post("/bench/replay-step", json={"run_id": 50, "model": {"provider": "deepseek", "model_name": "x"}})
        .json()
    )
    assert "no input_summary" in body["error"]


def test_replay_step_no_recipe():
    row = _agent_run_row(invocation_type="bogus.itype")
    app = make_app(pool=ScriptedPool(rules=[("FROM agent_runs WHERE id", [row])]))
    body = (
        TestClient(app)
        .post("/bench/replay-step", json={"run_id": 50, "model": {"provider": "deepseek", "model_name": "x"}})
        .json()
    )
    assert "no recipe for invocation_type" in body["error"]


def test_replay_step_schema_unresolvable():
    row = _agent_run_row(invocation_type="pi.exploration_kickoff")
    app = make_app(pool=ScriptedPool(rules=[("FROM agent_runs WHERE id", [row])]), schemas={})
    body = (
        TestClient(app)
        .post("/bench/replay-step", json={"run_id": 50, "model": {"provider": "deepseek", "model_name": "x"}})
        .json()
    )
    assert "not resolvable" in body["error"]


def test_replay_step_unknown_provider():
    row = _agent_run_row()
    app = make_app(pool=ScriptedPool(rules=[("FROM agent_runs WHERE id", [row])]))
    body = (
        TestClient(app)
        .post("/bench/replay-step", json={"run_id": 50, "model": {"provider": "nope", "model_name": "x"}})
        .json()
    )
    assert "unknown provider" in body["error"]


def test_replay_step_ok_frozen():
    row = _agent_run_row()
    router = FakeRouter(result=("{}", 9))
    app = make_app(pool=ScriptedPool(rules=[("FROM agent_runs WHERE id", [row])]), router=router)
    body = (
        TestClient(app)
        .post("/bench/replay-step", json={"run_id": 50, "model": {"provider": "deepseek", "model_name": "deepseek-chat"}})
        .json()
    )
    assert body["status"] == "ok"
    assert body["frozen"] is True  # no prompt_override
    assert body["valid"] is True  # ParsedPricing validates "{}"
    assert body["parsed"] == {}
    orig = body["original"]
    assert orig["run_id"] == 50
    assert orig["latency_ms"] == 0  # started==completed
    assert orig["parsed"] == {"orig": 1}  # output_summary parsed
    # router was fed the frozen input_summary (priority layer content)
    assert router.calls and router.calls[0][1] == "deepseek-chat"


def test_replay_step_prompt_override_not_frozen():
    row = _agent_run_row()
    app = make_app(
        pool=ScriptedPool(rules=[("FROM agent_runs WHERE id", [row])]), router=FakeRouter(result=("not json", 2))
    )
    body = (
        TestClient(app)
        .post(
            "/bench/replay-step",
            json={
                "run_id": 50,
                "model": {"provider": "deepseek", "model_name": "deepseek-chat"},
                "prompt_override": "MY EDITED PROMPT",
            },
        )
        .json()
    )
    assert body["frozen"] is False
    assert body["raw"] == "not json"  # non-json → raw kept
    assert body["valid"] is False


def test_replay_step_model_error():
    row = _agent_run_row()
    app = make_app(
        pool=ScriptedPool(rules=[("FROM agent_runs WHERE id", [row])]), router=FakeRouter(raises=TimeoutError("slow"))
    )
    body = (
        TestClient(app)
        .post("/bench/replay-step", json={"run_id": 50, "model": {"provider": "deepseek", "model_name": "deepseek-chat"}})
        .json()
    )
    assert body["status"] == "error"
    assert "TimeoutError: slow" in body["error"]
    assert body["original"]["run_id"] == 50
    assert body["frozen"] is True


def test_replay_original_no_timestamps_and_bad_output():
    row = _agent_run_row(started_at=None, completed_at=None, output_summary="not json")
    out = bench._replay_original(row)
    assert out["latency_ms"] is None  # missing timestamps
    assert out["parsed"] is None  # un-parseable output_summary


# ============================================================================
# AGENTLAB
# ============================================================================
def test_agentlab_agents_catalog(monkeypatch):
    app = make_app(claims=[_Claim(3, "a claim")])
    body = TestClient(app).get("/agentlab/agents").json()
    ids = {a["id"] for a in body["agents"]}
    assert {"mimir", "pi", "planner", "researcher", "collectors"} <= ids
    mimir = next(a for a in body["agents"] if a["id"] == "mimir")
    assert mimir["has_suite"] is True  # in SUITES
    pi = next(a for a in body["agents"] if a["id"] == "pi")
    kick = next(m for m in pi["modes"] if m["key"] == "exploration_kickoff")
    assert kick["invocation_type"] == "pi.exploration_kickoff"
    assert kick["model"]  # provider:model annotated
    assert "runnable" in kick
    assert kick["emits"] == "company.bootstrapped + claims"
    assert body["claims"] == [{"id": 3, "claim": "a claim"}]


def test_agentlab_agents_claims_failure_swallowed():
    class _BadState:
        async def get_active_claims(self, limit=25):
            raise RuntimeError("db down")

    eng = make_engine()
    eng.state = _BadState()
    app = make_app(engine=eng)
    body = TestClient(app).get("/agentlab/agents").json()
    assert body["claims"] == []  # exception swallowed → empty


def test_agentlab_run_unknown_agent_mode():
    app = make_app()
    body = TestClient(app).post("/agentlab/run", json={"agent": "nope", "mode": "nope"}).json()
    assert body["status"] == "error"
    assert "unknown agent/mode" in body["error"]


def test_agentlab_run_llm_ok():
    # researcher.execute_task → ResearcherFindings, which validates "{}" (findings
    # defaults to []). Script a task id so build_context grounds; no findings rows.
    pool = ScriptedPool(rules=[("SELECT id FROM tasks", 99)])
    router = FakeRouter(result=("{}", 13))
    app = make_app(pool=pool, router=router, claims=[_Claim(5, "claim five")])
    body = (
        TestClient(app).post("/agentlab/run", json={"agent": "researcher", "mode": "execute_task", "claim_id": 5}).json()
    )
    assert body["status"] == "ok"
    assert body["kind"] == "llm"
    assert body["dry_run"] is True
    assert body["invocation_type"] == "researcher.execute_task"
    assert body["valid"] is True
    assert body["parsed"] == {}
    assert body["would_emit"] == "findings + task.completed"
    assert body["model"]


def test_agentlab_run_llm_invalid_output():
    app = make_app(router=FakeRouter(result=("nope", 1)))
    body = TestClient(app).post("/agentlab/run", json={"agent": "pi", "mode": "exploration_kickoff"}).json()
    assert body["status"] == "ok"
    assert body["valid"] is False
    assert body["raw"] == "nope"
    assert body["validation_error"]


def test_agentlab_run_llm_schema_unresolvable():
    app = make_app(schemas={})
    body = TestClient(app).post("/agentlab/run", json={"agent": "pi", "mode": "exploration_kickoff"}).json()
    assert body["status"] == "error"
    assert "no resolvable output schema" in body["error"]


def test_agentlab_run_llm_context_error():
    # critic.kill_verdict needs a claim; none active + no findings → BenchContextError.
    pool = ScriptedPool(rules=[("FROM critic_verdicts", [])])
    app = make_app(pool=pool, claims=[])
    body = TestClient(app).post("/agentlab/run", json={"agent": "critic", "mode": "kill_verdict"}).json()
    assert body["status"] == "error"
    assert body["kind"] == "llm"
    assert "No active thesis" in body["error"]


def test_agentlab_run_llm_model_error():
    app = make_app(router=FakeRouter(raises=RuntimeError("model boom")))
    body = TestClient(app).post("/agentlab/run", json={"agent": "pi", "mode": "exploration_kickoff"}).json()
    assert body["status"] == "error"
    assert body["kind"] == "llm"
    assert "RuntimeError: model boom" in body["error"]
    assert body["tier"]


def test_agentlab_run_top_level_exception_caught(monkeypatch):
    # Force _run_llm to blow up unexpectedly → run() returns structured error.
    async def _boom(app, mode, claim_id):
        raise ValueError("kaboom")

    monkeypatch.setattr(agentlab, "_run_llm", _boom)
    app = make_app()
    body = TestClient(app).post("/agentlab/run", json={"agent": "pi", "mode": "exploration_kickoff"}).json()
    assert body["status"] == "error"
    assert body["kind"] == "llm"
    assert "ValueError: kaboom" in body["error"]


# ── mimir dispatch ──────────────────────────────────────────────────────────
def _patch_mimir_state(monkeypatch):
    async def _fake_state(app):
        return object()

    monkeypatch.setattr(agentlab, "_mimir_state", _fake_state)


def test_agentlab_run_mimir_classify(monkeypatch):
    async def _fake_classify(app, **kw):
        assert kw["arxiv_id"] == "1706.03762"
        return {"tier": "preprint", "blocked": False}

    monkeypatch.setattr(agentlab, "_classify", _fake_classify)
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = (
        TestClient(app)
        .post("/agentlab/run", json={"agent": "mimir", "mode": "classify", "inputs": {"arxiv_id": "1706.03762"}})
        .json()
    )
    assert body["status"] == "ok"
    assert body["action"] == "classify"
    assert body["result"]["tier"] == "preprint"


def test_agentlab_run_mimir_classify_missing_input(monkeypatch):
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = TestClient(app).post("/agentlab/run", json={"agent": "mimir", "mode": "classify", "inputs": {}}).json()
    assert body["status"] == "error"
    assert "provide an arXiv ID or a URL" in body["error"]


def test_agentlab_run_mimir_seed_arxiv(monkeypatch):
    captured = {}

    async def _fake_ingest(source, state, **kw):
        captured["source"] = source
        return {"ingested": True, "document_id": 42}

    monkeypatch.setattr(agentlab, "ingest_source", _fake_ingest)
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = (
        TestClient(app)
        .post("/agentlab/run", json={"agent": "mimir", "mode": "seed", "inputs": {"arxiv_id": "2401.0001"}})
        .json()
    )
    assert body["status"] == "ok"
    assert body["live"] is True
    assert body["result"]["document_id"] == 42
    assert captured["source"]["source_kind"] == "arxiv"
    assert captured["source"]["arxiv_id"] == "2401.0001"


def test_agentlab_run_mimir_seed_web(monkeypatch):
    captured = {}

    async def _fake_ingest(source, state, **kw):
        captured["source"] = source
        return {"ingested": False}

    monkeypatch.setattr(agentlab, "ingest_source", _fake_ingest)
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = (
        TestClient(app)
        .post("/agentlab/run", json={"agent": "mimir", "mode": "seed", "inputs": {"url": "https://example.com/post"}})
        .json()
    )
    assert body["status"] == "ok"
    assert captured["source"]["source_kind"] == "web"
    assert captured["source"]["url"] == "https://example.com/post"


def test_agentlab_run_mimir_seed_missing(monkeypatch):
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = TestClient(app).post("/agentlab/run", json={"agent": "mimir", "mode": "seed", "inputs": {}}).json()
    assert body["status"] == "error"
    assert "provide an arXiv ID or a URL" in body["error"]


def test_agentlab_run_mimir_seed_ingest_raises(monkeypatch):
    async def _fake_ingest(source, state, **kw):
        raise RuntimeError("ingest blew up")

    monkeypatch.setattr(agentlab, "ingest_source", _fake_ingest)
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = (
        TestClient(app)
        .post("/agentlab/run", json={"agent": "mimir", "mode": "seed", "inputs": {"arxiv_id": "2401.0001"}})
        .json()
    )
    assert body["status"] == "error"
    assert "RuntimeError: ingest blew up" in body["error"]


def test_agentlab_run_mimir_sweep(monkeypatch):
    async def _fake_sweep(topics, state, per_topic=4):
        assert topics == ["mixture of experts"]
        return {"scanned": 5, "discovered": 2}

    monkeypatch.setattr(agentlab, "run_discovery_sweep", _fake_sweep)
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = (
        TestClient(app)
        .post("/agentlab/run", json={"agent": "mimir", "mode": "sweep", "inputs": {"topic": "mixture of experts"}})
        .json()
    )
    assert body["status"] == "ok"
    assert body["result"]["discovered"] == 2
    assert "source.discovered" in body["note"]


def test_agentlab_run_mimir_sweep_raises(monkeypatch):
    async def _fake_sweep(topics, state, per_topic=4):
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(agentlab, "run_discovery_sweep", _fake_sweep)
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = TestClient(app).post("/agentlab/run", json={"agent": "mimir", "mode": "sweep", "inputs": {}}).json()
    assert body["status"] == "error"
    assert "RuntimeError: sweep failed" in body["error"]


def test_agentlab_run_mimir_acquire(monkeypatch):
    captured = {}

    async def _fake_acquire(event, shim):
        captured["event"] = event
        captured["shim"] = shim
        return {"status": "fulfilled", "document_id": 7}

    monkeypatch.setattr(agentlab, "handle_acquire_requested", _fake_acquire)
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = (
        TestClient(app)
        .post("/agentlab/run", json={"agent": "mimir", "mode": "acquire", "inputs": {"query": "speculative decoding"}})
        .json()
    )
    assert body["status"] == "ok"
    assert body["result"]["document_id"] == 7
    assert captured["event"]["payload"]["query"] == "speculative decoding"
    assert hasattr(captured["shim"], "router")


def test_agentlab_run_mimir_acquire_missing(monkeypatch):
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = TestClient(app).post("/agentlab/run", json={"agent": "mimir", "mode": "acquire", "inputs": {}}).json()
    assert body["status"] == "error"
    assert "provide a query or an arXiv ID" in body["error"]


def test_agentlab_run_mimir_acquire_raises(monkeypatch):
    async def _fake_acquire(event, shim):
        raise RuntimeError("acquire failed")

    monkeypatch.setattr(agentlab, "handle_acquire_requested", _fake_acquire)
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = (
        TestClient(app)
        .post("/agentlab/run", json={"agent": "mimir", "mode": "acquire", "inputs": {"arxiv_id": "2211.17192"}})
        .json()
    )
    assert body["status"] == "error"
    assert "RuntimeError: acquire failed" in body["error"]


# ── collectors dispatch ─────────────────────────────────────────────────────
def _desc(source_kind="arxiv", key="2401.0001"):
    return SourceDescriptor(
        kind="paper",
        source_kind=source_kind,
        canonical_key=key,
        url="http://x",
        arxiv_id=key,
        doi=None,
        title="T",
        why="w",
    )


def test_agentlab_run_collectors_scout(monkeypatch):
    async def _fake_scout(topics, per_topic=5):
        assert topics == ["large language models"]
        return [_desc(), _desc(key="2401.0002")]

    monkeypatch.setitem(agentlab._SCOUTS, "arxiv", _fake_scout)
    app = make_app()
    body = (
        TestClient(app)
        .post(
            "/agentlab/run", json={"agent": "collectors", "mode": "arxiv", "inputs": {"topic": "large language models"}}
        )
        .json()
    )
    assert body["status"] == "ok"
    assert body["kind"] == "collectors"
    assert body["scout"] == "arxiv"
    assert body["result"]["count"] == 2
    assert body["result"]["sources"][0]["canonical_key"] == "2401.0001"


def test_agentlab_run_collectors_scout_default_topic(monkeypatch):
    seen = {}

    async def _fake_scout(topics, per_topic=5):
        seen["topics"] = topics
        return []

    monkeypatch.setitem(agentlab._SCOUTS, "web", _fake_scout)
    app = make_app()
    body = TestClient(app).post("/agentlab/run", json={"agent": "collectors", "mode": "web", "inputs": {}}).json()
    assert seen["topics"] == ["large language models"]  # default topic
    assert body["result"]["count"] == 0


def test_agentlab_run_collectors_sweep(monkeypatch):
    async def _fake_sweep(topics, state, per_topic=4):
        return {"scanned": 3, "discovered": 1}

    monkeypatch.setattr(agentlab, "run_discovery_sweep", _fake_sweep)
    _patch_mimir_state(monkeypatch)
    app = make_app()
    body = (
        TestClient(app)
        .post(
            "/agentlab/run", json={"agent": "collectors", "mode": "sweep", "inputs": {"topic": "graph neural networks"}}
        )
        .json()
    )
    assert body["status"] == "ok"
    assert body["kind"] == "collectors"
    assert body["live"] is True
    assert body["result"]["scanned"] == 3


# ── suites ──────────────────────────────────────────────────────────────────
def test_agentlab_suite_known_and_unknown():
    app = make_app()
    client = TestClient(app)
    body = client.get("/agentlab/suite?agent=mimir").json()
    assert body["agent"] == "mimir"
    ids = {c["id"] for c in body["cases"]}
    assert "good_arxiv" in ids
    assert all({"id", "label", "question", "expect", "gap"} <= set(c) for c in body["cases"])

    unknown = client.get("/agentlab/suite?agent=nope").json()
    assert unknown == {"agent": "nope", "cases": []}


def test_agentlab_suite_run_executes_cases(monkeypatch):
    # Drive the collectors suite with mocked scouts/sweep so cases pass/fail
    # deterministically — and force one case to raise to hit the error branch.
    async def _good_scout(topics, per_topic=4):
        return [_desc(source_kind="arxiv", key="k1")]

    async def _empty_scout(topics, per_topic=4):
        return []

    async def _raise_scout(topics, per_topic=4):
        raise RuntimeError("github down")

    async def _fake_sweep(topics, state, per_topic=3):
        return {"scanned": 2, "discovered": 1}

    monkeypatch.setattr(agentlab, "scout_arxiv", _good_scout)
    monkeypatch.setattr(agentlab, "scout_web", _empty_scout)
    monkeypatch.setattr(agentlab, "scout_github", _raise_scout)
    monkeypatch.setattr(agentlab, "run_discovery_sweep", _fake_sweep)
    _patch_mimir_state(monkeypatch)

    app = make_app()
    body = TestClient(app).post("/agentlab/suite/run", json={"agent": "collectors"}).json()
    assert body["agent"] == "collectors"
    by_id = {r["id"]: r for r in body["results"]}
    assert by_id["scout_arxiv"]["status"] == "pass"  # well-formed descriptor
    assert by_id["scout_web"]["status"] == "fail"  # 0 results
    assert by_id["scout_github"]["status"] == "error"  # raised → caught
    assert by_id["sweep"]["status"] == "pass"  # scanned >= 1


def test_agentlab_suite_run_unknown_agent():
    app = make_app()
    body = TestClient(app).post("/agentlab/suite/run", json={"agent": "nope"}).json()
    assert body == {"agent": "nope", "results": []}


# ============================================================================
# bench.build_context — every invocation-type branch (direct, no HTTP)
# ============================================================================
def _ctx(invocation_type, claim_id, *, rules=None, claims=None, state=None):
    eng = make_engine(pool=ScriptedPool(rules=rules or []), claims=claims)
    if state is not None:
        eng.state = state
    return asyncio.run(bench.build_context(eng, invocation_type, claim_id))


def test_build_context_global_and_unknown():
    ctx, note = _ctx("pi.exploration_kickoff", None)
    assert ctx == {} and "global company state" in note
    ctx2, note2 = _ctx("planner.generate_tasks", None)
    assert ctx2 == {}
    ctx3, note3 = _ctx("some.unmapped_type", None)
    assert ctx3 == {} and "No tailored context" in note3


def test_build_context_critic_kill_with_finding():
    rules = [("SELECT id FROM findings WHERE claim_id=$1", 7)]
    ctx, note = _ctx("critic.kill_verdict", 3, rules=rules)
    assert ctx == {"claim_id": 3, "triggering_finding_id": 7}
    assert "T3" in note and "F7" in note


def test_build_context_critic_kill_no_finding_uses_active_claim():
    # claim_id None → _any_active_claim_id; no finding row → no triggering_finding_id.
    ctx, note = _ctx("critic.kill_verdict", None, claims=[_Claim(8, "c")])
    assert ctx == {"claim_id": 8}
    assert "F" not in note


def test_build_context_critic_kill_no_active_raises():
    with pytest.raises(bench.BenchContextError):
        _ctx("critic.kill_verdict", None, claims=[])


def test_build_context_claim_verdict_with_verdict():
    rules = [("FROM critic_verdicts WHERE claim_id=$1", 12)]
    ctx, note = _ctx("pi.claim_verdict", 4, rules=rules)
    assert ctx == {"claim_id": 4, "critic_verdict_id": 12}
    assert "verdict #12" in note


def test_build_context_claim_verdict_fallback_latest_verdict():
    # No per-claim verdict; falls back to the latest verdict anywhere.
    rules = [
        ("FROM critic_verdicts WHERE claim_id=$1", None),
        ("FROM critic_verdicts ORDER BY created_at DESC LIMIT 1", 99),
    ]
    ctx, _ = _ctx("pi.claim_verdict", 4, rules=rules)
    assert ctx["critic_verdict_id"] == 99


def test_build_context_claim_verdict_no_thesis_raises():
    with pytest.raises(bench.BenchContextError):
        _ctx("pi.claim_verdict", None, claims=[])


def test_build_context_claim_verdict_no_verdict_raises():
    rules = [("FROM critic_verdicts", None)]  # both verdict lookups None
    with pytest.raises(bench.BenchContextError):
        _ctx("pi.claim_verdict", 4, rules=rules)


def test_build_context_researcher_execute_with_findings():
    rows = [{"title": "T1", "summary": "S1"}, {"title": "T2", "summary": "S2"}]
    rules = [("SELECT id FROM tasks", 5), ("FROM findings WHERE task_id=$1", rows)]
    ctx, note = _ctx("researcher.execute_task", None, rules=rules)
    assert ctx["task_id"] == 5
    assert "T1\nS1" in ctx["raw_material"]
    assert "2 findings" in note


def test_build_context_researcher_execute_claim_fallback_findings():
    # task findings empty → falls back to claim findings.
    rows = [{"title": "C", "summary": "D"}]
    rules = [
        ("SELECT id FROM tasks", 5),
        ("FROM findings WHERE task_id=$1", []),
        ("FROM findings WHERE claim_id=$1", rows),
    ]
    ctx, note = _ctx("researcher.execute_task", 9, rules=rules)
    assert "C\nD" in ctx["raw_material"]


def test_build_context_researcher_execute_no_task_raises():
    with pytest.raises(bench.BenchContextError):
        _ctx("researcher.execute_task", None, rules=[("SELECT id FROM tasks", None)])


def test_build_context_slop_score():
    class _S(FakeState):
        async def get_task(self, tid):
            return {"id": tid}

        async def get_findings(self, ids):
            return [{"id": i} for i in ids]

    rules = [
        ("SELECT task_id FROM findings GROUP BY", 5),
        ("SELECT id FROM findings WHERE task_id=$1", [{"id": 1}, {"id": 2}]),
    ]
    ctx, note = _ctx("evaluation.slop_score", None, rules=rules, state=_S())
    assert ctx["task"] == {"id": 5}
    assert len(ctx["findings"]) == 2
    assert "2 findings" in note


def test_build_context_slop_score_no_findings_raises():
    with pytest.raises(bench.BenchContextError):
        _ctx("evaluation.slop_score", None, rules=[("SELECT task_id FROM findings GROUP BY", None)])


def test_build_context_reflect_lesson():
    row = {"invocation_type": "critic.kill_verdict", "output_summary": "a summary"}
    rules = [("FROM agent_runs", [row])]
    ctx, note = _ctx("reflect.lesson_propose", None, rules=rules)
    assert ctx == {"invocation_type": "critic.kill_verdict", "run_summary": "a summary"}
    assert "critic.kill_verdict" in note


def test_build_context_reflect_lesson_no_run_raises():
    with pytest.raises(bench.BenchContextError):
        _ctx("reflect.lesson_propose", None, rules=[("FROM agent_runs", [])])


def test_build_context_phase_transition_ratify():
    class _S(FakeState):
        async def get_company_state(self):
            return SimpleNamespace(current_phase="exploration")

    ctx, note = _ctx("pi.phase_transition_ratify", None, state=_S())
    assert ctx["from_phase"] == "exploration"
    assert ctx["target_phase"] == "convergence"
    assert "→" in note


def test_build_context_phase_transition_unknown_phase_default():
    class _S(FakeState):
        async def get_company_state(self):
            return SimpleNamespace(current_phase="weird")

    ctx, _ = _ctx("pi.phase_transition_ratify", None, state=_S())
    assert ctx["target_phase"] == "convergence"  # default when phase not in map


def test_build_context_phase_adjudicator_check():
    class _S(FakeState):
        async def get_company_state(self):
            return SimpleNamespace(
                current_phase="exploration",
                phase_started_at=datetime(2026, 6, 1, tzinfo=UTC),
            )

        async def get_active_claims(self, limit=10):
            return [SimpleNamespace(id=1, confidence=0.5, statement="claim")]

    ctx, note = _ctx("phase_adjudicator.check", None, state=_S())
    assert ctx["current_phase"] == "exploration"
    assert "T1" in ctx["theses_summary"]
    assert "1 theses" in note


def test_build_context_phase_adjudicator_no_theses():
    class _S(FakeState):
        async def get_company_state(self):
            return SimpleNamespace(
                current_phase="exploration",
                phase_started_at=datetime(2026, 6, 1, tzinfo=UTC),
            )

        async def get_active_claims(self, limit=10):
            return []

    ctx, _ = _ctx("phase_adjudicator.check", None, state=_S())
    assert ctx["theses_summary"] == "(none)"


def test_build_context_spawn_claim():
    rules = [("FROM claims WHERE status IN", 42)]
    ctx, note = _ctx("pi.spawn_claim", None, rules=rules)
    assert ctx == {"invalidated_claim_id": 42}
    assert "T42" in note


def test_build_context_spawn_claim_none_raises():
    with pytest.raises(bench.BenchContextError):
        _ctx("pi.spawn_claim", None, rules=[("FROM claims WHERE status IN", None)])


def test_build_context_extract_evidence():
    row = {
        "task_id": 3,
        "inquiry_id": 1,
        "sub_question_idx": 0,
        "url": "http://x",
        "title": "Title",
        "sub_questions": '[{"q": "what?"}]',  # json string → parsed
        "content": "page body",
    }
    rules = [("FROM evidence e", [row])]
    ctx, note = _ctx("researcher.extract_evidence", None, rules=rules)
    assert ctx["sub_question"] == "what?"
    assert ctx["content"] == "page body"
    assert "SQ0" in note


def test_build_context_extract_evidence_list_subqs_and_title_fallback():
    # sub_questions already a list (skips json.loads) + null title → url used in note.
    row = {
        "task_id": 3,
        "inquiry_id": 1,
        "sub_question_idx": 0,
        "url": "http://x",
        "title": None,
        "sub_questions": [{"q": "what?"}],
        "content": "body",
    }
    ctx, note = _ctx("researcher.extract_evidence", None, rules=[("FROM evidence e", [row])])
    assert ctx["title"] == ""  # None → ""
    assert "http://x" in note  # title None → url in note


def test_build_context_extract_evidence_none_raises():
    with pytest.raises(bench.BenchContextError):
        _ctx("researcher.extract_evidence", None, rules=[("FROM evidence e", [])])


def test_build_context_plan_inquiry():
    rules = [("SELECT id FROM tasks", 5), ("SELECT description FROM tasks WHERE id=$1", "do it")]
    ctx, note = _ctx("researcher.plan_inquiry", None, rules=rules)
    assert ctx == {"task_id": 5, "question": "do it", "iteration": 1, "prior_evidence": []}
    assert "iteration 1" in note


def test_build_context_plan_inquiry_no_task_raises():
    with pytest.raises(bench.BenchContextError):
        _ctx("researcher.plan_inquiry", None, rules=[("SELECT id FROM tasks", None)])


def test_build_context_synthesize():
    inq = {"task_id": 3, "question": "Q", "sub_questions": [{"q": "sq1"}]}  # list, not str
    ev = [
        {
            "id": 1,
            "sub_question_idx": 0,
            "url": "u",
            "title": "t",
            "quote": "q",
            "claim": "c",
            "stance": "support",
            "confidence": 0.9,
        }
    ]
    rules = [("FROM research_inquiries ri", [inq]), ("FROM evidence WHERE task_id=$1", ev)]
    ctx, note = _ctx("researcher.synthesize", None, rules=rules)
    assert ctx["question"] == "Q"
    assert ctx["sub_questions"] == ["sq1"]
    assert ctx["evidence"][0]["confidence"] == 0.9
    assert "1 evidence" in note


def test_build_context_synthesize_json_string_subqs():
    # sub_questions as a JSON string → exercises the json.loads arm.
    inq = {"task_id": 3, "question": "Q", "sub_questions": '[{"q": "sq1"}]'}
    rules = [("FROM research_inquiries ri", [inq]), ("FROM evidence WHERE task_id=$1", [])]
    ctx, _ = _ctx("researcher.synthesize", None, rules=rules)
    assert ctx["sub_questions"] == ["sq1"]
    assert ctx["evidence"] == []


def test_build_context_synthesize_none_raises():
    with pytest.raises(bench.BenchContextError):
        _ctx("researcher.synthesize", None, rules=[("FROM research_inquiries ri", [])])


# ============================================================================
# bench.get_engine + memory wrappers (lazy create path)
# ============================================================================
def test_get_engine_lazy_create(monkeypatch):
    sentinel = make_engine()

    async def _fake_create(pool):
        return sentinel

    monkeypatch.setattr(bench.BenchEngine, "create", classmethod(lambda cls, pool: _fake_create(pool)))
    app = FastAPI()
    app.state.pool = ScriptedPool()
    # no bench_engine pre-set → get_engine builds + caches it
    eng1 = asyncio.run(bench.get_engine(app))
    assert eng1 is sentinel
    assert app.state.bench_engine is sentinel
    eng2 = asyncio.run(bench.get_engine(app))  # cached
    assert eng2 is sentinel


def test_bench_engine_init_direct():
    eng = bench.BenchEngine(
        pool="P",
        state="S",
        memory="M",
        lessons="L",
        curator="C",
        router_="R",
        schemas={"x": 1},
    )
    assert eng.pool == "P"
    assert eng.router == "R"
    assert eng.schemas == {"x": 1}


def test_bench_engine_create_zep_up(monkeypatch):
    # Stub every heavy client so create() runs end-to-end with no network/DB.
    class _Zep:
        @classmethod
        def from_env(cls):
            return cls()

        async def ensure_user(self):
            return None

        async def ensure_session(self, s):
            return None

    monkeypatch.setattr(bench, "ZepClient", _Zep)
    monkeypatch.setattr(bench, "PostgresClient", lambda pool: ("state", pool))
    monkeypatch.setattr(bench, "LessonsClient", lambda pool: ("lessons", pool))
    monkeypatch.setattr(bench, "Curator", lambda **k: ("curator", k))
    monkeypatch.setattr(bench, "Router", lambda **k: ("router", k))
    monkeypatch.setattr(bench, "GPULock", lambda: "lock")
    monkeypatch.setattr(bench, "build_cloud_chain", lambda env: [])
    monkeypatch.setattr(bench, "build_premium_chain", lambda env: [])

    eng = asyncio.run(bench.BenchEngine.create("POOL"))
    assert eng.pool == "POOL"
    assert isinstance(eng.memory, bench._SafeMemory)  # Zep up → SafeMemory
    assert eng.schemas  # built registry


def test_bench_engine_create_zep_down(monkeypatch):
    class _Zep:
        @classmethod
        def from_env(cls):
            raise RuntimeError("zep unreachable")

    monkeypatch.setattr(bench, "ZepClient", _Zep)
    monkeypatch.setattr(bench, "PostgresClient", lambda pool: ("state", pool))
    monkeypatch.setattr(bench, "LessonsClient", lambda pool: ("lessons", pool))
    monkeypatch.setattr(bench, "Curator", lambda **k: ("curator", k))
    monkeypatch.setattr(bench, "Router", lambda **k: ("router", k))
    monkeypatch.setattr(bench, "GPULock", lambda: "lock")
    monkeypatch.setattr(bench, "build_cloud_chain", lambda env: [])
    monkeypatch.setattr(bench, "build_premium_chain", lambda env: [])

    eng = asyncio.run(bench.BenchEngine.create("POOL"))
    assert isinstance(eng.memory, bench._NullMemory)  # Zep down → NullMemory


def test_safe_memory_swallows_errors():
    class _Boom:
        async def recent(self, **k):
            raise RuntimeError("zep down")

        async def recall_episodic(self, **k):
            raise RuntimeError("zep down")

    sm = bench._SafeMemory(_Boom())
    assert asyncio.run(sm.recent("s")) == []
    assert asyncio.run(sm.recall_episodic("s", "q")) == []


def test_safe_memory_passthrough():
    class _Inner:
        async def recent(self, session_id, k=5):
            return ["a"]

        async def recall_episodic(self, session_id, query, k=5):
            return ["b"]

    sm = bench._SafeMemory(_Inner())
    assert asyncio.run(sm.recent("s")) == ["a"]
    assert asyncio.run(sm.recall_episodic("s", "q")) == ["b"]


def test_null_memory():
    nm = bench._NullMemory()
    assert asyncio.run(nm.recent("s")) == []
    assert asyncio.run(nm.recall_episodic("s", "q")) == []


# ============================================================================
# agentlab._classify — real body, every branch (deps monkeypatched)
# ============================================================================
class _TC:
    def __init__(self, tier, blocked, needs_llm, reason="r", signals=None):
        self.tier, self.blocked, self.needs_llm = tier, blocked, needs_llm
        self.reason, self.signals = reason, signals or {}


def _patch_classify_deps(monkeypatch, tc, verdict="__keep__"):
    async def _resolve(meta):
        return None

    monkeypatch.setattr(agentlab, "_resolve_signals", _resolve)
    monkeypatch.setattr(agentlab, "classify_trust", lambda meta: tc)
    if verdict != "__keep__":

        async def _certify(doc, curator, router, session):
            return verdict

        monkeypatch.setattr(agentlab, "_certify_llm", _certify)


def _classify(app, **kw):
    return asyncio.run(agentlab._classify(app, **kw))


def test_classify_no_llm_needed(monkeypatch):
    _patch_classify_deps(monkeypatch, _TC("preprint", False, False))
    out = _classify(make_app(), arxiv_id="1706.03762")
    assert out["tier"] == "preprint"
    assert out["used_llm"] is False


def test_classify_blocked_skips_llm(monkeypatch):
    _patch_classify_deps(monkeypatch, _TC("quarantined", True, True))
    out = _classify(make_app(), url="http://blog")
    assert out["used_llm"] is False
    assert out["blocked"] is True


def test_classify_llm_block(monkeypatch):
    v = SimpleNamespace(decision="block", reasons="spam", tier="quarantined")
    _patch_classify_deps(monkeypatch, _TC("web_unknown", False, True), verdict=v)
    out = _classify(make_app(), url="http://blog")
    assert out["used_llm"] is True
    assert out["tier"] == "quarantined" and out["blocked"] is True
    assert out["reason"] == "spam"


def test_classify_llm_allow(monkeypatch):
    v = SimpleNamespace(decision="allow", reasons="credible", tier="web_reputable")
    _patch_classify_deps(monkeypatch, _TC("web_unknown", False, True), verdict=v)
    out = _classify(make_app(), url="http://blog")
    assert out["used_llm"] is True
    assert out["tier"] == "web_reputable" and out["blocked"] is False


def test_classify_llm_verdict_none(monkeypatch):
    _patch_classify_deps(monkeypatch, _TC("web_unknown", False, True), verdict=None)
    out = _classify(make_app(), url="http://blog")
    assert out["used_llm"] is False
    assert out["tier"] == "web_unknown"


def test_classify_needs_llm_but_run_llm_false(monkeypatch):
    _patch_classify_deps(monkeypatch, _TC("web_unknown", False, True))
    out = _classify(make_app(), url="http://blog", run_llm=False)
    assert out["used_llm"] is False  # run_llm=False short-circuits


# ============================================================================
# agentlab._resolve_model + _scout_result direct
# ============================================================================
def test_resolve_model_premium_and_local():
    router = FakeRouter(premium=[_ChainProvider(Provider.DEEPSEEK, "deepseek-chat")])
    eng = make_engine(router=router)
    prov, name, tier = agentlab._resolve_model("pi.claim_verdict", eng)  # REASONING → premium
    assert prov == Provider.DEEPSEEK and name == "deepseek-chat" and tier == "reasoning"
    prov2, _, tier2 = agentlab._resolve_model("researcher.execute_task", eng)  # CODE → local
    assert prov2 == Provider.OLLAMA and tier2 == "code"


def test_resolve_model_premium_empty_falls_back():
    eng = make_engine(router=FakeRouter(premium=[]))
    prov, _, tier = agentlab._resolve_model("totally.unknown", eng)  # WORKHORSE default, no premium
    assert tier == "workhorse" and prov == Provider.OLLAMA


def test_scout_result_branches():
    good = [
        SourceDescriptor(
            kind="paper", source_kind="arxiv", canonical_key="k", url="u", arxiv_id="k", doi=None, title="t", why="w"
        )
    ]
    out = agentlab._scout_result("arxiv", good)
    assert out["status"] == "pass" and "1 sources" in out["actual"]

    empty = agentlab._scout_result("web", [])
    assert empty["status"] == "fail" and "0 results" in empty["note"]

    bad = [
        SourceDescriptor(
            kind="web", source_kind="web", canonical_key="", url="u", arxiv_id=None, doi=None, title="t", why="w"
        )
    ]  # empty key → not well-formed
    assert agentlab._scout_result("web", bad)["status"] == "fail"


# ============================================================================
# agentlab — mimir suite case functions + dispatch fallthroughs + _mimir_state
# ============================================================================
def test_mimir_suite_run_all_cases(monkeypatch):
    # Mock every external dependency the mimir cases touch so each runs deterministically.
    async def _classify(app, **kw):
        url = kw.get("url") or ""
        arxiv = kw.get("arxiv_id")
        lic = kw.get("license")
        doi = kw.get("doi")
        if arxiv:
            return {
                "tier": "preprint",
                "blocked": False,
                "needs_llm": False,
                "used_llm": False,
                "reason": "arxiv",
                "signals": {},
            }
        if doi:
            return {
                "tier": "peer_reviewed",
                "blocked": False,
                "needs_llm": False,
                "used_llm": False,
                "reason": "doi",
                "signals": {},
            }
        if lic:
            return {
                "tier": "quarantined",
                "blocked": True,
                "needs_llm": False,
                "used_llm": False,
                "reason": "license",
                "signals": {},
            }
        if "github.com" in url:
            return {
                "tier": "official_repo",
                "blocked": False,
                "needs_llm": False,
                "used_llm": False,
                "reason": "gh",
                "signals": {"stars": 99},
            }
        if "wikipedia" in url:
            return {
                "tier": "web_reputable",
                "blocked": False,
                "needs_llm": False,
                "used_llm": False,
                "reason": "wiki",
                "signals": {},
            }
        # unknown blog → needs_llm path
        return {
            "tier": "web_unknown",
            "blocked": False,
            "needs_llm": True,
            "used_llm": True,
            "reason": "ambiguous",
            "signals": {},
        }

    async def _withdrawn(arxiv_id):
        return True

    class _DupState:
        def __init__(self):
            self.pool = ScriptedPool(rules=[("canonical_key FROM documents", "2401.0001")])

        async def document_exists(self, kind, key):
            return True

    async def _state(app):
        return _DupState()

    monkeypatch.setattr(agentlab, "_classify", _classify)
    monkeypatch.setattr(agentlab, "_arxiv_withdrawn", _withdrawn)
    monkeypatch.setattr(agentlab, "_mimir_state", _state)

    app = make_app()
    body = TestClient(app).post("/agentlab/suite/run", json={"agent": "mimir"}).json()
    by_id = {r["id"]: r for r in body["results"]}
    assert by_id["good_arxiv"]["status"] == "pass"
    assert by_id["peer_reviewed"]["status"] == "pass"
    assert by_id["good_github"]["status"] == "pass"
    assert by_id["web_reputable"]["status"] == "pass"
    assert by_id["unknown_blog"]["status"] == "pass"
    assert by_id["restrictive_license"]["status"] == "pass"
    assert by_id["retracted"]["status"] == "pass"
    assert by_id["duplicate"]["status"] == "pass"


def test_mimir_suite_failing_branches(monkeypatch):
    # Drive the "fail/note" arms: wrong tiers, no withdrawal, no duplicate.
    async def _classify(app, **kw):
        return {
            "tier": "web_unknown",
            "blocked": False,
            "needs_llm": False,
            "used_llm": False,
            "reason": "x",
            "signals": {},
        }

    async def _withdrawn(arxiv_id):
        return False  # live detection unconfirmed (gate still passes)

    class _NoDupState:
        def __init__(self):
            self.pool = ScriptedPool(rules=[("canonical_key FROM documents", None)])

        async def document_exists(self, kind, key):
            return False

    async def _state(app):
        return _NoDupState()

    monkeypatch.setattr(agentlab, "_classify", _classify)
    monkeypatch.setattr(agentlab, "_arxiv_withdrawn", _withdrawn)
    monkeypatch.setattr(agentlab, "_mimir_state", _state)

    app = make_app()
    body = TestClient(app).post("/agentlab/suite/run", json={"agent": "mimir"}).json()
    by_id = {r["id"]: r for r in body["results"]}
    assert by_id["good_arxiv"]["status"] == "fail"  # tier != preprint
    assert by_id["good_github"]["status"] == "fail"
    assert by_id["good_github"]["note"]  # note populated on the fail branch
    assert by_id["retracted"]["status"] == "pass"  # gate is deterministic regardless of live probe
    assert by_id["duplicate"]["status"] == "fail"  # no document found


def test_run_mimir_unknown_action_fallthrough(monkeypatch):
    async def _state(app):
        return object()

    monkeypatch.setattr(agentlab, "_mimir_state", _state)
    out = asyncio.run(agentlab._run_mimir(make_app(), {"action": "bogus"}, {}))
    assert out["status"] == "error"
    assert "unknown mimir action" in out["error"]


def test_run_collectors_unknown_action_fallthrough():
    out = asyncio.run(agentlab._run_collectors(make_app(), {"action": "bogus"}, {}))
    assert out["status"] == "error"
    assert "unknown collectors action" in out["error"]


def test_mimir_init_registers_codecs():
    calls = []

    class _Conn:
        async def set_type_codec(self, *a, **k):
            calls.append(("codec", a, k))

    def _fake_register(conn):
        calls.append(("register", conn))

        async def _noop():
            return None

        return _noop()

    import pgvector.asyncpg as pg

    orig = pg.register_vector
    pg.register_vector = _fake_register
    try:
        asyncio.run(agentlab._mimir_init(_Conn()))
    finally:
        pg.register_vector = orig
    assert any(c[0] == "codec" for c in calls)
    assert any(c[0] == "register" for c in calls)


def test_mimir_state_cached():
    # When app.state.agentlab_mimir_state is preset, _mimir_state returns it without
    # touching asyncpg.create_pool (no DATABASE_URL needed).
    app = make_app()
    sentinel = object()
    app.state.agentlab_mimir_state = sentinel
    assert asyncio.run(agentlab._mimir_state(app)) is sentinel


def test_scout_arxiv_case_retries(monkeypatch):
    # _c_scout_arxiv retries once on an empty first result; cover the sleep+retry arm.
    calls = {"n": 0}

    async def _scout(topics, per_topic=4):
        calls["n"] += 1
        return (
            []
            if calls["n"] == 1
            else [
                SourceDescriptor(
                    kind="paper",
                    source_kind="arxiv",
                    canonical_key="k",
                    url="u",
                    arxiv_id="k",
                    doi=None,
                    title="t",
                    why="w",
                )
            ]
        )

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(agentlab, "scout_arxiv", _scout)
    monkeypatch.setattr(agentlab.asyncio, "sleep", _no_sleep)
    out = asyncio.run(agentlab._c_scout_arxiv(make_app()))
    assert out["status"] == "pass"
    assert calls["n"] == 2  # retried after the empty first attempt


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
