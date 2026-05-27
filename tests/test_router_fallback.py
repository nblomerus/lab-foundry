"""
Router provider-dispatch + fallback tests.

The model calls are monkeypatched so these run without Ollama or a network —
the point is the dispatch logic: cloud-first, local on any failure, and the
recorded "used" spec reflecting what actually produced the output.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from boardroom.harness.router import (
    GPULock, Provider, Router, Tier,
)


class _Out(BaseModel):
    x: int


def _router(cloud: bool = True) -> Router:
    return Router(pool=None, gpu_lock=GPULock(), gemini_api_key=("key" if cloud else None))


@pytest.mark.asyncio
async def test_cloud_success_uses_cloud_and_skips_local():
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

    parsed, _text, _toks, used = await r._invoke_with_fallback(
        r._call_order(Tier.CODE), "sys", _Out,
    )
    assert parsed.x == 1
    assert used.provider == Provider.GEMINI
    assert calls == {"cloud": 1, "ollama": 0}


@pytest.mark.asyncio
async def test_cloud_failure_falls_back_to_local():
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

    parsed, _text, _toks, used = await r._invoke_with_fallback(
        r._call_order(Tier.CODE), "sys", _Out,
    )
    assert parsed.x == 2
    assert used.provider == Provider.OLLAMA
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

    parsed, _text, _toks, used = await r._invoke_with_fallback(
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
async def test_all_candidates_fail_raises_last_error():
    r = _router(cloud=True)

    async def boom_cloud(spec, system, schema):
        raise RuntimeError("cloud down")

    async def boom_ollama(spec, system, schema):
        raise RuntimeError("ollama down")

    r._call_openai_compatible = boom_cloud  # type: ignore[method-assign]
    r._call_ollama = boom_ollama            # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="ollama down"):
        await r._invoke_with_fallback(r._call_order(Tier.CODE), "sys", _Out)
