"""Unit tests for `agents.llm` — the shared DeepSeek→local LLM chain.

We exercise `_chain_complete` for real (it is the unit under test) and mock only its
dependencies: httpx (a fake `AsyncClient.post`), the env (DEEPSEEK_API_KEY / OLLAMA_URL /
ARIADNE_LOCAL_MODEL drive `_llm_chain`), and the session ContextVar that `_record_run` reads.

Retry-backoff tests stay fast by stubbing `asyncio.sleep` to a no-op so the exponential
delays never actually pause.
"""

from __future__ import annotations

import asyncio
import contextvars
import types

import httpx
import pytest

from agents import llm
from harness.router import Provider
from tests._helpers import ScriptedPool


# ── fake httpx response ───────────────────────────────────────────────────────
class FakeResponse:
    """Mimics the slice of httpx.Response that `_llm_post` / `_chain_complete` touch."""

    def __init__(self, status_code=200, content="hello", *, usage=None, request=None):
        self.status_code = status_code
        self._content = content
        self._usage = usage if usage is not None else {"prompt_tokens": 1, "completion_tokens": 1}
        self.reason_phrase = "OK" if status_code < 400 else "ERR"
        self.request = request or httpx.Request("POST", "http://test/chat/completions")

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}}],
            "usage": self._usage,
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"{self.status_code} {self.reason_phrase}", request=self.request, response=None)


def _patch_post(monkeypatch, handler):
    """Monkeypatch httpx.AsyncClient.post → `handler(url, **kwargs)` (may be sync or a coroutine)."""

    async def _post(self, url, **kwargs):
        res = handler(url, **kwargs)
        if asyncio.iscoroutine(res):
            res = await res
        return res

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)


def _fake_session(pool, *, sid=7, triggered_by_event_id=3, step_order=0):
    """A minimal stand-in for harness.session.Session that `_record_run` reads."""
    sess = types.SimpleNamespace()
    sess._pool = pool
    sess.id = sid
    sess.triggered_by_event_id = triggered_by_event_id
    sess._n = step_order

    def _next():
        sess._n += 1
        return sess._n

    sess.next_step_order = _next
    return sess


@pytest.fixture
def set_session(monkeypatch):
    """Swap `agents.llm._current_session` for a fresh ContextVar this test fully owns, then
    return a setter. A per-test var means `.set()` never leaks across tests and there is no
    cross-context `reset()` (pytest-asyncio runs the coroutine in its own contextvars Context)."""
    var = contextvars.ContextVar("test_current_session", default=None)
    monkeypatch.setattr(llm, "_current_session", var)
    return var.set


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make backoff instantaneous so retry tests don't wall-clock the suite."""

    async def _sleep(_delay):
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", _sleep)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start each test from a known LLM env: no DeepSeek key, default local model."""
    for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "OLLAMA_URL", "OLLAMA_API_KEY", "ARIADNE_LOCAL_MODEL"):
        monkeypatch.delenv(k, raising=False)


# ── _strip_fences ─────────────────────────────────────────────────────────────
def test_strip_fences_plain():
    assert llm._strip_fences('{"a": 1}') == '{"a": 1}'


def test_strip_fences_removes_think_block():
    raw = '<think>reasoning here\nmore</think>\n{"a": 1}'
    assert llm._strip_fences(raw) == '{"a": 1}'


def test_strip_fences_removes_json_code_fence():
    raw = '```json\n{"a": 1}\n```'
    assert llm._strip_fences(raw) == '{"a": 1}'


def test_strip_fences_removes_bare_fence():
    raw = '```\n{"a": 1}\n```'
    assert llm._strip_fences(raw) == '{"a": 1}'


def test_strip_fences_think_and_fence_combined():
    raw = '<think>plan</think>\n```json\n{"ok": true}\n```'
    assert llm._strip_fences(raw) == '{"ok": true}'


# ── _llm_chain ────────────────────────────────────────────────────────────────
def test_llm_chain_without_deepseek_key_is_local_only():
    chain = llm._llm_chain()
    assert len(chain) == 1
    assert chain[0].provider == Provider.OLLAMA
    # Default OLLAMA_URL + /v1, default local model.
    assert chain[0].base_url == "http://localhost:11434/v1"
    assert chain[0].model_name == "qwen3:14b"
    assert chain[0].api_key == "ollama"


def test_llm_chain_with_deepseek_key_leads_with_deepseek(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-custom")
    chain = llm._llm_chain()
    assert len(chain) == 2
    assert chain[0].provider == Provider.DEEPSEEK
    assert chain[0].base_url == "https://api.deepseek.com"
    assert chain[0].api_key == "sk-test"
    assert chain[0].model_name == "deepseek-custom"
    assert chain[1].provider == Provider.OLLAMA


def test_llm_chain_respects_ollama_url_and_local_model(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://gpu-box:11434/")
    monkeypatch.setenv("ARIADNE_LOCAL_MODEL", "qwen3:32b")
    # ARIADNE_LOCAL_MODEL is read at import → patch the module constant too.
    monkeypatch.setattr(llm, "LOCAL_MODEL", "qwen3:32b")
    chain = llm._llm_chain()
    assert chain[-1].base_url == "http://gpu-box:11434/v1"  # trailing slash stripped
    assert chain[-1].model_name == "qwen3:32b"


# ── _llm_post ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_llm_post_success_returns_response(monkeypatch):
    _patch_post(monkeypatch, lambda url, **kw: FakeResponse(200))
    async with httpx.AsyncClient() as client:
        resp = await llm._llm_post(client, "http://x/chat/completions")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_llm_post_4xx_raises_immediately(monkeypatch):
    calls = {"n": 0}

    def _h(url, **kw):
        calls["n"] += 1
        return FakeResponse(400)

    _patch_post(monkeypatch, _h)
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await llm._llm_post(client, "http://x/chat/completions", retries=4)
    assert calls["n"] == 1  # no retry on a 4xx


@pytest.mark.asyncio
async def test_llm_post_5xx_retried_then_raises(monkeypatch):
    calls = {"n": 0}

    def _h(url, **kw):
        calls["n"] += 1
        return FakeResponse(503)

    _patch_post(monkeypatch, _h)
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await llm._llm_post(client, "http://x/chat/completions", retries=3)
    assert calls["n"] == 3  # exhausted all attempts


@pytest.mark.asyncio
async def test_llm_post_5xx_then_200_succeeds(monkeypatch):
    seq = [FakeResponse(500), FakeResponse(200, "recovered")]

    def _h(url, **kw):
        return seq.pop(0)

    _patch_post(monkeypatch, _h)
    async with httpx.AsyncClient() as client:
        resp = await llm._llm_post(client, "http://x/chat/completions", retries=4)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "recovered"


@pytest.mark.asyncio
async def test_llm_post_timeout_retried_then_raises(monkeypatch):
    calls = {"n": 0}

    def _h(url, **kw):
        calls["n"] += 1
        raise httpx.TimeoutException("slow")

    _patch_post(monkeypatch, _h)
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.TimeoutException):
            await llm._llm_post(client, "http://x/chat/completions", retries=2)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_llm_post_transport_error_then_success(monkeypatch):
    state = {"first": True}

    def _h(url, **kw):
        if state["first"]:
            state["first"] = False
            raise httpx.TransportError("conn reset")
        return FakeResponse(200, "ok-after-transport")

    _patch_post(monkeypatch, _h)
    async with httpx.AsyncClient() as client:
        resp = await llm._llm_post(client, "http://x/chat/completions", retries=4)
    assert resp.json()["choices"][0]["message"]["content"] == "ok-after-transport"


# ── _chain_complete ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_chain_complete_no_provider_raises(monkeypatch):
    # Force an empty chain.
    monkeypatch.setattr(llm, "_llm_chain", lambda: [])
    with pytest.raises(RuntimeError, match="no LLM provider"):
        await llm._chain_complete(
            [{"role": "user", "content": "hi"}],
            temperature=0.2,
            invocation_type="mimir.ask",
            step_name="ask",
        )


@pytest.mark.asyncio
async def test_chain_complete_primary_success(monkeypatch, set_session):
    set_session(None)  # no recording branch
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    seen = {}

    def _h(url, **kw):
        seen["url"] = url
        seen["headers"] = kw.get("headers")
        seen["json"] = kw.get("json")
        return FakeResponse(200, "  primary-answer  ")

    _patch_post(monkeypatch, _h)
    out = await llm._chain_complete(
        [{"role": "user", "content": "q"}],
        temperature=0.5,
        invocation_type="ariadne.deliberate",
        step_name="deliberate",
    )
    assert out == "primary-answer"  # .strip() applied
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["json"]["temperature"] == 0.5


@pytest.mark.asyncio
async def test_chain_complete_primary_model_override(monkeypatch, set_session):
    set_session(None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    seen = {}

    def _h(url, **kw):
        seen["model"] = kw["json"]["model"]
        return FakeResponse(200, "x")

    _patch_post(monkeypatch, _h)
    await llm._chain_complete(
        [{"role": "user", "content": "q"}],
        temperature=0.1,
        invocation_type="mimir.ask",
        step_name="ask",
        primary_model="deepseek-reasoner",
    )
    assert seen["model"] == "deepseek-reasoner"


@pytest.mark.asyncio
async def test_chain_complete_falls_over_to_local(monkeypatch, set_session):
    set_session(None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    def _h(url, **kw):
        if "deepseek.com" in url:
            raise httpx.TimeoutException("deepseek down")
        return FakeResponse(200, "local-answer")

    _patch_post(monkeypatch, _h)
    out = await llm._chain_complete(
        [{"role": "user", "content": "q"}],
        temperature=0.3,
        invocation_type="mimir.ask",
        step_name="ask",
    )
    assert out == "local-answer"


@pytest.mark.asyncio
async def test_chain_complete_all_providers_fail_raises(monkeypatch, set_session):
    set_session(None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    def _h(url, **kw):
        raise httpx.TransportError("everything is down")

    _patch_post(monkeypatch, _h)
    with pytest.raises(httpx.TransportError):
        await llm._chain_complete(
            [{"role": "user", "content": "q"}],
            temperature=0.3,
            invocation_type="mimir.ask",
            step_name="ask",
        )


# ── _record_run (via _chain_complete) ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_record_run_persists_when_session_present(monkeypatch, set_session):
    pool = ScriptedPool()
    sess = _fake_session(pool, sid=42, triggered_by_event_id=99)
    set_session(sess)
    _patch_post(
        monkeypatch,
        lambda url, **kw: FakeResponse(200, "answer", usage={"prompt_tokens": 11, "completion_tokens": 5}),
    )
    out = await llm._chain_complete(
        [{"role": "user", "content": "hi"}],
        temperature=0.2,
        invocation_type="mimir.ask",
        step_name="ask",
    )
    assert out == "answer"
    inserts = [c for c in pool.calls if c[0] == "execute" and "INSERT INTO agent_runs" in c[1]]
    assert len(inserts) == 1
    args = inserts[0][2]
    assert args[0] == "mimir"  # agent derived from invocation_type prefix
    assert args[1] == "mimir.ask"
    assert args[3] == 99  # triggered_by_event_id
    assert args[4] == 11  # prompt_tokens
    assert args[5] == 5  # completion_tokens
    assert args[6] == 42  # session id


@pytest.mark.asyncio
async def test_record_run_noop_when_no_session(monkeypatch, set_session):
    set_session(None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    _patch_post(monkeypatch, lambda url, **kw: FakeResponse(200, "ok"))
    # No session → no DB to touch; just assert it doesn't raise.
    out = await llm._chain_complete(
        [{"role": "user", "content": "q"}],
        temperature=0.2,
        invocation_type="ariadne.deliberate",
        step_name="deliberate",
    )
    assert out == "ok"


@pytest.mark.asyncio
async def test_record_run_noop_when_session_id_zero(monkeypatch, set_session):
    pool = ScriptedPool()
    sess = _fake_session(pool, sid=0)  # not started → id falsy
    set_session(sess)
    _patch_post(monkeypatch, lambda url, **kw: FakeResponse(200, "ok"))
    out = await llm._chain_complete(
        [{"role": "user", "content": "q"}],
        temperature=0.2,
        invocation_type="mimir.ask",
        step_name="ask",
    )
    assert out == "ok"
    assert not any("INSERT INTO agent_runs" in c[1] for c in pool.calls if c[0] == "execute")


@pytest.mark.asyncio
async def test_record_run_noop_when_pool_none(monkeypatch, set_session):
    sess = _fake_session(None, sid=5)  # has id but no _pool
    set_session(sess)
    _patch_post(monkeypatch, lambda url, **kw: FakeResponse(200, "ok"))
    out = await llm._chain_complete(
        [{"role": "user", "content": "q"}],
        temperature=0.2,
        invocation_type="mimir.ask",
        step_name="ask",
    )
    assert out == "ok"  # no pool → quiet no-op, call still succeeds


@pytest.mark.asyncio
async def test_record_run_swallows_db_exception(monkeypatch, set_session):
    # A pool whose execute raises — _record_run must swallow it and NOT fail the call.
    pool = ScriptedPool()

    async def _boom(*a, **kw):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(pool.conn, "execute", _boom)
    sess = _fake_session(pool, sid=3)
    set_session(sess)
    _patch_post(monkeypatch, lambda url, **kw: FakeResponse(200, "still-ok"))
    out = await llm._chain_complete(
        [{"role": "user", "content": "q"}],
        temperature=0.2,
        invocation_type="mimir.ask",
        step_name="ask",
    )
    assert out == "still-ok"  # observability failure swallowed
