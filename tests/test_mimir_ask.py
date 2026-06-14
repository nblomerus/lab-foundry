"""Unit tests for `agents.mimir.ask` and `agents.mimir.focus` — fully mocked.

NO real Postgres / Neo4j / Ollama / DeepSeek / network. The corpus search, the
LLM chain, the Neo4j driver, and the conversation telemetry are all patched to
deterministic fakes (see tests/_helpers.py). We never touch DATABASE_URL or the
`db` fixture.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.mimir.ask as ask
import agents.mimir.focus as focus
from agents.mimir.acquire import ALLOWED_REQUESTERS
from tests._helpers import FakeNeoDriver, make_state, patch_chain

pytestmark = pytest.mark.asyncio


# ── fixtures / builders ───────────────────────────────────────────────────────
def _chunk(document_id, title, trust_tier, text):
    return SimpleNamespace(document_id=document_id, title=title, trust_tier=trust_tier, text=text)


def _chunks():
    return [
        _chunk(11, "Attention Is All You Need", "certified", "  transformers everywhere  " + "x" * 400),
        _chunk(11, "Attention Is All You Need", "certified", "duplicate doc, second chunk"),
        _chunk(22, None, "community", "untitled paper text body"),
    ]


_ANSWER_JSON = json.dumps(
    {
        "answer": "Transformers dominate via self-attention." + "y" * 400,
        "citations": ["Attention Is All You Need", "BERT"],
        "related_concepts": ["self-attention", "BERT"],
        "gaps": ["long-context attention", "efficient kernels", "g3", "g4", "g5", "g6", "g7"],
    }
)


def _neo_on_run(direct_records, co_records):
    """Vary fake Neo4j records by query: the 2-hop co-occurrence query uses `<>`."""

    def on_run(query, params):
        return co_records if "c <> seed" in query else direct_records

    return on_run


_DIRECT = [
    {"kind": "Method", "name": "self-attention", "rel": "USES", "local": 3, "total": 40},
    {"kind": "Task", "name": "translation", "rel": "ADDRESSES", "local": 1, "total": 7},
]
_CO = [{"kind": "Dataset", "name": "WMT14", "strength": 5}]


def _patch_corpus(monkeypatch, chunks):
    mock = AsyncMock(return_value=chunks)
    monkeypatch.setattr(ask, "corpus_search", mock)
    return mock


def _patch_driver(monkeypatch, driver):
    monkeypatch.setattr(ask, "_get_driver", AsyncMock(return_value=driver))


# ── _ask_target_id ────────────────────────────────────────────────────────────
async def test_ask_target_id_is_stable_and_int():
    a = ask._ask_target_id("ariadne", "what is attention?")
    b = ask._ask_target_id("ariadne", "what is attention?")
    assert a == b
    assert isinstance(a, int) and a > 0


async def test_ask_target_id_varies_by_asker_and_question():
    base = ask._ask_target_id("ariadne", "q1")
    assert base != ask._ask_target_id("ariadne", "q2")
    assert base != ask._ask_target_id("mimir", "q1")


# ── retrieve (no LLM) ─────────────────────────────────────────────────────────
async def test_retrieve_builds_refs_no_llm(monkeypatch):
    mock = _patch_corpus(monkeypatch, _chunks())
    # If retrieve ever called the LLM the patched chain would record it.
    chain_calls = patch_chain(monkeypatch, ask, content=_ANSWER_JSON)

    refs = await ask.retrieve("attention", k=4)

    mock.assert_awaited_once_with("attention", k=4, exclude_lab=False)
    assert chain_calls == []  # retrieve must NOT synthesize
    assert len(refs) == 3
    assert all(isinstance(r, ask.RetrievedRef) for r in refs)
    first = refs[0]
    assert first.document_id == 11
    assert first.title == "Attention Is All You Need"
    assert first.trust_tier == "certified"
    # snippet is text[:300] stripped
    assert first.snippet.startswith("transformers everywhere")
    assert len(first.snippet) <= 300
    # title=None preserved on the untitled doc
    assert refs[2].title is None


async def test_retrieve_empty(monkeypatch):
    _patch_corpus(monkeypatch, [])
    refs = await ask.retrieve("nothing")
    assert refs == []


# ── _graph_neighborhood ───────────────────────────────────────────────────────
async def test_graph_neighborhood_empty_doc_ids():
    assert await ask._graph_neighborhood([]) == "(no graph context)"


async def test_graph_neighborhood_happy(monkeypatch):
    driver = FakeNeoDriver(on_run=_neo_on_run(_DIRECT, _CO))
    _patch_driver(monkeypatch, driver)

    out = await ask._graph_neighborhood([11, 22])

    assert "Concepts in the retrieved papers" in out
    assert "self-attention [Method/USES] · 3 here / 40 in corpus" in out
    assert "translation [Task/ADDRESSES] · 1 here / 7 in corpus" in out
    # co-occurring section present when co records exist
    assert "Co-occurring concepts (2-hop neighbourhood)" in out
    assert "WMT14 [Dataset] · 5" in out
    # two cypher queries were run (direct + co)
    assert len(driver.sessions[0].queries) == 2


async def test_graph_neighborhood_no_co_records(monkeypatch):
    driver = FakeNeoDriver(on_run=_neo_on_run(_DIRECT, []))
    _patch_driver(monkeypatch, driver)

    out = await ask._graph_neighborhood([11])

    assert "self-attention" in out
    # the co-occurring header is omitted when there are none
    assert "Co-occurring concepts" not in out


async def test_graph_neighborhood_exception_swallowed(monkeypatch):
    monkeypatch.setattr(ask, "_get_driver", AsyncMock(side_effect=RuntimeError("neo down")))
    out = await ask._graph_neighborhood([11, 22])
    assert out == "(concept graph unavailable)"


# ── _emit ─────────────────────────────────────────────────────────────────────
async def test_emit_state_none_is_noop():
    # state=None → returns without raising; nothing to assert beyond no exception.
    assert await ask._emit(None, "mimir.ask", asker="a", tid=1, nonce=2, payload={}) is None


async def test_emit_calls_state(monkeypatch):
    state = make_state()
    await ask._emit(state, "mimir.ask", asker="ariadne", tid=99, nonce=7, payload={"question": "q"})
    state.emit_corpus_event.assert_awaited_once()
    kwargs = state.emit_corpus_event.await_args.kwargs
    assert kwargs["target_type"] == "conversation"
    assert kwargs["target_id"] == 99
    assert kwargs["payload"] == {"asker": "ariadne", "question": "q"}
    assert kwargs["dedup_key"] == "mimir.ask-ariadne-7"


async def test_emit_swallows_emit_failure():
    state = make_state()
    state.emit_corpus_event = AsyncMock(side_effect=RuntimeError("bus down"))
    # must NOT raise
    await ask._emit(state, "mimir.answered", asker="a", tid=1, nonce=1, payload={})
    state.emit_corpus_event.assert_awaited_once()


# ── answer_question ───────────────────────────────────────────────────────────
async def test_answer_question_happy_with_emit(monkeypatch):
    corpus = _patch_corpus(monkeypatch, _chunks())
    driver = FakeNeoDriver(on_run=_neo_on_run(_DIRECT, _CO))
    _patch_driver(monkeypatch, driver)
    chain_calls = patch_chain(monkeypatch, ask, content=_ANSWER_JSON)
    state = make_state()

    ans = await ask.answer_question("what is attention?", k=5, state=state, asker="ariadne")

    assert isinstance(ans, ask.MimirAnswer)
    assert ans.citations == ["Attention Is All You Need", "BERT"]
    assert "self-attention" in ans.related_concepts
    assert len(ans.gaps) == 7

    corpus.assert_awaited_once_with("what is attention?", k=5, exclude_lab=False)

    # the user prompt fed to the LLM carries passages + graph context
    user_msg = chain_calls[0][0][1]["content"]
    assert "Retrieved passages" in user_msg
    assert "Concepts in the retrieved papers" in user_msg
    assert "[certified] Attention Is All You Need" in user_msg
    assert "[community] untitled" in user_msg  # title None → 'untitled'
    assert chain_calls[0][1]["invocation_type"] == "mimir.ask"

    # both ask + answered events emitted, sharing the same target_id
    assert state.emit_corpus_event.await_count == 2
    events = [c.args[0] for c in state.emit_corpus_event.await_args_list]
    assert events == ["mimir.ask", "mimir.answered"]
    tids = {c.kwargs["target_id"] for c in state.emit_corpus_event.await_args_list}
    assert len(tids) == 1
    answered_payload = state.emit_corpus_event.await_args_list[1].kwargs["payload"]
    assert answered_payload["citations"] == 2
    assert answered_payload["gaps"] == ans.gaps[:6]
    assert len(answered_payload["gaps"]) == 6


async def test_answer_question_no_emit_when_state_none(monkeypatch):
    _patch_corpus(monkeypatch, _chunks())
    _patch_driver(monkeypatch, FakeNeoDriver(on_run=_neo_on_run(_DIRECT, _CO)))
    patch_chain(monkeypatch, ask, content=_ANSWER_JSON)

    ans = await ask.answer_question("q", asker="someone")
    assert isinstance(ans, ask.MimirAnswer)
    # nothing to emit on; default asker path also covered via explicit asker here


async def test_answer_question_no_passages_placeholder(monkeypatch):
    _patch_corpus(monkeypatch, [])  # no chunks → no doc_ids
    # _graph_neighborhood([]) short-circuits, so the driver is never touched
    _patch_driver(monkeypatch, FakeNeoDriver(on_run=lambda q, p: (_ for _ in ()).throw(AssertionError("unused"))))
    chain_calls = patch_chain(monkeypatch, ask, content=_ANSWER_JSON)

    ans = await ask.answer_question("empty corpus question")

    assert isinstance(ans, ask.MimirAnswer)
    user_msg = chain_calls[0][0][1]["content"]
    assert "(no passages retrieved)" in user_msg
    assert "(no graph context)" in user_msg


async def test_answer_question_default_asker(monkeypatch):
    _patch_corpus(monkeypatch, _chunks())
    _patch_driver(monkeypatch, FakeNeoDriver(on_run=_neo_on_run(_DIRECT, _CO)))
    patch_chain(monkeypatch, ask, content=_ANSWER_JSON)
    state = make_state()

    # asker is not passed → defaults to "ariadne", which flows into the emit payload
    await ask.answer_question("q", state=state)
    assert state.emit_corpus_event.await_args_list[0].kwargs["payload"]["asker"] == "ariadne"


async def test_answer_question_strips_fences(monkeypatch):
    _patch_corpus(monkeypatch, _chunks())
    _patch_driver(monkeypatch, FakeNeoDriver(on_run=_neo_on_run(_DIRECT, _CO)))
    fenced = "<think>reasoning…</think>```json\n" + _ANSWER_JSON + "\n```"
    patch_chain(monkeypatch, ask, content=fenced)

    ans = await ask.answer_question("q")
    assert ans.citations == ["Attention Is All You Need", "BERT"]


# ════════════════════════════════════════════════════════════════════════════
# focus.py
# ════════════════════════════════════════════════════════════════════════════
async def test_request_focus_rejects_unknown_requester():
    state = make_state()
    with pytest.raises(ValueError, match="not allowed to direct scouts"):
        await focus.request_focus(state, topics=["x"], requester="stranger")
    state.emit_corpus_event.assert_not_awaited()


async def test_request_focus_happy_emits_directed_topics():
    requester = sorted(ALLOWED_REQUESTERS)[0]
    state = make_state()
    out = await focus.request_focus(state, topics=["  attention  ", "", "  ", "rl"], requester=requester, why="because")

    assert out == {"directed": ["attention", "rl"]}  # trimmed, blanks dropped
    state.emit_corpus_event.assert_awaited_once()
    args, kwargs = state.emit_corpus_event.await_args
    assert args[0] == "library.sweep_requested"
    assert kwargs["target_type"] == "ingest_source"
    assert kwargs["payload"] == {
        "topics": ["attention", "rl"],
        "focus": True,
        "requested_by": requester,
        "why": "because",
    }
    assert kwargs["dedup_key"].startswith("focus-")


async def test_request_focus_caps_topics():
    requester = sorted(ALLOWED_REQUESTERS)[0]
    state = make_state()
    many = [f"t{i}" for i in range(focus._MAX_FOCUS_TOPICS + 5)]
    out = await focus.request_focus(state, topics=many, requester=requester)
    assert len(out["directed"]) == focus._MAX_FOCUS_TOPICS
    assert out["directed"] == many[: focus._MAX_FOCUS_TOPICS]


async def test_request_focus_no_topics_returns_reason():
    requester = sorted(ALLOWED_REQUESTERS)[0]
    state = make_state()
    out = await focus.request_focus(state, topics=["", "   ", None], requester=requester)
    assert out == {"directed": [], "reason": "no topics"}
    state.emit_corpus_event.assert_not_awaited()


async def test_request_focus_none_topics():
    requester = sorted(ALLOWED_REQUESTERS)[0]
    state = make_state()
    out = await focus.request_focus(state, topics=None, requester=requester)
    assert out == {"directed": [], "reason": "no topics"}


async def test_current_focus_agenda(monkeypatch):
    state = make_state()
    monkeypatch.setattr(focus, "plan_sweep", AsyncMock(return_value=(["claim A", "claim B"], 4)))
    monkeypatch.setattr(focus, "ariadne_active", lambda: True)

    out = await focus.current_focus(state)
    assert out == {"ariadne_active": True, "source": "agenda", "topics": ["claim A", "claim B"]}


async def test_current_focus_frontier(monkeypatch):
    state = make_state()
    monkeypatch.setattr(focus, "plan_sweep", AsyncMock(return_value=(["frontier 1"], 8)))
    monkeypatch.setattr(focus, "ariadne_active", lambda: False)

    out = await focus.current_focus(state)
    assert out == {"ariadne_active": False, "source": "frontier", "topics": ["frontier 1"]}
