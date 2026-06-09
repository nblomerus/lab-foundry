"""Unit tests for the Researcher's task handlers — the dispatch/claim/complete scaffolding around
the research engines. Everything external is mocked (no Postgres, no Ollama, no network):

  - grounded_handler.handle_grounded_research : mode gate (off/shadow skip; advisory/active run),
    no-claimable-task, investigate raises → fail_task, result None → fail_task, the feedback
    try/except guard (apply_feedback raises → complete with feedback_error), happy path.
  - handler.handle_task_created             : v2 (default) vs legacy dispatch, no-claimable-task,
    findings / zero-findings, v2 failure → fail_task, legacy source-failure + no-raw-material.

The engines themselves (investigate_task / run_research_task / the feedback seam) are covered in
their own suites; here they're stubbed so we exercise ONLY the handler's control flow.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.researcher.grounded_handler as GH
import agents.researcher.handler as RH
from tests._helpers import ScriptedPool, make_dispatcher, make_state

pytestmark = pytest.mark.asyncio


# ── builders ───────────────────────────────────────────────────────────────────
def _task(task_id=1, *, description="test direction", claim_id=7, payload=None):
    return SimpleNamespace(id=task_id, description=description, claim_id=claim_id, payload=payload or {})


def _finding(**over):
    base = dict(
        verdict="supports",
        blocker="none",
        confidence=0.8,
        summary="the corpus supports it",
        key_evidence=["Paper A", "Paper B"],
        kill_condition_check="nothing trips it",
        gaps=["scaling"],
        acquire_queries=["a", "b", "c"],
        next_step="benchmark",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(claim_id=7, direction="dir"):
    return {"task_id": 1, "claim_id": claim_id, "direction": direction, "queries": ["q1"]}


def _disp_with_state(**state_returns):
    """A dispatcher whose state has a ScriptedPool + presettable async methods."""
    state = make_state(ScriptedPool(), **state_returns)
    return make_dispatcher(state)


def _aconst(val):
    async def f(*_a, **_k):
        return val

    return f


def _patch_legacy_sources(monkeypatch, *, web=None, reddit=None, hacker_news=None):
    """Patch the legacy source tools. The handler resolves them through the module-level
    `_LEGACY_SOURCE_TOOLS` dict (built at import time), so patch the dict entries directly."""
    mapping = {"web": web, "reddit": reddit, "hacker_news": hacker_news}
    for name, fn in mapping.items():
        if fn is not None:
            monkeypatch.setitem(RH._LEGACY_SOURCE_TOOLS, name, fn)


def _set_mode(monkeypatch, module, mode):
    monkeypatch.setattr(module, "get_agent_mode", AsyncMock(return_value=mode))


# ══════════════════════════════════════════════════════════════════════════════
# grounded_handler.handle_grounded_research — mode gate
# ══════════════════════════════════════════════════════════════════════════════
async def test_grounded_off_mode_skips(monkeypatch):
    _set_mode(monkeypatch, GH, "off")
    disp = _disp_with_state()
    out = await GH.handle_grounded_research({}, disp)
    assert out == {"skipped": True, "reason": "researcher mode off"}
    disp.state.claim_task.assert_not_awaited()  # off never claims


async def test_grounded_shadow_mode_skips(monkeypatch):
    _set_mode(monkeypatch, GH, "shadow")
    disp = _disp_with_state()
    out = await GH.handle_grounded_research({}, disp)
    assert out == {"skipped": True, "reason": "researcher mode shadow"}
    disp.state.claim_task.assert_not_awaited()


async def test_grounded_no_claimable_task(monkeypatch):
    _set_mode(monkeypatch, GH, "active")
    disp = _disp_with_state(claim_task=None)  # nothing pending
    out = await GH.handle_grounded_research({}, disp)
    assert out == {"skipped": True, "reason": "no claimable research task"}


async def test_grounded_investigate_raises_fails_task(monkeypatch):
    _set_mode(monkeypatch, GH, "active")
    disp = _disp_with_state(claim_task=_task(11))

    async def _boom(*_a, **_k):
        raise RuntimeError("retrieval exploded")

    monkeypatch.setattr(GH, "investigate_task", _boom)
    out = await GH.handle_grounded_research({}, disp)
    assert out == {"task_id": 11, "failed": True, "reason": "retrieval exploded"}
    disp.state.fail_task.assert_awaited_once()
    _, kw = disp.state.fail_task.await_args
    assert "grounded researcher" in kw["error"]
    disp.state.complete_task.assert_not_awaited()


async def test_grounded_result_none_fails_task(monkeypatch):
    _set_mode(monkeypatch, GH, "advisory")
    disp = _disp_with_state(claim_task=_task(12))
    monkeypatch.setattr(GH, "investigate_task", _aconst(None))  # task context missing
    out = await GH.handle_grounded_research({}, disp)
    assert out == {"task_id": 12, "failed": True, "reason": "no context"}
    _, kw = disp.state.fail_task.await_args
    assert kw["error"] == "task context missing"


# ── happy path (advisory/active): claim → investigate → grade → feedback → complete ──
def _patch_engine(monkeypatch, *, finding=None, grade=None, ctx=None, refs=None, applied=None):
    finding = finding or _finding()
    ctx = ctx or _ctx()
    refs = refs if refs is not None else ["r1", "r2"]
    monkeypatch.setattr(GH, "investigate_task", _aconst((ctx, refs, "mimir", finding)))
    monkeypatch.setattr(GH, "grade_finding", lambda f, r: grade or {"grounded": 0.9})
    monkeypatch.setattr(GH, "disposition", lambda f: "supported")
    monkeypatch.setattr(GH, "refine_disposition", _aconst("supported"))
    monkeypatch.setattr(GH, "finding_feedback", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(GH, "aggregate_direction", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(GH, "apply_feedback", _aconst(applied or {"confidence": [0.5, 0.58], "acquires_fired": 2}))


async def test_grounded_happy_path_active(monkeypatch):
    _set_mode(monkeypatch, GH, "active")
    disp = _disp_with_state(claim_task=_task(20))
    _patch_engine(monkeypatch)
    out = await GH.handle_grounded_research({}, disp)
    assert out["task_id"] == 20
    assert out["disposition"] == "supported"
    assert out["applied"] == {"confidence": [0.5, 0.58], "acquires_fired": 2}
    disp.state.complete_task.assert_awaited_once()
    _, kw = disp.state.complete_task.await_args
    assert kw["task_id"] == 20
    assert kw["result"]["verdict"] == "supports"
    assert kw["result"]["disposition"] == "supported"
    assert kw["result"]["grounded"] == 0.9
    assert kw["result"]["applied"]["acquires_fired"] == 2
    assert kw["result"]["n_evidence"] == 2
    disp.state.fail_task.assert_not_awaited()


async def test_grounded_happy_path_advisory(monkeypatch):
    _set_mode(monkeypatch, GH, "advisory")
    disp = _disp_with_state(claim_task=_task(21))
    _patch_engine(monkeypatch)
    out = await GH.handle_grounded_research({}, disp)
    assert out["task_id"] == 21
    disp.state.complete_task.assert_awaited_once()


async def test_grounded_investigate_emit_true(monkeypatch):
    # the handler always calls investigate_task with emit=True (live conversation).
    _set_mode(monkeypatch, GH, "active")
    disp = _disp_with_state(claim_task=_task(22))
    spy = AsyncMock(return_value=(_ctx(), ["r1"], "mimir", _finding()))
    monkeypatch.setattr(GH, "investigate_task", spy)
    monkeypatch.setattr(GH, "grade_finding", lambda f, r: {"grounded": 1.0})
    monkeypatch.setattr(GH, "disposition", lambda f: "supported")
    monkeypatch.setattr(GH, "refine_disposition", _aconst("supported"))
    monkeypatch.setattr(GH, "finding_feedback", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(GH, "aggregate_direction", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(GH, "apply_feedback", _aconst({}))
    await GH.handle_grounded_research({}, disp)
    _, kw = spy.await_args
    assert kw["emit"] is True
    assert spy.await_args.args[1] == 22  # task.id positionally


# ── the feedback try/except guard: apply_feedback raises → complete with feedback_error ──
async def test_grounded_feedback_failure_still_completes(monkeypatch):
    _set_mode(monkeypatch, GH, "active")
    disp = _disp_with_state(claim_task=_task(30))
    finding = _finding(verdict="contradicts")
    monkeypatch.setattr(GH, "investigate_task", _aconst((_ctx(), ["r1"], "mimir", finding)))
    monkeypatch.setattr(GH, "grade_finding", lambda f, r: {"grounded": 0.7})
    # disposition(finding) is used as the fallback when steering blows up
    monkeypatch.setattr(GH, "disposition", lambda f: "contradicted")
    monkeypatch.setattr(GH, "refine_disposition", _aconst("contradicted"))
    monkeypatch.setattr(GH, "finding_feedback", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(GH, "aggregate_direction", lambda *a, **k: SimpleNamespace())

    async def _boom(*_a, **_k):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(GH, "apply_feedback", _boom)
    out = await GH.handle_grounded_research({}, disp)
    # task still completes (never stranded) with the fallback disposition + a feedback_error
    assert out["disposition"] == "contradicted"
    assert out["applied"] == {"feedback_error": "db write failed"}
    disp.state.complete_task.assert_awaited_once()
    _, kw = disp.state.complete_task.await_args
    assert kw["result"]["applied"] == {"feedback_error": "db write failed"}
    disp.state.fail_task.assert_not_awaited()  # NOT failed — completed despite steering error


async def test_grounded_refine_disposition_failure_caught(monkeypatch):
    # a failure earlier in the try block (refine_disposition) is also caught by the same guard.
    _set_mode(monkeypatch, GH, "active")
    disp = _disp_with_state(claim_task=_task(31))
    monkeypatch.setattr(GH, "investigate_task", _aconst((_ctx(), ["r1"], "mimir", _finding())))
    monkeypatch.setattr(GH, "grade_finding", lambda f, r: {"grounded": 0.9})
    monkeypatch.setattr(GH, "disposition", lambda f: "supported")

    async def _boom(*_a, **_k):
        raise RuntimeError("refine failed")

    monkeypatch.setattr(GH, "refine_disposition", _boom)
    out = await GH.handle_grounded_research({}, disp)
    assert out["disposition"] == "supported"  # fell back to disposition(finding)
    assert "feedback_error" in out["applied"]
    disp.state.complete_task.assert_awaited_once()


async def test_grounded_result_truncates_lists(monkeypatch):
    # key_evidence/gaps/acquire_queries are sliced to 6 in the completed result.
    _set_mode(monkeypatch, GH, "active")
    disp = _disp_with_state(claim_task=_task(40))
    finding = _finding(
        key_evidence=[f"E{i}" for i in range(10)],
        gaps=[f"G{i}" for i in range(10)],
        acquire_queries=[f"Q{i}" for i in range(10)],
    )
    monkeypatch.setattr(GH, "investigate_task", _aconst((_ctx(), ["r1"], "mimir", finding)))
    monkeypatch.setattr(GH, "grade_finding", lambda f, r: {"grounded": 1.0})
    monkeypatch.setattr(GH, "disposition", lambda f: "supported")
    monkeypatch.setattr(GH, "refine_disposition", _aconst("supported"))
    monkeypatch.setattr(GH, "finding_feedback", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(GH, "aggregate_direction", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(GH, "apply_feedback", _aconst({}))
    await GH.handle_grounded_research({}, disp)
    _, kw = disp.state.complete_task.await_args
    assert len(kw["result"]["key_evidence"]) == 6
    assert len(kw["result"]["gaps"]) == 6
    assert len(kw["result"]["acquire_queries"]) == 6


# ══════════════════════════════════════════════════════════════════════════════
# handler.handle_task_created — v2 / legacy dispatch
# ══════════════════════════════════════════════════════════════════════════════
async def test_handle_task_created_no_claimable_task():
    disp = _disp_with_state(claim_task=None)
    out = await RH.handle_task_created({"id": 1}, disp)
    assert out == {"skipped": True, "reason": "no claimable research task"}


def _patch_run_research(monkeypatch, summary=None, *, raises=None):
    """Patch run_research_task where the deferred import resolves it (agents.researcher.loop)."""
    import agents.researcher.loop as loop_mod

    if raises is not None:

        async def _r(*_a, **_k):
            raise raises

        monkeypatch.setattr(loop_mod, "run_research_task", _r, raising=False)
    else:
        monkeypatch.setattr(loop_mod, "run_research_task", _aconst(summary), raising=False)


def _summary(**over):
    base = dict(findings=[1, 2], iterations=2, inquiry_ids=[10, 11], evidence_count=5, experiments_run=1)
    base.update(over)
    return base


async def test_handle_task_created_v2_default(monkeypatch):
    monkeypatch.delenv("RESEARCHER_LOOP", raising=False)  # default → v2
    disp = _disp_with_state(claim_task=_task(50))
    _patch_run_research(monkeypatch, _summary())
    out = await RH.handle_task_created({"id": 99}, disp)
    assert out["impl"] == "v2"
    assert out["task_id"] == 50
    assert out["findings"] == 2
    assert out["iterations"] == 2
    assert out["evidence_count"] == 5
    disp.state.complete_task.assert_awaited_once()
    _, kw = disp.state.complete_task.await_args
    assert kw["result"]["impl"] == "v2"
    assert kw["result"]["finding_ids"] == [1, 2]


async def test_handle_task_created_v2_zero_findings_still_completes(monkeypatch):
    monkeypatch.setenv("RESEARCHER_LOOP", "v2")
    disp = _disp_with_state(claim_task=_task(51))
    _patch_run_research(monkeypatch, _summary(findings=[]))
    out = await RH.handle_task_created({"id": 1}, disp)
    assert out["findings"] == 0
    disp.state.complete_task.assert_awaited_once()  # zero findings is a legit result
    disp.state.fail_task.assert_not_awaited()


async def test_handle_task_created_v2_failure_fails_task(monkeypatch):
    monkeypatch.delenv("RESEARCHER_LOOP", raising=False)
    disp = _disp_with_state(claim_task=_task(52))
    _patch_run_research(monkeypatch, raises=RuntimeError("loop blew up"))
    out = await RH.handle_task_created({"id": 1}, disp)
    assert out == {"task_id": 52, "failed": True, "reason": "loop blew up"}
    disp.state.fail_task.assert_awaited_once()
    _, kw = disp.state.fail_task.await_args
    assert "researcher v2" in kw["error"]


# ══════════════════════════════════════════════════════════════════════════════
# handler — legacy path
# ══════════════════════════════════════════════════════════════════════════════
def _result(title="T", url="https://x", snippet="snip"):
    return SimpleNamespace(title=title, url=url, snippet=snippet)


async def test_legacy_dispatch_findings(monkeypatch):
    monkeypatch.setenv("RESEARCHER_LOOP", "legacy")
    task = _task(60, payload={"query": "GP kernels", "sources": ["web"]})
    disp = _disp_with_state(claim_task=task)
    # legacy curator/router are awaited — give them concrete returns
    disp.curator = AsyncMock()
    disp.curator.build.return_value = {"prompt": "p"}
    findings = RH.ResearcherFindings(
        findings=[
            RH.FindingOut(
                source="web",
                url="https://a",
                title="A",
                summary="s",
                relevance_score=8.0,
                why_it_matters="matters",
                supports_thesis=True,
            )
        ]
    )
    disp.router = AsyncMock()
    disp.router.invoke.return_value = (findings, 321)
    disp.state.record_finding = AsyncMock(side_effect=[101])
    # patch the legacy source tools (search_reddit/search_hacker_news/search_web)
    _patch_legacy_sources(
        monkeypatch,
        web=_aconst([_result()]),
        reddit=_aconst([_result(title="R")]),
        hacker_news=_aconst([_result(title="HN")]),
    )

    out = await RH.handle_task_created({"id": 7}, disp)
    assert out["impl"] == "legacy"
    assert out["findings"] == 1
    assert out["run_id"] == 321
    disp.state.record_finding.assert_awaited_once()
    _, kw = disp.state.record_finding.await_args
    assert kw["claim_id"] == 7  # task.claim_id threaded
    disp.state.complete_task.assert_awaited_once()


async def test_legacy_zero_findings(monkeypatch):
    monkeypatch.setenv("RESEARCHER_LOOP", "legacy")
    disp = _disp_with_state(claim_task=_task(61, payload={}))
    disp.curator = AsyncMock()
    disp.curator.build.return_value = {"prompt": "p"}
    disp.router = AsyncMock()
    disp.router.invoke.return_value = (RH.ResearcherFindings(findings=[]), 42)
    _patch_legacy_sources(monkeypatch, web=_aconst([_result()]), reddit=_aconst([]), hacker_news=_aconst([]))
    out = await RH.handle_task_created({"id": 7}, disp)
    assert out["findings"] == 0
    disp.state.record_finding.assert_not_awaited()
    disp.state.complete_task.assert_awaited_once()


async def test_legacy_no_raw_material_fails(monkeypatch):
    monkeypatch.setenv("RESEARCHER_LOOP", "legacy")
    disp = _disp_with_state(claim_task=_task(62, payload={"query": "q"}))
    # every source returns empty → no raw material → fail_task
    _patch_legacy_sources(monkeypatch, web=_aconst([]), reddit=_aconst([]), hacker_news=_aconst([]))
    out = await RH.handle_task_created({"id": 7}, disp)
    assert out == {"task_id": 62, "failed": True, "reason": "no raw material"}
    disp.state.fail_task.assert_awaited_once()


# ── _legacy_gather_raw_material directly: source failure + unknown source + capping ──
async def test_legacy_gather_skips_unknown_source(monkeypatch):
    _patch_legacy_sources(monkeypatch, web=_aconst([_result()]), reddit=_aconst([]), hacker_news=_aconst([]))
    raw = await RH._legacy_gather_raw_material("q", ["web", "arxiv"])  # arxiv not a legacy tool → skipped
    assert "web" in raw
    assert "T" in raw  # the web result title


async def test_legacy_gather_source_failure_swallowed(monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("search 500")

    _patch_legacy_sources(monkeypatch, web=_boom, reddit=_aconst([_result(title="R")]), hacker_news=_aconst([]))
    raw = await RH._legacy_gather_raw_material("q", ["web", "reddit"])
    assert "reddit" in raw  # the surviving source still contributed
    assert "web —" not in raw  # the failed source produced no block


async def test_legacy_gather_empty_results_skipped(monkeypatch):
    _patch_legacy_sources(monkeypatch, web=_aconst([]))  # returns [] → no header block
    raw = await RH._legacy_gather_raw_material("q", ["web"])
    assert raw == ""


async def test_legacy_gather_caps_per_source(monkeypatch):
    # first block fits the per-source budget; the second pushes over it → loop breaks before it.
    first = _result(title="FIRST", snippet="a" * 1500)
    second = _result(title="SECOND", snippet="b" * 1500)
    _patch_legacy_sources(monkeypatch, web=_aconst([first, second]))
    raw = await RH._legacy_gather_raw_material("q", ["web"], cap_chars=2000)  # per_source budget 2000
    assert "FIRST" in raw  # first block (~1.5k) included
    assert "SECOND" not in raw  # second would exceed budget → skipped
