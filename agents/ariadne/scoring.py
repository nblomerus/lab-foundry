"""
Ariadne's decision framework — turn the 9 per-direction scores into one priority.

The diagram's DECISION FRAMEWORK, made deterministic: the LLM scores each direction on
nine 1–5 dimensions; the COMPOSITE and priority label are computed here, not by the model,
so ranking is principled and inspectable. Weights reflect a research lab chasing novel,
paper-worthy contributions — novelty / paper-potential / differentiation lead; cost is a
light tie-breaker.
"""

from __future__ import annotations

DIMENSIONS = [
    "novelty",
    "feasibility",
    "evidence_availability",
    "paper_potential",
    "reviewer_interest",
    "technical_depth",
    "differentiation",
    "cost_efficiency",
    "lab_alignment",
]

WEIGHTS = {
    "novelty": 0.20,
    "paper_potential": 0.18,
    "differentiation": 0.14,
    "feasibility": 0.12,
    "evidence_availability": 0.10,
    "reviewer_interest": 0.10,
    "technical_depth": 0.08,
    "lab_alignment": 0.05,
    "cost_efficiency": 0.03,
}  # sums to 1.0


def _val(scores, dim: str) -> int:
    v = scores[dim] if isinstance(scores, dict) else getattr(scores, dim, None)
    try:
        return max(1, min(5, int(v)))  # clamp — a stray score never breaks the composite
    except (TypeError, ValueError):
        return 1


def is_wellformed(scores) -> bool:
    """True iff every dimension is an integer in 1..5 (the grade predicate)."""
    if scores is None:
        return False
    for dim in DIMENSIONS:
        v = scores[dim] if isinstance(scores, dict) else getattr(scores, dim, None)
        if not isinstance(v, int) or v < 1 or v > 5:
            return False
    return True


def composite(scores) -> float:
    """Weighted mean of the nine dimensions, in [1.0, 5.0]."""
    return round(sum(WEIGHTS[d] * _val(scores, d) for d in WEIGHTS), 2)


def priority_label(comp: float) -> str:
    if comp >= 3.8:
        return "high"
    if comp >= 2.8:
        return "medium"
    return "low"
