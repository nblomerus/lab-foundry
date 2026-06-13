"""Pure unit tests for Ariadne's deterministic decision-framework (agents.ariadne.scoring).

The LLM scores nine 1-5 dimensions; the composite + priority are computed here, not by the
model — so this is the testable, deterministic core of the ranking. No DB / no network."""

from agents.ariadne.scoring import DIMENSIONS, WEIGHTS, composite, is_wellformed, priority_label


def _scores(**over):
    base = {d: 3 for d in DIMENSIONS}
    base.update(over)
    return base


def test_weights_cover_every_dimension_and_sum_to_one():
    assert set(WEIGHTS) == set(DIMENSIONS)
    assert round(sum(WEIGHTS.values()), 6) == 1.0


def test_composite_bounds_and_midpoint():
    assert composite({d: 5 for d in DIMENSIONS}) == 5.0
    assert composite({d: 1 for d in DIMENSIONS}) == 1.0
    assert composite(_scores()) == 3.0


def test_composite_weights_novelty_above_cost():
    base = composite(_scores())
    nov = composite(_scores(novelty=5)) - base  # novelty weight 0.18
    cost = composite(_scores(cost_efficiency=5)) - base  # cost weight 0.03
    assert nov > cost > 0


def test_impact_co_leads_with_novelty():
    # impact (significance) is the top-weighted dimension — a clear answer that
    # changes a real decision outranks publishability/novelty alone.
    base = composite(_scores())
    impact = composite(_scores(impact=5)) - base  # impact weight 0.20
    nov = composite(_scores(novelty=5)) - base  # novelty weight 0.18
    review = composite(_scores(reviewer_interest=5)) - base  # reviewer_interest weight 0.04
    assert impact >= nov > review > 0


def test_val_clamps_out_of_range_and_bad_types():
    assert composite(_scores(novelty=99)) == composite(_scores(novelty=5))
    assert composite(_scores(novelty=0)) == composite(_scores(novelty=1))
    assert composite(_scores(novelty="oops")) == composite(_scores(novelty=1))


def test_priority_label_thresholds():
    assert priority_label(5.0) == "high"
    assert priority_label(3.8) == "high"
    assert priority_label(3.79) == "medium"
    assert priority_label(2.8) == "medium"
    assert priority_label(2.79) == "low"
    assert priority_label(1.0) == "low"


def test_is_wellformed():
    assert is_wellformed(_scores()) is True
    assert is_wellformed(None) is False
    assert is_wellformed(_scores(novelty=6)) is False  # out of range high
    assert is_wellformed(_scores(novelty=0)) is False  # out of range low
