"""Unit tests for library.ingest.pipeline + library.ingest.scouts.

EVERYTHING is mocked — NO real Postgres, Ollama, or network. The DB is an
AsyncMock state (no `db` fixture, no DATABASE_URL); the fetcher (`web_fetch`),
parser, chunker and embedder (`_get_embedder`) are monkeypatched in the pipeline
module; httpx.AsyncClient is replaced by a scripted fake for the scouts and the
github/dataset/openml resolvers.

We drive stage->finalize, the dedup path, every quality/no-chunk/blocked/embed
branch, and each scout's happy / empty / non-200 / outage path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from library.ingest import pipeline, scouts
from library.ingest.scouts import SourceDescriptor

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fakes (reuse the test_ingest / test_search_arxiv patterns)
# --------------------------------------------------------------------------


class _FakeFetchedPage:
    """Mimics fetcher.FetchedPage's duck type (only .content / .url are read)."""

    def __init__(self, url: str, content: str):
        self.url = url
        self.content = content
        self.status_code = 200


class _FakeEmbedder:
    """Deterministic stand-in for the corpus Embedder — embed(text)->vector."""

    def __init__(self, *, fail_on: set[str] | None = None):
        self._fail_on = fail_on or set()

    async def embed(self, text: str) -> list[float]:
        if text in self._fail_on:
            raise RuntimeError(f"embed boom for {text!r}")
        v = [0.0] * 8
        v[(len(text) or 1) % 8] = 1.0
        return v


class _FakeParsed:
    """Stand-in for ParsedDoc — pipeline only reads these attrs off it."""

    def __init__(self, *, title="A Title", authors=None, doi=None, arxiv_id=None):
        self.title = title
        self.authors = authors or ["Author One"]
        self.doi = doi
        self.arxiv_id = arxiv_id


class _FakeResp:
    """A canned httpx-style response: .status_code, .json(), .text."""

    def __init__(self, status_code=200, json_data=None, text="", raise_exc=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self._raise = raise_exc

    def json(self):
        if self._raise is not None:
            raise self._raise
        return self._json


class _FakeClient:
    """Async context-manager httpx client whose get/post are scripted by a
    handler(method, url, kwargs) -> _FakeResp (or raises). Records calls."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._handler("GET", url, kw)

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._handler("POST", url, kw)


def _patch_httpx(monkeypatch, module, handler) -> list[_FakeClient]:
    """Replace `module.httpx.AsyncClient` with the scripted fake. Returns the
    list of instantiated clients so a test can inspect `.calls`."""
    made: list[_FakeClient] = []

    def _factory(*_a, **_k):
        c = _FakeClient(handler)
        made.append(c)
        return c

    monkeypatch.setattr(module.httpx, "AsyncClient", _factory)
    return made


def _make_state(**returns) -> AsyncMock:
    """An AsyncMock state with the pipeline's DB methods preset."""
    st = AsyncMock()
    for name, val in returns.items():
        getattr(st, name).return_value = val
    return st


def _good_body(n: int = 800) -> str:
    """Body comfortably over MIN_QUALITY_CHARS with no junk markers."""
    return "Real research content about retrieval and trust. " * (n // 50 + 1)


def _plan(n: int = 2) -> list[dict]:
    return [
        {
            "ordinal": i,
            "content_hash": f"h{i}",
            "text": f"chunk text {i}",
            "has_embedding": False,
        }
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# stage_source — happy / dedup / quality / no-chunks
# --------------------------------------------------------------------------


async def _patch_stage(monkeypatch, *, body, plan, parsed=None):
    async def _fake_fetch(url, state, *, force=False, client=None):
        return _FakeFetchedPage(url, body) if body is not None else None

    monkeypatch.setattr(pipeline, "web_fetch", _fake_fetch)
    monkeypatch.setattr(pipeline, "parse_paper", lambda *a, **k: parsed or _FakeParsed())
    monkeypatch.setattr(pipeline.PaperChunker, "plan", lambda self, doc: plan)


async def test_stage_source_happy_stages_and_emits(monkeypatch):
    await _patch_stage(monkeypatch, body=_good_body(), plan=_plan(3))
    st = _make_state(upsert_document=(42, True), stage_chunk_plan=3)

    desc = SourceDescriptor(kind="web", source_kind="web", canonical_key="k1", url="https://x.test/p")
    res = await pipeline.stage_source(desc, st)

    assert res == {"document_id": 42, "n_chunks": 3, "awaiting": "mimir"}
    st.upsert_document.assert_awaited_once()
    st.stage_chunk_plan.assert_awaited_once()
    # content_hash is a sha256 hex of the resolved text.
    _, kw = st.upsert_document.await_args
    assert len(kw["content_hash"]) == 64
    # document.parsed emitted with the chunk count.
    ev_type = st.emit_corpus_event.await_args.args[0]
    assert ev_type == "document.parsed"


async def test_stage_source_accepts_dict_source(monkeypatch):
    await _patch_stage(monkeypatch, body=_good_body(), plan=_plan(1))
    st = _make_state(upsert_document=(7, True), stage_chunk_plan=1)

    source = {"kind": "web", "source_kind": "web", "canonical_key": "k", "url": "https://x.test/a"}
    res = await pipeline.stage_source(source, st)
    assert res["document_id"] == 7 and res["awaiting"] == "mimir"


async def test_stage_source_dedupes_when_not_new(monkeypatch):
    await _patch_stage(monkeypatch, body=_good_body(), plan=_plan(2))
    st = _make_state(upsert_document=(99, False))  # is_new=False

    desc = SourceDescriptor(kind="web", source_kind="web", canonical_key="dup", url="https://x.test/d")
    res = await pipeline.stage_source(desc, st)

    assert res == {"document_id": 99, "deduped": True}
    st.stage_chunk_plan.assert_not_awaited()  # no staging on a dedup
    st.emit_corpus_event.assert_not_awaited()  # no document.parsed on a dedup


async def test_stage_source_quality_gate_rejects_thin(monkeypatch):
    await _patch_stage(monkeypatch, body="too short", plan=_plan(2))
    st = _make_state(upsert_document=(1, True))

    desc = SourceDescriptor(kind="web", source_kind="web", canonical_key="thin", url="https://x.test/t")
    res = await pipeline.stage_source(desc, st)

    assert res["skipped"] is True
    assert res["reason"].startswith("low_quality")
    st.upsert_document.assert_not_awaited()
    # an ingest_rejected telemetry event is emitted at the quality stage.
    assert st.emit_corpus_event.await_args.args[0] == "library.ingest_rejected"
    assert st.emit_corpus_event.await_args.kwargs["payload"]["stage"] == "quality"


async def test_stage_source_no_fetchable_content_rejected(monkeypatch):
    # web_fetch returns None -> _resolve_fulltext yields "" -> quality fails.
    await _patch_stage(monkeypatch, body=None, plan=_plan(2))
    st = _make_state()

    desc = SourceDescriptor(kind="web", source_kind="web", canonical_key="empty", url="https://x.test/e")
    res = await pipeline.stage_source(desc, st)
    assert res["skipped"] is True


async def test_stage_source_zero_chunks_skipped(monkeypatch):
    await _patch_stage(monkeypatch, body=_good_body(), plan=[])  # chunker -> nothing
    st = _make_state()

    desc = SourceDescriptor(kind="web", source_kind="web", canonical_key="nochunk", url="https://x.test/n")
    res = await pipeline.stage_source(desc, st)

    assert res == {"skipped": True, "reason": "no_chunks"}
    st.upsert_document.assert_not_awaited()
    assert st.emit_corpus_event.await_args.args[0] == "library.ingest_rejected"
    assert st.emit_corpus_event.await_args.kwargs["payload"]["stage"] == "quality"


async def test_emit_rejected_swallows_telemetry_failure(monkeypatch):
    # emit_corpus_event raising must NOT break the ingest path.
    await _patch_stage(monkeypatch, body="x", plan=_plan(2))
    st = _make_state()
    st.emit_corpus_event.side_effect = RuntimeError("bus down")

    desc = SourceDescriptor(kind="web", source_kind="web", canonical_key="boom", url="https://x.test/b")
    res = await pipeline.stage_source(desc, st)  # must not raise
    assert res["skipped"] is True


# --------------------------------------------------------------------------
# _resolve_fulltext dispatch (web fallback no-url branch)
# --------------------------------------------------------------------------


async def test_resolve_fulltext_web_no_url_returns_empty(monkeypatch):
    st = _make_state()
    desc = SourceDescriptor(kind="web", source_kind="web", canonical_key="k", url=None)
    text, url = await pipeline._resolve_fulltext(desc, st)
    assert text == "" and url is None


async def test_resolve_fulltext_web_empty_page(monkeypatch):
    async def _fake_fetch(url, state, *, force=False, client=None):
        return _FakeFetchedPage(url, "   ")  # whitespace only -> stripped empty

    monkeypatch.setattr(pipeline, "web_fetch", _fake_fetch)
    st = _make_state()
    desc = SourceDescriptor(kind="web", source_kind="web", canonical_key="k", url="https://x.test/w")
    text, url = await pipeline._resolve_fulltext(desc, st)
    assert text == "" and url == "https://x.test/w"


# --------------------------------------------------------------------------
# _resolve_arxiv_fulltext
# --------------------------------------------------------------------------


async def test_resolve_arxiv_fulltext_full_body(monkeypatch):
    async def _fake_fetch(url, state, *, force=False, client=None):
        assert "ar5iv.org/abs/2401.1" in url
        return _FakeFetchedPage(url, _good_body(2000))

    monkeypatch.setattr(pipeline, "web_fetch", _fake_fetch)
    desc = SourceDescriptor(kind="paper", source_kind="arxiv", canonical_key="2401.1", arxiv_id="2401.1")
    text, url = await pipeline._resolve_arxiv_fulltext(desc, _make_state())
    assert len(text) >= pipeline._MIN_FULLTEXT_CHARS
    assert url == "https://ar5iv.org/abs/2401.1"


async def test_resolve_arxiv_fulltext_empty_page_then_abstract(monkeypatch):
    # ar5iv returns a page with empty content -> text stays "" -> fallback.
    async def _fake_fetch(url, state, *, force=False, client=None):
        return _FakeFetchedPage(url, "")  # page exists but no content

    class _AR:
        abstract = "An abstract that is comfortably longer than nothing. " * 5

    async def _fake_search(query, max_results=1, **kw):
        return [_AR()]

    monkeypatch.setattr(pipeline, "web_fetch", _fake_fetch)
    monkeypatch.setattr(pipeline, "search_arxiv", _fake_search)
    desc = SourceDescriptor(kind="paper", source_kind="arxiv", canonical_key="2401.5", arxiv_id="2401.5")
    text, _ = await pipeline._resolve_arxiv_fulltext(desc, _make_state())
    assert "abstract that is comfortably longer" in text


async def test_resolve_arxiv_fulltext_empty_results_returns_short(monkeypatch):
    # search returns [] -> keep the (short) ar5iv text.
    async def _fake_fetch(url, state, *, force=False, client=None):
        return _FakeFetchedPage(url, "short")

    async def _fake_search(query, max_results=1, **kw):
        return []

    monkeypatch.setattr(pipeline, "web_fetch", _fake_fetch)
    monkeypatch.setattr(pipeline, "search_arxiv", _fake_search)
    desc = SourceDescriptor(kind="paper", source_kind="arxiv", canonical_key="2401.6", arxiv_id="2401.6")
    text, _ = await pipeline._resolve_arxiv_fulltext(desc, _make_state())
    assert text == "short"


async def test_resolve_arxiv_fulltext_falls_back_to_abstract(monkeypatch):
    async def _fake_fetch(url, state, *, force=False, client=None):
        return _FakeFetchedPage(url, "tiny")  # below _MIN_FULLTEXT_CHARS

    class _AR:
        abstract = "A longer abstract describing the paper in detail. " * 10

    async def _fake_search(query, max_results=1, **kw):
        assert query == "id:2401.2"
        return [_AR()]

    monkeypatch.setattr(pipeline, "web_fetch", _fake_fetch)
    monkeypatch.setattr(pipeline, "search_arxiv", _fake_search)
    desc = SourceDescriptor(kind="paper", source_kind="arxiv", canonical_key="2401.2", arxiv_id="2401.2")
    text, url = await pipeline._resolve_arxiv_fulltext(desc, _make_state())
    assert "longer abstract" in text


async def test_resolve_arxiv_fulltext_search_raises_returns_short(monkeypatch):
    async def _fake_fetch(url, state, *, force=False, client=None):
        return _FakeFetchedPage(url, "small body")

    async def _boom(query, max_results=1, **kw):
        raise RuntimeError("arxiv down")

    monkeypatch.setattr(pipeline, "web_fetch", _fake_fetch)
    monkeypatch.setattr(pipeline, "search_arxiv", _boom)
    desc = SourceDescriptor(kind="paper", source_kind="arxiv", canonical_key="2401.3", arxiv_id=None)
    text, url = await pipeline._resolve_arxiv_fulltext(desc, _make_state())
    assert text == "small body"  # keeps whatever ar5iv gave


async def test_resolve_arxiv_fulltext_abstract_not_longer_keeps_body(monkeypatch):
    async def _fake_fetch(url, state, *, force=False, client=None):
        return _FakeFetchedPage(url, "a moderately sized ar5iv body here")

    class _AR:
        abstract = "short"

    async def _fake_search(query, max_results=1, **kw):
        return [_AR()]

    monkeypatch.setattr(pipeline, "web_fetch", _fake_fetch)
    monkeypatch.setattr(pipeline, "search_arxiv", _fake_search)
    desc = SourceDescriptor(kind="paper", source_kind="arxiv", canonical_key="2401.4", arxiv_id="2401.4")
    text, _ = await pipeline._resolve_arxiv_fulltext(desc, _make_state())
    assert text == "a moderately sized ar5iv body here"  # abstract shorter -> body wins


# --------------------------------------------------------------------------
# _resolve_github_fulltext
# --------------------------------------------------------------------------


async def test_resolve_github_fulltext_happy(monkeypatch):
    def _handler(method, url, kw):
        if url.endswith("/readme"):
            return _FakeResp(200, text="# Readme\nUse this.")
        return _FakeResp(
            200,
            json_data={
                "description": "A great repo",
                "stargazers_count": 1234,
                "forks_count": 5,
                "language": "Python",
                "topics": ["ml", "nlp"],
                "license": {"spdx_id": "MIT"},
                "pushed_at": "2026-01-01",
                "html_url": "https://github.com/o/r",
            },
        )

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="code", source_kind="github", canonical_key="o/r", url="https://github.com/o/r")
    text, url = await pipeline._resolve_github_fulltext(desc, _make_state())
    assert "# o/r" in text and "A great repo" in text
    assert "Stars: 1234" in text and "License: MIT" in text and "## README" in text
    assert url == "https://github.com/o/r"


async def test_resolve_github_fulltext_meta_404_readme_missing(monkeypatch):
    def _handler(method, url, kw):
        if url.endswith("/readme"):
            return _FakeResp(404, text="not found")
        return _FakeResp(404, json_data={})

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="code", source_kind="github", canonical_key="o/r", url="https://github.com/o/r")
    text, url = await pipeline._resolve_github_fulltext(desc, _make_state())
    assert text == "# o/r"  # only the header, no meta/readme
    assert url == "https://github.com/o/r"  # falls back to desc.url


async def test_resolve_github_fulltext_readme_request_raises(monkeypatch):
    def _handler(method, url, kw):
        if url.endswith("/readme"):
            raise httpx.ReadTimeout("readme unreachable")
        return _FakeResp(200, json_data={"description": "repo desc"})

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="code", source_kind="github", canonical_key="o/r", url="https://github.com/o/r")
    text, _ = await pipeline._resolve_github_fulltext(desc, _make_state())
    assert "repo desc" in text and "## README" not in text  # readme swallowed


async def test_resolve_github_fulltext_meta_request_raises(monkeypatch):
    def _handler(method, url, kw):
        if url.endswith("/readme"):
            return _FakeResp(200, text="readme body")
        raise httpx.ConnectError("repo meta unreachable")

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="code", source_kind="github", canonical_key="o/r", url="https://github.com/o/r")
    text, _ = await pipeline._resolve_github_fulltext(desc, _make_state())
    assert "## README" in text  # meta swallowed, readme still landed


async def test_resolve_github_fulltext_uses_github_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok123")
    seen_headers = {}

    def _handler(method, url, kw):
        seen_headers.update(kw.get("headers") or {})
        return _FakeResp(404, json_data={})

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="code", source_kind="github", canonical_key="o/r", url="https://github.com/o/r")
    await pipeline._resolve_github_fulltext(desc, _make_state())
    assert seen_headers.get("Authorization") == "Bearer tok123"


# --------------------------------------------------------------------------
# _resolve_dataset_fulltext
# --------------------------------------------------------------------------


async def test_resolve_dataset_fulltext_happy(monkeypatch):
    def _handler(method, url, kw):
        if "/api/datasets/" in url:
            return _FakeResp(
                200,
                json_data={
                    "description": "A dataset",
                    "tags": [
                        "task_categories:text-classification",
                        "modality:text",
                        "language:en",
                        "size_categories:1K<n<10K",
                    ],
                    "downloads": 9000,
                    "likes": 12,
                    "lastModified": "2026-02-02",
                },
            )
        if url.endswith("/splits"):
            return _FakeResp(200, json_data={"splits": [{"config": "default", "split": "train"}]})
        if url.endswith("/first-rows"):
            return _FakeResp(
                200,
                json_data={
                    "features": [{"name": "text"}, {"name": "label"}, {}],
                    "rows": [{"row": {"text": "hi", "label": 1}}, {"row": {"text": "bye", "label": 0}}, {"row": {}}],
                },
            )
        return _FakeResp(404, json_data={})

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="dataset", source_kind="dataset", canonical_key="o/ds")
    text, url = await pipeline._resolve_dataset_fulltext(desc, _make_state())
    assert "# Dataset: o/ds" in text and "A dataset" in text
    assert "Tasks: text-classification" in text and "Modalities: text" in text
    assert "Languages: en" in text and "Downloads (30d): 9000" in text
    assert "Schema / features: text, label" in text and "Sample rows:" in text
    assert url == "https://huggingface.co/datasets/o/ds"


async def test_resolve_dataset_fulltext_meta_fails_preview_unavailable(monkeypatch):
    def _handler(method, url, kw):
        if "/api/datasets/" in url:
            raise httpx.ReadTimeout("hf hub slow")
        if url.endswith("/splits"):
            return _FakeResp(200, json_data={"splits": []})  # no splits -> no preview
        return _FakeResp(500, json_data={})

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="dataset", source_kind="dataset", canonical_key="o/ds")
    text, url = await pipeline._resolve_dataset_fulltext(desc, _make_state())
    assert text == "# Dataset: o/ds"  # nothing enriched
    assert url == "https://huggingface.co/datasets/o/ds"


async def test_resolve_dataset_fulltext_meta_non200_firstrows_non200(monkeypatch):
    # meta non-200 (meta stays {}) and first-rows non-200 (no features/rows).
    def _handler(method, url, kw):
        if "/api/datasets/" in url:
            return _FakeResp(404, json_data={"description": "ignored"})
        if url.endswith("/splits"):
            return _FakeResp(200, json_data={"splits": [{"config": "default", "split": "train"}]})
        if url.endswith("/first-rows"):
            return _FakeResp(500, json_data={"features": [{"name": "x"}]})
        return _FakeResp(404, json_data={})

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="dataset", source_kind="dataset", canonical_key="o/ds")
    text, _ = await pipeline._resolve_dataset_fulltext(desc, _make_state())
    assert text == "# Dataset: o/ds"  # meta 404 + first-rows 500 -> nothing enriched


async def test_resolve_dataset_fulltext_preview_raises(monkeypatch):
    def _handler(method, url, kw):
        if "/api/datasets/" in url:
            return _FakeResp(200, json_data={"description": "d"})
        raise httpx.ConnectError("datasets-server down")

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="dataset", source_kind="dataset", canonical_key="o/ds")
    text, _ = await pipeline._resolve_dataset_fulltext(desc, _make_state())
    assert "# Dataset: o/ds" in text and "d" in text  # preview swallowed, meta kept


# --------------------------------------------------------------------------
# _resolve_openml_fulltext
# --------------------------------------------------------------------------


async def test_resolve_openml_fulltext_happy(monkeypatch):
    def _handler(method, url, kw):
        if "/data/qualities/" in url:
            return _FakeResp(
                200,
                json_data={
                    "data_qualities": {
                        "quality": [
                            {"name": "NumberOfInstances", "value": "150"},
                            {"name": "NumberOfFeatures", "value": "4"},
                            {"name": "NumberOfClasses", "value": "3"},
                        ]
                    }
                },
            )
        return _FakeResp(
            200,
            json_data={
                "data_set_description": {
                    "name": "iris",
                    "description": "Classic iris dataset",
                    "default_target_attribute": "class",
                    "format": "ARFF",
                    "upload_date": "2014-01-01",
                }
            },
        )

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="dataset", source_kind="openml", canonical_key="openml:61")
    text, url = await pipeline._resolve_openml_fulltext(desc, _make_state())
    assert "# OpenML dataset: iris" in text and "Classic iris dataset" in text
    assert "Instances: 150" in text and "Features: 4" in text and "Classes: 3" in text
    assert "Target: class" in text and "Format: ARFF" in text
    assert url == "https://www.openml.org/d/61"


async def test_resolve_openml_fulltext_both_fail(monkeypatch):
    def _handler(method, url, kw):
        raise httpx.ConnectError("openml down")

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="dataset", source_kind="openml", canonical_key="openml:99")
    text, url = await pipeline._resolve_openml_fulltext(desc, _make_state())
    assert text == "# OpenML dataset: 99"  # falls back to the did, no meta
    assert url == "https://www.openml.org/d/99"


async def test_resolve_openml_fulltext_non200(monkeypatch):
    def _handler(method, url, kw):
        return _FakeResp(500, json_data={})

    _patch_httpx(monkeypatch, pipeline, _handler)
    desc = SourceDescriptor(kind="dataset", source_kind="openml", canonical_key="openml:7")
    text, _ = await pipeline._resolve_openml_fulltext(desc, _make_state())
    assert text == "# OpenML dataset: 7"


# --------------------------------------------------------------------------
# _resolve_fulltext dispatch by source_kind (exercise the routing)
# --------------------------------------------------------------------------


async def test_resolve_fulltext_routes_to_arxiv(monkeypatch):
    called = {}

    async def _fake_arxiv(desc, state):
        called["arxiv"] = True
        return "body", "url"

    monkeypatch.setattr(pipeline, "_resolve_arxiv_fulltext", _fake_arxiv)
    desc = SourceDescriptor(kind="paper", source_kind="arxiv", canonical_key="x")
    assert await pipeline._resolve_fulltext(desc, _make_state()) == ("body", "url")
    assert called["arxiv"]


async def test_resolve_fulltext_routes_to_github_dataset_openml(monkeypatch):
    async def _mk(name):
        async def _f(desc, state):
            return name, f"{name}-url"

        return _f

    monkeypatch.setattr(pipeline, "_resolve_github_fulltext", await _mk("github"))
    monkeypatch.setattr(pipeline, "_resolve_dataset_fulltext", await _mk("dataset"))
    monkeypatch.setattr(pipeline, "_resolve_openml_fulltext", await _mk("openml"))

    for sk in ("github", "dataset", "openml"):
        desc = SourceDescriptor(kind="code", source_kind=sk, canonical_key="x")
        text, _ = await pipeline._resolve_fulltext(desc, _make_state())
        assert text == sk


# --------------------------------------------------------------------------
# embed_and_finalize
# --------------------------------------------------------------------------


def _patch_embedder(monkeypatch, embedder):
    from library.corpus import tools as corpus_tools

    async def _get():
        return embedder

    monkeypatch.setattr(corpus_tools, "_get_embedder", _get)


async def test_finalize_happy(monkeypatch):
    _patch_embedder(monkeypatch, _FakeEmbedder())

    async def _merge(*a, **k):
        return None

    monkeypatch.setattr(pipeline, "merge_paper", _merge)
    doc = {
        "kind": "paper",
        "title": "T",
        "doi": None,
        "arxiv_id": "1",
        "trust_tier": "preprint",
        "published_at": None,
        "source_url": "u",
        "authors": ["A"],
        "status": "certified",
        "trust_state": "provisional",
    }
    st = _make_state(get_document=doc, get_chunk_plan=_plan(3))

    res = await pipeline.embed_and_finalize(5, st)
    assert res == {"document_id": 5, "queryable": True, "embedded": 3}
    st.set_chunk_embeddings.assert_awaited_once()
    st.set_document_queryable.assert_awaited_once_with(5, True)
    assert st.emit_corpus_event.await_args.args[0] == "document.ingested"


async def test_finalize_doc_not_found(monkeypatch):
    st = _make_state(get_document=None)
    res = await pipeline.embed_and_finalize(1, st)
    assert res == {"skipped": True, "reason": "not_found"}
    st.set_document_queryable.assert_not_awaited()


@pytest.mark.parametrize(
    "doc",
    [
        {"status": "blocked", "trust_state": "provisional"},
        {"status": "certified", "trust_state": "quarantined"},
        {"status": "certified", "trust_state": "decayed"},
    ],
)
async def test_finalize_blocked(monkeypatch, doc):
    st = _make_state(get_document=doc)
    res = await pipeline.embed_and_finalize(2, st)
    assert res == {"skipped": True, "reason": "blocked"}
    st.get_chunk_plan.assert_not_awaited()


async def test_finalize_no_chunks(monkeypatch):
    doc = {"status": "certified", "trust_state": "provisional"}
    st = _make_state(get_document=doc, get_chunk_plan=[])
    res = await pipeline.embed_and_finalize(3, st)
    assert res == {"skipped": True, "reason": "no_chunks"}
    st.set_document_queryable.assert_not_awaited()


async def test_finalize_all_chunks_already_embedded(monkeypatch):
    # every chunk has_embedding -> _embed_pending returns ([],0,0) -> no_embeddings
    _patch_embedder(monkeypatch, _FakeEmbedder())
    doc = {"status": "certified", "trust_state": "provisional"}
    plan = [{"ordinal": 0, "content_hash": "h", "text": "t", "has_embedding": True}]
    st = _make_state(get_document=doc, get_chunk_plan=plan)
    res = await pipeline.embed_and_finalize(4, st)
    assert res == {"skipped": True, "reason": "no_embeddings"}
    st.set_chunk_embeddings.assert_not_awaited()
    st.set_document_queryable.assert_not_awaited()


async def test_finalize_embed_all_fail_no_embeddings(monkeypatch):
    # every embed raises -> rows empty, embedded==0 -> no_embeddings
    _patch_embedder(monkeypatch, _FakeEmbedder(fail_on={"chunk text 0", "chunk text 1"}))
    doc = {"status": "certified", "trust_state": "provisional"}
    st = _make_state(get_document=doc, get_chunk_plan=_plan(2))
    res = await pipeline.embed_and_finalize(6, st)
    assert res == {"skipped": True, "reason": "no_embeddings"}


async def test_finalize_partial_embed_failure_still_finalizes(monkeypatch):
    # one of two chunks fails -> embedded==1, doc finalized
    _patch_embedder(monkeypatch, _FakeEmbedder(fail_on={"chunk text 1"}))

    async def _merge(*a, **k):
        return None

    monkeypatch.setattr(pipeline, "merge_paper", _merge)
    doc = {"status": "certified", "trust_state": "provisional", "published_at": None}
    st = _make_state(get_document=doc, get_chunk_plan=_plan(2))
    res = await pipeline.embed_and_finalize(7, st)
    assert res["queryable"] is True and res["embedded"] == 1


async def test_finalize_merge_paper_failure_swallowed(monkeypatch):
    _patch_embedder(monkeypatch, _FakeEmbedder())

    async def _boom(*a, **k):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(pipeline, "merge_paper", _boom)
    doc = {"status": "certified", "trust_state": "provisional", "published_at": None}
    st = _make_state(get_document=doc, get_chunk_plan=_plan(1))
    res = await pipeline.embed_and_finalize(8, st)  # merge raises but is swallowed
    assert res["queryable"] is True
    st.set_document_queryable.assert_awaited_once_with(8, True)


async def test_finalize_published_at_year_passed_to_merge(monkeypatch):
    _patch_embedder(monkeypatch, _FakeEmbedder())
    captured = {}

    async def _merge(document_id, **k):
        captured.update(k)

    monkeypatch.setattr(pipeline, "merge_paper", _merge)

    class _Dt:
        year = 2025

    doc = {"status": "certified", "trust_state": "provisional", "published_at": _Dt(), "authors": None}
    st = _make_state(get_document=doc, get_chunk_plan=_plan(1))
    await pipeline.embed_and_finalize(9, st)
    assert captured["year"] == 2025
    assert captured["authors"] == []  # None -> []


# --------------------------------------------------------------------------
# _embed_pending direct (batch boundary + empty)
# --------------------------------------------------------------------------


async def test_embed_pending_empty_plan(monkeypatch):
    rows, embedded, failed = await pipeline._embed_pending([])
    assert (rows, embedded, failed) == ([], 0, 0)


async def test_embed_pending_batches_over_boundary(monkeypatch):
    _patch_embedder(monkeypatch, _FakeEmbedder())
    n = pipeline._EMBED_BATCH + 3  # force a second batch
    plan = [{"ordinal": i, "content_hash": f"h{i}", "text": f"t{i}", "has_embedding": False} for i in range(n)]
    rows, embedded, failed = await pipeline._embed_pending(plan)
    assert embedded == n and failed == 0 and len(rows) == n
    assert rows[0]["embed_model"]  # carries the model id


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def test_source_target_id_stable_and_positive():
    a = pipeline._source_target_id("some/key")
    b = pipeline._source_target_id("some/key")
    assert a == b and a > 0
    assert a != pipeline._source_target_id("other/key")


# ==========================================================================
# scouts
# ==========================================================================


async def test_scout_web_happy(monkeypatch):
    def _handler(method, url, kw):
        return _FakeResp(
            200,
            json_data={
                "results": [
                    {"url": "https://a.test", "title": "A"},
                    {"url": "https://b.test", "title": "B"},
                    {"url": "https://a.test", "title": "dup"},  # dupe url -> deduped
                    {"title": "no url"},  # missing url -> skipped
                ]
            },
        )

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_web(["topic"], per_topic=5)
    keys = sorted(d.canonical_key for d in out)
    assert keys == ["https://a.test", "https://b.test"]
    assert all(d.source_kind == "web" and d.kind == "web" for d in out)


async def test_scout_web_non200_logged_skipped(monkeypatch):
    def _handler(method, url, kw):
        return _FakeResp(503, json_data={})

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_web(["t1", "t2"])
    assert out == []


async def test_scout_web_request_raises(monkeypatch):
    def _handler(method, url, kw):
        raise httpx.ConnectError("searxng unreachable")

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_web(["t"])
    assert out == []


async def test_scout_web_pageno_from_start(monkeypatch):
    seen = {}

    def _handler(method, url, kw):
        seen.update(kw.get("params") or {})
        return _FakeResp(200, json_data={"results": []})

    _patch_httpx(monkeypatch, scouts, _handler)
    await scouts.scout_web(["t"], per_topic=5, start=10)
    assert seen["pageno"] == 10 // 5 + 1  # == 3


async def test_scout_github_happy(monkeypatch):
    def _handler(method, url, kw):
        return _FakeResp(
            200,
            json_data={
                "items": [
                    {"full_name": "o/r1", "html_url": "https://github.com/o/r1"},
                    {"full_name": "o/r2", "html_url": "https://github.com/o/r2"},
                    {"full_name": "o/r1"},  # dup -> skipped
                    {"html_url": "https://github.com/o/r3"},  # no full_name -> skipped
                ]
            },
        )

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_github(["t"], per_topic=5)
    assert sorted(d.canonical_key for d in out) == ["o/r1", "o/r2"]
    assert all(d.kind == "code" and d.source_kind == "github" for d in out)


async def test_scout_github_non200_empty(monkeypatch):
    def _handler(method, url, kw):
        return _FakeResp(403, json_data={"items": [{"full_name": "x/y"}]})

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_github(["t"])
    assert out == []  # non-200 -> items treated as []


async def test_scout_github_request_raises(monkeypatch):
    def _handler(method, url, kw):
        raise httpx.ReadTimeout("github slow")

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_github(["a", "b"])  # two topics, both fail
    assert out == []


async def test_scout_github_uses_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc")
    captured = {}

    def _factory(*a, **k):
        captured.update(k.get("headers") or {})
        return _FakeClient(lambda *_: _FakeResp(200, json_data={"items": []}))

    monkeypatch.setattr(scouts.httpx, "AsyncClient", _factory)
    await scouts.scout_github(["t"])
    assert captured.get("Authorization") == "Bearer ghp_abc"


async def test_scout_dataset_happy(monkeypatch):
    def _handler(method, url, kw):
        return _FakeResp(
            200,
            json_data=[
                {"id": "o/ds1", "downloads": 100},
                {"id": "o/ds2", "downloads": 50},
                {"id": "o/ds1"},  # dup -> skipped
                {"downloads": 9},  # no id -> skipped
            ],
        )

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_dataset(["t"], per_topic=5)
    assert sorted(d.canonical_key for d in out) == ["o/ds1", "o/ds2"]
    d = next(x for x in out if x.canonical_key == "o/ds1")
    assert d.url == "https://huggingface.co/datasets/o/ds1"
    assert "downloads=100" in (d.why or "")


async def test_scout_dataset_non200_empty(monkeypatch):
    def _handler(method, url, kw):
        return _FakeResp(500, json_data=[{"id": "x/y"}])

    _patch_httpx(monkeypatch, scouts, _handler)
    assert await scouts.scout_dataset(["t"]) == []


async def test_scout_dataset_request_raises(monkeypatch):
    def _handler(method, url, kw):
        raise httpx.ConnectError("hf down")

    _patch_httpx(monkeypatch, scouts, _handler)
    assert await scouts.scout_dataset(["t"]) == []


# --------------------------------------------------------------------------
# scout_openml — exact + substring + 412 fallback
# --------------------------------------------------------------------------


async def test_openml_topic_tokens():
    assert scouts._openml_topic_tokens("Image Classification ML") == ["image", "classification"]
    assert scouts._openml_topic_tokens("a bb ccc") == []  # all < 4 chars


async def test_scout_openml_exact_and_substring(monkeypatch):
    def _handler(method, url, kw):
        if "/data/list/status/active/" in url:  # active pool
            return _FakeResp(
                200,
                json_data={
                    "data": {
                        "dataset": [
                            {"did": 10, "name": "mnist_784"},
                            {"did": 11, "name": "fashion_image_set"},
                            {"did": 12, "name": "unrelated"},
                        ]
                    }
                },
            )
        if "/data/list/data_name/" in url:  # exact lookup
            return _FakeResp(200, json_data={"data": {"dataset": [{"did": 99, "name": "image"}]}})
        return _FakeResp(404, json_data={})

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_openml(["image classification"], per_topic=5)
    keys = {d.canonical_key for d in out}
    # exact (did 99) + substring matches on "image"/"classification" tokens (did 11)
    assert "openml:99" in keys and "openml:11" in keys
    assert "openml:12" not in keys  # 'unrelated' matched no token
    assert all(d.source_kind == "openml" and d.kind == "dataset" for d in out)


async def test_scout_openml_exact_412_fallback_to_substring(monkeypatch):
    def _handler(method, url, kw):
        if "/data/list/status/active/" in url:
            return _FakeResp(200, json_data={"data": {"dataset": [{"did": 5, "name": "credit_data"}]}})
        if "/data/list/data_name/" in url:
            return _FakeResp(412, json_data={})  # the EXPECTED 'No results' miss
        return _FakeResp(404, json_data={})

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_openml(["credit scoring"], per_topic=5)
    assert {d.canonical_key for d in out} == {"openml:5"}  # exact missed, substring caught it


async def test_scout_openml_exact_dedupes_against_substring(monkeypatch):
    # exact and substring both surface the same did -> deduped by key.
    def _handler(method, url, kw):
        if "/data/list/status/active/" in url:
            return _FakeResp(200, json_data={"data": {"dataset": [{"did": 7, "name": "image_bench"}]}})
        if "/data/list/data_name/" in url:
            return _FakeResp(200, json_data={"data": {"dataset": [{"did": 7, "name": "image_bench"}]}})
        return _FakeResp(404, json_data={})

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_openml(["image"], per_topic=5)
    assert [d.canonical_key for d in out] == ["openml:7"]  # only once despite both hits


async def test_scout_openml_exact_request_raises(monkeypatch):
    # the exact-lookup request raising is swallowed -> falls through to substring.
    def _handler(method, url, kw):
        if "/data/list/status/active/" in url:
            return _FakeResp(200, json_data={"data": {"dataset": [{"did": 3, "name": "credit_image"}]}})
        if "/data/list/data_name/" in url:
            raise httpx.ConnectError("exact lookup down")
        return _FakeResp(404, json_data={})

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_openml(["image"], per_topic=5)
    assert {d.canonical_key for d in out} == {"openml:3"}  # exact raised, substring caught it


async def test_scout_openml_active_pool_non200(monkeypatch):
    def _handler(method, url, kw):
        if "/data/list/status/active/" in url:
            return _FakeResp(503, json_data={})  # outage -> [] (logged)
        if "/data/list/data_name/" in url:
            return _FakeResp(200, json_data={"data": {"dataset": [{"did": 1, "name": "iris"}]}})
        return _FakeResp(404, json_data={})

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_openml(["irisset"], per_topic=5)
    # pool empty (degraded) but exact lookup on 'irisset' still returns the dataset
    assert {d.canonical_key for d in out} == {"openml:1"}


async def test_scout_openml_active_pool_raises(monkeypatch):
    def _handler(method, url, kw):
        if "/data/list/status/active/" in url:
            raise httpx.ConnectError("openml down")
        return _FakeResp(412, json_data={})  # exact also misses

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_openml(["benchmark"], per_topic=5)
    assert out == []


async def test_scout_openml_no_alpha_keyword_skips_exact(monkeypatch):
    seen_urls = []

    def _handler(method, url, kw):
        seen_urls.append(url)
        if "/data/list/status/active/" in url:
            return _FakeResp(200, json_data={"data": {"dataset": []}})
        return _FakeResp(404, json_data={})

    _patch_httpx(monkeypatch, scouts, _handler)
    # topic with only short words -> kw="" -> no exact call, no tokens -> no substr
    await scouts.scout_openml(["a bb c"], per_topic=5)
    assert not any("/data_name/" in u for u in seen_urls)


async def test_scout_openml_did_none_skipped_and_per_topic_cap(monkeypatch):
    def _handler(method, url, kw):
        if "/data/list/status/active/" in url:
            return _FakeResp(
                200,
                json_data={
                    "data": {
                        "dataset": [
                            {"did": None, "name": "imageA"},  # did None -> skipped
                            {"did": 1, "name": "imageB"},
                            {"did": 2, "name": "imageC"},
                            {"did": 3, "name": "imageD"},
                        ]
                    }
                },
            )
        return _FakeResp(412, json_data={})

    _patch_httpx(monkeypatch, scouts, _handler)
    out = await scouts.scout_openml(["image"], per_topic=2)  # cap at 2 added
    assert len(out) == 2
    assert {d.canonical_key for d in out} == {"openml:1", "openml:2"}


async def test_scout_openml_active_pool_offset_in_url(monkeypatch):
    seen_urls = []

    def _handler(method, url, kw):
        seen_urls.append(url)
        if "/data/list/status/active/" in url:
            return _FakeResp(200, json_data={"data": {"dataset": []}})
        return _FakeResp(412, json_data={})

    _patch_httpx(monkeypatch, scouts, _handler)
    await scouts.scout_openml(["benchmark"], per_topic=5, start=200)
    assert any("/offset/200" in u for u in seen_urls)


# --------------------------------------------------------------------------
# scout_arxiv — failure path (happy + dedupe already covered in test_search_arxiv)
# --------------------------------------------------------------------------


async def test_scout_arxiv_topic_failure_continues(monkeypatch):
    class _R:
        def __init__(self, aid):
            self.arxiv_id = aid
            self.pdf_url = f"https://arxiv.org/pdf/{aid}"
            self.title = f"T{aid}"

    calls = {"n": 0}

    async def _search(query, max_results=5, start=0, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first topic down")  # must not sink the sweep
        return [_R("2401.9")]

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(scouts, "search_arxiv", _search)
    monkeypatch.setattr(scouts.asyncio, "sleep", _no_sleep)
    out = await scouts.scout_arxiv(["bad", "good"], per_topic=5)
    assert [d.canonical_key for d in out] == ["2401.9"]


async def test_scout_arxiv_dedupes_within_results(monkeypatch):
    class _R:
        def __init__(self, aid):
            self.arxiv_id = aid
            self.pdf_url = f"https://arxiv.org/pdf/{aid}"
            self.title = f"T{aid}"

    async def _search(query, max_results=5, start=0, **kw):
        return [_R("2401.7"), _R("2401.7"), _R("2401.8")]  # repeated id -> deduped

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(scouts, "search_arxiv", _search)
    monkeypatch.setattr(scouts.asyncio, "sleep", _no_sleep)
    out = await scouts.scout_arxiv(["t"], per_topic=5)
    assert sorted(d.canonical_key for d in out) == ["2401.7", "2401.8"]


async def test_scout_arxiv_query_prefixing(monkeypatch):
    seen = []

    async def _search(query, max_results=5, start=0, **kw):
        seen.append(query)
        return []

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(scouts, "search_arxiv", _search)
    monkeypatch.setattr(scouts.asyncio, "sleep", _no_sleep)
    await scouts.scout_arxiv(["plain topic", "cat:cs.CL"], per_topic=3)
    assert seen[0] == "all:plain topic"  # no ':' -> prefixed
    assert seen[1] == "cat:cs.CL"  # already qualified -> untouched
