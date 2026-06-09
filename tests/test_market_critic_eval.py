"""Pytest coverage for the market-era Critic and Evaluation agents.

Targets (all mocked — NO Postgres/Neo4j/Ollama/DeepSeek/network):
  - agents.critic.handler        (finding.high_signal → watch/weaken/kill)
  - agents.critic.loop           (four-step refutation pass + prompt builders)
  - agents.evaluation.handler    (task.completed → per-finding slop scoring)
  - agents.evaluation.loop       (two-step cross_check + batch_score)
  - agents.evaluation.slop_handler (audit.slop_detected circuit-breaker)

LLM is reached via dispatcher.router.invoke (an AsyncMock returning (verdict, run_id));
the Curator is an AsyncMock; state methods are AsyncMocks; the DB is a ScriptedPool.
Graph sinks (library.graph.tools) are monkeypatched so Neo4j is never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.critic.loop as critic_loop
import agents.evaluation.loop as eval_loop
import agents.researcher.experiments as exp_mod
import library.graph.tools as graph_tools
from agents.critic.handler import (
    DEFAULT_WEAKEN_DELTA,
    AdversaryVerdictOut,
    _build_adversary_task_data,
    handle_finding_high_signal,
)
from agents.critic.loop import (
    _build_extract_counter,
    _build_judge_verdict,
    _build_plan_attack,
    _build_stress_test_interp,
    _search_for_weak_point,
    run_adversary_loop,
)
from agents.critic.schemas import (
    AttackPlan,
    CounterEvidenceBatch,
    CounterEvidenceItem,
    ExperimentProposal,
    StressTestInterp,
    WeakPoint,
)
from agents.evaluation.handler import (
    AuditBatch,
    AuditScore,
    _build_evaluation_task_data,
    _verdict_from_score,
    handle_task_completed,
)
from agents.evaluation.loop import (
    _build_batch_score,
    _build_cross_check_finding,
    run_audit_loop,
)
from agents.evaluation.schemas import (
    ClaimCheck,
    EvidenceCrossCheck,
)
from agents.evaluation.slop_handler import handle_audit_slop_detected
from agents.researcher.tools import SearchResult
from tests._helpers import ScriptedPool

pytestmark = pytest.mark.asyncio


# ── shared builders / stubs ─────────────────────────────────────────────────────
_BORN = datetime(2026, 1, 2, tzinfo=UTC)


def _claim(cid=10, *, claim="thesis claim", status="active", conf=0.6):
    return SimpleNamespace(id=cid, claim=claim, status=status, confidence=conf, created_at=_BORN)


def _finding(fid=1, *, source="web", rel=9, supports=True, audit="pass", claim_id=10):
    return SimpleNamespace(
        id=fid,
        source=source,
        relevance_score=rel,
        supports_thesis=supports,
        audit_verdict=audit,
        title=f"finding {fid}",
        summary="a summary " * 30,
        url=f"http://ex/{fid}",
        why_it_matters="matters",
        claim_id=claim_id,
        created_at=_BORN,
    )


def _task(tid=5, *, dept="research", desc="probe the market", ttype="survey"):
    return SimpleNamespace(id=tid, department=dept, description=desc, task_type=ttype)


def _verdict(action="watch", *, conf=0.7, reasoning="x" * 40, cited=None, delta=None):
    """A real AdversaryVerdictOut (runs the model_validator)."""
    return AdversaryVerdictOut(
        action=action,
        confidence=conf,
        reasoning=reasoning,
        cited_finding_ids=cited if cited is not None else [],
        proposed_confidence_delta=delta,
    )


def _patch_graph(monkeypatch, *, raises=False):
    """Make all three graph sinks AsyncMocks (or raising) so Neo4j is never hit."""
    for name in (
        "merge_critic_verdict_challenged_claim",
        "merge_claim",
        "merge_finding_grounds_claim",
    ):
        if raises:
            monkeypatch.setattr(graph_tools, name, AsyncMock(side_effect=RuntimeError("neo down")))
        else:
            monkeypatch.setattr(graph_tools, name, AsyncMock())


def _critic_dispatcher(monkeypatch, *, loop_return=None, legacy_return=None):
    """Dispatcher for the critic handler. By default monkeypatches run_adversary_loop
    (the v2 path) so the loop's real signature mismatch / network are bypassed."""
    d = AsyncMock()
    d.router = AsyncMock()
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.state = AsyncMock()
    d.memory = AsyncMock()
    d.set_cooldown = AsyncMock()
    if loop_return is not None:
        monkeypatch.setattr(critic_loop, "run_adversary_loop", AsyncMock(return_value=loop_return))
    if legacy_return is not None:
        d.router.invoke = AsyncMock(return_value=legacy_return)
    return d


# =================================================================================
# critic.handler — _verdict schema validator
# =================================================================================
async def test_weaken_with_no_delta_defaults():
    v = _verdict("weaken", delta=None)
    assert v.proposed_confidence_delta == DEFAULT_WEAKEN_DELTA


async def test_weaken_with_zero_delta_defaults():
    v = _verdict("weaken", delta=0.0)
    assert v.proposed_confidence_delta == DEFAULT_WEAKEN_DELTA


async def test_weaken_keeps_explicit_delta():
    v = _verdict("weaken", delta=-0.25)
    assert v.proposed_confidence_delta == -0.25


async def test_non_weaken_clears_delta():
    # A kill that the model tagged with a delta — validator must null it.
    v = AdversaryVerdictOut(action="kill", confidence=0.9, reasoning="x" * 40, proposed_confidence_delta=-0.5)
    assert v.proposed_confidence_delta is None


async def test_watch_clears_delta():
    v = AdversaryVerdictOut(action="watch", confidence=0.5, reasoning="y" * 40, proposed_confidence_delta=-0.3)
    assert v.proposed_confidence_delta is None


# =================================================================================
# critic.handler — _build_adversary_task_data
# =================================================================================
async def test_build_adversary_task_data_with_findings():
    state = AsyncMock()
    state.get_claim = AsyncMock(return_value=_claim(10))
    state.get_recent_findings_for_claim = AsyncMock(return_value=[_finding(1), _finding(2)])
    ctx = {"claim_id": 10, "triggering_finding_id": 7}
    layer = await _build_adversary_task_data(ctx, state, None)
    assert layer.name == "task_data"
    assert "Target claim under adversarial review" in layer.content
    assert "F1 [web, rel 9" in layer.content
    assert "triggered by finding F7" in layer.content


async def test_build_adversary_task_data_no_findings_no_trigger():
    state = AsyncMock()
    state.get_claim = AsyncMock(return_value=_claim(10))
    state.get_recent_findings_for_claim = AsyncMock(return_value=[])
    layer = await _build_adversary_task_data({"claim_id": 10}, state, None)
    assert "(no findings yet — early in research)" in layer.content
    assert "triggered by finding" not in layer.content


# =================================================================================
# critic.handler — handle_finding_high_signal  (v2 path)
# =================================================================================
async def test_handler_v2_watch(monkeypatch):
    _patch_graph(monkeypatch)
    d = _critic_dispatcher(monkeypatch, loop_return=(_verdict("watch"), 99, []))
    d.state.create_critic_verdict = AsyncMock(return_value=500)
    d.state.get_claim = AsyncMock(return_value=_claim(10))
    event = {"id": 1, "target_id": 10, "payload": {"finding_id": 7}}

    res = await handle_finding_high_signal(event, d)

    assert res == {"claim_id": 10, "action": "watch", "verdict_id": 500, "run_id": 99}
    d.set_cooldown.assert_awaited_once()
    # watch never kills or weakens
    d.state.invalidate_claim.assert_not_awaited()
    d.state.update_claim_confidence.assert_not_awaited()
    # always narrates to dissent
    sessions = [c.kwargs["session_id"] for c in d.memory.write_message.await_args_list]
    assert sessions == ["dissent"]
    critic_loop.run_adversary_loop.assert_awaited_once()
    # the loop was called with claim_id (the handler's keyword)
    assert critic_loop.run_adversary_loop.await_args.kwargs["claim_id"] == 10


async def test_handler_v2_kill(monkeypatch):
    _patch_graph(monkeypatch)
    v = _verdict("kill", conf=0.9, cited=[1, 2])
    d = _critic_dispatcher(monkeypatch, loop_return=(v, 77, []))
    d.state.create_critic_verdict = AsyncMock(return_value=501)
    d.state.get_claim = AsyncMock(return_value=_claim(10))
    killed = SimpleNamespace(status="killed", statement="the claim", confidence=0.0)
    d.state.invalidate_claim = AsyncMock(return_value=killed)
    event = {"id": 2, "target_id": 10, "payload": {}}

    res = await handle_finding_high_signal(event, d)

    assert res["action"] == "kill" and res["killed"] is True
    d.state.invalidate_claim.assert_awaited_once()
    # graph claim-invalidation + verdict both written
    graph_tools.merge_claim.assert_awaited_once()
    # two memory writes: claims-lifecycle + dissent
    sessions = [c.kwargs["session_id"] for c in d.memory.write_message.await_args_list]
    assert sessions == ["claims-lifecycle", "dissent"]


async def test_handler_v2_kill_not_actually_killed(monkeypatch):
    _patch_graph(monkeypatch)
    d = _critic_dispatcher(monkeypatch, loop_return=(_verdict("kill"), 5, []))
    d.state.create_critic_verdict = AsyncMock(return_value=1)
    d.state.get_claim = AsyncMock(return_value=_claim(10))
    # invalidate returns a non-killed status (e.g. race) → result["killed"] False
    d.state.invalidate_claim = AsyncMock(return_value=SimpleNamespace(status="active", statement="s", confidence=0.4))
    res = await handle_finding_high_signal({"id": 3, "target_id": 10, "payload": {}}, d)
    assert res["killed"] is False


async def test_handler_v2_weaken_applies_delta(monkeypatch):
    _patch_graph(monkeypatch)
    v = _verdict("weaken", delta=-0.2)
    d = _critic_dispatcher(monkeypatch, loop_return=(v, 88, []))
    d.state.create_critic_verdict = AsyncMock(return_value=502)
    # get_claim called twice: graph-sink + weaken-recompute → keep returning same claim
    d.state.get_claim = AsyncMock(return_value=_claim(10, conf=0.6))
    res = await handle_finding_high_signal({"id": 4, "target_id": 10, "payload": {}}, d)
    assert res["applied_delta"] == -0.2
    assert res["new_confidence"] == pytest.approx(0.4)
    d.state.update_claim_confidence.assert_awaited_once()
    assert d.state.update_claim_confidence.await_args.kwargs["new_confidence"] == pytest.approx(0.4)


async def test_handler_v2_weaken_floored_at_zero(monkeypatch):
    _patch_graph(monkeypatch)
    v = _verdict("weaken", delta=-0.9)
    d = _critic_dispatcher(monkeypatch, loop_return=(v, 1, []))
    d.state.create_critic_verdict = AsyncMock(return_value=1)
    d.state.get_claim = AsyncMock(return_value=_claim(10, conf=0.3))
    res = await handle_finding_high_signal({"id": 5, "target_id": 10, "payload": {}}, d)
    assert res["new_confidence"] == 0.0  # max(0.0, 0.3 - 0.9)


async def test_handler_v2_weaken_falsy_delta_uses_default(monkeypatch):
    # A loop returning a mock verdict with delta=0.0 hits the handler's own
    # `if not delta` guard (a real schema would have defaulted it already).
    _patch_graph(monkeypatch)
    mock_verdict = SimpleNamespace(
        action="weaken",
        confidence=0.6,
        reasoning="z" * 40,
        cited_finding_ids=[],
        proposed_confidence_delta=0.0,
    )
    d = _critic_dispatcher(monkeypatch, loop_return=(mock_verdict, 2, []))
    d.state.create_critic_verdict = AsyncMock(return_value=9)
    d.state.get_claim = AsyncMock(return_value=_claim(10, conf=0.5))
    res = await handle_finding_high_signal({"id": 6, "target_id": 10, "payload": {}}, d)
    assert res["applied_delta"] == DEFAULT_WEAKEN_DELTA
    assert res["new_confidence"] == pytest.approx(0.4)


async def test_handler_graph_failure_is_non_fatal(monkeypatch):
    # verdict graph write raises → handler logs + continues, still returns result
    _patch_graph(monkeypatch, raises=True)
    d = _critic_dispatcher(monkeypatch, loop_return=(_verdict("watch"), 3, []))
    d.state.create_critic_verdict = AsyncMock(return_value=7)
    d.state.get_claim = AsyncMock(return_value=_claim(10))
    res = await handle_finding_high_signal({"id": 7, "target_id": 10, "payload": {}}, d)
    assert res["action"] == "watch" and res["verdict_id"] == 7


async def test_handler_kill_graph_failure_non_fatal(monkeypatch):
    _patch_graph(monkeypatch, raises=True)
    d = _critic_dispatcher(monkeypatch, loop_return=(_verdict("kill"), 4, []))
    d.state.create_critic_verdict = AsyncMock(return_value=8)
    d.state.get_claim = AsyncMock(return_value=_claim(10))
    d.state.invalidate_claim = AsyncMock(return_value=SimpleNamespace(status="killed", statement="s", confidence=0.0))
    res = await handle_finding_high_signal({"id": 8, "target_id": 10, "payload": {}}, d)
    assert res["killed"] is True  # claim-invalidation graph write raised but handler ok


async def test_handler_legacy_path(monkeypatch):
    # ADVERSARY_LOOP=legacy → curator.build + router.invoke single-shot
    monkeypatch.setenv("ADVERSARY_LOOP", "legacy")
    _patch_graph(monkeypatch)
    d = _critic_dispatcher(monkeypatch, legacy_return=(_verdict("watch"), 42))
    d.state.create_critic_verdict = AsyncMock(return_value=600)
    d.state.get_claim = AsyncMock(return_value=_claim(10))
    res = await handle_finding_high_signal({"id": 9, "target_id": 10, "payload": {}}, d)
    assert res == {"claim_id": 10, "action": "watch", "verdict_id": 600, "run_id": 42}
    d.curator.build.assert_awaited_once()
    d.router.invoke.assert_awaited_once()


async def test_handler_payload_missing(monkeypatch):
    # event without payload → triggering_finding_id None, no crash
    _patch_graph(monkeypatch)
    d = _critic_dispatcher(monkeypatch, loop_return=(_verdict("watch"), 1, []))
    d.state.create_critic_verdict = AsyncMock(return_value=1)
    d.state.get_claim = AsyncMock(return_value=_claim(10))
    res = await handle_finding_high_signal({"id": 10, "target_id": 10}, d)
    assert res["action"] == "watch"


# =================================================================================
# critic.loop — prompt builders
# =================================================================================
def _thesis(tid=10, *, claim="thesis claim", status="active", conf=0.6):
    return SimpleNamespace(id=tid, claim=claim, status=status, confidence=conf, created_at=_BORN)


async def test_build_plan_attack_with_findings():
    state = AsyncMock()
    state.get_thesis = AsyncMock(return_value=_thesis(10))
    state.get_recent_findings_for_thesis = AsyncMock(return_value=[_finding(1)])
    layer = await _build_plan_attack({"thesis_id": 10, "triggering_finding_id": 7}, state, None)
    assert "Target thesis under adversarial review" in layer.content
    assert "F1 [web, rel 9" in layer.content
    assert "triggered by finding F7" in layer.content


async def test_build_plan_attack_no_findings():
    state = AsyncMock()
    state.get_thesis = AsyncMock(return_value=_thesis(10))
    state.get_recent_findings_for_thesis = AsyncMock(return_value=[])
    layer = await _build_plan_attack({"thesis_id": 10}, state, None)
    assert "(no findings yet — early in research)" in layer.content
    assert "triggered by finding" not in layer.content


async def test_build_extract_counter_short_page():
    wp = {"hypothesis": "incumbents already win"}
    layer = await _build_extract_counter(
        {"weak_point": wp, "url": "http://x", "title": "T", "content": "short body"},
        None,
        None,
    )
    assert "incumbents already win" in layer.content
    assert "http://x" in layer.content
    assert "page truncated" not in layer.content


async def test_build_extract_counter_truncates_long_page():
    wp = {"hypothesis": "h"}
    layer = await _build_extract_counter({"weak_point": wp, "url": "u", "content": "x" * 20_000}, None, None)
    assert "page truncated" in layer.content
    assert "(none)" in layer.content  # title defaulted


async def test_build_stress_test_interp():
    layer = await _build_stress_test_interp(
        {
            "kind": "fetch_pricing",
            "params": {"company": "OpenAI"},
            "result": {"price": 20},
            "thesis_claim": "pricing is high",
            "weak_points": [{"hypothesis": "cheaper rivals exist"}],
        },
        None,
        None,
    )
    assert "fetch_pricing" in layer.content
    assert "cheaper rivals exist" in layer.content
    assert "pricing is high" in layer.content


async def test_build_judge_verdict_with_everything():
    state = AsyncMock()
    state.get_thesis = AsyncMock(return_value=_thesis(10))
    state.get_recent_findings_for_thesis = AsyncMock(return_value=[_finding(1)])
    ctx = {
        "thesis_id": 10,
        "weak_points": [{"hypothesis": "h1"}],
        "counter_evidence": [
            {
                "stance": "refutes",
                "confidence": 0.8,
                "url": "http://c",
                "claim": "rivals undercut",
                "quote": "they charge $5/mo",
            }
        ],
        "stress_test": {
            "summary": "rivals cheaper",
            "bears_against_thesis": True,
            "confidence": 0.7,
        },
    }
    layer = await _build_judge_verdict(ctx, state, None)
    assert "Final adversarial verdict" in layer.content
    assert "rivals undercut" in layer.content
    assert "Stress test result" in layer.content
    assert "F1 [web, rel 9" in layer.content


async def test_build_judge_verdict_empty_branches():
    state = AsyncMock()
    state.get_thesis = AsyncMock(return_value=_thesis(10))
    state.get_recent_findings_for_thesis = AsyncMock(return_value=[])
    ctx = {
        "thesis_id": 10,
        "weak_points": [{"hypothesis": "h1"}],
        "counter_evidence": [],
        "stress_test": None,
    }
    layer = await _build_judge_verdict(ctx, state, None)
    assert "(no counter-evidence gathered" in layer.content
    assert "(no stress test was proposed)" in layer.content
    assert "(no findings yet)" in layer.content


# =================================================================================
# critic.loop — _search_for_weak_point
# =================================================================================
def _sr(url, *, source="web", title="t"):
    return SearchResult(title=title, url=url, snippet="s", source=source)


async def test_search_for_weak_point_collects_and_caps(monkeypatch):
    async def fake_web(query, limit):
        return [_sr(f"http://{query}/{i}") for i in range(5)]

    monkeypatch.setitem(critic_loop._SOURCE_TOOLS, "web", fake_web)
    out = await _search_for_weak_point(["q1"], ["web"])
    assert len(out) == 3  # hits[:3]


async def test_search_for_weak_point_unknown_source_skipped(monkeypatch):
    async def fake_web(query, limit):
        return [_sr("http://a")]

    monkeypatch.setitem(critic_loop._SOURCE_TOOLS, "web", fake_web)
    out = await _search_for_weak_point(["q"], ["web", "bogus"])
    assert [r.url for r in out] == ["http://a"]


async def test_search_for_weak_point_tool_exception_swallowed(monkeypatch):
    async def boom(query, limit):
        raise RuntimeError("net down")

    monkeypatch.setitem(critic_loop._SOURCE_TOOLS, "web", boom)
    out = await _search_for_weak_point(["q"], ["web"])
    assert out == []


# =================================================================================
# critic.loop — run_adversary_loop orchestrator
# =================================================================================
def _loop_dispatcher(invoke_side_effect):
    d = AsyncMock()
    d.router = AsyncMock()
    d.router.invoke = AsyncMock(side_effect=invoke_side_effect)
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.state = AsyncMock()
    d.session = object()
    return d


def _plan(weak_points=None, experiment=None):
    if weak_points is None:
        weak_points = [WeakPoint(hypothesis="h1", search_queries=["q1"], sources=["web"])]
    return AttackPlan(weak_points=weak_points, proposed_experiment=experiment, rationale="r")


async def test_run_adversary_loop_full_flow(monkeypatch):
    # plan → search → fetch → extract_counter → judge
    plan = _plan()
    verdict = _verdict("weaken", delta=-0.15)
    batch = CounterEvidenceBatch(
        items=[CounterEvidenceItem(quote="they charge $5", claim="cheaper", stance="refutes", confidence=0.8)]
    )
    d = _loop_dispatcher([(plan, 11), (batch, 22), (verdict, 33)])

    async def fake_search(queries, sources):
        return [_sr("http://page1")]

    monkeypatch.setattr(critic_loop, "_search_for_weak_point", fake_search)

    async def fake_fetch(urls, state, concurrency=4):
        return [SimpleNamespace(url="http://page1", content="real body")]

    monkeypatch.setattr(critic_loop, "web_fetch_many", fake_fetch)

    v, run_id, ce = await run_adversary_loop(thesis_id=10, triggering_finding_id=7, dispatcher=d, triggered_by_event_id=1)
    assert v is verdict and run_id == 33
    assert len(ce) == 1 and ce[0]["stance"] == "refutes" and ce[0]["url"] == ""


async def test_run_adversary_loop_caps_pages_per_weakpoint(monkeypatch):
    # >MAX_PAGES_PER_WEAKPOINT distinct urls → the `break` at the cap fires,
    # so only the first 3 urls are fetched.
    plan = _plan()
    verdict = _verdict("watch")
    d = _loop_dispatcher([(plan, 11), (verdict, 33)])

    async def fake_search(queries, sources):
        return [_sr(f"http://u{i}") for i in range(6)]

    monkeypatch.setattr(critic_loop, "_search_for_weak_point", fake_search)

    fetched_urls: list[list[str]] = []

    async def fake_fetch(urls, state, concurrency=4):
        fetched_urls.append(list(urls))
        return []  # no pages → no extract tasks; we only check the cap

    monkeypatch.setattr(critic_loop, "web_fetch_many", fake_fetch)
    v, _rid, ce = await run_adversary_loop(thesis_id=10, triggering_finding_id=None, dispatcher=d)
    assert v is verdict and ce == []
    assert fetched_urls == [["http://u0", "http://u1", "http://u2"]]  # capped at 3


async def test_run_adversary_loop_skips_non_item_extracts(monkeypatch):
    # A batch whose items aren't CounterEvidenceItem instances are skipped by
    # the isinstance guard (line 512). We feed a batch carrying a bare dict.
    plan = _plan()
    verdict = _verdict("watch")
    bad_batch = SimpleNamespace(items=[{"not": "an item"}])
    d = _loop_dispatcher([(plan, 11), (bad_batch, 22), (verdict, 33)])

    async def fake_search(queries, sources):
        return [_sr("http://page1")]

    monkeypatch.setattr(critic_loop, "_search_for_weak_point", fake_search)

    async def fake_fetch(urls, state, concurrency=4):
        return [SimpleNamespace(url="http://page1", content="body")]

    monkeypatch.setattr(critic_loop, "web_fetch_many", fake_fetch)
    v, _rid, ce = await run_adversary_loop(thesis_id=10, triggering_finding_id=None, dispatcher=d)
    assert v is verdict and ce == []  # non-item dropped


async def test_run_adversary_loop_no_urls(monkeypatch):
    # search returns nothing → no extract tasks, judge still runs with empty CE
    plan = _plan()
    verdict = _verdict("watch")
    d = _loop_dispatcher([(plan, 11), (verdict, 33)])

    async def empty_search(queries, sources):
        return []

    monkeypatch.setattr(critic_loop, "_search_for_weak_point", empty_search)
    v, run_id, ce = await run_adversary_loop(thesis_id=10, triggering_finding_id=None, dispatcher=d)
    assert v is verdict and run_id == 33 and ce == []


async def test_run_adversary_loop_skips_empty_pages(monkeypatch):
    # fetched pages None or blank-content are skipped; dedup drops repeat urls
    plan = _plan(weak_points=[WeakPoint(hypothesis="h", search_queries=["q"], sources=["web"])])
    verdict = _verdict("watch")
    d = _loop_dispatcher([(plan, 11), (verdict, 33)])

    async def fake_search(queries, sources):
        return [_sr("http://a"), _sr("http://a"), _sr("http://b")]  # dup http://a

    monkeypatch.setattr(critic_loop, "_search_for_weak_point", fake_search)

    async def fake_fetch(urls, state, concurrency=4):
        return [None, SimpleNamespace(url="http://b", content="   ")]  # blank skipped

    monkeypatch.setattr(critic_loop, "web_fetch_many", fake_fetch)
    v, run_id, ce = await run_adversary_loop(thesis_id=10, triggering_finding_id=None, dispatcher=d)
    assert v is verdict and ce == []


async def test_run_adversary_loop_extract_failure_swallowed(monkeypatch):
    # router.invoke raises on the extract step → that page yields nothing, judge still runs
    plan = _plan()
    verdict = _verdict("watch")

    def invoke_side(*, output_schema_class, **kw):
        if output_schema_class is AttackPlan:
            return (plan, 11)
        if output_schema_class is CounterEvidenceBatch:
            raise RuntimeError("extract boom")
        return (verdict, 33)

    d = AsyncMock()
    d.router = AsyncMock()
    d.router.invoke = AsyncMock(side_effect=invoke_side)
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.state = AsyncMock()
    d.session = None

    async def fake_search(queries, sources):
        return [_sr("http://page1")]

    monkeypatch.setattr(critic_loop, "_search_for_weak_point", fake_search)

    async def fake_fetch(urls, state, concurrency=4):
        return [SimpleNamespace(url="http://page1", content="body")]

    monkeypatch.setattr(critic_loop, "web_fetch_many", fake_fetch)
    v, run_id, ce = await run_adversary_loop(thesis_id=10, triggering_finding_id=None, dispatcher=d)
    assert v is verdict and ce == []


async def test_run_adversary_loop_with_experiment(monkeypatch):
    # proposed_experiment present → exp_dispatch + stress_test_interp run
    plan = _plan(experiment=ExperimentProposal(kind="fetch_pricing", params={"company": "OpenAI"}, why="cost check"))
    st = StressTestInterp(summary="cheaper rivals", bears_against_thesis=True, confidence=0.7)
    verdict = _verdict("kill", cited=[1])
    d = _loop_dispatcher([(plan, 11), (st, 22), (verdict, 33)])
    d.state.get_thesis = AsyncMock(return_value=_thesis(10))

    async def empty_search(queries, sources):
        return []

    monkeypatch.setattr(critic_loop, "_search_for_weak_point", empty_search)

    monkeypatch.setattr(exp_mod, "dispatch", AsyncMock(return_value={"price": 5}), raising=False)
    v, run_id, ce = await run_adversary_loop(thesis_id=10, triggering_finding_id=None, dispatcher=d)
    assert v is verdict and run_id == 33
    # stress test interp invoked → 3 router calls total (plan, st, judge)
    assert d.router.invoke.await_count == 3


async def test_run_adversary_loop_experiment_dispatch_fails(monkeypatch):
    # exp_dispatch raises → result={"error":...}; stress interp still runs
    plan = _plan(experiment=ExperimentProposal(kind="gh_search_trend", params={}, why="w"))
    st = StressTestInterp(summary="null", bears_against_thesis=False, confidence=0.1)
    verdict = _verdict("watch")
    d = _loop_dispatcher([(plan, 11), (st, 22), (verdict, 33)])
    d.state.get_thesis = AsyncMock(return_value=_thesis(10))

    async def empty_search(queries, sources):
        return []

    monkeypatch.setattr(critic_loop, "_search_for_weak_point", empty_search)

    monkeypatch.setattr(exp_mod, "dispatch", AsyncMock(side_effect=RuntimeError("exp boom")), raising=False)
    v, _rid, _ce = await run_adversary_loop(thesis_id=10, triggering_finding_id=None, dispatcher=d)
    assert v is verdict


async def test_run_adversary_loop_stress_interp_fails(monkeypatch):
    # the stress_test_interp router call raises → stress_test_dict stays None, judge runs
    plan = _plan(experiment=ExperimentProposal(kind="count_demand_signal", params={}, why="w"))
    verdict = _verdict("watch")

    def invoke_side(*, output_schema_class, **kw):
        if output_schema_class is AttackPlan:
            return (plan, 11)
        if output_schema_class is StressTestInterp:
            raise RuntimeError("interp boom")
        return (verdict, 33)

    d = AsyncMock()
    d.router = AsyncMock()
    d.router.invoke = AsyncMock(side_effect=invoke_side)
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.state = AsyncMock()
    d.state.get_thesis = AsyncMock(return_value=_thesis(10))
    d.session = None

    async def empty_search(queries, sources):
        return []

    monkeypatch.setattr(critic_loop, "_search_for_weak_point", empty_search)

    monkeypatch.setattr(exp_mod, "dispatch", AsyncMock(return_value={}), raising=False)
    v, _rid, _ce = await run_adversary_loop(thesis_id=10, triggering_finding_id=None, dispatcher=d)
    assert v is verdict


# =================================================================================
# evaluation.handler — _verdict_from_score
# =================================================================================
async def test_verdict_from_score_bands():
    assert _verdict_from_score(0.0) == "slop"
    assert _verdict_from_score(0.29) == "slop"
    assert _verdict_from_score(0.3) == "unclear"
    assert _verdict_from_score(0.69) == "unclear"
    assert _verdict_from_score(0.7) == "pass"
    assert _verdict_from_score(1.0) == "pass"


# =================================================================================
# evaluation.handler — _build_evaluation_task_data
# =================================================================================
def _evidence(url="http://p", *, sq=0, stance="supports", conf=0.7, claim="c", quote="q"):
    return {
        "url": url,
        "sub_question_idx": sq,
        "stance": stance,
        "confidence": conf,
        "claim": claim,
        "quote": quote,
    }


def _experiment(xid=1, *, kind="compare_repo_growth", status="completed", interp="grew 2x"):
    return {
        "id": xid,
        "kind": kind,
        "status": status,
        "params": {"repos": ["a/b"]},
        "interpretation": interp,
    }


async def test_build_evaluation_task_data_full():
    layer = await _build_evaluation_task_data(
        {
            "task": _task(),
            "findings": [_finding(1), _finding(2)],
            "evidence": [_evidence(), _evidence(url="http://p", sq=1)],
            "experiments": [_experiment(1)],
        },
        None,
        None,
    )
    assert "Task being audited" in layer.content
    assert "Finding F1" in layer.content
    assert "Experiment X1 (compare_repo_growth)" in layer.content
    assert "grew 2x" in layer.content


async def test_build_evaluation_task_data_empty_everything():
    layer = await _build_evaluation_task_data(
        {"task": _task(), "findings": [], "evidence": [], "experiments": []}, None, None
    )
    assert "(no findings)" in layer.content
    assert "(no evidence trail" in layer.content
    assert "(no experiments" in layer.content


async def test_build_evaluation_task_data_skips_incomplete_experiment():
    layer = await _build_evaluation_task_data(
        {
            "task": _task(),
            "findings": [_finding(1)],
            "evidence": [],
            "experiments": [_experiment(1, status="running")],
        },
        None,
        None,
    )
    assert "Experiment X1" not in layer.content
    assert "(no experiments" in layer.content


# =================================================================================
# evaluation.handler — handle_task_completed
# =================================================================================
def _audit_score(fid=1, score=0.9, verdict="pass"):
    return AuditScore(finding_id=fid, audit_score=score, verdict=verdict, reasoning="ok")


def _eval_dispatcher(monkeypatch, *, loop_return=None, legacy_return=None):
    d = AsyncMock()
    d.state = AsyncMock()
    d.memory = AsyncMock()
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.router = AsyncMock()
    if legacy_return is not None:
        d.router.invoke = AsyncMock(return_value=legacy_return)
    if loop_return is not None:
        monkeypatch.setattr(eval_loop, "run_audit_loop", AsyncMock(return_value=loop_return))
    return d


async def test_eval_handler_skips_non_research(monkeypatch):
    d = _eval_dispatcher(monkeypatch)
    d.state.get_task = AsyncMock(return_value=_task(dept="ops"))
    res = await handle_task_completed({"id": 1, "target_id": 5}, d)
    assert res == {"skipped": True, "reason": "non-research task"}


async def test_eval_handler_skips_no_findings(monkeypatch):
    d = _eval_dispatcher(monkeypatch)
    d.state.get_task = AsyncMock(return_value=_task())
    d.state.get_unaudited_findings_for_task = AsyncMock(return_value=[])
    res = await handle_task_completed({"id": 1, "target_id": 5}, d)
    assert res == {"skipped": True, "reason": "no unaudited findings"}


# NOTE — handler.py:295 reads `score.relevance_score`, but `score` is an
# AuditScore which has NO relevance_score field. Any finding whose DERIVED
# verdict is "pass" (audit_score >= 0.7) therefore raises an unhandled
# AttributeError *outside* the try/except, crashing the handler before the
# reinforcement / slop-breaker / dissent tail can run. The tests below pin
# that real behavior: pass-path crashes; the rest of the handler is only
# reachable with no pass-scored findings.


async def test_eval_handler_v2_pass_score_raises_relevance_bug(monkeypatch):
    # Derived verdict "pass" (score 0.9) hits the buggy score.relevance_score
    # access on handler.py:295 and raises (genuine production bug).
    _patch_graph(monkeypatch)
    findings = [_finding(1, rel=9, supports=True, claim_id=10)]
    batch = AuditBatch(scores=[_audit_score(1, 0.9)])
    d = _eval_dispatcher(monkeypatch, loop_return=(batch, 55))
    d.state.get_task = AsyncMock(return_value=_task())
    d.state.get_unaudited_findings_for_task = AsyncMock(return_value=findings)
    d.state.get_evidence_for_task = AsyncMock(return_value=[])
    d.state.get_experiment_runs_for_task = AsyncMock(return_value=[])
    with pytest.raises(AttributeError, match="relevance_score"):
        await handle_task_completed({"id": 9, "target_id": 5}, d)
    # the verdict was persisted before the crash
    d.state.update_finding_audit.assert_awaited_once()


async def test_eval_handler_v2_all_cross_checks_failed(monkeypatch):
    # run_id == 0 sentinel → handler short-circuits
    d = _eval_dispatcher(monkeypatch, loop_return=(AuditBatch(scores=[]), 0))
    d.state.get_task = AsyncMock(return_value=_task())
    d.state.get_unaudited_findings_for_task = AsyncMock(return_value=[_finding(1)])
    d.state.get_evidence_for_task = AsyncMock(return_value=[])
    d.state.get_experiment_runs_for_task = AsyncMock(return_value=[])
    res = await handle_task_completed({"id": 1, "target_id": 5}, d)
    assert res == {"skipped": True, "reason": "v2 audit: all cross_check steps failed"}
    d.state.update_finding_audit.assert_not_awaited()


async def test_eval_handler_all_slop_trips_breaker_and_narrates(monkeypatch):
    # No pass-scored finding → handler reaches breaker + dissent tail. Two slop
    # findings on the same claim; the breaker trips and dissent is narrated.
    _patch_graph(monkeypatch)
    findings = [_finding(1, claim_id=10), _finding(2, claim_id=10)]
    batch = AuditBatch(scores=[_audit_score(1, 0.1, "slop"), _audit_score(2, 0.2, "slop")])
    d = _eval_dispatcher(monkeypatch, loop_return=(batch, 70))
    d.state.get_task = AsyncMock(return_value=_task(desc="x" * 100))  # >80 chars branch
    d.state.get_unaudited_findings_for_task = AsyncMock(return_value=findings)
    d.state.get_evidence_for_task = AsyncMock(return_value=[])
    d.state.get_experiment_runs_for_task = AsyncMock(return_value=[])
    d.state.detect_slop_breaker = AsyncMock(return_value=True)  # breaker trips

    res = await handle_task_completed({"id": 3, "target_id": 5}, d)
    assert res["slop"] == 2 and res["breakers_tripped"] == [10]
    assert res["run_id"] == 70 and res["audited"] == 2
    assert d.state.update_finding_audit.await_count == 2
    # no pass → no reinforcement
    d.state.update_claim_confidence.assert_not_awaited()
    # slop_count > 0 → dissent narrative written
    d.memory.write_message.assert_awaited_once()
    assert d.memory.write_message.await_args.kwargs["session_id"] == "dissent"


async def test_eval_handler_unclear_no_breaker_no_dissent(monkeypatch):
    # Single unclear finding: no slop, breaker quiet, no dissent narration,
    # no reinforcement (not a pass). Exercises the clean no-op tail.
    _patch_graph(monkeypatch)
    findings = [_finding(1, rel=9, supports=True, claim_id=10)]
    batch = AuditBatch(scores=[_audit_score(1, 0.5, "unclear")])
    d = _eval_dispatcher(monkeypatch, loop_return=(batch, 12))
    d.state.get_task = AsyncMock(return_value=_task(desc="short"))
    d.state.get_unaudited_findings_for_task = AsyncMock(return_value=findings)
    d.state.get_evidence_for_task = AsyncMock(return_value=[])
    d.state.get_experiment_runs_for_task = AsyncMock(return_value=[])
    d.state.detect_slop_breaker = AsyncMock(return_value=False)
    res = await handle_task_completed({"id": 4, "target_id": 5}, d)
    assert res["slop"] == 0 and res["unclear"] == 1 and res["breakers_tripped"] == []
    d.state.update_claim_confidence.assert_not_awaited()
    d.memory.write_message.assert_not_awaited()
    graph_tools.merge_finding_grounds_claim.assert_not_awaited()


async def test_eval_handler_finding_without_claim_id_excluded(monkeypatch):
    # A slop finding with claim_id=None is excluded from claims_seen → no breaker
    # check for it (covers the `if f.claim_id is not None` guards).
    _patch_graph(monkeypatch)
    findings = [_finding(1, claim_id=None)]
    batch = AuditBatch(scores=[_audit_score(1, 0.1, "slop")])
    d = _eval_dispatcher(monkeypatch, loop_return=(batch, 1))
    d.state.get_task = AsyncMock(return_value=_task())
    d.state.get_unaudited_findings_for_task = AsyncMock(return_value=findings)
    d.state.get_evidence_for_task = AsyncMock(return_value=[])
    d.state.get_experiment_runs_for_task = AsyncMock(return_value=[])
    res = await handle_task_completed({"id": 5, "target_id": 5}, d)
    assert res["slop"] == 1 and res["breakers_tripped"] == []
    d.state.detect_slop_breaker.assert_not_awaited()


async def test_eval_handler_legacy_unclear_path(monkeypatch):
    monkeypatch.setenv("AUDITOR_LOOP", "legacy")
    _patch_graph(monkeypatch)
    findings = [_finding(1, rel=5, claim_id=10)]
    batch = AuditBatch(scores=[_audit_score(1, 0.5, "unclear")])
    d = _eval_dispatcher(monkeypatch, legacy_return=(batch, 44))
    d.state.get_task = AsyncMock(return_value=_task())
    d.state.get_unaudited_findings_for_task = AsyncMock(return_value=findings)
    d.state.get_evidence_for_task = AsyncMock(return_value=[])
    d.state.get_experiment_runs_for_task = AsyncMock(return_value=[])
    d.state.detect_slop_breaker = AsyncMock(return_value=False)
    res = await handle_task_completed({"id": 1, "target_id": 5}, d)
    assert res["unclear"] == 1 and res["run_id"] == 44
    d.curator.build.assert_awaited_once()
    d.router.invoke.assert_awaited_once()


# =================================================================================
# evaluation.loop — prompt builders
# =================================================================================
async def test_build_cross_check_finding_full():
    layer = await _build_cross_check_finding(
        {
            "task": _task(),
            "finding": _finding(1),
            "evidence": [_evidence(), _evidence(url="http://q")],
            "experiments": [_experiment(1)],
        },
        None,
        None,
    )
    assert "Audit one finding against its evidence trail" in layer.content
    assert "F1" in layer.content
    assert "Experiment X1" in layer.content
    assert "grew 2x" in layer.content


async def test_build_cross_check_finding_skips_incomplete_experiment():
    # An experiment that isn't completed is skipped (continue branch).
    layer = await _build_cross_check_finding(
        {
            "task": _task(),
            "finding": _finding(1),
            "evidence": [],
            "experiments": [_experiment(1, status="running")],
        },
        None,
        None,
    )
    assert "Experiment X1" not in layer.content
    assert "(no completed experiments)" in layer.content


async def test_build_cross_check_finding_empty():
    layer = await _build_cross_check_finding(
        {"task": _task(), "finding": _finding(1), "evidence": [], "experiments": []},
        None,
        None,
    )
    assert "(no evidence trail)" in layer.content
    assert "(no completed experiments)" in layer.content


async def test_build_batch_score_full():
    cc = EvidenceCrossCheck(
        finding_id=1,
        claims=[
            ClaimCheck(claim="rivals cheaper", quote="they charge $5", source_url="u", match="yes"),
            ClaimCheck(claim="no backing", quote=None, source_url=None, match="no"),
        ],
        substance="high",
        duplicate_of_finding_id=2,
        notes="load-bearing",
    )
    layer = await _build_batch_score(
        {"task": _task(), "findings": [_finding(1)], "cross_checks": [cc.model_dump()]},
        None,
        None,
    )
    assert "Final audit scoring" in layer.content
    assert "DUPLICATE of F2" in layer.content
    assert "[yes] rivals cheaper" in layer.content
    assert "load-bearing" in layer.content


async def test_build_batch_score_unknown_finding_skipped():
    cc = EvidenceCrossCheck(finding_id=999, claims=[], substance="low", notes="")
    layer = await _build_batch_score(
        {"task": _task(), "findings": [_finding(1)], "cross_checks": [cc.model_dump()]},
        None,
        None,
    )
    # finding 999 isn't in the batch → its block skipped → no findings rendered
    assert "(no findings — emit an empty scores list)" in layer.content


async def test_build_batch_score_no_claims_block():
    cc = EvidenceCrossCheck(finding_id=1, claims=[], substance="medium", notes="n")
    layer = await _build_batch_score(
        {"task": _task(), "findings": [_finding(1)], "cross_checks": [cc.model_dump()]},
        None,
        None,
    )
    assert "(no claims extracted)" in layer.content


# =================================================================================
# evaluation.loop — run_audit_loop orchestrator
# =================================================================================
def _audit_loop_dispatcher(invoke_side_effect):
    d = AsyncMock()
    d.router = AsyncMock()
    d.router.invoke = AsyncMock(side_effect=invoke_side_effect)
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.session = object()
    return d


def _cross(fid=1):
    return EvidenceCrossCheck(finding_id=fid, claims=[], substance="high", notes="n")


async def test_run_audit_loop_full(monkeypatch):
    findings = [_finding(1), _finding(2)]
    batch = AuditBatch(scores=[_audit_score(1, 0.9), _audit_score(2, 0.2)])
    # 2 cross_check calls (one per finding) + 1 batch_score
    d = _audit_loop_dispatcher([(_cross(1), 11), (_cross(2), 12), (batch, 99)])
    final, run_id = await run_audit_loop(
        task=_task(),
        findings=findings,
        evidence=[],
        experiments=[],
        dispatcher=d,
        triggered_by_event_id=1,
    )
    assert final is batch and run_id == 99
    assert d.router.invoke.await_count == 3


async def test_run_audit_loop_all_cross_checks_fail():
    findings = [_finding(1)]

    d = AsyncMock()
    d.router = AsyncMock()
    d.router.invoke = AsyncMock(side_effect=RuntimeError("cross boom"))
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.session = None
    final, run_id = await run_audit_loop(task=_task(), findings=findings, evidence=[], experiments=[], dispatcher=d)
    assert final.scores == [] and run_id == 0


async def test_run_audit_loop_partial_cross_check_failure():
    # one cross_check fails, one succeeds → batch_score still runs over the survivor
    findings = [_finding(1), _finding(2)]
    batch = AuditBatch(scores=[_audit_score(1, 0.8)])

    def invoke_side(*, output_schema_class, step_name=None, **kw):
        if output_schema_class is EvidenceCrossCheck:
            if "F2" in (step_name or ""):
                raise RuntimeError("F2 cross boom")
            return (_cross(1), 11)
        return (batch, 99)

    d = AsyncMock()
    d.router = AsyncMock()
    d.router.invoke = AsyncMock(side_effect=invoke_side)
    d.curator = AsyncMock()
    d.curator.build = AsyncMock(return_value="PROMPT")
    d.session = None
    final, run_id = await run_audit_loop(task=_task(), findings=findings, evidence=[], experiments=[], dispatcher=d)
    assert final is batch and run_id == 99


# =================================================================================
# evaluation.slop_handler — handle_audit_slop_detected
# =================================================================================
def _slop_dispatcher(*, claim=None, claim_exc=None, halted=2):
    d = AsyncMock()
    d.pool = ScriptedPool(rules=[("FROM halted", halted)])
    d.state = AsyncMock()
    if claim_exc is not None:
        d.state.get_claim = AsyncMock(side_effect=claim_exc)
    else:
        d.state.get_claim = AsyncMock(return_value=claim)
    d.memory = AsyncMock()
    return d


async def test_slop_handler_active_claim_lowers_confidence():
    d = _slop_dispatcher(claim=_claim(10, status="active", conf=0.6), halted=3)
    event = {"target_id": 10, "payload": {"slop_rate": 0.55}}
    res = await handle_audit_slop_detected(event, d)
    assert res["claim_id"] == 10
    assert res["tasks_halted"] == 3
    assert res["new_confidence"] == "0.40"  # 0.6 - 0.20
    assert res["slop_rate"] == 0.55
    d.state.update_claim_confidence.assert_awaited_once()
    assert d.state.update_claim_confidence.await_args.kwargs["new_confidence"] == pytest.approx(0.4)
    d.memory.write_message.assert_awaited_once()


async def test_slop_handler_confidence_floored():
    d = _slop_dispatcher(claim=_claim(10, status="active", conf=0.1))
    res = await handle_audit_slop_detected({"target_id": 10, "payload": {"slop_rate": 0.9}}, d)
    assert res["new_confidence"] == "0.00"  # max(0.0, 0.1 - 0.2)


async def test_slop_handler_inactive_claim_no_update():
    d = _slop_dispatcher(claim=_claim(10, status="killed", conf=0.5))
    res = await handle_audit_slop_detected({"target_id": 10, "payload": {"slop_rate": 0.5}}, d)
    assert res["new_confidence"] == "n/a (claim not active)"
    d.state.update_claim_confidence.assert_not_awaited()


async def test_slop_handler_missing_claim():
    d = _slop_dispatcher(claim_exc=ValueError("no such claim"))
    res = await handle_audit_slop_detected({"target_id": 10, "payload": {"slop_rate": 0.5}}, d)
    assert res["new_confidence"] == "n/a (claim missing)"
    d.state.update_claim_confidence.assert_not_awaited()


async def test_slop_handler_no_payload_defaults_rate_zero():
    d = _slop_dispatcher(claim=_claim(10, status="active", conf=0.5))
    res = await handle_audit_slop_detected({"target_id": 10}, d)
    assert res["slop_rate"] == 0.0
    assert res["new_confidence"] == "0.30"  # 0.5 - 0.2


async def test_slop_handler_no_tasks_halted():
    # fetchval returns None → tasks_halted coerces to 0
    d = _slop_dispatcher(claim=_claim(10, status="active", conf=0.5), halted=None)
    res = await handle_audit_slop_detected({"target_id": 10, "payload": {"slop_rate": 0.4}}, d)
    assert res["tasks_halted"] == 0
