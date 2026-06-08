"""Pure unit tests for the Researcher feedback seam's steering math (agents.researcher.feedback).

disposition / finding_feedback / aggregate_direction are pure — they turn a finding into the
confidence/acquire proposal Ariadne's reflection reads. (refine_disposition / apply_feedback hit
the DB and are exercised live, not here.) No DB / no network."""

from agents.researcher.feedback import aggregate_direction, disposition, finding_feedback
from agents.researcher.grounded import GroundedFinding

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
