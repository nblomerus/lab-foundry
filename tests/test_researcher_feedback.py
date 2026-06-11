"""Unit tests for the Researcher feedback seam (agents.researcher.feedback).

disposition / finding_feedback / aggregate_direction are pure — they turn a finding into the
confidence/acquire proposal Ariadne's reflection reads. refine_disposition (escalation read) and
apply_feedback (the live write path) hit the DB; here they run against a ScriptedPool with the
acquire ledger / claim rows scripted and request_acquire monkeypatched. No real DB / no network."""

from __future__ import annotations

import pytest

from agents.researcher import feedback as fb_mod
from agents.researcher.feedback import (
    DirectionFeedback,
    aggregate_direction,
    apply_feedback,
    disposition,
    finding_feedback,
    refine_disposition,
)
from agents.researcher.grounded import GroundedFinding
from tests._helpers import ScriptedPool, make_state

_CTX = {"task_id": 1}


def _finding(verdict="inconclusive", blocker="none", confidence=1.0, acquire_queries=None):
    return GroundedFinding(
        verdict=verdict,
        blocker=blocker,
        confidence=confidence,
        summary="s",
        key_evidence=[],
        kill_condition_check="k",
        gaps=[],
        acquire_queries=acquire_queries or [],
        next_step="n",
    )


def test_disposition_maps_verdict_then_blocker():
    assert disposition(_finding(verdict="supports")) == "supported"
    assert disposition(_finding(verdict="contradicts")) == "contradicted"
    assert disposition(_finding(blocker="thin_corpus")) == "thin_corpus"
    assert disposition(_finding(blocker="needs_experiment")) == "needs_experiment"
    assert disposition(_finding()) == "inconclusive"


def test_supported_moves_confidence_only_when_grounded():
    f = _finding(verdict="supports", confidence=1.0)
    grounded = finding_feedback(_CTX, f, 1.0)
    assert grounded.confidence_delta > 0 and grounded.set_last_evidence is True
    ungrounded = finding_feedback(_CTX, f, 0.0)  # cited evidence didn't resolve
    assert ungrounded.confidence_delta == 0.0 and ungrounded.set_last_evidence is False


def test_contradiction_bites_harder_than_support():
    sup = finding_feedback(_CTX, _finding(verdict="supports", confidence=1.0), 1.0)
    con = finding_feedback(_CTX, _finding(verdict="contradicts", confidence=1.0), 1.0)
    assert con.confidence_delta < 0 < sup.confidence_delta
    assert abs(con.confidence_delta) > abs(sup.confidence_delta)


def test_thin_corpus_fires_acquires_without_moving_confidence():
    ff = finding_feedback(_CTX, _finding(blocker="thin_corpus", acquire_queries=["q1", "q2"]), 1.0)
    assert ff.disposition == "thin_corpus"
    assert ff.confidence_delta == 0.0
    assert ff.acquire_queries == ["q1", "q2"]


def test_corpus_exhausted_nudges_down_without_evidence():
    ff = finding_feedback(_CTX, _finding(blocker="thin_corpus"), 0.0, disposition_override="corpus_exhausted")
    assert ff.disposition == "corpus_exhausted"
    assert ff.confidence_delta < 0
    assert ff.set_last_evidence is False  # a structural nudge, not gathered evidence


def test_aggregate_clamps_net_and_picks_decisive_dominant():
    items = [
        finding_feedback(_CTX, _finding(verdict="contradicts", confidence=1.0), 1.0),
        finding_feedback(_CTX, _finding(verdict="supports", confidence=1.0), 1.0),
    ]
    fb = aggregate_direction(7, "dir", items)
    assert fb.claim_id == 7
    assert -0.20 <= fb.confidence_delta <= 0.20
    assert fb.dominant == "contradicted"  # decisive verdict outranks
    assert fb.set_last_evidence is True


def test_aggregate_dedups_acquire_union():
    items = [
        finding_feedback(_CTX, _finding(blocker="thin_corpus", acquire_queries=["a", "b"]), 1.0),
        finding_feedback(_CTX, _finding(blocker="thin_corpus", acquire_queries=["b", "c"]), 1.0),
    ]
    fb = aggregate_direction(7, "dir", items)
    assert fb.acquire_queries == ["a", "b", "c"]
    assert fb.dominant == "thin_corpus"


# ════════════════════════════════════════════════════════════════════════════════
# refine_disposition — the thin_corpus → corpus_exhausted escalation (async read)
# ════════════════════════════════════════════════════════════════════════════════
def _df(claim_id=7, *, confidence_delta=0.0, dominant="inconclusive", set_last_evidence=False, acquire_queries=None):
    return DirectionFeedback(
        claim_id=claim_id,
        direction="dir",
        n_findings=1,
        confidence_delta=confidence_delta,
        dominant=dominant,
        set_last_evidence=set_last_evidence,
        acquire_queries=acquire_queries or [],
        items=[],
    )


@pytest.mark.asyncio
async def test_refine_disposition_returns_base_when_not_thin_corpus():
    # non-thin base never reads the ledger — the pool must stay untouched
    pool = ScriptedPool()
    state = make_state(pool=pool)
    assert await refine_disposition(state, 7, "supported") == "supported"
    assert pool.calls == []


@pytest.mark.asyncio
async def test_refine_disposition_returns_base_when_claim_id_none():
    pool = ScriptedPool()
    state = make_state(pool=pool)
    assert await refine_disposition(state, None, "thin_corpus") == "thin_corpus"
    assert pool.calls == []


@pytest.mark.asyncio
async def test_refine_disposition_escalates_only_after_acquire_AND_scout_exhausted():
    # >=2 'already_have' AND a targeted scout sweep already ran for this direction → genuine gap
    pool = ScriptedPool(
        rules=[
            ("acquire.fulfilled", [{"status": "already_have"}, {"status": "already_have"}]),
            ("library.sweep_requested", 1),  # a closure scout sweep fired for this claim
        ]
    )
    state = make_state(pool=pool)
    assert await refine_disposition(state, 7, "thin_corpus") == "corpus_exhausted"
    assert pool.calls[0][2] == (7,)  # claim_id bound to the query


@pytest.mark.asyncio
async def test_refine_disposition_holds_thin_when_acquire_exhausted_but_not_scouted():
    # 2 'already_have' but NO scout sweep yet → acquire ≠ scouting → NOT a gap → stays thin_corpus
    # (the closure ladder will fire the targeted scout sweep before this can escalate)
    pool = ScriptedPool(rules=[("acquire.fulfilled", [{"status": "already_have"}, {"status": "already_have"}])])
    state = make_state(pool=pool)
    assert await refine_disposition(state, 7, "thin_corpus") == "thin_corpus"
    # it DID consult the scout ledger (the new gate) before holding
    assert any("library.sweep_requested" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_refine_disposition_no_escalation_when_mixed_statuses():
    # a genuine new ingest among the replies → still acquirable → stays thin_corpus
    pool = ScriptedPool(rules=[("acquire.fulfilled", [{"status": "already_have"}, {"status": "ingested"}])])
    state = make_state(pool=pool)
    assert await refine_disposition(state, 7, "thin_corpus") == "thin_corpus"


@pytest.mark.asyncio
async def test_refine_disposition_no_escalation_when_too_few_rows():
    # only one reply (< 2) → not enough history to call it exhausted
    pool = ScriptedPool(rules=[("acquire.fulfilled", [{"status": "already_have"}])])
    state = make_state(pool=pool)
    assert await refine_disposition(state, 7, "thin_corpus") == "thin_corpus"


@pytest.mark.asyncio
async def test_refine_disposition_no_escalation_when_no_history():
    # no claim-attributed replies yet → empty ledger → stays thin_corpus
    pool = ScriptedPool(rules=[("acquire.fulfilled", [])])
    state = make_state(pool=pool)
    assert await refine_disposition(state, 7, "thin_corpus") == "thin_corpus"


# ════════════════════════════════════════════════════════════════════════════════
# apply_feedback — the live write path (advisory/active only)
# ════════════════════════════════════════════════════════════════════════════════
def _active_pool(confidence=0.5):
    """A pool whose claims SELECT returns an ACTIVE claim's current confidence."""
    return ScriptedPool(rules=[("SELECT confidence FROM claims", confidence)])


@pytest.mark.asyncio
async def test_apply_feedback_skips_when_no_claim_id():
    state = make_state()
    res = await apply_feedback(state, _df(claim_id=None))
    assert res == {"skipped": "no claim_id"}
    state.update_claim_confidence.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_feedback_moves_confidence_on_active_claim(monkeypatch):
    monkeypatch.setattr(fb_mod, "request_acquire", _noop_acquire)
    pool = _active_pool(0.5)
    state = make_state(pool=pool)
    res = await apply_feedback(state, _df(confidence_delta=0.08, dominant="supported"), run_id=42)
    # 0.5 + 0.08 = 0.58, clamped into [0,1]
    assert res["confidence"] == [0.5, 0.58]
    _args, kwargs = state.update_claim_confidence.call_args
    assert _args[0] == 7 and abs(_args[1] - 0.58) < 1e-9
    assert kwargs["reason"] == "researcher findings: supported"
    assert kwargs["run_id"] == 42


@pytest.mark.asyncio
async def test_apply_feedback_research_ceiling_and_clamp(monkeypatch):
    monkeypatch.setattr(fb_mod, "request_acquire", _noop_acquire)
    # From below: a positive literature move can't push past the 0.85 research ceiling.
    res = await apply_feedback(make_state(pool=_active_pool(0.80)), _df(confidence_delta=0.20, dominant="supported"))
    assert res["confidence"] == [0.8, 0.85]  # 1.0 capped at the ceiling
    # Already above the ceiling (experiment-earned): a positive research move is HELD, not raised.
    res2 = await apply_feedback(make_state(pool=_active_pool(0.95)), _df(confidence_delta=0.20, dominant="supported"))
    assert res2["confidence"] == [0.95, 0.95]
    # A negative move is never lifted by the ceiling — still clamps at the 0.0 floor.
    res3 = await apply_feedback(make_state(pool=_active_pool(0.05)), _df(confidence_delta=-0.12, dominant="contradicted"))
    assert res3["confidence"] == [0.05, 0.0]


@pytest.mark.asyncio
async def test_apply_feedback_skips_confidence_when_claim_not_active(monkeypatch):
    monkeypatch.setattr(fb_mod, "request_acquire", _noop_acquire)
    # default_val None → the active-status SELECT finds no row (invalidated/merged/retired)
    pool = ScriptedPool()
    state = make_state(pool=pool)
    res = await apply_feedback(state, _df(confidence_delta=-0.12, dominant="contradicted"))
    assert "confidence" not in res
    state.update_claim_confidence.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_feedback_swallows_value_error_from_race(monkeypatch):
    monkeypatch.setattr(fb_mod, "request_acquire", _noop_acquire)
    pool = _active_pool(0.5)
    state = make_state(pool=pool)
    state.update_claim_confidence.side_effect = ValueError("claim 7 not active")
    res = await apply_feedback(state, _df(confidence_delta=0.08, dominant="supported"))
    # the race is logged, not raised, and no confidence pair recorded
    assert "confidence" not in res
    state.update_claim_confidence.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_feedback_sets_last_evidence(monkeypatch):
    monkeypatch.setattr(fb_mod, "request_acquire", _noop_acquire)
    pool = ScriptedPool()
    state = make_state(pool=pool)
    res = await apply_feedback(state, _df(set_last_evidence=True))
    assert res["last_evidence_at"] == "now"
    assert any("UPDATE claims SET last_evidence_at = now()" in c[1] for c in pool.calls)


@pytest.mark.asyncio
async def test_apply_feedback_fires_acquires(monkeypatch):
    seen = []

    async def _acq(state, mreq):
        seen.append(mreq)
        return True  # emitted (not held by backpressure)

    monkeypatch.setattr(fb_mod, "request_acquire", _acq)
    pool = ScriptedPool()
    state = make_state(pool=pool)
    res = await apply_feedback(state, _df(acquire_queries=["q1", "q2"]))
    assert res["acquires_fired"] == 2
    assert "acquires_held_backpressure" not in res
    assert [m.query for m in seen] == ["q1", "q2"]
    assert all(m.requester == "researcher" and m.claim_id == 7 and m.kind == "paper" for m in seen)
    assert all(len(m.why) >= 30 for m in seen)  # AcquireRequest.why min_length=30 satisfied


@pytest.mark.asyncio
async def test_apply_feedback_swallows_acquire_failure(monkeypatch):
    calls = {"n": 0}

    async def _boom(state, mreq):
        calls["n"] += 1
        raise RuntimeError("acquire blew up")

    monkeypatch.setattr(fb_mod, "request_acquire", _boom)
    state = make_state()
    res = await apply_feedback(state, _df(acquire_queries=["q1", "q2"]))
    # both attempts raised, all swallowed → no acquires_fired key, no propagation
    assert "acquires_fired" not in res
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_apply_feedback_full_path_combines_all_effects(monkeypatch):
    seen = []

    async def _acq(state, mreq):
        seen.append(mreq)
        return True  # emitted

    monkeypatch.setattr(fb_mod, "request_acquire", _acq)
    pool = _active_pool(0.5)
    state = make_state(pool=pool)
    res = await apply_feedback(
        state,
        _df(confidence_delta=-0.12, dominant="contradicted", set_last_evidence=True, acquire_queries=["q"]),
        run_id=9,
    )
    assert res["claim_id"] == 7 and res["dominant"] == "contradicted"
    assert res["confidence"] == [0.5, 0.38]
    assert res["last_evidence_at"] == "now"
    assert res["acquires_fired"] == 1
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_apply_feedback_records_backpressure_holds(monkeypatch):
    # request_acquire returns False when Mimir's queue is deep → the acquires are
    # HELD, surfaced as acquires_held_backpressure, and NOT counted as fired.
    held = []

    async def _held(state, mreq):
        held.append(mreq)
        return False

    monkeypatch.setattr(fb_mod, "request_acquire", _held)
    state = make_state(pool=ScriptedPool())
    res = await apply_feedback(state, _df(acquire_queries=["q1", "q2", "q3"]))
    assert res["acquires_held_backpressure"] == 3
    assert "acquires_fired" not in res
    assert len(held) == 3  # all attempted, all held


async def _noop_acquire(state, mreq):
    return True
