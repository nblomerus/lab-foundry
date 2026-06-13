"""
Schema for the novelty agent — the independent adjudicator.

One LLM step (`novelty.adjudicate`) is shown a proposed direction, the ACTUAL nearest
prior art retrieved from the corpus, and the lab's OWN recent directions, and returns an
independent read of whether the direction is genuinely novel + decision-grade and whether
it re-treads ground the lab already worked. The proposer's self-scores are NOT shown — the
whole point is an external check. The pass/hold verdict is derived from these fields
deterministically by the handler (not set by the model).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DirectionAdjudication(BaseModel):
    """An independent reviewer's read of a proposed direction (novelty + impact + rut)."""

    novelty_independent: int = Field(
        ...,
        ge=1,
        le=5,
        description=(
            "Independent novelty vs the NEAREST PRIOR ART shown: 5 = clearly advances beyond it; "
            "3 = a worthwhile new angle; 1 = the prior art already answers this. Be a tough reviewer — "
            "default LOW unless the direction clearly goes beyond what the retrieved papers already establish."
        ),
    )
    impact_independent: int = Field(
        ...,
        ge=1,
        le=5,
        description=(
            "Independent significance: would a CLEAR answer change a real build/deploy decision a named "
            "practitioner faces? 5 = flips a real decision; 1 = nobody would act on the answer. Score the "
            "DECISION VALUE, not how interesting it sounds."
        ),
    )
    is_novel: bool = Field(
        ..., description="True only if the direction genuinely advances beyond the nearest prior art shown."
    )
    is_impactful: bool = Field(
        ..., description="True only if a clear answer would change a concrete practitioner decision."
    )
    redundant: bool = Field(
        ...,
        description="True if this re-treads a topic the lab ALREADY worked (see the prior lab directions shown) — a rut.",
    )
    redundant_note: str = Field("", description="If redundant, which prior direction/topic it repeats; else empty.")
    rationale: str = Field(
        ..., description="2-3 sentences: the closest prior work, what's new (or not), and the decision at stake."
    )
