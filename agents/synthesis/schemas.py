"""
Schema for the synthesis agent.

One LLM step (`synthesis.compose`) reads a direction's completed experiments + the
researcher's findings and returns a ResearchFinding — the paper-shaped terminal the
loop was missing: a single defensible claim, the method that settles it, the actual
numbers, honest limitations, and the "so what" (who acts on it and how). It is NOT a
new hypothesis — it is the answer the accumulated evidence supports.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ResearchFinding(BaseModel):
    """A paper-shaped conclusion assembled from a direction's completed experiments."""

    headline: str = Field(
        ...,
        description=(
            "One sentence stating the result, paper-title style: 'On <task>, <method A> "
            "<verb> <method B> by <quantified effect>.' Concrete and falsifiable — no hedging."
        ),
    )
    claim: str = Field(
        ...,
        description="The single defensible claim the evidence supports, stated so it could be cited.",
    )
    supported: Literal["supported", "refuted", "mixed", "inconclusive"] = Field(
        ...,
        description=(
            "How the accumulated experiments bear on the direction's hypothesis: supported = the "
            "evidence backs the claim; refuted = it contradicts it (a negative result is still a "
            "finding); mixed = it depends on conditions; inconclusive = the evidence cannot decide."
        ),
    )
    method: str = Field(
        ..., description="How it was tested across the experiments — datasets, models, metrics, controls."
    )
    key_numbers: str = Field(
        ..., description="The actual numbers that carry the claim — effects, deltas, CIs, costs. Cite them."
    )
    limitations: str = Field(
        ..., description="Honest scope limits — toy data, single GPU, small N, confounds, what was NOT shown."
    )
    so_what: str = Field(
        ...,
        description="Who acts on this and what they do differently — the decision it changes (the paper's 'so what').",
    )
    next_step: str = Field(
        "", description="The single most valuable experiment to harden or extend this finding (optional)."
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How load-bearing the finding is — calibrate DOWN for toy/synthetic/small-N evidence.",
    )
    grounded_in_experiments: list[int] = Field(
        default_factory=list,
        description="The experiment ids (from those shown) this finding rests on. Cite only ones you used.",
    )
