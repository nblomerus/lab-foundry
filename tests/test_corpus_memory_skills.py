"""Mocked unit tests for library.corpus.tools, memory.client, skills.client.

NO real Postgres / Ollama / network / Zep. The corpus pool + embedder are
monkeypatched to a ScriptedPool and a tiny embedder stub; the Zep client is a
hand-rolled async fake; LessonsClient runs against a ScriptedPool. None of these
touch DATABASE_URL or the `db` fixture.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import library.corpus.tools as ct
import memory.client as mc
import skills.client as sk
from memory.client import RecalledMessage, ZepClient, _coerce_dt
from skills.client import Lesson, LessonsClient
from tests._helpers import ScriptedPool

# distinctive SQL fragments (first-match-wins in ScriptedConn.rules)
DENSE = "ORDER BY c.embedding <=> $1"
LEXICAL = "ts_rank(_tsv"
GET_DOC = "FROM documents d"
DATASETS = "FROM datasets"


# ── stub embedder + pool wiring ────────────────────────────────────────────────
class _StubEmbedder:
    """corpus Embedder.embed(text:str) -> list[float] (NOT the helpers' list API)."""

    def __init__(self, dim: int = ct.EMBED_DIM):
        self.dim = dim
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        v = [0.0] * self.dim
        v[(len(text) or 1) % self.dim] = 1.0
        return v


def _wire(monkeypatch, pool, embedder=None):
    emb = embedder if embedder is not None else _StubEmbedder()

    async def _fake_pool():
        return pool

    async def _fake_embedder():
        return emb

    monkeypatch.setattr(ct, "_get_pool", _fake_pool)
    monkeypatch.setattr(ct, "_get_embedder", _fake_embedder)
    return emb


def _chunk_row(
    cid,
    *,
    doc=10,
    ordinal=0,
    text="hello",
    distance=0.1,
    tier="peer_reviewed",
    ingested=None,
    kind="paper",
    title="T",
    url="u",
    tokens=4,
):
    return {
        "id": cid,
        "document_id": doc,
        "ordinal": ordinal,
        "text": text,
        "token_count": tokens,
        "kind": kind,
        "title": title,
        "source_url": url,
        "trust_tier": tier,
        "ingested_at": ingested,
        "distance": distance,
    }


# =========================================================================
# Pure rerank helpers
# =========================================================================
def test_recency_weight_none_is_zero():
    assert ct._recency_weight(None) == 0.0


def test_recency_weight_naive_gets_utc_and_decays():
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    w = ct._recency_weight(now_naive)
    assert 0.95 < w <= 1.0  # ~now → ~1.0


def test_recency_weight_old_decays_low():
    old = datetime.now(UTC) - timedelta(days=ct.RECENCY_HALFLIFE_DAYS * 4)
    assert ct._recency_weight(old) < 0.1


def test_recency_weight_future_clamped_to_now():
    fut = datetime.now(UTC) + timedelta(days=100)
    assert ct._recency_weight(fut) == pytest.approx(1.0)


def test_score_row_known_tier():
    sim, tw, rec, score = ct._score_row(0.2, "peer_reviewed", None)
    assert sim == pytest.approx(0.8)
    assert tw == 1.0
    assert rec == 0.0
    assert score == pytest.approx(ct.W_SIM * 0.8 + ct.W_TRUST * 1.0)


def test_score_row_unknown_tier_defaults():
    _, tw, _, _ = ct._score_row(0.0, "no_such_tier", None)
    assert tw == ct.DEFAULT_TRUST_W


def test_score_row_none_tier_defaults():
    _, tw, _, _ = ct._score_row(0.0, None, None)
    assert tw == ct.DEFAULT_TRUST_W


def test_score_row_distance_clamped():
    # distance > 1 → sim clamped to 0; distance < 0 → sim clamped to 1
    assert ct._score_row(2.0, "web_unknown", None)[0] == 0.0
    assert ct._score_row(-0.5, "web_unknown", None)[0] == 1.0


def test_row_to_chunk_score_zero():
    c = ct._row_to_chunk(_chunk_row(1, distance=0.25))
    assert c.score == 0.0
    assert c.sim == pytest.approx(0.75)
    assert c.chunk_id == 1


# =========================================================================
# _search_by_vector (dense-only) + rerank truncation
# =========================================================================
@pytest.mark.asyncio
async def test_search_by_vector_reranks_and_truncates(monkeypatch):
    rows = [
        _chunk_row(1, doc=1, distance=0.9, tier="web_unknown"),  # low sim+trust
        _chunk_row(2, doc=2, distance=0.05, tier="peer_reviewed"),  # best
        _chunk_row(3, doc=3, distance=0.5, tier="web_reputable"),
    ]
    pool = ScriptedPool(rules=[(DENSE, rows)])
    _wire(monkeypatch, pool)
    out = await ct._search_by_vector([0.0] * ct.EMBED_DIM, k=2)
    assert [c.chunk_id for c in out] == [2, 3]  # best two by score, truncated
    assert out[0].score > out[1].score


@pytest.mark.asyncio
async def test_search_by_vector_min_trust_floor_passed_to_sql(monkeypatch):
    pool = ScriptedPool(rules=[(DENSE, [_chunk_row(1)])])
    _wire(monkeypatch, pool)
    await ct._search_by_vector([0.0] * ct.EMBED_DIM, k=4, min_trust="preprint", kind="paper")
    _, sql, args = pool.calls[-1]
    assert args[1] == "paper"  # $2 kind
    assert args[2] == "preprint"  # $3 floor
    assert args[3] == max(4 * 4, 32)  # $4 candidate pool N


@pytest.mark.asyncio
async def test_search_by_vector_default_floor_quarantined(monkeypatch):
    pool = ScriptedPool(rules=[(DENSE, [])])
    _wire(monkeypatch, pool)
    await ct._search_by_vector([0.0] * ct.EMBED_DIM)
    _, _, args = pool.calls[-1]
    assert args[2] == "quarantined"


# =========================================================================
# _search_hybrid (RRF fusion) — both arms, lexical-empty, query-cap
# =========================================================================
@pytest.mark.asyncio
async def test_search_hybrid_fuses_both_arms(monkeypatch):
    dense = [_chunk_row(1, doc=1, distance=0.2), _chunk_row(2, doc=2, distance=0.3)]
    lex = [_chunk_row(2, doc=2, distance=0.3), _chunk_row(9, doc=9, distance=0.8)]
    pool = ScriptedPool(rules=[(LEXICAL, lex), (DENSE, dense)])
    _wire(monkeypatch, pool)
    out = await ct._search_hybrid([0.0] * ct.EMBED_DIM, "rare token", k=8)
    # chunk 2 appears in both arms → highest fused RRF score, ranks first
    assert out[0].chunk_id == 2
    ids = {c.chunk_id for c in out}
    assert ids == {1, 2, 9}
    s2 = next(c for c in out if c.chunk_id == 2).score
    s1 = next(c for c in out if c.chunk_id == 1).score
    assert s2 > s1


@pytest.mark.asyncio
async def test_search_hybrid_blank_query_skips_lexical(monkeypatch):
    dense = [_chunk_row(1)]
    pool = ScriptedPool(rules=[(LEXICAL, [_chunk_row(99)]), (DENSE, dense)])
    _wire(monkeypatch, pool)
    out = await ct._search_hybrid([0.0] * ct.EMBED_DIM, "   ", k=8)
    assert [c.chunk_id for c in out] == [1]
    # only the dense query was issued (no lexical fetch)
    assert all(LEXICAL not in c[1] for c in pool.calls)


@pytest.mark.asyncio
async def test_search_hybrid_lexical_args_include_cap(monkeypatch):
    pool = ScriptedPool(rules=[(LEXICAL, [_chunk_row(2)]), (DENSE, [_chunk_row(1)])])
    _wire(monkeypatch, pool)
    await ct._search_hybrid([0.0] * ct.EMBED_DIM, "deep learning", k=8)
    lex_call = next(c for c in pool.calls if LEXICAL in c[1])
    args = lex_call[2]
    assert args[4] == "deep learning"  # $5 raw query text
    assert args[5] == ct.LEX_SCAN_CAP  # $6 scan cap
    assert args[3] == max(4 * 8, 64)  # $4 limit / candidate pool


@pytest.mark.asyncio
async def test_search_hybrid_tiebreak_by_distance(monkeypatch):
    # two chunks each appear once (same RRF rank-1 in their single arm) → tie on
    # score; closer (smaller distance) wins the tiebreak.
    dense = [_chunk_row(1, doc=1, distance=0.7)]
    lex = [_chunk_row(2, doc=2, distance=0.1)]
    pool = ScriptedPool(rules=[(LEXICAL, lex), (DENSE, dense)])
    _wire(monkeypatch, pool)
    out = await ct._search_hybrid([0.0] * ct.EMBED_DIM, "x", k=8)
    assert out[0].chunk_id == 2  # smaller distance ranks first on the tie


# =========================================================================
# corpus_search dispatch (hybrid vs dense) + embed wiring
# =========================================================================
@pytest.mark.asyncio
async def test_corpus_search_hybrid_default(monkeypatch):
    pool = ScriptedPool(rules=[(LEXICAL, []), (DENSE, [_chunk_row(1)])])
    emb = _wire(monkeypatch, pool)
    out = await ct.corpus_search("a query")
    assert emb.calls == ["a query"]
    assert [c.chunk_id for c in out] == [1]
    # lexical arm WAS attempted (hybrid path)
    assert any(LEXICAL in c[1] for c in pool.calls)


@pytest.mark.asyncio
async def test_corpus_search_dense_branch(monkeypatch):
    pool = ScriptedPool(rules=[(DENSE, [_chunk_row(1)])])
    _wire(monkeypatch, pool)
    out = await ct.corpus_search("q", hybrid=False)
    assert [c.chunk_id for c in out] == [1]
    assert all(LEXICAL not in c[1] for c in pool.calls)  # never touched lexical


# =========================================================================
# build_context — greedy fill, spans, drop, degenerate
# =========================================================================
@pytest.mark.asyncio
async def test_build_context_packs_chunks_and_spans(monkeypatch):
    async def fake_search(query, k, *, kind=None, min_trust=None):
        return [
            ct._row_to_chunk(_chunk_row(1, doc=1, ordinal=0, text="alpha", tokens=2)),
            ct._row_to_chunk(_chunk_row(2, doc=2, ordinal=1, text="beta", tokens=2)),
        ]

    monkeypatch.setattr(ct, "corpus_search", fake_search)
    block = await ct.build_context("q", max_tokens=100)
    assert block.dropped == 0
    assert block.total_tokens == 4
    assert len(block.spans) == 2
    # span char range must slice the exact chunk text out of the rendered block
    sp = block.spans[0]
    assert block.text[sp.char_start : sp.char_end] == "alpha"
    sp1 = block.spans[1]
    assert block.text[sp1.char_start : sp1.char_end] == "beta"
    assert block.text.startswith("[#0] alpha\n[#1] beta\n")


@pytest.mark.asyncio
async def test_build_context_drops_when_over_budget(monkeypatch):
    async def fake_search(query, k, *, kind=None, min_trust=None):
        return [
            ct._row_to_chunk(_chunk_row(1, text="keep", tokens=2)),
            ct._row_to_chunk(_chunk_row(2, text="drop", tokens=50)),
        ]

    monkeypatch.setattr(ct, "corpus_search", fake_search)
    block = await ct.build_context("q", max_tokens=3)
    assert block.dropped == 1
    assert block.total_tokens == 2
    assert [s.chunk_id for s in block.spans] == [1]


@pytest.mark.asyncio
async def test_build_context_token_estimate_when_count_missing(monkeypatch):
    long = "x" * 40  # len//4 == 10 tokens

    async def fake_search(query, k, *, kind=None, min_trust=None):
        return [ct._row_to_chunk(_chunk_row(1, text=long, tokens=None))]

    monkeypatch.setattr(ct, "corpus_search", fake_search)
    block = await ct.build_context("q", max_tokens=100)
    assert block.total_tokens == 10


@pytest.mark.asyncio
async def test_build_context_single_oversize_chunk_returns_empty(monkeypatch):
    async def fake_search(query, k, *, kind=None, min_trust=None):
        return [ct._row_to_chunk(_chunk_row(1, text="big", tokens=9999))]

    monkeypatch.setattr(ct, "corpus_search", fake_search)
    block = await ct.build_context("q", max_tokens=10)
    assert block.text == ""
    assert block.spans == []
    assert block.dropped == 1
    assert block.total_tokens == 0


# =========================================================================
# corpus_get_document
# =========================================================================
def _doc_row(**over):
    base = {
        "id": 5,
        "kind": "paper",
        "title": "Paper",
        "authors": ["A", "B"],
        "source_kind": "arxiv",
        "source_url": "http://x",
        "doi": None,
        "arxiv_id": "2401.0",
        "published_at": None,
        "ingested_at": None,
        "license": "cc",
        "status": "certified",
        "trust_tier": "preprint",
        "trust_state": "certified",
        "queryable": True,
        "chunk_count": 3,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_corpus_get_document_found(monkeypatch):
    pool = ScriptedPool(rules=[(GET_DOC, [_doc_row()])])
    _wire(monkeypatch, pool)
    doc = await ct.corpus_get_document(5)
    assert doc is not None
    assert doc.id == 5
    assert doc.authors == ["A", "B"]
    assert doc.chunk_count == 3


@pytest.mark.asyncio
async def test_corpus_get_document_null_authors(monkeypatch):
    pool = ScriptedPool(rules=[(GET_DOC, [_doc_row(authors=None)])])
    _wire(monkeypatch, pool)
    doc = await ct.corpus_get_document(5)
    assert doc.authors == []


@pytest.mark.asyncio
async def test_corpus_get_document_missing_returns_none(monkeypatch):
    pool = ScriptedPool(rules=[(GET_DOC, [])])  # empty-list rule → no row
    _wire(monkeypatch, pool)
    assert await ct.corpus_get_document(404) is None


# =========================================================================
# list_datasets
# =========================================================================
def _ds_row(**over):
    base = {
        "id": 1,
        "name": "MNIST",
        "url": "http://d",
        "modality": "image",
        "task": "classification",
        "size": "60k",
        "license": "cc",
        "notes": "n",
        "document_id": 7,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_list_datasets_maps_rows(monkeypatch):
    pool = ScriptedPool(rules=[(DATASETS, [_ds_row(), _ds_row(id=2, name="CIFAR")])])
    _wire(monkeypatch, pool)
    out = await ct.list_datasets()
    assert [d.name for d in out] == ["MNIST", "CIFAR"]
    assert out[0].document_id == 7
    _, _, args = pool.calls[-1]
    assert args[0] is None  # no task filter


@pytest.mark.asyncio
async def test_list_datasets_task_filter(monkeypatch):
    pool = ScriptedPool(rules=[(DATASETS, [])])
    _wire(monkeypatch, pool)
    out = await ct.list_datasets(task="segmentation")
    assert out == []
    _, _, args = pool.calls[-1]
    assert args[0] == "segmentation"


# =========================================================================
# Embedder — dim check + endpoint shapes (no real HTTP)
# =========================================================================
class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeHTTP:
    """Scripts POSTs by URL suffix → _FakeResp."""

    def __init__(self, routes):
        self.routes = routes
        self.posts: list[tuple] = []

    async def post(self, url, json=None):
        self.posts.append((url, json))
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return resp
        raise AssertionError(f"unrouted POST {url}")

    async def aclose(self):
        pass


def _embedder_with(monkeypatch, routes):
    """Build an Embedder whose GPULock import fails → local Semaphore path, and
    swap in a fake http client."""
    monkeypatch.setattr(ct, "EMBED_DIM", 3, raising=True)
    e = ct.Embedder.__new__(ct.Embedder)
    e.ollama_url = "http://ollama"
    e.model = "m"
    e._http = _FakeHTTP(routes)
    e._gpu_lock = None
    e._sem = __import__("asyncio").Semaphore(1)
    return e


@pytest.mark.asyncio
async def test_embedder_modern_embeddings_key(monkeypatch):
    e = _embedder_with(monkeypatch, {"/api/embed": _FakeResp(200, {"embeddings": [[1, 2, 3]]})})
    assert await e.embed("hi") == [1, 2, 3]


@pytest.mark.asyncio
async def test_embedder_modern_legacy_key_on_embed(monkeypatch):
    e = _embedder_with(monkeypatch, {"/api/embed": _FakeResp(200, {"embedding": [4, 5, 6]})})
    assert await e.embed("hi") == [4, 5, 6]


@pytest.mark.asyncio
async def test_embedder_legacy_404_fallback(monkeypatch):
    routes = {
        "/api/embed": _FakeResp(404),
        "/api/embeddings": _FakeResp(200, {"embedding": [7, 8, 9]}),
    }
    e = _embedder_with(monkeypatch, routes)
    assert await e.embed("hi") == [7, 8, 9]


@pytest.mark.asyncio
async def test_embedder_unexpected_keys_raises(monkeypatch):
    e = _embedder_with(monkeypatch, {"/api/embed": _FakeResp(200, {"nope": 1})})
    with pytest.raises(ValueError, match="unexpected Ollama embed response keys"):
        await e.embed("hi")


@pytest.mark.asyncio
async def test_embedder_dim_mismatch_raises(monkeypatch):
    e = _embedder_with(monkeypatch, {"/api/embed": _FakeResp(200, {"embeddings": [[1, 2]]})})
    with pytest.raises(ValueError, match="embed dim mismatch"):
        await e.embed("hi")


@pytest.mark.asyncio
async def test_embedder_gpu_lock_path(monkeypatch):
    """When a GPULock is present, embed acquires it instead of the semaphore."""
    acquired = []

    class _Lock:
        def acquire(self, model):
            acquired.append(model)

            class _C:
                async def __aenter__(s):
                    return None

                async def __aexit__(s, *a):
                    return False

            return _C()

    monkeypatch.setattr(ct, "EMBED_DIM", 3, raising=True)
    e = ct.Embedder.__new__(ct.Embedder)
    e.ollama_url = "http://ollama"
    e.model = "mymodel"
    e._http = _FakeHTTP({"/api/embed": _FakeResp(200, {"embeddings": [[1, 2, 3]]})})
    e._gpu_lock = _Lock()
    e._sem = None
    assert await e.embed("hi") == [1, 2, 3]
    assert acquired == ["mymodel"]


@pytest.mark.asyncio
async def test_embedder_close(monkeypatch):
    e = _embedder_with(monkeypatch, {})
    await e.close()  # _FakeHTTP.aclose is a no-op; just exercises the path


def test_embedder_init_falls_back_to_semaphore(monkeypatch):
    """GPULock import failing → local Semaphore(1) (the out-of-harness path)."""
    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "harness.router":
            raise ImportError("no harness")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    e = ct.Embedder(ollama_url="http://x", model="m")
    assert e._gpu_lock is None
    assert e._sem is not None


# =========================================================================
# memory/client.py — _coerce_dt + RecalledMessage
# =========================================================================
def test_coerce_dt_iso_and_passthrough_and_fallback():
    assert _coerce_dt(datetime(2026, 1, 1, tzinfo=UTC)).year == 2026
    assert _coerce_dt("2026-05-27T13:00:00Z").hour == 13
    assert isinstance(_coerce_dt(None), datetime)
    assert isinstance(_coerce_dt("garbage"), datetime)
    assert isinstance(_coerce_dt(12345), datetime)  # non-str/non-dt → now


# ── fake Zep client (namespaces: thread/graph/user) ────────────────────────────
class _Raiser:
    def __init__(self, exc):
        self.exc = exc

    async def __call__(self, *a, **k):
        raise self.exc


class _NS:
    """A namespace whose attribute methods are set per-test."""


def _zep_with(**ns):
    client = _NS()
    for name, obj in ns.items():
        setattr(client, name, obj)
    return ZepClient(client)


class _Recorder:
    def __init__(self, ret=None):
        self.ret = ret
        self.calls: list[dict] = []

    async def __call__(self, **k):
        self.calls.append(k)
        return self.ret


@pytest.mark.asyncio
async def test_ping_missing_namespace_raises():
    z = _zep_with(thread=_NS())  # no graph/user attrs
    with pytest.raises(RuntimeError, match="missing 'graph'"):
        await z.ping()


@pytest.mark.asyncio
async def test_ping_not_found_is_healthy():
    thread = _NS()
    thread.get = _Raiser(Exception("404 Not Found"))
    z = _zep_with(thread=thread, graph=_NS(), user=_NS())
    await z.ping()  # returns cleanly


@pytest.mark.asyncio
async def test_ping_other_error_reraises():
    thread = _NS()
    thread.get = _Raiser(Exception("connection refused"))
    z = _zep_with(thread=thread, graph=_NS(), user=_NS())
    with pytest.raises(Exception, match="connection refused"):
        await z.ping()


@pytest.mark.asyncio
async def test_ping_ok():
    thread = _NS()
    thread.get = _Recorder(ret=None)
    z = _zep_with(thread=thread, graph=_NS(), user=_NS())
    await z.ping()
    assert thread.get.calls[0]["thread_id"] == "__boardroom_healthcheck__"


@pytest.mark.asyncio
async def test_ensure_user_suppresses_error():
    user = _NS()
    user.add = _Raiser(Exception("already exists"))
    z = _zep_with(user=user)
    await z.ensure_user()  # suppressed, no raise


@pytest.mark.asyncio
async def test_ensure_user_calls_add():
    user = _NS()
    user.add = _Recorder()
    z = _zep_with(user=user)
    await z.ensure_user()
    assert user.add.calls[0]["user_id"] == ZepClient.USER_ID


@pytest.mark.asyncio
async def test_ensure_session_creates_once_and_caches():
    thread = _NS()
    thread.create = _Recorder()
    z = _zep_with(thread=thread)
    await z.ensure_session("dissent")
    await z.ensure_session("dissent")  # cached → no second create
    assert len(thread.create.calls) == 1
    assert "dissent" in z._ensured_sessions


@pytest.mark.asyncio
async def test_ensure_session_swallows_create_error_and_caches():
    thread = _NS()
    thread.create = _Raiser(Exception("429 rate limit"))
    z = _zep_with(thread=thread)
    await z.ensure_session("charter")
    assert "charter" in z._ensured_sessions  # cached despite failure


@pytest.mark.asyncio
async def test_write_message_returns_uuid(monkeypatch):
    thread = _NS()
    thread.create = _Recorder()
    result = type("R", (), {"message_uuids": ["uuid-123"]})()
    thread.add_messages = _Recorder(ret=result)
    z = _zep_with(thread=thread)
    uid = await z.write_message("dissent", "hello", role_type="pi", metadata={"k": 1})
    assert uid == "uuid-123"
    msgs = thread.add_messages.calls[0]["messages"]
    assert msgs[0].name == "pi"
    assert msgs[0].role == "assistant"


@pytest.mark.asyncio
async def test_write_message_no_uuids_returns_empty(monkeypatch):
    thread = _NS()
    thread.create = _Recorder()
    thread.add_messages = _Recorder(ret=type("R", (), {"message_uuids": []})())
    z = _zep_with(thread=thread)
    assert await z.write_message("s", "c") == ""


@pytest.mark.asyncio
async def test_write_message_failure_returns_empty(monkeypatch):
    thread = _NS()
    thread.create = _Recorder()
    thread.add_messages = _Raiser(Exception("boom"))
    z = _zep_with(thread=thread)
    assert await z.write_message("s", "c") == ""


@pytest.mark.asyncio
async def test_recent_maps_messages():
    msg = type(
        "M",
        (),
        {
            "content": "hi",
            "created_at": "2026-01-02T00:00:00Z",
            "name": "pi",
            "uuid_": "u1",
        },
    )()
    result = type("R", (), {"messages": [msg]})()
    thread = _NS()
    thread.get = _Recorder(ret=result)
    z = _zep_with(thread=thread)
    out = await z.recent("dissent", k=3)
    assert len(out) == 1
    assert out[0].content == "hi"
    assert out[0].role_type == "pi"
    assert out[0].uuid == "u1"
    assert isinstance(out[0].created_at, datetime)


@pytest.mark.asyncio
async def test_recent_role_falls_back_to_role_then_agent():
    # name missing, role present
    m1 = type("M", (), {"content": None, "created_at": None, "role": "user", "uuid_": None})()
    # name+role missing → 'agent'
    m2 = type("M2", (), {"content": "x", "created_at": None})()
    result = type("R", (), {"messages": [m1, m2]})()
    thread = _NS()
    thread.get = _Recorder(ret=result)
    z = _zep_with(thread=thread)
    out = await z.recent("s")
    assert out[0].role_type == "user"
    assert out[0].content == ""  # None → ""
    assert out[0].uuid == ""
    assert out[1].role_type == "agent"


@pytest.mark.asyncio
async def test_recent_error_returns_empty():
    thread = _NS()
    thread.get = _Raiser(Exception("down"))
    z = _zep_with(thread=thread)
    assert await z.recent("s") == []


@pytest.mark.asyncio
async def test_recent_no_messages_attr():
    thread = _NS()
    thread.get = _Recorder(ret=type("R", (), {})())  # no .messages
    z = _zep_with(thread=thread)
    assert await z.recent("s") == []


@pytest.mark.asyncio
async def test_recall_episodic_maps_and_scores():
    ep = type(
        "E",
        (),
        {
            "content": "episode",
            "created_at": "2026-03-03T00:00:00Z",
            "role_type": "adversary",
            "uuid_": "e1",
            "score": 0.42,
        },
    )()
    result = type("R", (), {"episodes": [ep]})()
    graph = _NS()
    graph.search = _Recorder(ret=result)
    z = _zep_with(graph=graph)
    out = await z.recall_episodic("s", "query", k=2)
    assert out[0].relevance == 0.42
    assert out[0].role_type == "adversary"
    assert graph.search.calls[0]["scope"] == "episodes"


@pytest.mark.asyncio
async def test_recall_episodic_error_returns_empty():
    graph = _NS()
    graph.search = _Raiser(Exception("graph down"))
    z = _zep_with(graph=graph)
    assert await z.recall_episodic("s", "q") == []


@pytest.mark.asyncio
async def test_recall_episodic_role_fallback_and_no_episodes():
    ep = type("E", (), {"role": "fallback"})()  # no role_type, no content
    result = type("R", (), {"episodes": [ep]})()
    graph = _NS()
    graph.search = _Recorder(ret=result)
    z = _zep_with(graph=graph)
    out = await z.recall_episodic("s", "q")
    assert out[0].role_type == "fallback"
    assert out[0].content == ""


@pytest.mark.asyncio
async def test_recall_graph_dict_and_obj_edges():
    class _Edge:
        def dict(self):
            return {"rel": "USES"}

    result = type("R", (), {"edges": [_Edge(), {"rel": "ADDRESSES"}]})()
    graph = _NS()
    graph.search = _Recorder(ret=result)
    z = _zep_with(graph=graph)
    out = await z.recall_graph("q", entity_type="Method", limit=3)
    assert out == [{"rel": "USES"}, {"rel": "ADDRESSES"}]
    assert graph.search.calls[0]["search_filters"] == {"entity_type": "Method"}
    assert graph.search.calls[0]["limit"] == 3


@pytest.mark.asyncio
async def test_recall_graph_no_entity_type_no_filter():
    result = type("R", (), {"edges": []})()
    graph = _NS()
    graph.search = _Recorder(ret=result)
    z = _zep_with(graph=graph)
    out = await z.recall_graph("q")
    assert out == []
    assert "search_filters" not in graph.search.calls[0]


@pytest.mark.asyncio
async def test_recall_graph_error_returns_empty():
    graph = _NS()
    graph.search = _Raiser(Exception("nope"))
    z = _zep_with(graph=graph)
    assert await z.recall_graph("q") == []


def test_from_env_constructs(monkeypatch):
    """from_env reads env + builds an AsyncZep; we stub the SDK import."""
    captured = {}

    class _AsyncZep:
        def __init__(self, **kw):
            captured.update(kw)

    import sys
    import types

    fake_mod = types.ModuleType("zep_cloud.client")
    fake_mod.AsyncZep = _AsyncZep
    monkeypatch.setitem(sys.modules, "zep_cloud", types.ModuleType("zep_cloud"))
    monkeypatch.setitem(sys.modules, "zep_cloud.client", fake_mod)
    monkeypatch.setenv("ZEP_API_KEY", "k")
    monkeypatch.setenv("ZEP_BASE_URL", "http://self-hosted")
    z = mc.ZepClient.from_env()
    assert isinstance(z, mc.ZepClient)
    assert captured == {"api_key": "k", "base_url": "http://self-hosted"}


def test_from_env_without_base_url(monkeypatch):
    captured = {}

    class _AsyncZep:
        def __init__(self, **kw):
            captured.update(kw)

    import sys
    import types

    fake_mod = types.ModuleType("zep_cloud.client")
    fake_mod.AsyncZep = _AsyncZep
    monkeypatch.setitem(sys.modules, "zep_cloud", types.ModuleType("zep_cloud"))
    monkeypatch.setitem(sys.modules, "zep_cloud.client", fake_mod)
    monkeypatch.setenv("ZEP_API_KEY", "k")
    monkeypatch.delenv("ZEP_BASE_URL", raising=False)
    mc.ZepClient.from_env()
    assert captured == {"api_key": "k"}  # no base_url key


@pytest.mark.asyncio
async def test_write_message_uses_real_message_type(monkeypatch):
    """Exercise the `from zep_cloud.types import Message` line with a stub SDK."""
    import sys
    import types

    class _Msg:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    fake = types.ModuleType("zep_cloud.types")
    fake.Message = _Msg
    monkeypatch.setitem(sys.modules, "zep_cloud", types.ModuleType("zep_cloud"))
    monkeypatch.setitem(sys.modules, "zep_cloud.types", fake)
    thread = _NS()
    thread.create = _Recorder()
    thread.add_messages = _Recorder(ret=type("R", (), {"message_uuids": ["x"]})())
    z = _zep_with(thread=thread)
    assert await z.write_message("s", "c", role_type="critic") == "x"
    sent = thread.add_messages.calls[0]["messages"][0]
    assert sent.name == "critic"


def test_recalled_message_dataclass_defaults():
    m = RecalledMessage(content="c", created_at=datetime.now(UTC), role_type="r", uuid="u")
    assert m.relevance is None


# =========================================================================
# skills/client.py — LessonsClient (asyncpg-backed, ScriptedPool)
# =========================================================================
APPLICABLE = "FROM active_lessons_by_invocation"
INSERT = "INSERT INTO lessons"
RECONCILE = "reconcile_lessons()"
DECAY = "decay_lessons()"
PENDING = "FROM lesson_applications la"
UPDATE_OUT = "UPDATE lesson_applications"
NEAR_DUP = "SELECT id FROM lessons"
CREDIT = "INSERT INTO lesson_applications"


def _lesson_row(**over):
    base = {
        "id": 1,
        "lesson_text": "do X",
        "confidence": 0.8,
        "status": "active",
        "promotion_run_count": 2,
        "contradiction_run_count": 0,
        "applies_when": {"phase": "scout"},
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_fetch_applicable_matches_predicate():
    rows = [
        _lesson_row(id=1, applies_when={"phase": "scout"}),
        _lesson_row(id=2, applies_when={"phase": "other"}),  # filtered out
        _lesson_row(id=3, applies_when={}),  # empty → matches all
    ]
    pool = ScriptedPool(rules=[(APPLICABLE, rows)])
    lc = LessonsClient(pool)
    out = await lc.fetch_applicable("scout_run", {"phase": "scout"}, limit=5)
    ids = [le.id for le in out]
    assert ids == [1, 3]
    assert all(isinstance(le, Lesson) for le in out)
    # over-fetch: SQL limit arg is limit*4
    _, _, args = pool.calls[-1]
    assert args == ("scout_run", 20)


@pytest.mark.asyncio
async def test_fetch_applicable_json_string_applies_when():
    rows = [_lesson_row(applies_when=json.dumps({"phase": "scout"}))]
    pool = ScriptedPool(rules=[(APPLICABLE, rows)])
    lc = LessonsClient(pool)
    out = await lc.fetch_applicable("inv", {"phase": "scout"})
    assert out[0].applies_when == {"phase": "scout"}  # parsed from JSON string


@pytest.mark.asyncio
async def test_fetch_applicable_respects_limit_break():
    rows = [_lesson_row(id=i, applies_when={}) for i in range(10)]
    pool = ScriptedPool(rules=[(APPLICABLE, rows)])
    lc = LessonsClient(pool)
    out = await lc.fetch_applicable("inv", {}, limit=3)
    assert len(out) == 3  # stops at limit even though more match


def test_predicate_matches_branches():
    lc = LessonsClient(ScriptedPool())
    assert lc._predicate_matches({}, {"anything": 1}) is True
    # glob suffix
    assert lc._predicate_matches({"source": "reddit*"}, {"source": "reddit-r/py"}) is True
    assert lc._predicate_matches({"source": "reddit*"}, {"source": "twitter"}) is False
    # glob expected but actual not a str
    assert lc._predicate_matches({"source": "reddit*"}, {"source": 5}) is False
    # key missing from context
    assert lc._predicate_matches({"phase": "scout"}, {"agent": "x"}) is False
    # exact equality mismatch
    assert lc._predicate_matches({"phase": "scout"}, {"phase": "build"}) is False
    # exact equality hit
    assert lc._predicate_matches({"phase": "scout"}, {"phase": "scout"}) is True
    # unknown vocab key → logged, still never matches (no key in ctx)
    assert lc._predicate_matches({"weird": "x"}, {"phase": "scout"}) is False


@pytest.mark.asyncio
async def test_insert_lesson_candidate_returns_id():
    pool = ScriptedPool(rules=[(INSERT, 99)])
    lc = LessonsClient(pool)
    new_id = await lc.insert_lesson_candidate("inv", {"phase": "scout"}, "text", "why", 12, "reflection")
    assert new_id == 99
    _, _, args = pool.calls[-1]
    assert args[0] == "inv"
    assert json.loads(args[1]) == {"phase": "scout"}  # serialized to JSON
    assert args[4] == 12


@pytest.mark.asyncio
async def test_reconcile_returns_dicts():
    pool = ScriptedPool(rules=[(RECONCILE, [{"lesson_id": 1, "action": "promote", "new_status": "active"}])])
    lc = LessonsClient(pool)
    out = await lc.reconcile()
    assert out == [{"lesson_id": 1, "action": "promote", "new_status": "active"}]


@pytest.mark.asyncio
async def test_decay_returns_dicts():
    pool = ScriptedPool(rules=[(DECAY, [{"lesson_id": 7, "action": "retire"}])])
    lc = LessonsClient(pool)
    out = await lc.decay()
    assert out == [{"lesson_id": 7, "action": "retire"}]


@pytest.mark.asyncio
async def test_fetch_pending_applications():
    row = {
        "agent_run_id": 5,
        "invocation_type": "scout",
        "run_status": "completed",
        "output_summary": "ok",
        "expectation": "e",
        "outcome": "o",
        "lesson_id": 2,
        "lesson_text": "t",
    }
    pool = ScriptedPool(rules=[(PENDING, [row])])
    lc = LessonsClient(pool)
    out = await lc.fetch_pending_applications(limit=10)
    assert out == [row]
    _, _, args = pool.calls[-1]
    assert args == (10,)


@pytest.mark.asyncio
async def test_set_application_outcome_executes():
    pool = ScriptedPool(rules=[(UPDATE_OUT, "UPDATE 1")])
    lc = LessonsClient(pool)
    await lc.set_application_outcome(2, 5, "supportive", judged_by_run_id=9)
    kind, sql, args = pool.calls[-1]
    assert kind == "execute"
    assert args == (2, 5, "supportive", 9)


@pytest.mark.asyncio
async def test_find_near_duplicate_returns_id_or_none():
    pool = ScriptedPool(rules=[(NEAR_DUP, 42)])
    lc = LessonsClient(pool)
    assert await lc.find_near_duplicate("inv", "some text", threshold=0.7) == 42
    _, _, args = pool.calls[-1]
    assert args == ("inv", "some text", 0.7)

    pool2 = ScriptedPool(default_val=None)  # no matching rule → default None
    lc2 = LessonsClient(pool2)
    assert await lc2.find_near_duplicate("inv", "x") is None


@pytest.mark.asyncio
async def test_credit_recurrence_executes():
    pool = ScriptedPool(rules=[(CREDIT, "INSERT 0 1")])
    lc = LessonsClient(pool)
    await lc.credit_recurrence(3, 88)
    kind, _, args = pool.calls[-1]
    assert kind == "execute"
    assert args == (3, 88)


def test_lesson_dataclass_shape():
    le = Lesson(
        id=1,
        lesson_text="t",
        confidence=0.5,
        status="active",
        promotion_run_count=0,
        contradiction_run_count=0,
        applies_when={},
    )
    assert le.confidence == 0.5
    # STANDARD_CONTEXT_KEYS sanity (module constant referenced by predicate hygiene)
    assert "phase" in sk.STANDARD_CONTEXT_KEYS
