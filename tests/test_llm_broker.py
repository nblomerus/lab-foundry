"""Experiment LLM broker (harness/llm_broker.py) + the sandbox helper (agents/experiments/
sandbox_llm.py) — integration over a REAL temp unix socket, with the Ollama upstream mocked
(no network, no GPU, no Docker). The helper is exactly what generated experiment code runs,
so these tests prove the full in-container call path short of Docker itself."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

import agents.experiments.sandbox as sandbox_mod
import agents.experiments.sandbox_llm as llm_helper
from harness.llm_broker import LLMBroker

pytestmark = pytest.mark.asyncio


class _FakeLock:
    """Records GPULock-style acquisitions (model names)."""

    def __init__(self):
        self.acquired: list[str] = []

    def acquire(self, model):
        lock = self

        class _Ctx:
            async def __aenter__(self):
                lock.acquired.append(model)
                return None

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class _FakeUpstreamResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.content = json.dumps(payload).encode()


@pytest_asyncio.fixture
async def broker(tmp_path, monkeypatch):
    """A live broker on a tmp socket with a mocked Ollama upstream; the helper is pointed
    at the same socket via its env override."""
    sock = str(tmp_path / "broker.sock")
    b = LLMBroker(gpu_lock=_FakeLock(), socket_path=sock)
    await b.start()
    b._client.post = AsyncMock(return_value=_FakeUpstreamResponse({"response": "42", "message": {"content": "hi"}}))
    b._client.get = AsyncMock(return_value=_FakeUpstreamResponse({"models": [{"name": "mistral:7b"}]}))
    monkeypatch.setattr(llm_helper, "SOCKET", sock)
    yield b
    await b.stop()


async def _in_thread(fn, *args, **kw):
    # the helper is synchronous stdlib code (it runs inside the container's script);
    # drive it off-thread so the broker's event loop can serve it.
    return await asyncio.to_thread(fn, *args, **kw)


async def test_helper_generate_roundtrip_forces_nonstream(broker):
    out = await _in_thread(llm_helper.generate, "mistral:7b", "what is 6*7?", temperature=0.0)
    assert out == "42"
    # the broker forced stream=False and forwarded the model/prompt upstream
    _, kwargs = broker._client.post.await_args
    assert kwargs["json"]["stream"] is False
    assert kwargs["json"]["model"] == "mistral:7b"
    assert broker._gpu_lock.acquired == ["mistral:7b"]  # serialized with the agents' GPU work


async def test_helper_chat_and_models(broker):
    reply = await _in_thread(llm_helper.chat, "mistral:7b", [{"role": "user", "content": "hi"}])
    assert reply == "hi"
    names = await _in_thread(llm_helper.models)
    assert names == ["mistral:7b"]


async def test_broker_rejects_non_whitelisted_paths(broker):
    """/api/pull (disk abuse), /api/delete etc. are NOT brokered — 404, never forwarded."""
    with pytest.raises(RuntimeError, match="404"):
        await _in_thread(llm_helper._request, "POST", "/api/pull", {"model": "x"})
    broker._client.post.assert_not_awaited()


async def test_broker_requires_model(broker):
    with pytest.raises(RuntimeError, match="missing 'model'"):
        await _in_thread(llm_helper._request, "POST", "/api/generate", {"prompt": "no model"})


async def test_sandbox_mounts_socket_and_helper_when_broker_up(tmp_path, monkeypatch):
    sock = tmp_path / "broker.sock"
    sock.touch()
    monkeypatch.setattr(sandbox_mod, "LLM_SOCKET", str(sock))
    cmd = sandbox_mod._build_cmd(
        "lf-exp-1",
        "/tmp/exp.py",
        mem_mb=512,
        cpus=1.0,
        requires_gpu=False,
        gpu_device=None,
        datasets_dir=None,
        llm_socket=str(sock) if os.path.exists(str(sock)) else None,
    )
    joined = " ".join(cmd)
    assert f"{sock}:/sock/ollama.sock" in joined
    assert "/opt/lab/llm.py:ro" in joined
    assert "--network none" in joined  # isolation unchanged


async def test_sandbox_no_mounts_when_broker_down():
    cmd = sandbox_mod._build_cmd(
        "lf-exp-1",
        "/tmp/exp.py",
        mem_mb=512,
        cpus=1.0,
        requires_gpu=False,
        gpu_device=None,
        datasets_dir=None,
        llm_socket=None,
    )
    joined = " ".join(cmd)
    assert "/sock/ollama.sock" not in joined and "/opt/lab/llm.py" not in joined


async def test_sandbox_mounts_benchmark_pack_when_present(tmp_path, monkeypatch):
    """The /data benchmark pack mounts condition-driven, exactly like the LLM socket."""
    pack = tmp_path / "benchmarks"
    pack.mkdir()
    monkeypatch.setattr(sandbox_mod, "DATASETS_DIR", str(pack))
    cmd = sandbox_mod._build_cmd(
        "lf-exp-1",
        "/tmp/exp.py",
        mem_mb=512,
        cpus=1.0,
        requires_gpu=False,
        gpu_device=None,
        datasets_dir=str(pack),
        llm_socket=None,
    )
    assert f"{pack}:/data:ro" in " ".join(cmd)


async def test_design_prompt_renders_dataset_manifest(tmp_path, monkeypatch):
    """The design prompt's /data section is rendered from the pack's manifest at call time."""
    import agents.experiments.handler as EH

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "name": "gsm8k_test",
                    "file": "gsm8k_test.jsonl",
                    "modality": "text",
                    "task_type": "qa",
                    "task": "math",
                    "n": 1319,
                    "fields": "question, answer, final_answer",
                    "license": "MIT",
                    "source": "x",
                }
            ]
        )
    )
    monkeypatch.setattr(sandbox_mod, "DATASETS_DIR", str(tmp_path))
    block = EH._datasets_block()
    assert "/data/gsm8k_test.jsonl — [text/qa] 1319 rows" in block  # real-first rendering w/ modality/task_type
    assert "PREFER THESE" in block  # real data is the default
    monkeypatch.setattr(sandbox_mod, "DATASETS_DIR", str(tmp_path / "missing"))
    assert EH._datasets_block() == ""
