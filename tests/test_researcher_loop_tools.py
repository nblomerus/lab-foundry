"""Tests for agents.researcher.loop (orchestrator) and agents.researcher.tools.

No real Postgres / network / LLM. The loop drives the model through
`dispatcher.router.invoke(..., output_schema_class=...)` (returns
``(obj, run_id)``) and `dispatcher.curator.build(...)`, so we hand it a
`_Router` stub that returns canned REAL pydantic schema objects keyed by the
requested `output_schema_class`, and a curator stub that returns a dummy prompt.
Source search, page fetch, and experiment dispatch are monkeypatched at the
loop's module boundary. tools.py network (httpx GET/POST) is patched at the
call site.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agents.researcher import loop as rloop
from agents.researcher import tools as rtools
from agents.researcher.schemas import (
    EvidenceBatch,
    EvidenceItem,
    ExperimentInterpretation,
    FindingOut,
    GapCheck,
    InquiryPlan,
    ProposedExperiment,
    SubQuestion,
    Synthesis,
)
from agents.researcher.tools import (
    SearchResult,
    search_hacker_news,
    search_reddit,
    search_web,
)
from tests._helpers import make_dispatcher, make_state

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Task:
    """Minimal stand-in for the claimed task row the loop reads."""

    def __init__(self, *, tid=1, description="Is MCP adoption real?", thesis_id=7, claim_id=11):
        self.id = tid
        self.description = description
        self.thesis_id = thesis_id
        self.claim_id = claim_id


class _Router:
    """Stub for dispatcher.router. `invoke()` returns canned (obj, run_id)
    tuples keyed by the `output_schema_class`. Each invoke increments a run-id
    counter so the loop's persistence calls get distinct ids. The call log lets
    tests assert which schemas were requested and with what step_name."""

    def __init__(self, by_schema: dict):
        self.by_schema = by_schema
        self._next_run = 100
        self.calls: list[dict] = []

    async def invoke(self, *, prompt, output_schema_class, **kw):
        self._next_run += 1
        self.calls.append({"schema": output_schema_class, "kw": kw, "prompt": prompt})
        canned = self.by_schema.get(output_schema_class)
        if canned is None:
            raise AssertionError(f"router got an unexpected schema: {output_schema_class!r}")
        obj = canned.pop(0) if isinstance(canned, list) else canned
        if isinstance(obj, Exception):
            raise obj
        return obj, self._next_run

    def schemas_seen(self):
        return [c["schema"] for c in self.calls]


class _Curator:
    """Stub for dispatcher.curator. `build()` returns a trivial prompt object
    and records the invocation_type so tests can assert the recipe flow."""

    def __init__(self):
        self.built: list[str] = []

    async def build(self, *, invocation_type, context):
        self.built.append(invocation_type)
        return {"invocation_type": invocation_type, "context": context}


def _page(url, content="real page body with a concrete number 42 and a price $9"):
    """A duck-typed fetched page (loop only reads `.url` and `.content`)."""
    return types.SimpleNamespace(url=url, content=content)


def _sub_q(q="What is the adoption rate?", sources=("web",), k=3):
    return SubQuestion(q=q, sources=list(sources), why="matters because", k=k)


def _evidence_batch(n=1):
    return EvidenceBatch(
        evidence=[
            EvidenceItem(quote=f"verbatim line {i}", claim=f"claim {i}", stance="supports", confidence=0.7)
            for i in range(n)
        ]
    )


def _synthesis(findings=1, summary="Adoption is real.", weakest=-1, open_qs=()):
    return Synthesis(
        summary=summary,
        findings=[
            FindingOut(
                source="web",
                url=f"https://ex.test/{i}",
                title=f"Finding {i}",
                summary="A concrete finding.",
                relevance_score=5.0,
                why_it_matters="it matters",
                supports_thesis=True,
            )
            for i in range(findings)
        ],
        weakest_subquestion_idx=weakest,
        open_questions=list(open_qs),
    )


def _gap(*, has_gaps=False, should_iterate=False, followups=(), gaps=()):
    return GapCheck(
        has_gaps=has_gaps,
        gaps=list(gaps),
        proposed_followups=list(followups),
        should_iterate=should_iterate,
        reason="stopping is the right call",
    )


def _make_dispatcher(router, curator, **state_returns):
    """Dispatcher wired with a state whose persistence methods return ids."""
    defaults = dict(
        record_inquiry=500,
        record_evidence=600,
        record_finding=700,
        start_experiment=800,
        complete_experiment=None,
        fail_experiment=None,
        get_findings=[],
    )
    defaults.update(state_returns)
    state = make_state(**defaults)
    return make_dispatcher(state=state, router=router, curator=curator, session=None)


def _patch_sources(monkeypatch, by_query=None, default=None, raises=None):
    """Patch loop._SOURCE_TOOLS so every named source returns canned hits.

    `default` is a list of SearchResult. `raises` (exc) makes the tool blow up
    (loop must swallow it). `by_query` maps source->callable for fine control.
    """
    default = (
        default
        if default is not None
        else [
            SearchResult(title="T", url="https://ex.test/a", snippet="s", source="web"),
        ]
    )

    def _make(src):
        async def _tool(query, limit, **_):
            if raises is not None:
                raise raises
            return list(default)

        return _tool

    new = {}
    for src in ("web", "reddit", "hacker_news"):
        new[src] = (by_query or {}).get(src) or _make(src)
    monkeypatch.setattr(rloop, "_SOURCE_TOOLS", new)


def _patch_fetch(monkeypatch, pages):
    async def _fetch_many(urls, state, concurrency=4):
        return list(pages)

    monkeypatch.setattr(rloop, "web_fetch_many", _fetch_many)


# ===========================================================================
# loop: run_research_task — happy path, one iteration
# ===========================================================================


@pytest.mark.asyncio
async def test_single_iteration_full_flow(monkeypatch):
    plan = InquiryPlan(question="frame?", sub_questions=[_sub_q()], proposed_experiments=[])
    router = _Router({InquiryPlan: plan, EvidenceBatch: _evidence_batch(2), Synthesis: _synthesis(findings=1)})
    curator = _Curator()
    disp = _make_dispatcher(router, curator)
    _patch_sources(monkeypatch)
    _patch_fetch(monkeypatch, [_page("https://ex.test/a")])

    # MAX_ITERATIONS is 2; force a single pass so gap_check is never reached.
    monkeypatch.setattr(rloop, "MAX_ITERATIONS", 1)

    out = await rloop.run_research_task(_Task(), disp, triggered_by_event_id=9)

    assert out["iterations"] == 1
    assert out["inquiry_ids"] == [500]
    assert out["evidence_count"] == 2
    assert out["experiments_run"] == 0
    assert out["findings"] == [700]
    # plan + extract + synthesize requested; no gap_check on the last iteration.
    assert InquiryPlan in router.schemas_seen()
    assert EvidenceBatch in router.schemas_seen()
    assert Synthesis in router.schemas_seen()
    assert GapCheck not in router.schemas_seen()
    assert curator.built[0] == "researcher.plan_inquiry"
    disp.state.record_inquiry.assert_awaited()
    assert disp.state.record_evidence.await_count == 2
    disp.state.record_finding.assert_awaited()


@pytest.mark.asyncio
async def test_zero_findings_synthesis(monkeypatch):
    plan = InquiryPlan(question="frame?", sub_questions=[_sub_q()], proposed_experiments=[])
    router = _Router({InquiryPlan: plan, EvidenceBatch: _evidence_batch(1), Synthesis: _synthesis(findings=0)})
    disp = _make_dispatcher(router, _Curator())
    _patch_sources(monkeypatch)
    _patch_fetch(monkeypatch, [_page("https://ex.test/a")])
    monkeypatch.setattr(rloop, "MAX_ITERATIONS", 1)

    out = await rloop.run_research_task(_Task(), disp)

    assert out["findings"] == []
    disp.state.record_finding.assert_not_called()


@pytest.mark.asyncio
async def test_no_search_results_skips_subquestion(monkeypatch):
    plan = InquiryPlan(question="frame?", sub_questions=[_sub_q()], proposed_experiments=[])
    router = _Router({InquiryPlan: plan, Synthesis: _synthesis(findings=0)})
    disp = _make_dispatcher(router, _Curator())
    _patch_sources(monkeypatch, default=[])  # no hits → continue, no fetch
    fetch_called = {"n": 0}

    async def _fetch_many(urls, state, concurrency=4):
        fetch_called["n"] += 1
        return []

    monkeypatch.setattr(rloop, "web_fetch_many", _fetch_many)
    monkeypatch.setattr(rloop, "MAX_ITERATIONS", 1)

    out = await rloop.run_research_task(_Task(), disp)

    assert out["evidence_count"] == 0
    assert fetch_called["n"] == 0  # never fetched: no urls
    assert EvidenceBatch not in router.schemas_seen()


@pytest.mark.asyncio
async def test_blank_and_none_pages_skipped(monkeypatch):
    plan = InquiryPlan(question="frame?", sub_questions=[_sub_q()], proposed_experiments=[])
    router = _Router({InquiryPlan: plan, EvidenceBatch: _evidence_batch(1), Synthesis: _synthesis(0)})
    disp = _make_dispatcher(router, _Curator())
    _patch_sources(monkeypatch)
    # None page and a whitespace-only page are both skipped before extract.
    _patch_fetch(monkeypatch, [None, _page("https://ex.test/blank", content="   \n  ")])
    monkeypatch.setattr(rloop, "MAX_ITERATIONS", 1)

    out = await rloop.run_research_task(_Task(), disp)

    assert out["evidence_count"] == 0
    assert EvidenceBatch not in router.schemas_seen()


@pytest.mark.asyncio
async def test_page_cap_limits_fetched_urls(monkeypatch):
    # Source returns 10 hits but MAX_PAGES_PER_SUBQ caps the urls passed to fetch.
    many = [SearchResult(title=f"T{i}", url=f"https://ex.test/{i}", snippet="s", source="web") for i in range(10)]
    # k=8 lets all 10 hits through the per-source cap so MAX_PAGES is the binder.
    plan = InquiryPlan(question="frame?", sub_questions=[_sub_q(k=8)], proposed_experiments=[])
    router = _Router({InquiryPlan: plan, EvidenceBatch: _evidence_batch(0), Synthesis: _synthesis(0)})
    disp = _make_dispatcher(router, _Curator())
    _patch_sources(monkeypatch, default=many)
    captured = {}

    async def _fetch_many(urls, state, concurrency=4):
        captured["urls"] = list(urls)
        return []

    monkeypatch.setattr(rloop, "web_fetch_many", _fetch_many)
    monkeypatch.setattr(rloop, "MAX_ITERATIONS", 1)

    await rloop.run_research_task(_Task(), disp)

    assert len(captured["urls"]) == rloop.MAX_PAGES_PER_SUBQ


@pytest.mark.asyncio
async def test_extract_failure_is_swallowed(monkeypatch):
    plan = InquiryPlan(question="frame?", sub_questions=[_sub_q()], proposed_experiments=[])
    # EvidenceBatch invoke raises once → that page is skipped; loop still synthesizes.
    router = _Router({InquiryPlan: plan, EvidenceBatch: [RuntimeError("extract boom")], Synthesis: _synthesis(0)})
    disp = _make_dispatcher(router, _Curator())
    _patch_sources(monkeypatch)
    _patch_fetch(monkeypatch, [_page("https://ex.test/a")])
    monkeypatch.setattr(rloop, "MAX_ITERATIONS", 1)

    out = await rloop.run_research_task(_Task(), disp)

    assert out["evidence_count"] == 0  # the only page failed extract
    disp.state.record_evidence.assert_not_called()


@pytest.mark.asyncio
async def test_source_failure_is_non_fatal(monkeypatch):
    plan = InquiryPlan(
        question="frame?",
        sub_questions=[_sub_q(sources=("web", "reddit"))],
        proposed_experiments=[],
    )
    router = _Router({InquiryPlan: plan, EvidenceBatch: _evidence_batch(1), Synthesis: _synthesis(0)})
    disp = _make_dispatcher(router, _Curator())

    async def _ok(query, limit, **_):
        return [SearchResult(title="ok", url="https://ex.test/ok", snippet="s", source="web")]

    async def _boom(query, limit, **_):
        raise RuntimeError("reddit down")

    monkeypatch.setattr(rloop, "_SOURCE_TOOLS", {"web": _ok, "reddit": _boom, "hacker_news": _ok})
    _patch_fetch(monkeypatch, [_page("https://ex.test/ok")])
    monkeypatch.setattr(rloop, "MAX_ITERATIONS", 1)

    out = await rloop.run_research_task(_Task(), disp)

    # web hit survived; reddit failure swallowed → still got evidence.
    assert out["evidence_count"] == 1


@pytest.mark.asyncio
async def test_search_for_sub_question_merges_and_caps(monkeypatch):
    async def _web(query, limit, **_):
        return [SearchResult(title=f"w{i}", url=f"https://w/{i}", snippet="s", source="web") for i in range(5)]

    async def _hn(query, limit, **_):
        return [SearchResult(title="h0", url="https://h/0", snippet="s", source="hacker_news")]

    monkeypatch.setattr(rloop, "_SOURCE_TOOLS", {"web": _web, "hacker_news": _hn, "reddit": _web})
    out = await rloop._search_for_sub_question("q", ["web", "hacker_news"], k=2)
    # k=2 caps each source's contribution: 2 from web + 1 from hn.
    assert [r.url for r in out] == ["https://w/0", "https://w/1", "https://h/0"]


@pytest.mark.asyncio
async def test_search_for_sub_question_unknown_source_skipped(monkeypatch):
    async def _web(query, limit, **_):
        return [SearchResult(title="w", url="https://w/0", snippet="s", source="web")]

    monkeypatch.setattr(rloop, "_SOURCE_TOOLS", {"web": _web, "hacker_news": _web, "reddit": _web})
    out = await rloop._search_for_sub_question("q", ["nope", "web"], k=3)
    assert [r.url for r in out] == ["https://w/0"]


# ===========================================================================
# loop: experiments path
# ===========================================================================


@pytest.mark.asyncio
async def test_experiment_runs_and_interprets(monkeypatch):
    plan = InquiryPlan(
        question="frame?",
        sub_questions=[_sub_q()],
        proposed_experiments=[ProposedExperiment(kind="count_demand_signal", params={"phrases": ["x"]}, why="w")],
    )
    interp = ExperimentInterpretation(summary="useful", bears_on_subquestion_idxs=[0], confidence=0.6)
    router = _Router(
        {
            InquiryPlan: plan,
            EvidenceBatch: _evidence_batch(0),
            ExperimentInterpretation: interp,
            Synthesis: _synthesis(1),
        }
    )
    disp = _make_dispatcher(router, _Curator())
    _patch_sources(monkeypatch)
    _patch_fetch(monkeypatch, [_page("https://ex.test/a")])
    monkeypatch.setattr(rloop, "MAX_ITERATIONS", 1)

    async def _dispatch(kind, params, *, dispatcher):
        return {"grand_total": 42, "kind": kind}

    monkeypatch.setattr("agents.researcher.experiments.dispatch", _dispatch)

    out = await rloop.run_research_task(_Task(), disp)

    assert out["experiments_run"] == 1
    disp.state.start_experiment.assert_awaited()
    disp.state.complete_experiment.assert_awaited()
    assert ExperimentInterpretation in router.schemas_seen()


@pytest.mark.asyncio
async def test_experiment_failure_recorded_and_swallowed(monkeypatch):
    plan = InquiryPlan(
        question="frame?",
        sub_questions=[_sub_q()],
        proposed_experiments=[ProposedExperiment(kind="compare_repo_growth", params={"repos": ["a/b"]}, why="w")],
    )
    router = _Router({InquiryPlan: plan, EvidenceBatch: _evidence_batch(0), Synthesis: _synthesis(0)})
    disp = _make_dispatcher(router, _Curator())
    _patch_sources(monkeypatch)
    _patch_fetch(monkeypatch, [_page("https://ex.test/a")])
    monkeypatch.setattr(rloop, "MAX_ITERATIONS", 1)

    async def _dispatch(kind, params, *, dispatcher):
        raise RuntimeError("experiment exploded")

    monkeypatch.setattr("agents.researcher.experiments.dispatch", _dispatch)

    out = await rloop.run_research_task(_Task(), disp)

    # Experiment ran (one dict) but as a failure; no interpret call happened.
    assert out["experiments_run"] == 1
    disp.state.fail_experiment.assert_awaited()
    disp.state.complete_experiment.assert_not_called()
    assert ExperimentInterpretation not in router.schemas_seen()


# ===========================================================================
# loop: two iterations + gap_check
# ===========================================================================


@pytest.mark.asyncio
async def test_iterate_when_gap_check_says_so(monkeypatch):
    plan1 = InquiryPlan(question="frame?", sub_questions=[_sub_q()], proposed_experiments=[])
    plan2 = InquiryPlan(question="frame2?", sub_questions=[_sub_q(q="follow up?")], proposed_experiments=[])
    gap = _gap(has_gaps=True, should_iterate=True, followups=[_sub_q(q="next?")], gaps=["missing X"])
    router = _Router(
        {
            InquiryPlan: [plan1, plan2],
            EvidenceBatch: _evidence_batch(1),
            Synthesis: [_synthesis(1), _synthesis(1)],
            GapCheck: gap,
        }
    )
    disp = _make_dispatcher(router, _Curator(), get_findings=[])
    _patch_sources(monkeypatch)
    _patch_fetch(monkeypatch, [_page("https://ex.test/a")])
    # default MAX_ITERATIONS = 2

    out = await rloop.run_research_task(_Task(claim_id=None), disp)

    assert out["iterations"] == 2
    assert len(out["inquiry_ids"]) == 2
    assert router.schemas_seen().count(InquiryPlan) == 2


@pytest.mark.asyncio
async def test_gap_check_no_iterate_stops(monkeypatch):
    plan = InquiryPlan(question="frame?", sub_questions=[_sub_q()], proposed_experiments=[])
    gap = _gap(has_gaps=False, should_iterate=False)
    router = _Router({InquiryPlan: plan, EvidenceBatch: _evidence_batch(1), Synthesis: _synthesis(1), GapCheck: gap})
    disp = _make_dispatcher(router, _Curator())
    _patch_sources(monkeypatch)
    _patch_fetch(monkeypatch, [_page("https://ex.test/a")])

    out = await rloop.run_research_task(_Task(claim_id=None), disp)

    # gap_check ran but should_iterate is False → single planning pass.
    assert out["iterations"] == 1
    assert GapCheck in router.schemas_seen()
    assert router.schemas_seen().count(InquiryPlan) == 1


@pytest.mark.asyncio
async def test_gap_iterate_true_but_no_followups_stops(monkeypatch):
    plan = InquiryPlan(question="frame?", sub_questions=[_sub_q()], proposed_experiments=[])
    gap = _gap(has_gaps=True, should_iterate=True, followups=[])  # no followups → stop
    router = _Router({InquiryPlan: plan, EvidenceBatch: _evidence_batch(1), Synthesis: _synthesis(1), GapCheck: gap})
    disp = _make_dispatcher(router, _Curator())
    _patch_sources(monkeypatch)
    _patch_fetch(monkeypatch, [_page("https://ex.test/a")])

    out = await rloop.run_research_task(_Task(claim_id=None), disp)

    assert out["iterations"] == 1


@pytest.mark.asyncio
async def test_second_iteration_pulls_prior_findings(monkeypatch):
    plan = InquiryPlan(question="frame?", sub_questions=[_sub_q()], proposed_experiments=[])
    gap = _gap(has_gaps=True, should_iterate=True, followups=[_sub_q(q="next?")], gaps=["g"])
    prior = types.SimpleNamespace(id=700, source="web", title="Prior", summary="prior summary", relevance_score=4.0)
    router = _Router(
        {
            InquiryPlan: [plan, plan],
            EvidenceBatch: _evidence_batch(1),
            Synthesis: [_synthesis(1), _synthesis(1)],
            GapCheck: gap,
        }
    )
    disp = _make_dispatcher(router, _Curator(), get_findings=[prior])
    _patch_sources(monkeypatch)
    _patch_fetch(monkeypatch, [_page("https://ex.test/a")])

    out = await rloop.run_research_task(_Task(claim_id=None), disp)

    assert out["iterations"] == 2
    # get_findings is consulted on iteration 2 because iter-1 emitted a finding.
    disp.state.get_findings.assert_awaited()


# ===========================================================================
# loop: gap-source acquire request (MIMIR_LOOP gating)
# ===========================================================================


@pytest.mark.asyncio
async def test_request_gap_sources_noop_without_mimir_loop(monkeypatch):
    monkeypatch.delenv("MIMIR_LOOP", raising=False)
    state = make_state()
    # Should return early; no exception even with a fully-populated gap.
    await rloop._request_gap_sources(_gap(has_gaps=True, gaps=["x"]), _Task(), state)


@pytest.mark.asyncio
async def test_request_gap_sources_noop_when_no_gaps(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    state = make_state()
    await rloop._request_gap_sources(_gap(has_gaps=True, gaps=[]), _Task(), state)
    await rloop._request_gap_sources(_gap(has_gaps=True, gaps=["   "]), _Task(), state)


@pytest.mark.asyncio
async def test_request_gap_sources_fires_acquire(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    captured = {}

    async def _request_acquire(state, req):
        captured["req"] = req

    fake_mod = types.SimpleNamespace(
        AcquireRequest=lambda **kw: types.SimpleNamespace(**kw),
        request_acquire=_request_acquire,
    )
    monkeypatch.setitem(__import__("sys").modules, "agents.mimir.acquire", fake_mod)

    state = make_state()
    await rloop._request_gap_sources(_gap(has_gaps=True, gaps=["close gap Y"]), _Task(), state)
    assert captured["req"].query == "close gap Y"
    assert captured["req"].requester == "researcher"


@pytest.mark.asyncio
async def test_request_gap_sources_swallows_errors(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")

    async def _boom(state, req):
        raise RuntimeError("acquire down")

    fake_mod = types.SimpleNamespace(
        AcquireRequest=lambda **kw: types.SimpleNamespace(**kw),
        request_acquire=_boom,
    )
    monkeypatch.setitem(__import__("sys").modules, "agents.mimir.acquire", fake_mod)
    state = make_state()
    # Must not raise — a failed acquire can never sink the research task.
    await rloop._request_gap_sources(_gap(has_gaps=True, gaps=["g"]), _Task(), state)


@pytest.mark.asyncio
async def test_gap_with_claim_id_requests_sources(monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    plan = InquiryPlan(question="frame?", sub_questions=[_sub_q()], proposed_experiments=[])
    # has_gaps but should not iterate → loop still fires the acquire request.
    gap = _gap(has_gaps=True, should_iterate=False, gaps=["need a source"])
    router = _Router({InquiryPlan: plan, EvidenceBatch: _evidence_batch(1), Synthesis: _synthesis(1), GapCheck: gap})
    disp = _make_dispatcher(router, _Curator())
    _patch_sources(monkeypatch)
    _patch_fetch(monkeypatch, [_page("https://ex.test/a")])

    captured = {}

    async def _request_acquire(state, req):
        captured["req"] = req

    fake_mod = types.SimpleNamespace(
        AcquireRequest=lambda **kw: types.SimpleNamespace(**kw),
        request_acquire=_request_acquire,
    )
    monkeypatch.setitem(__import__("sys").modules, "agents.mimir.acquire", fake_mod)

    out = await rloop.run_research_task(_Task(claim_id=55), disp)
    assert out["iterations"] == 1
    assert captured["req"].claim_id == 55


# ===========================================================================
# tools: search_hacker_news
# ===========================================================================


def _http_resp(json_body, *, status=200):
    return httpx.Response(status, json=json_body, request=httpx.Request("GET", "https://x.test/"))


@pytest.mark.asyncio
async def test_search_hacker_news_parses_hits():
    body = {
        "hits": [
            {"title": "HN story", "url": "https://news.test/a", "story_text": "body", "objectID": "1"},
            # No url → falls back to the item permalink; story_title used.
            {"story_title": "comment story", "comment_text": "c", "objectID": "2"},
            # No title at all → dropped.
            {"objectID": "3"},
        ]
    }

    async def _get(self, url, params=None, **_):
        return _http_resp(body)

    with patch.object(httpx.AsyncClient, "get", new=_get):
        out = await search_hacker_news("mcp", limit=5)

    assert [r.title for r in out] == ["HN story", "comment story"]
    assert out[1].url == "https://news.ycombinator.com/item?id=2"
    assert all(r.source == "hacker_news" for r in out)


# ===========================================================================
# tools: search_web (SearXNG path, DuckDuckGo fallback, failure)
# ===========================================================================


@pytest.mark.asyncio
async def test_search_web_searxng_path(monkeypatch):
    body = {"results": [{"title": "W", "url": "https://w.test/1", "content": "snippet"}]}

    async def _get(self, url, params=None, **_):
        assert "/search" in url
        return _http_resp(body)

    with patch.object(httpx.AsyncClient, "get", new=_get):
        out = await search_web("query", limit=3)

    assert len(out) == 1
    assert out[0].url == "https://w.test/1"
    assert out[0].source == "web"


@pytest.mark.asyncio
async def test_search_web_falls_back_to_duckduckgo(monkeypatch):
    # SearXNG returns empty results → fall through to the DDG HTML fallback.
    async def _get(self, url, params=None, **_):
        return _http_resp({"results": []})

    ddg_hits = [SearchResult(title="DDG", url="https://ddg.test/1", snippet="s", source="web")]

    async def _ddg(query, limit=10):
        return ddg_hits

    monkeypatch.setattr(rtools, "_search_duckduckgo", _ddg)
    with patch.object(httpx.AsyncClient, "get", new=_get):
        out = await search_web("query", limit=3)

    assert out == ddg_hits


@pytest.mark.asyncio
async def test_search_web_searxng_exception_then_fallback(monkeypatch):
    async def _get(self, url, params=None, **_):
        raise httpx.ConnectError("searxng down")

    async def _ddg(query, limit=10):
        return [SearchResult(title="DDG", url="https://ddg.test/1", snippet="s", source="web")]

    monkeypatch.setattr(rtools, "_search_duckduckgo", _ddg)
    with patch.object(httpx.AsyncClient, "get", new=_get):
        out = await search_web("query", limit=3)

    assert out[0].url == "https://ddg.test/1"


@pytest.mark.asyncio
async def test_search_web_fallback_http_error_returns_empty(monkeypatch):
    async def _get(self, url, params=None, **_):
        return _http_resp({"results": []})  # empty → fall through

    async def _ddg(query, limit=10):
        raise httpx.HTTPError("ddg boom")

    monkeypatch.setattr(rtools, "_search_duckduckgo", _ddg)
    with patch.object(httpx.AsyncClient, "get", new=_get):
        out = await search_web("query", limit=3)

    assert out == []


@pytest.mark.asyncio
async def test_search_web_non_200_status_falls_through(monkeypatch):
    async def _get(self, url, params=None, **_):
        return _http_resp({"results": [{"title": "x", "url": "u"}]}, status=500)

    async def _ddg(query, limit=10):
        return [SearchResult(title="DDG", url="https://ddg.test/x", snippet="s", source="web")]

    monkeypatch.setattr(rtools, "_search_duckduckgo", _ddg)
    with patch.object(httpx.AsyncClient, "get", new=_get):
        out = await search_web("query", limit=3)

    assert out[0].url == "https://ddg.test/x"


# ===========================================================================
# tools: _search_duckduckgo (HTML parse)
# ===========================================================================

_DDG_HTML = """
<html><body>
  <div class="result">
    <a class="result__a" href="https://a.test/1">Alpha Result</a>
    <div class="result__snippet">alpha snippet text</div>
  </div>
  <div class="result">
    <a class="result__a" href="https://b.test/2">Beta Result</a>
  </div>
  <div class="result">
    <!-- no anchor → skipped -->
    <div class="result__snippet">orphan</div>
  </div>
  <div class="result">
    <a class="result__a" href="">Empty Href</a>
  </div>
</body></html>
"""


@pytest.mark.asyncio
async def test_duckduckgo_parses_html():
    async def _post(self, url, data=None, **_):
        return httpx.Response(200, text=_DDG_HTML, request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "post", new=_post):
        out = await rtools._search_duckduckgo("query", limit=10)

    urls = [r.url for r in out]
    assert "https://a.test/1" in urls
    assert "https://b.test/2" in urls
    assert out[0].snippet == "alpha snippet text"
    assert out[1].snippet == ""  # no snippet element
    # The empty-href result is filtered out.
    assert all(r.url for r in out)


@pytest.mark.asyncio
async def test_duckduckgo_respects_limit():
    async def _post(self, url, data=None, **_):
        return httpx.Response(200, text=_DDG_HTML, request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "post", new=_post):
        out = await rtools._search_duckduckgo("query", limit=1)

    assert len(out) == 1


# ===========================================================================
# tools: search_reddit (relevance filter, subreddit, no-key path)
# ===========================================================================


def _reddit_body(children):
    return {"data": {"children": [{"data": c} for c in children]}}


@pytest.mark.asyncio
async def test_search_reddit_filters_offtopic():
    body = _reddit_body(
        [
            {"title": "MCP adoption is rising", "selftext": "devs love it", "permalink": "/r/x/1"},
            {"title": "Kittens rescued from drain", "selftext": "", "permalink": "/r/cats/2"},
            {"title": "", "permalink": "/r/empty/3"},  # no title → dropped
        ]
    )

    async def _get(self, url, params=None, **_):
        return _http_resp(body)

    with patch.object(httpx.AsyncClient, "get", new=_get):
        out = await search_reddit("MCP adoption developers", limit=5)

    assert len(out) == 1
    assert out[0].title == "MCP adoption is rising"
    assert out[0].url == "https://www.reddit.com/r/x/1"
    assert out[0].source == "reddit"


@pytest.mark.asyncio
async def test_search_reddit_subreddit_param_and_snippet_fallback():
    captured = {}
    body = _reddit_body(
        [
            # No selftext → snippet falls back to the r/sub — comments/score line.
            {"title": "MCP thread", "subreddit": "mcp", "num_comments": 12, "score": 99, "permalink": "/r/mcp/9"},
        ]
    )

    async def _get(self, url, params=None, **_):
        captured["url"] = url
        captured["params"] = params
        return _http_resp(body)

    with patch.object(httpx.AsyncClient, "get", new=_get):
        out = await search_reddit("MCP", subreddit="mcp", limit=3)

    assert "/r/mcp/search.json" in captured["url"]
    assert captured["params"]["restrict_sr"] == "true"
    assert "12 comments" in out[0].snippet
    assert "score 99" in out[0].snippet


@pytest.mark.asyncio
async def test_search_reddit_limit_truncates():
    body = _reddit_body([{"title": f"MCP item {i}", "selftext": "mcp body", "permalink": f"/r/x/{i}"} for i in range(8)])

    async def _get(self, url, params=None, **_):
        return _http_resp(body)

    with patch.object(httpx.AsyncClient, "get", new=_get):
        out = await search_reddit("MCP", limit=3)

    assert len(out) == 3


# ===========================================================================
# tools: fetch_url
# ===========================================================================


@pytest.mark.asyncio
async def test_fetch_url_strips_html_boilerplate():
    html = (
        "<html><head><title>T</title></head><body>"
        "<nav>NAVBAR</nav><script>var x=1;</script>"
        "<p>Real article content about MCP.</p>"
        "<footer>FOOT</footer></body></html>"
    )

    async def _get(self, url, **_):
        return httpx.Response(200, text=html, headers={"content-type": "text/html"}, request=httpx.Request("GET", url))

    with patch.object(httpx.AsyncClient, "get", new=_get):
        text = await rtools.fetch_url("https://ex.test/article")

    assert "Real article content about MCP." in text
    assert "NAVBAR" not in text
    assert "var x=1" not in text
    assert "FOOT" not in text


@pytest.mark.asyncio
async def test_fetch_url_non_html_returns_raw_truncated():
    body = "x" * 20_000  # well over the 10K truncation cap

    async def _get(self, url, **_):
        return httpx.Response(
            200, text=body, headers={"content-type": "application/json"}, request=httpx.Request("GET", url)
        )

    with patch.object(httpx.AsyncClient, "get", new=_get):
        text = await rtools.fetch_url("https://ex.test/data.json")

    assert text == "x" * 10_000


@pytest.mark.asyncio
async def test_fetch_url_raises_on_http_error():
    async def _get(self, url, **_):
        return httpx.Response(404, text="nope", request=httpx.Request("GET", url))

    with patch.object(httpx.AsyncClient, "get", new=_get), pytest.raises(httpx.HTTPStatusError):
        await rtools.fetch_url("https://ex.test/missing")


# ===========================================================================
# tools: pure helpers
# ===========================================================================


# ===========================================================================
# loop: curator recipe builders (task_data prompt construction)
# ===========================================================================


def _thesis(tid=7, claim="MCP wins"):
    return types.SimpleNamespace(id=tid, claim=claim)


def _builder_state(task=None, theses=None):
    """A state whose get_task / get_active_theses the builders await via _gather."""
    st = AsyncMock()
    st.get_task.return_value = task if task is not None else _Task()
    st.get_active_theses.return_value = list(theses) if theses is not None else [_thesis()]
    return st


@pytest.mark.asyncio
async def test_build_plan_inquiry_with_prior_and_theses():
    st = _builder_state(task=_Task(description="Probe MCP", thesis_id=7), theses=[_thesis(7, "MCP wins")])
    ctx = {
        "task_id": 1,
        "question": "Is it real?",
        "iteration": 2,
        "prior_evidence": [{"stance": "supports", "confidence": 0.8, "claim": "lots of repos"}],
    }
    layer = await rloop._build_plan_inquiry(ctx, st, None)
    assert layer.name == "task_data"
    assert "Probe MCP" in layer.content
    assert "T7" in layer.content
    assert "Evidence already gathered" in layer.content
    assert "iteration 2" in layer.content


@pytest.mark.asyncio
async def test_build_plan_inquiry_no_theses_no_prior_exploratory():
    st = _builder_state(task=_Task(thesis_id=None), theses=[])
    layer = await rloop._build_plan_inquiry({"task_id": 1, "question": "q"}, st, None)
    assert "no active theses" in layer.content
    assert "(exploratory)" in layer.content
    assert "Evidence already gathered" not in layer.content


@pytest.mark.asyncio
async def test_build_extract_evidence_truncates_long_page():
    long_content = "A" * 13_000
    ctx = {"sub_question": "sq?", "url": "https://e.test/p", "title": "Title", "content": long_content}
    layer = await rloop._build_extract_evidence(ctx, AsyncMock(), None)
    assert "page truncated" in layer.content
    assert "https://e.test/p" in layer.content
    assert "Title" in layer.content


@pytest.mark.asyncio
async def test_build_extract_evidence_short_page_no_truncation():
    ctx = {"sub_question": "sq?", "url": "https://e.test/p", "content": "short body"}
    layer = await rloop._build_extract_evidence(ctx, AsyncMock(), None)
    assert "page truncated" not in layer.content
    assert "(none)" in layer.content  # title defaulted


@pytest.mark.asyncio
async def test_build_synthesize_with_evidence_experiments_prior():
    st = _builder_state(task=_Task(thesis_id=7), theses=[_thesis()])
    ctx = {
        "task_id": 1,
        "question": "q?",
        "sub_questions": ["sq0", "sq1"],
        "evidence": [
            {
                "sub_question_idx": 0,
                "stance": "supports",
                "confidence": 0.9,
                "url": "https://e.test/1",
                "claim": "c",
                "quote": "q" * 250,
            }
        ],
        "experiments": [
            {
                "kind": "count_demand_signal",
                "status": "completed",
                "params": {"phrases": ["x"]},
                "interpretation": "useful result",
            }
        ],
        "prior_findings": [{"id": 9, "source": "web", "relevance_score": 4.0, "title": "Prior", "summary": "s"}],
    }
    layer = await rloop._build_synthesize(ctx, st, None)
    assert "Evidence collected (1 items)" in layer.content
    assert "count_demand_signal" in layer.content
    assert "Findings already emitted" in layer.content


@pytest.mark.asyncio
async def test_build_synthesize_empty_blocks():
    st = _builder_state(task=_Task(thesis_id=None), theses=[])
    ctx = {"task_id": 1, "question": "q?", "sub_questions": [], "evidence": [], "experiments": []}
    layer = await rloop._build_synthesize(ctx, st, None)
    assert "(no evidence collected)" in layer.content
    assert "(no experiments run)" in layer.content
    assert "Findings already emitted" not in layer.content


@pytest.mark.asyncio
async def test_build_synthesize_experiment_without_interpretation():
    st = _builder_state()
    ctx = {
        "task_id": 1,
        "question": "q?",
        "sub_questions": ["sq0"],
        "evidence": [],
        "experiments": [{"kind": "gh_search_trend", "status": "failed", "params": {}}],
    }
    layer = await rloop._build_synthesize(ctx, st, None)
    assert "(no interpretation)" in layer.content


@pytest.mark.asyncio
async def test_build_gap_check_coverage_table():
    ctx = {
        "question": "q?",
        "sub_questions": ["sq0", "sq1"],
        "evidence": [{"sub_question_idx": 0}, {"sub_question_idx": 0}, {"sub_question_idx": 1}],
        "synthesis": {"summary": "answer", "open_questions": ["what next"], "weakest_subquestion_idx": 1},
        "iteration": 1,
        "max_iterations": 2,
    }
    layer = await rloop._build_gap_check(ctx, AsyncMock(), None)
    assert "[0]: 2 items" in layer.content
    assert "[1]: 1 items" in layer.content
    assert "answer" in layer.content


@pytest.mark.asyncio
async def test_build_gap_check_no_open_questions_default():
    ctx = {
        "question": "q?",
        "sub_questions": ["sq0"],
        "evidence": [],
        "synthesis": {"summary": "answer"},
    }
    layer = await rloop._build_gap_check(ctx, AsyncMock(), None)
    assert "(none)" in layer.content
    assert "iteration 1 of 2" in layer.content


@pytest.mark.asyncio
async def test_build_interpret_experiment():
    ctx = {
        "kind": "compare_repo_growth",
        "params": {"repos": ["a/b"]},
        "result": {"repos": [{"full_name": "a/b", "stars": 10}]},
        "question": "q?",
        "sub_questions": ["sq0"],
    }
    layer = await rloop._build_interpret_experiment(ctx, AsyncMock(), None)
    assert "compare_repo_growth" in layer.content
    assert "a/b" in layer.content
    assert "[0] sq0" in layer.content


@pytest.mark.asyncio
async def test_gather_runs_coros_concurrently():
    async def _one():
        return 1

    async def _two():
        return 2

    assert await rloop._gather(_one(), _two()) == [1, 2]


def test_meaningful_terms_dedupes_and_filters():
    terms = rtools._meaningful_terms("MCP mcp adoption the and ZZ")
    assert "mcp" in terms
    assert terms.count("mcp") == 1
    assert "the" not in terms and "and" not in terms
    assert "zz" not in terms  # length < 3


def test_reddit_relevant_matches_substring():
    terms = rtools._meaningful_terms("vector database")
    assert rtools._reddit_relevant("Best Vector DB", "", terms) is True
    assert rtools._reddit_relevant("cat pictures", "", terms) is False
    assert rtools._reddit_relevant("anything", "", []) is True
