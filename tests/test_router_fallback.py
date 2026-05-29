"""
Router provider-dispatch + fallback tests.

The model calls are monkeypatched so these run without Ollama or a network —
the point is the dispatch logic. Most tiers are cloud-first with local as the
fallback; `Tier.CODE` is intentionally local-first (qwen2.5-coder:7b chosen
for calibration on per-page extraction), with cloud trailing.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from boardroom.harness.router import (
    CloudProvider, GPULock, Provider, Router, Tier,
)


class _Out(BaseModel):
    x: int


def _router(cloud: bool = True) -> Router:
    chain = (
        [CloudProvider(Provider.GEMINI, "http://example", "key", "test-model", "json_schema")]
        if cloud else []
    )
    return Router(pool=None, gpu_lock=GPULock(), cloud_chain=chain)


@pytest.mark.asyncio
async def test_cloud_success_uses_cloud_and_skips_local():
    """For cloud-first tiers (WORKHORSE/FAST/REASONING), a working cloud
    response wins and local is never touched."""
    r = _router(cloud=True)
    calls = {"cloud": 0, "ollama": 0}

    async def fake_cloud(spec, system, schema):
        calls["cloud"] += 1
        return '{"x": 1}', 5

    async def fake_ollama(spec, system, schema):
        calls["ollama"] += 1
        return '{"x": 2}', 5

    r._call_openai_compatible = fake_cloud  # type: ignore[method-assign]
    r._call_ollama = fake_ollama            # type: ignore[method-assign]

    parsed, _text, _toks, used, _attempts = await r._invoke_with_fallback(
        r._call_order(Tier.WORKHORSE), "sys", _Out,
    )
    assert parsed.x == 1
    assert used.provider == Provider.GEMINI
    assert calls == {"cloud": 1, "ollama": 0}


@pytest.mark.asyncio
async def test_cloud_failure_falls_back_to_local():
    """For cloud-first tiers, a cloud error trips the fallback to local."""
    r = _router(cloud=True)
    calls = {"cloud": 0, "ollama": 0}

    async def fake_cloud(spec, system, schema):
        calls["cloud"] += 1
        raise RuntimeError("429 rate limited upstream")

    async def fake_ollama(spec, system, schema):
        calls["ollama"] += 1
        return '{"x": 2}', 7

    r._call_openai_compatible = fake_cloud  # type: ignore[method-assign]
    r._call_ollama = fake_ollama            # type: ignore[method-assign]

    parsed, _text, _toks, used, _attempts = await r._invoke_with_fallback(
        r._call_order(Tier.WORKHORSE), "sys", _Out,
    )
    assert parsed.x == 2
    assert used.provider == Provider.OLLAMA
    assert calls == {"cloud": 1, "ollama": 1}


@pytest.mark.asyncio
async def test_code_tier_is_local_first():
    """CODE tier is intentionally local-first: the bench validated qwen2.5-coder:7b
    as both more calibrated AND faster than the 70B cloud free models on
    per-page extraction. A working local response must win without touching
    cloud."""
    r = _router(cloud=True)
    calls = {"cloud": 0, "ollama": 0}

    async def fake_cloud(spec, system, schema):
        calls["cloud"] += 1
        return '{"x": 1}', 5

    async def fake_ollama(spec, system, schema):
        calls["ollama"] += 1
        return '{"x": 2}', 5

    r._call_openai_compatible = fake_cloud  # type: ignore[method-assign]
    r._call_ollama = fake_ollama            # type: ignore[method-assign]

    parsed, _text, _toks, used, _attempts = await r._invoke_with_fallback(
        r._call_order(Tier.CODE), "sys", _Out,
    )
    assert parsed.x == 2
    assert used.provider == Provider.OLLAMA
    assert calls == {"cloud": 0, "ollama": 1}


@pytest.mark.asyncio
async def test_code_tier_falls_back_to_cloud_on_local_failure():
    """If qwen2.5-coder:7b errors / returns garbage, CODE still degrades to
    the cloud chain rather than failing the call outright."""
    r = _router(cloud=True)
    calls = {"cloud": 0, "ollama": 0}

    async def fake_cloud(spec, system, schema):
        calls["cloud"] += 1
        return '{"x": 1}', 5

    async def fake_ollama(spec, system, schema):
        calls["ollama"] += 1
        raise RuntimeError("ollama down")

    r._call_openai_compatible = fake_cloud  # type: ignore[method-assign]
    r._call_ollama = fake_ollama            # type: ignore[method-assign]

    parsed, _text, _toks, used, _attempts = await r._invoke_with_fallback(
        r._call_order(Tier.CODE), "sys", _Out,
    )
    assert parsed.x == 1
    assert used.provider == Provider.GEMINI
    assert calls == {"cloud": 1, "ollama": 1}


@pytest.mark.asyncio
async def test_cloud_unparseable_output_falls_back_to_local():
    r = _router(cloud=True)

    async def fake_cloud(spec, system, schema):
        return "Sure! Here is the answer in prose, not JSON.", 5  # ignores the schema

    async def fake_ollama(spec, system, schema):
        return '{"x": 3}', 6

    r._call_openai_compatible = fake_cloud  # type: ignore[method-assign]
    r._call_ollama = fake_ollama            # type: ignore[method-assign]

    parsed, _text, _toks, used, _attempts = await r._invoke_with_fallback(
        r._call_order(Tier.WORKHORSE), "sys", _Out,
    )
    assert parsed.x == 3
    assert used.provider == Provider.OLLAMA


@pytest.mark.asyncio
async def test_no_key_means_local_only():
    r = _router(cloud=False)
    order = r._call_order(Tier.REASONING)
    assert [s.provider for s in order] == [Provider.OLLAMA]
    assert r.cloud_enabled is False


@pytest.mark.asyncio
async def test_premium_tier_leads_with_premium_chain():
    free = [CloudProvider(Provider.GROQ, "http://g", "k", "llama", "json_object")]
    premium = [CloudProvider(Provider.OPENAI, "http://o", "k", "gpt-5.5", "json_schema", False)]
    r = Router(pool=None, gpu_lock=GPULock(), cloud_chain=free, premium_chain=premium)
    # Reasoning (premium tier): OpenAI leads, then free, then local.
    reasoning = [s.provider for s in r._call_order(Tier.REASONING)]
    assert reasoning == [Provider.OPENAI, Provider.GROQ, Provider.OLLAMA]
    # Non-premium tier: free chain only, no premium lead.
    fast = [s.provider for s in r._call_order(Tier.FAST)]
    assert fast == [Provider.GROQ, Provider.OLLAMA]


@pytest.mark.asyncio
async def test_openai_premium_omits_temperature_for_gpt5():
    # send_temperature=False → the cloud call must not include temperature.
    premium = [CloudProvider(Provider.OPENAI, "http://o", "k", "gpt-5.5", "json_schema", False)]
    r = Router(pool=None, gpu_lock=GPULock(), cloud_chain=[], premium_chain=premium)
    captured = {}

    async def fake_post(url, headers=None, json=None):
        captured["payload"] = json
        class _R:
            status_code = 200
            def raise_for_status(self): ...
            def json(self_inner):
                return {"choices": [{"message": {"content": '{"x": 1}'}}], "usage": {"completion_tokens": 1}}
        return _R()

    r._http.post = fake_post  # type: ignore[method-assign]
    spec = r._call_order(Tier.REASONING)[0]
    assert spec.provider == Provider.OPENAI
    await r._call_openai_compatible(spec, "sys", _Out)
    assert "temperature" not in captured["payload"], "gpt-5.x must not receive temperature"


@pytest.mark.asyncio
async def test_all_candidates_fail_raises_last_error():
    r = _router(cloud=True)

    async def boom_cloud(spec, system, schema):
        raise RuntimeError("cloud down")

    async def boom_ollama(spec, system, schema):
        raise RuntimeError("ollama down")

    r._call_openai_compatible = boom_cloud  # type: ignore[method-assign]
    r._call_ollama = boom_ollama            # type: ignore[method-assign]

    # Use WORKHORSE here so the order is cloud -> ollama, and the last error
    # (the one re-raised after both fail) is from ollama.
    with pytest.raises(RuntimeError, match="ollama down"):
        await r._invoke_with_fallback(r._call_order(Tier.WORKHORSE), "sys", _Out)
