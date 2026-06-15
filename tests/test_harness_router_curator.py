"""Unit tests for harness.router and harness.curator — fully mocked.

No real Postgres / Ollama / DeepSeek / network. The DB is a ScriptedPool from
tests._helpers; provider calls are monkeypatched on the Router instance or
driven through a faked httpx.AsyncClient.post/get; asyncio.sleep is a no-op so
any backoff is instant.

Coverage targets: harness.router (build_cloud_chain / build_premium_chain /
tier routing / Router.invoke success+failover+all-fail+cost-cap-degrade /
_record_cost OLLAMA-skip / agent_run insert+update / langfuse trace) and
harness.curator (constitution layer with NO charter / system-prompt build /
recall / lessons / budget enforcement).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

import harness.router as router_mod
from harness.curator import (
    BuiltPrompt,
    Curator,
    PromptLayer,
    _build_exploration_kickoff_task_data,
    _build_mimir_certify_task_data,
    _build_researcher_task_data,
)
from harness.router import (
    MODELS,
    CloudProvider,
    CostCapExceeded,
    GPULock,
    Provider,
    Router,
    Tier,
    build_cloud_chain,
    build_premium_chain,
)
from tests._helpers import ScriptedPool


class _Out(BaseModel):
    x: int


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make any asyncio.sleep instant so backoff never wall-clocks the tests."""

    async def _instant(*_a, **_kw):
        return None

    monkeypatch.setattr(router_mod.asyncio, "sleep", _instant, raising=False)


@pytest.fixture(autouse=True)
def _no_identity_registry(monkeypatch):
    """These curator tests assert the SYSTEM_PROMPTS code anchors, i.e. the no-registry fallback.
    The positive persona-resolution path (registry row → system layer) is covered in
    tests/test_agent_identity.py. Neutralise the lookup so an AsyncMock pool can't feed a
    Mock-shaped persona into the system message."""

    async def _none(*_a, **_kw):
        return None

    monkeypatch.setattr("harness.curator.persona_for", _none, raising=False)


def _prompt(invocation_type: str = "pi.exploration_kickoff", *, tokens: int = 42) -> BuiltPrompt:
    """A minimal BuiltPrompt the Router can persist + render."""
    return BuiltPrompt(
        layers=[PromptLayer(name="system", content="be terse", priority=0)],
        tool_names=[],
        output_schema="_Out",
        lesson_ids=[],
        total_tokens=tokens,
        invocation_type=invocation_type,
    )


def _router(
    *,
    cloud: bool = False,
    premium: bool = False,
    pool: ScriptedPool | None = None,
    langfuse=None,
) -> Router:
    cloud_chain = [CloudProvider(Provider.GEMINI, "http://example", "key", "test-model", "json_schema")] if cloud else []
    premium_chain = (
        [CloudProvider(Provider.DEEPSEEK, "https://api.deepseek.com", "k", "deepseek-v4-flash", "json_object")]
        if premium
        else []
    )
    return Router(
        pool=pool if pool is not None else ScriptedPool(),
        gpu_lock=GPULock(),
        cloud_chain=cloud_chain,
        premium_chain=premium_chain,
        langfuse_client=langfuse,
    )


# ---------------------------------------------------------------------------
# build_cloud_chain / build_premium_chain — the policy lock
# ---------------------------------------------------------------------------


def test_build_cloud_chain_always_empty():
    """Lab policy: free cloud chain is intentionally empty (Gemini/Groq/etc dropped)."""
    assert build_cloud_chain({}) == []
    assert build_cloud_chain({"GEMINI_API_KEY": "x", "GROQ_API_KEY": "y"}) == []


def test_build_premium_chain_deepseek_only_with_key():
    chain = build_premium_chain({"DEEPSEEK_API_KEY": "sk-xyz"})
    assert len(chain) == 1
    cp = chain[0]
    assert cp.provider == Provider.DEEPSEEK
    assert cp.base_url == "https://api.deepseek.com"
    assert cp.api_key == "sk-xyz"
    assert cp.model_name == "deepseek-v4-flash"  # default model
    assert cp.structured_mode == "json_object"


def test_build_premium_chain_respects_model_override():
    chain = build_premium_chain({"DEEPSEEK_API_KEY": "sk", "DEEPSEEK_MODEL": "deepseek-reasoner"})
    assert chain[0].model_name == "deepseek-reasoner"


def test_build_premium_chain_empty_without_key():
    assert build_premium_chain({}) == []
    assert build_premium_chain({"DEEPSEEK_MODEL": "deepseek-reasoner"}) == []


# ---------------------------------------------------------------------------
# Tier routing / _call_order / _downgrade
# ---------------------------------------------------------------------------


def test_route_table_resolves_known_invocation_types():
    assert router_mod.ROUTE["pi.claim_verdict"] == Tier.REASONING
    assert router_mod.ROUTE["planner.generate_tasks"] == Tier.WORKHORSE
    assert router_mod.ROUTE["evaluation.relevance_verify"] == Tier.FAST
    assert router_mod.ROUTE["evaluation.audit_finding"] == Tier.WORKHORSE  # verification spine (Aletheia)
    # extract_evidence moved CODE -> WORKHORSE (2026-06-10) so researchers run on
    # cloud and parallelize instead of queueing on the local GPU lock.
    assert router_mod.ROUTE["researcher.extract_evidence"] == Tier.WORKHORSE
    assert router_mod.ROUTE["researcher.parse_pricing"] == Tier.CODE  # a still-local CODE route


def test_call_order_local_only_when_no_chains():
    r = _router()
    for tier in (Tier.REASONING, Tier.WORKHORSE, Tier.FAST, Tier.CODE):
        order = r._call_order(tier)
        assert [s.provider for s in order] == [Provider.OLLAMA]
        assert order[0].model_name == MODELS[tier].model_name
    assert r.cloud_enabled is False


def test_call_order_premium_leads_for_premium_tiers_only():
    r = _router(premium=True)
    # REASONING is a premium tier → DeepSeek leads, then local.
    assert [s.provider for s in r._call_order(Tier.REASONING)] == [Provider.DEEPSEEK, Provider.OLLAMA]
    assert [s.provider for s in r._call_order(Tier.WORKHORSE)] == [Provider.DEEPSEEK, Provider.OLLAMA]
    # FAST is NOT a premium tier → local only (cloud chain empty under policy).
    assert [s.provider for s in r._call_order(Tier.FAST)] == [Provider.OLLAMA]


def test_call_order_code_is_local_first():
    """CODE deliberately leads local even when a cloud chain exists."""
    r = _router(cloud=True)
    order = r._call_order(Tier.CODE)
    assert order[0].provider == Provider.OLLAMA
    assert order[-1].provider == Provider.GEMINI


def test_call_order_cloud_specs_widen_context_limit():
    r = _router(cloud=True)
    spec = next(s for s in r._call_order(Tier.WORKHORSE) if s.provider == Provider.GEMINI)
    assert spec.context_limit >= 128_000


def test_downgrade_only_reasoning():
    r = _router()
    assert r._downgrade(Tier.REASONING) == Tier.WORKHORSE
    assert r._downgrade(Tier.WORKHORSE) is None
    assert r._downgrade(Tier.FAST) is None
    assert r._downgrade(Tier.CODE) is None


def test_provider_cfg_built_from_both_chains():
    r = _router(cloud=True, premium=True)
    assert Provider.GEMINI in r._provider_cfg
    assert Provider.DEEPSEEK in r._provider_cfg
    assert r.cloud_enabled is True


# ---------------------------------------------------------------------------
# _record_cost / _calls_today / _summarize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_cost_inserts_call_row():
    r = _router()
    pool = ScriptedPool()
    async with pool.acquire() as conn:
        await r._record_cost(conn, Tier.WORKHORSE, in_toks=1000, out_toks=500)
    execs = [c for c in pool.calls if c[0] == "execute"]
    assert execs, "expected a cost_tracking INSERT"
    sql = execs[-1][1]
    assert "INSERT INTO cost_tracking" in sql
    assert "workhorse_calls" in sql


@pytest.mark.asyncio
async def test_calls_today_returns_value_and_zero_default():
    r = _router()
    pool = ScriptedPool(rules=[("reasoning_calls", 7)])
    async with pool.acquire() as conn:
        assert await r._calls_today(conn, Tier.REASONING) == 7
    pool2 = ScriptedPool()  # fetchval default None → coerced to 0
    async with pool2.acquire() as conn:
        assert await r._calls_today(conn, Tier.FAST) == 0


def test_summarize_truncates_long_output():
    r = _router()

    class _Big(BaseModel):
        s: str

    short = r._summarize(_Big(s="hi"))
    assert "…" not in short
    long = r._summarize(_Big(s="z" * 2000))
    assert long.endswith("…")
    assert len(long) <= 501


# ---------------------------------------------------------------------------
# _emit_cap_hit — telemetry must never raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_cap_hit_inserts_event_and_flag():
    r = _router()
    pool = ScriptedPool()
    async with pool.acquire() as conn:
        await r._emit_cap_hit(conn, Tier.REASONING, used_today=50)
    sqls = " ".join(c[1] for c in pool.calls if c[0] == "execute")
    assert "cost.cap_reached" in sqls
    assert "cap_reached = TRUE" in sqls


@pytest.mark.asyncio
async def test_emit_cap_hit_swallows_db_error():
    r = _router()
    conn = AsyncMock()
    conn.execute.side_effect = RuntimeError("db gone")
    # Must not propagate — telemetry is best-effort.
    await r._emit_cap_hit(conn, Tier.REASONING, used_today=99)


# ---------------------------------------------------------------------------
# Router.invoke — full path
# ---------------------------------------------------------------------------


def _invoke_pool(*, used_today: int = 0, run_id: int = 1234) -> ScriptedPool:
    """A pool that answers the cap read + the agent_runs INSERT ... RETURNING id."""
    return ScriptedPool(
        rules=[
            (f"{Tier.WORKHORSE.value}_calls FROM cost_tracking", used_today),
            (f"{Tier.REASONING.value}_calls FROM cost_tracking", used_today),
            ("RETURNING id", run_id),
        ]
    )


@pytest.mark.asyncio
async def test_invoke_unknown_invocation_type_raises():
    r = _router()
    with pytest.raises(ValueError, match="No route"):
        await r.invoke(_prompt(invocation_type="does.not.exist"), _Out)


@pytest.mark.asyncio
async def test_invoke_primary_success_persists_run_and_cost():
    pool = _invoke_pool(run_id=55)
    r = _router(premium=True, pool=pool)

    async def fake_cloud(spec, system, schema):
        return '{"x": 9}', 12

    r._call_openai_compatible = fake_cloud  # type: ignore[method-assign]
    # WORKHORSE is a premium tier → DeepSeek leads, so the cloud path wins.
    parsed, rid = await r.invoke(_prompt("planner.generate_tasks"), _Out)
    assert parsed.x == 9
    assert rid == 55

    sqls = [c[1] for c in pool.calls]
    assert any("INSERT INTO agent_runs" in s for s in sqls)
    assert any("UPDATE agent_runs" in s and "completed" in s for s in sqls)
    # DeepSeek is cloud → cost recorded.
    assert any("INSERT INTO cost_tracking" in s for s in sqls)


@pytest.mark.asyncio
async def test_invoke_records_input_and_output_summaries():
    pool = _invoke_pool(run_id=7)
    r = _router(pool=pool)

    async def fake_ollama(spec, system, schema):
        return '{"x": 3}', 4

    r._call_ollama = fake_ollama  # type: ignore[method-assign]
    await r.invoke(_prompt("planner.generate_tasks"), _Out)

    insert = next(c for c in pool.calls if c[0] == "fetchval" and "INSERT INTO agent_runs" in c[1])
    # input_summary (the assembled system text) is persisted on the row.
    assert "be terse" in " ".join(str(a) for a in insert[2])
    update = next(c for c in pool.calls if c[0] == "execute" and "UPDATE agent_runs" in c[1] and "completed" in c[1])
    # output_summary is the canonical JSON of the parsed model.
    assert any('"x": 3' in str(a) for a in update[2])


@pytest.mark.asyncio
async def test_invoke_local_ollama_not_counted_against_cap():
    """WORKHORSE leads local here (no premium chain) → provider OLLAMA → no cost row."""
    pool = _invoke_pool()
    r = _router(pool=pool)

    async def fake_ollama(spec, system, schema):
        return '{"x": 1}', 2

    r._call_ollama = fake_ollama  # type: ignore[method-assign]
    await r.invoke(_prompt("planner.generate_tasks"), _Out)
    assert not any("INSERT INTO cost_tracking" in c[1] for c in pool.calls if c[0] == "execute")


@pytest.mark.asyncio
async def test_invoke_failover_cloud_to_local():
    pool = _invoke_pool(run_id=88)
    r = _router(premium=True, pool=pool)

    async def boom_cloud(spec, system, schema):
        raise RuntimeError("429 rate limited")

    async def fake_ollama(spec, system, schema):
        return '{"x": 2}', 3

    r._call_openai_compatible = boom_cloud  # type: ignore[method-assign]
    r._call_ollama = fake_ollama  # type: ignore[method-assign]
    parsed, _rid = await r.invoke(_prompt("planner.generate_tasks"), _Out)
    assert parsed.x == 2
    # The winner is local OLLAMA → no cost; fallback_attempts records the cloud miss.
    update = next(c for c in pool.calls if c[0] == "execute" and "fallback_attempts" in c[1])
    assert "429 rate limited" in " ".join(str(a) for a in update[2])
    assert not any("INSERT INTO cost_tracking" in c[1] for c in pool.calls if c[0] == "execute")


@pytest.mark.asyncio
async def test_invoke_all_fail_marks_run_failed_and_raises():
    pool = _invoke_pool(run_id=42)
    r = _router(premium=True, pool=pool)

    async def boom(spec, system, schema):
        raise RuntimeError("ollama down")

    r._call_openai_compatible = boom  # type: ignore[method-assign]
    r._call_ollama = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="ollama down"):
        await r.invoke(_prompt("planner.generate_tasks"), _Out)
    fail = next(c for c in pool.calls if c[0] == "execute" and "status = 'failed'" in c[1])
    assert "ollama down" in " ".join(str(a) for a in fail[2])


@pytest.mark.asyncio
async def test_invoke_reasoning_cap_downgrades_to_workhorse_scaffold():
    """REASONING capped → downgrade to WORKHORSE and append the reasoning scaffold."""
    pool = ScriptedPool(
        rules=[
            ("reasoning_calls FROM cost_tracking", 999),  # over the 50/day cap
            ("RETURNING id", 1),
        ]
    )
    r = _router(premium=True, pool=pool)
    seen = {}

    async def fake_cloud(spec, system, schema):
        seen["system"] = system
        return '{"x": 5}', 1

    r._call_openai_compatible = fake_cloud  # type: ignore[method-assign]
    parsed, _rid = await r.invoke(_prompt("pi.claim_verdict"), _Out)
    assert parsed.x == 5
    assert "Reasoning scaffold" in seen["system"]
    # Downgraded tier is recorded on the row as workhorse.
    insert = next(c for c in pool.calls if c[0] == "fetchval" and "INSERT INTO agent_runs" in c[1])
    assert Tier.WORKHORSE.value in [str(a) for a in insert[2]]
    # Cap-hit telemetry fired.
    assert any("cost.cap_reached" in c[1] for c in pool.calls if c[0] == "execute")


@pytest.mark.asyncio
async def test_invoke_cap_no_downgrade_degrades_to_local_only():
    """FAST capped (no downgrade path) → local-only: cloud is dropped from specs."""
    pool = ScriptedPool(
        rules=[
            ("fast_calls FROM cost_tracking", 999_999),
            ("RETURNING id", 1),
        ]
    )
    r = _router(cloud=True, pool=pool)
    calls = {"cloud": 0, "ollama": 0}

    async def fake_cloud(spec, system, schema):
        calls["cloud"] += 1
        return '{"x": 1}', 1

    async def fake_ollama(spec, system, schema):
        calls["ollama"] += 1
        return '{"x": 2}', 1

    r._call_openai_compatible = fake_cloud  # type: ignore[method-assign]
    r._call_ollama = fake_ollama  # type: ignore[method-assign]
    parsed, _rid = await r.invoke(_prompt("evaluation.relevance_verify"), _Out)
    assert parsed.x == 2
    assert calls == {"cloud": 0, "ollama": 1}  # cloud never attempted under local-only


@pytest.mark.asyncio
async def test_invoke_writes_lesson_applications():
    pool = _invoke_pool(run_id=321)
    r = _router(pool=pool)
    p = _prompt("planner.generate_tasks")
    p.lesson_ids = [11, 22]

    async def fake_ollama(spec, system, schema):
        return '{"x": 1}', 1

    r._call_ollama = fake_ollama  # type: ignore[method-assign]
    await r.invoke(p, _Out)
    lesson_inserts = [c for c in pool.calls if c[0] == "execute" and "lesson_applications" in c[1]]
    assert len(lesson_inserts) == 2
    assert {c[2][0] for c in lesson_inserts} == {11, 22}


# ---------------------------------------------------------------------------
# Langfuse tracing
# ---------------------------------------------------------------------------


def _fake_langfuse(*, trace_id="trace-abc", fail_start=False):
    lf = MagicMock()
    span = MagicMock()
    span.trace_id = trace_id
    if fail_start:
        lf.start_observation.side_effect = RuntimeError("lf boom")
    else:
        lf.start_observation.return_value = span
    return lf, span


@pytest.mark.asyncio
async def test_invoke_persists_langfuse_trace_id():
    pool = _invoke_pool(run_id=9)
    lf, span = _fake_langfuse(trace_id="tr-xyz")
    r = _router(pool=pool, langfuse=lf)

    async def fake_ollama(spec, system, schema):
        return '{"x": 1}', 1

    r._call_ollama = fake_ollama  # type: ignore[method-assign]
    await r.invoke(_prompt("planner.generate_tasks"), _Out)
    insert = next(c for c in pool.calls if c[0] == "fetchval" and "INSERT INTO agent_runs" in c[1])
    assert "tr-xyz" in [str(a) for a in insert[2]]
    span.update.assert_called()
    span.end.assert_called()


@pytest.mark.asyncio
async def test_invoke_langfuse_start_failure_is_swallowed():
    pool = _invoke_pool(run_id=9)
    lf, _span = _fake_langfuse(fail_start=True)
    r = _router(pool=pool, langfuse=lf)

    async def fake_ollama(spec, system, schema):
        return '{"x": 1}', 1

    r._call_ollama = fake_ollama  # type: ignore[method-assign]
    parsed, _rid = await r.invoke(_prompt("planner.generate_tasks"), _Out)
    assert parsed.x == 1  # run still completes; trace_id ends up NULL
    insert = next(c for c in pool.calls if c[0] == "fetchval" and "INSERT INTO agent_runs" in c[1])
    assert None in insert[2]  # trace_id arg is None


@pytest.mark.asyncio
async def test_invoke_langfuse_error_path_updates_span():
    pool = _invoke_pool(run_id=9)
    lf, span = _fake_langfuse()
    r = _router(pool=pool, langfuse=lf)

    async def boom(spec, system, schema):
        raise RuntimeError("ollama down")

    r._call_ollama = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await r.invoke(_prompt("planner.generate_tasks"), _Out)
    # On failure the span is updated with the ERROR level and ended.
    assert any(kw.get("level") == "ERROR" for _a, kw in span.update.call_args_list)


# ---------------------------------------------------------------------------
# _maybe_langfuse
# ---------------------------------------------------------------------------


def test_maybe_langfuse_none_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert router_mod._maybe_langfuse() is None


def test_maybe_langfuse_handles_init_failure(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    # Force the Langfuse constructor to raise so the except branch returns None
    # (the SDK is installed in this env, so we can't rely on an ImportError).
    import langfuse

    def _boom(*_a, **_kw):
        raise RuntimeError("langfuse init exploded")

    monkeypatch.setattr(langfuse, "Langfuse", _boom)
    assert router_mod._maybe_langfuse() is None


def test_maybe_langfuse_returns_client_on_success(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    import langfuse

    sentinel = object()
    monkeypatch.setattr(langfuse, "Langfuse", lambda **_kw: sentinel)
    assert router_mod._maybe_langfuse() is sentinel


# ---------------------------------------------------------------------------
# Session linkage on invoke
# ---------------------------------------------------------------------------


def _fake_session(*, sid=5, mode="live"):
    s = MagicMock()
    s.id = sid
    s.mode = mode
    s.last_step_id = None
    s.next_step_order.return_value = 1
    s.emit_event = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_invoke_with_session_emits_step_events():
    pool = _invoke_pool(run_id=77)
    r = _router(pool=pool)
    sess = _fake_session()

    async def fake_ollama(spec, system, schema):
        return '{"x": 1}', 1

    r._call_ollama = fake_ollama  # type: ignore[method-assign]
    await r.invoke(_prompt("planner.generate_tasks"), _Out, session=sess, step_name="plan")
    events = [c.kwargs["event_type"] for c in sess.emit_event.call_args_list]
    assert "step.started" in events
    assert "step.completed" in events
    assert sess.last_step_id == 77


@pytest.mark.asyncio
async def test_invoke_replay_session_skips_cost():
    pool = _invoke_pool(run_id=77)
    r = _router(premium=True, pool=pool)
    sess = _fake_session(mode="replay")

    async def fake_cloud(spec, system, schema):
        return '{"x": 1}', 1

    r._call_openai_compatible = fake_cloud  # type: ignore[method-assign]
    await r.invoke(_prompt("planner.generate_tasks"), _Out, session=sess)
    # Replay → cost tracking skipped even though DeepSeek (cloud) won.
    assert not any("INSERT INTO cost_tracking" in c[1] for c in pool.calls if c[0] == "execute")


@pytest.mark.asyncio
async def test_invoke_session_failure_emits_step_failed():
    pool = _invoke_pool(run_id=77)
    r = _router(pool=pool)
    sess = _fake_session()

    async def boom(spec, system, schema):
        raise RuntimeError("kaboom")

    r._call_ollama = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await r.invoke(_prompt("planner.generate_tasks"), _Out, session=sess, step_name="plan")
    events = [c.kwargs["event_type"] for c in sess.emit_event.call_args_list]
    assert "step.failed" in events


# ---------------------------------------------------------------------------
# Provider call shapes (_call_openai_compatible / _call_ollama)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_call_openai_compatible_json_object_mode_embeds_schema():
    premium = [CloudProvider(Provider.DEEPSEEK, "https://api.deepseek.com/", "k", "ds", "json_object")]
    r = Router(pool=ScriptedPool(), gpu_lock=GPULock(), premium_chain=premium)
    captured = {}

    async def fake_post(url, headers=None, json=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp({"choices": [{"message": {"content": '{"x": 1}'}}], "usage": {"completion_tokens": 8}})

    r._http.post = fake_post  # type: ignore[method-assign]
    spec = r._call_order(Tier.REASONING)[0]
    text, toks = await r._call_openai_compatible(spec, "do the thing", _Out)
    assert text == '{"x": 1}'
    assert toks == 8
    # Trailing slash stripped, single /chat/completions appended.
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["json"]["response_format"] == {"type": "json_object"}
    # json_object mode appends the schema to the system message.
    assert "conforms to this schema" in captured["json"]["messages"][0]["content"]
    assert captured["headers"]["Authorization"] == "Bearer k"
    await r.close()


@pytest.mark.asyncio
async def test_call_openai_compatible_json_schema_mode():
    cloud = [CloudProvider(Provider.GEMINI, "http://g", "k", "m", "json_schema")]
    r = Router(pool=ScriptedPool(), gpu_lock=GPULock(), cloud_chain=cloud)

    async def fake_post(url, headers=None, json=None):
        # Verify strict structured-output request shape.
        assert json["response_format"]["type"] == "json_schema"
        assert json["temperature"] == pytest.approx(0.4)  # WORKHORSE temp
        return _Resp({"choices": [{"message": {"content": '{"x": 2}'}}]})

    r._http.post = fake_post  # type: ignore[method-assign]
    spec = next(s for s in r._call_order(Tier.WORKHORSE) if s.provider == Provider.GEMINI)
    text, toks = await r._call_openai_compatible(spec, "sys", _Out)
    assert text == '{"x": 2}'
    assert toks == 0  # no usage block → default 0
    await r.close()


@pytest.mark.asyncio
async def test_call_openai_compatible_omits_temperature_when_disabled():
    """send_temperature=False (GPT-5/o-series) → temperature is not in the payload."""
    cloud = [CloudProvider(Provider.GEMINI, "http://g", "k", "m", "json_schema", False)]
    r = Router(pool=ScriptedPool(), gpu_lock=GPULock(), cloud_chain=cloud)
    captured = {}

    async def fake_post(url, headers=None, json=None):
        captured["json"] = json
        return _Resp({"choices": [{"message": {"content": '{"x": 1}'}}], "usage": {"completion_tokens": 2}})

    r._http.post = fake_post  # type: ignore[method-assign]
    spec = next(s for s in r._call_order(Tier.WORKHORSE) if s.provider == Provider.GEMINI)
    await r._call_openai_compatible(spec, "sys", _Out)
    assert "temperature" not in captured["json"]
    await r.close()


@pytest.mark.asyncio
async def test_invoke_langfuse_update_exception_is_swallowed():
    """If lf_span.update / end raise on the success path, the run still completes."""
    pool = _invoke_pool(run_id=9)
    lf, span = _fake_langfuse()
    span.update.side_effect = RuntimeError("span update boom")
    r = _router(pool=pool, langfuse=lf)

    async def fake_ollama(spec, system, schema):
        return '{"x": 1}', 1

    r._call_ollama = fake_ollama  # type: ignore[method-assign]
    parsed, _rid = await r.invoke(_prompt("planner.generate_tasks"), _Out)
    assert parsed.x == 1  # swallowed → run completes


@pytest.mark.asyncio
async def test_invoke_langfuse_error_update_exception_is_swallowed():
    """On the failure path, a raising lf_span.update must not mask the real error."""
    pool = _invoke_pool(run_id=9)
    lf, span = _fake_langfuse()
    span.update.side_effect = RuntimeError("span update boom")
    r = _router(pool=pool, langfuse=lf)

    async def boom(spec, system, schema):
        raise RuntimeError("real failure")

    r._call_ollama = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="real failure"):
        await r.invoke(_prompt("planner.generate_tasks"), _Out)


@pytest.mark.asyncio
async def test_call_ollama_posts_and_parses():
    r = _router()

    async def fake_post(url, json=None):
        assert url.endswith("/api/chat")
        assert json["stream"] is False
        assert json["format"] == _Out.model_json_schema()
        return _Resp({"message": {"content": '{"x": 4}'}, "eval_count": 11})

    r._http.post = fake_post  # type: ignore[method-assign]
    spec = MODELS[Tier.FAST]
    text, toks = await r._call_ollama(spec, "sys", _Out)
    assert text == '{"x": 4}'
    assert toks == 11
    await r.close()


@pytest.mark.asyncio
async def test_run_single_ollama_and_cloud():
    premium = [CloudProvider(Provider.DEEPSEEK, "https://api.deepseek.com", "k", "ds", "json_object")]
    r = Router(pool=ScriptedPool(), gpu_lock=GPULock(), premium_chain=premium)

    async def fake_ollama(model, system, schema):
        return '{"x": 1}', 5

    async def fake_cloud(model, system, schema):
        return '{"x": 2}', 6

    r._call_ollama = fake_ollama  # type: ignore[method-assign]
    r._call_openai_compatible = fake_cloud  # type: ignore[method-assign]
    p = _prompt()
    out_local = await r.run_single(p, _Out, Provider.OLLAMA, "qwen3:14b")
    assert out_local == ('{"x": 1}', 5)
    out_cloud = await r.run_single(p, _Out, Provider.DEEPSEEK, "ds")
    assert out_cloud == ('{"x": 2}', 6)
    await r.close()


# ---------------------------------------------------------------------------
# GPULock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gpu_lock_tracks_in_flight():
    lock = GPULock(max_in_flight=2)
    async with lock.acquire("qwen3:14b"):
        assert lock._in_flight["qwen3:14b"] == 1
    assert lock._in_flight["qwen3:14b"] == 0
    # Same model reuses the same per-model lock object.
    async with lock.acquire("qwen3:14b"):
        pass
    assert "qwen3:14b" in lock._per_model_locks


@pytest.mark.asyncio
async def test_gpu_lock_reuses_existing_lock_for_same_model():
    """Two acquisitions of the same model name share the lazily-created lock
    object (the `lock is not None` branch in _model_lock)."""
    lock = GPULock(max_in_flight=4)
    first = await lock._model_lock("qwen3:14b")
    second = await lock._model_lock("qwen3:14b")
    assert first is second  # second call hits the already-populated branch
    # And concurrent acquires of the same model serialize on that one lock.
    order = []

    async def worker(tag):
        async with lock.acquire("qwen3:14b"):
            order.append(tag)
            await asyncio.sleep(0)

    await asyncio.gather(worker("a"), worker("b"))
    assert sorted(order) == ["a", "b"]


def test_cost_cap_exceeded_is_exception():
    assert issubclass(CostCapExceeded, Exception)


# ===========================================================================
# Curator
# ===========================================================================


def _company_state(**over):
    base = dict(
        current_phase="exploration",
        phase_started_at=datetime.now(UTC) - timedelta(days=3),
        bootstrap_at=datetime.now(UTC) - timedelta(days=10),
        deadline=datetime.now(UTC) + timedelta(days=30),
        problem_statement="Find the niche.",
        stance="No hype.",
        success_criterion="Establish one rigorous result.",
        thesis=None,
        niche=None,
        audience=None,
        charter="SECRET MARKET CHARTER — must NOT leak into prompts",
        paused=False,
        paused_reason=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _curator(*, cs=None, lessons=None, recall=None):
    state = AsyncMock()
    # No agent_identities registry in unit context → persona_for is skipped and the curator falls
    # back to the SYSTEM_PROMPTS code constants. (A truthy AsyncMock pool would otherwise feed a
    # Mock-shaped persona into the system layer.)
    state.pool = None
    state.get_company_state.return_value = cs if cs is not None else _company_state()
    state.count_active_theses.return_value = 4

    memory = AsyncMock()
    if recall is not None:
        memory.recent.return_value = recall
        memory.recall_episodic.return_value = recall
    else:
        memory.recent.return_value = []
        memory.recall_episodic.return_value = []

    lessons_client = AsyncMock()
    lessons_client.fetch_applicable.return_value = lessons or []

    return Curator(state, memory, lessons_client)


# ---------------------------------------------------------------------------
# Constitution layer — charter NO LONGER injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_constitution_layer_omits_charter():
    cur = _curator()
    layer = await cur._constitution_layer()
    assert layer.name == "constitution"
    assert layer.priority == 0
    assert "Seed problem" in layer.content
    assert "Find the niche." in layer.content
    assert "No hype." in layer.content
    assert "Establish one rigorous result." in layer.content
    # The market charter must never appear.
    assert "SECRET MARKET CHARTER" not in layer.content
    assert "charter" not in layer.content.lower()


@pytest.mark.asyncio
async def test_constitution_layer_unset_stance_and_criterion():
    cur = _curator(cs=_company_state(stance=None, success_criterion=None))
    layer = await cur._constitution_layer()
    assert "(unset)" in layer.content


# ---------------------------------------------------------------------------
# Phase layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_layer_reports_phase_and_counts():
    cur = _curator()
    layer = await cur._phase_layer()
    assert layer.name == "phase"
    assert layer.priority == 1
    assert "exploration" in layer.content
    assert "Active theses: 4" in layer.content


# ---------------------------------------------------------------------------
# Lessons layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lessons_layer_empty_when_none():
    cur = _curator(lessons=[])
    layer, ids = await cur._lessons_layer("pi.claim_verdict", {})
    assert layer.content == ""
    assert ids == []


@pytest.mark.asyncio
async def test_lessons_layer_renders_verified_and_unverified():
    lessons = [
        SimpleNamespace(id=1, status="active", lesson_text="Prefer falsifiable claims."),
        SimpleNamespace(id=2, status="proposed", lesson_text="Distrust single-source findings."),
    ]
    cur = _curator(lessons=lessons)
    layer, ids = await cur._lessons_layer("pi.claim_verdict", {})
    assert ids == [1, 2]
    assert "(verified) Prefer falsifiable" in layer.content
    assert "(unverified) Distrust single-source" in layer.content


# ---------------------------------------------------------------------------
# Recall layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_layer_empty_no_episodes():
    from harness.curator import RECIPES

    cur = _curator(recall=[])
    # Patch the recall query lookup so it doesn't hit state.get_thesis.
    layer = await cur._recall_layer(RECIPES["mimir.certify"], {})  # cold path, no sessions
    assert "(no relevant prior episodes)" in layer.content
    assert layer.priority == 3


@pytest.mark.asyncio
async def test_recall_layer_renders_episodes_cold_path():
    from harness.curator import RECIPES

    episodes = [SimpleNamespace(created_at=datetime(2026, 6, 1, 12, 30), content="prior PI dissent")]
    cur = _curator(recall=episodes)
    recipe = RECIPES["pi.claim_verdict"]  # cold path + recall sessions
    cur.state.get_thesis.return_value = SimpleNamespace(claim="claim text")
    layer = await cur._recall_layer(recipe, {"thesis_id": 1})
    assert "prior PI dissent" in layer.content
    cur.memory.recall_episodic.assert_awaited()


@pytest.mark.asyncio
async def test_recall_layer_uses_recent_on_hot_path():
    from harness.curator import Recipe

    episodes = [SimpleNamespace(created_at=datetime(2026, 6, 1, 9, 0), content="hot episode")]
    cur = _curator(recall=episodes)
    recipe = Recipe(
        invocation_type="pi.weekly_synthesis",
        description="d",
        agent="pi",
        total_budget=4000,
        use_cold_path=False,  # hot path → memory.recent
        recall_sessions=["weekly"],
        recall_k=3,
    )
    layer = await cur._recall_layer(recipe, {})
    assert "hot episode" in layer.content
    cur.memory.recent.assert_awaited()


# ---------------------------------------------------------------------------
# _recall_query branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_query_branches():
    from harness.curator import RECIPES, Recipe

    cur = _curator()
    cur.state.get_thesis.return_value = SimpleNamespace(claim="X beats Y")
    q = await cur._recall_query(RECIPES["pi.claim_verdict"], {"thesis_id": 9})
    assert "X beats Y" in q

    def _r(it):
        return Recipe(invocation_type=it, description="d", agent="pi", total_budget=1, recall_sessions=["s"])

    assert "thesis activity" in await cur._recall_query(_r("pi.weekly_synthesis"), {})
    assert "phase transition" in await cur._recall_query(_r("pi.phase_transition_ratify"), {})
    assert "slop" in await cur._recall_query(_r("evaluation.slop_score"), {})
    assert await cur._recall_query(_r("planner.generate_tasks"), {}) == ""


# ---------------------------------------------------------------------------
# build() — full assembly + system prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_unknown_invocation_type_raises():
    cur = _curator()
    with pytest.raises(ValueError, match="No recipe"):
        await cur.build("nope.nope", {})


@pytest.mark.asyncio
async def test_build_assembles_layers_and_system_prompt():
    cur = _curator()
    prompt = await cur.build("pi.exploration_kickoff", {})
    assert prompt.invocation_type == "pi.exploration_kickoff"
    assert prompt.output_schema == "ExplorationKickoffOutput"
    names = [layer.name for layer in prompt.layers]
    # System anchor + constitution + phase + task_data + schema (no recall/lessons here).
    assert names[0] == "system"
    assert "constitution" in names
    assert "phase" in names
    assert "task_data" in names
    assert names[-1] == "schema"
    # System role anchor is the PI prompt.
    sys_msg = prompt.as_system_message()
    assert "Principal Investigator" in sys_msg
    assert "research directions" in sys_msg  # the kickoff task body
    assert prompt.total_tokens > 0
    assert prompt.tool_names == ["labfoundry-state", "labfoundry-memory", "labfoundry-events", "labfoundry-artifacts"]


@pytest.mark.asyncio
async def test_build_includes_lessons_and_recall_layers():
    lessons = [SimpleNamespace(id=3, status="active", lesson_text="A learned heuristic worth keeping.")]
    episodes = [SimpleNamespace(created_at=datetime(2026, 6, 2, 8, 0), content="prior verdict episode")]
    cur = _curator(lessons=lessons, recall=episodes)
    cur.state.get_thesis.return_value = SimpleNamespace(
        claim="c", status="active", confidence=0.5, created_at=datetime(2026, 5, 1)
    )
    cur.state.get_adversary_verdict.return_value = SimpleNamespace(
        verdict="kill", confidence=0.9, reasoning="weak", cited_finding_ids=[]
    )
    cur.state.get_active_theses.return_value = []
    cur.state.get_findings.return_value = []
    prompt = await cur.build("pi.claim_verdict", {"thesis_id": 1, "adversary_verdict_id": 2})
    names = [layer.name for layer in prompt.layers]
    assert "lessons" in names
    assert "recall" in names
    assert prompt.lesson_ids == [3]


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_enforce_budget_under_budget_noop():
    cur = _curator()
    layers = [PromptLayer(name="system", content="short", priority=0)]
    out = cur._enforce_budget(list(layers), budget=10_000)
    assert out[0].content == "short"


def test_enforce_budget_compacts_recall_first():
    """Step 1 compacts recall; a separate droppable lessons layer absorbs the
    residual overflow in step 2 so the compacted recall survives."""
    cur = _curator()
    layers = [
        PromptLayer(name="system", content="anchor", priority=0),
        PromptLayer(name="lessons", content="lesson " * 100, priority=2),
        PromptLayer(name="recall", content="word " * 400, priority=3),
    ]
    out = cur._enforce_budget(layers, budget=300)
    recall = next(layer for layer in out if layer.name == "recall")
    lessons = next(layer for layer in out if layer.name == "lessons")
    # Recall was compacted (truncation marker present), lessons dropped to clear
    # the marker-sized residual overflow.
    assert "[…truncated]" in recall.content
    assert lessons.content == ""
    total = sum(layer.token_count(cur.tokenizer) for layer in out)
    assert total <= 300


def test_enforce_budget_drops_low_priority_when_still_over():
    cur = _curator()
    # A huge lessons layer (priority 2) with no recall to compact → must be dropped.
    layers = [
        PromptLayer(name="system", content="anchor", priority=0),
        PromptLayer(name="lessons", content="lesson " * 4000, priority=2),
    ]
    out = cur._enforce_budget(layers, budget=50)
    lessons = next(layer for layer in out if layer.name == "lessons")
    assert lessons.content == ""


def test_enforce_budget_drops_all_droppable_still_over():
    """Even after compacting + dropping every priority>=2 layer, an oversized
    priority-0 anchor keeps total over budget — the step-2 loop falls through
    without breaking and budget is simply not met (priority-0 is never dropped)."""
    cur = _curator()
    layers = [
        PromptLayer(name="system", content="anchor " * 200, priority=0),
        PromptLayer(name="lessons", content="lesson " * 50, priority=2),
        PromptLayer(name="recall", content="word " * 50, priority=3),
    ]
    out = cur._enforce_budget(layers, budget=10)
    lessons = next(layer for layer in out if layer.name == "lessons")
    recall = next(layer for layer in out if layer.name == "recall")
    assert lessons.content == ""  # both droppables cleared
    assert recall.content == ""
    # System (priority 0) survives untouched even though we're still over budget.
    assert out[0].content.startswith("anchor")
    assert sum(layer.token_count(cur.tokenizer) for layer in out) > 10


def test_compact_recall_returns_input_when_under_target():
    cur = _curator()
    text = "tiny content"
    assert cur._compact_recall(text, target_tokens=10_000) == text


def test_compact_recall_truncates_over_target():
    cur = _curator()
    text = "word " * 500
    out = cur._compact_recall(text, target_tokens=20)
    assert out.endswith("[…truncated]")
    assert len(cur.tokenizer.encode(out)) < len(cur.tokenizer.encode(text))


# ---------------------------------------------------------------------------
# Standalone task-data builders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exploration_kickoff_task_data():
    layer = await _build_exploration_kickoff_task_data({}, AsyncMock(), AsyncMock())
    assert layer.name == "task_data"
    assert layer.priority == 1
    assert "research directions" in layer.content


@pytest.mark.asyncio
async def test_mimir_certify_task_data_with_and_without_fields():
    full = await _build_mimir_certify_task_data(
        {"title": "T", "source_url": "http://u", "host": "h"}, AsyncMock(), AsyncMock()
    )
    assert "T" in full.content and "http://u" in full.content and "h" in full.content
    empty = await _build_mimir_certify_task_data({}, AsyncMock(), AsyncMock())
    assert "(none)" in empty.content
    assert "(unknown)" in empty.content


@pytest.mark.asyncio
async def test_researcher_task_data_renders_task_and_theses():
    state = AsyncMock()
    state.get_task.return_value = SimpleNamespace(description="Probe chunking", task_type="literature", thesis_id=7)
    state.get_active_theses.return_value = [SimpleNamespace(id=7, claim="Chunking dominates")]
    layer = await _build_researcher_task_data({"task_id": 1, "raw_material": "RAW BODY"}, state, AsyncMock())
    assert layer.name == "task_data"
    assert "Probe chunking" in layer.content
    assert "T7: Chunking dominates" in layer.content
    assert "RAW BODY" in layer.content


@pytest.mark.asyncio
async def test_researcher_task_data_exploratory_no_thesis_no_material():
    state = AsyncMock()
    state.get_task.return_value = SimpleNamespace(description="Open scan", task_type="exploratory", thesis_id=None)
    state.get_active_theses.return_value = []
    layer = await _build_researcher_task_data({"task_id": 2}, state, AsyncMock())
    assert "(none — exploratory)" in layer.content
    assert "no active theses" in layer.content


@pytest.mark.asyncio
async def test_build_recipe_without_recall_omits_layer():
    """mimir.certify has empty recall_sessions; with no applicable lessons, both
    optional layers are omitted — exercising the None/skip branches in build()."""
    cs = _company_state()
    state = AsyncMock()
    state.get_company_state.return_value = cs
    state.count_active_theses.return_value = 0
    memory = AsyncMock()
    lessons = AsyncMock()
    lessons.fetch_applicable.return_value = []
    cur = Curator(state, memory, lessons)
    prompt = await cur.build("mimir.certify", {"title": "T", "source_url": "http://u", "host": "h"})
    names = [layer.name for layer in prompt.layers]
    assert "recall" not in names  # recipe has no recall_sessions
    assert "lessons" not in names  # no applicable lessons
    assert "task_data" in names
    assert "Warden of the Library" in prompt.as_system_message()


def test_enforce_budget_drops_recall_after_compaction_residual():
    """When recall is the sole overflow source, step 1 compacts it but the
    truncation-marker residual keeps total over budget, so step 2 drops it."""
    cur = _curator()
    layers = [
        PromptLayer(name="system", content="anchor", priority=0),
        PromptLayer(name="recall", content="word " * 400, priority=3),
    ]
    out = cur._enforce_budget(layers, budget=120)
    recall = next(layer for layer in out if layer.name == "recall")
    assert recall.content == ""  # compacted then dropped to clear residual
    total = sum(layer.token_count(cur.tokenizer) for layer in out)
    assert total <= 120
