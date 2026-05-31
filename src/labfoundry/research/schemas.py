"""
Schemas for the agentic researcher loop.

Each step of the loop has its own output schema so the LLM call is bounded
and the structured output stays JSON-disciplined. The final `Synthesis`
emits the legacy `FindingOut` shape so the downstream evaluation / critic /
PI paths see no change.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# -------------------------------------------------------------------------
# Sources the researcher can dispatch to per sub-question
# -------------------------------------------------------------------------

SourceKind = Literal["web", "hacker_news", "reddit"]


# -------------------------------------------------------------------------
# Plan
# -------------------------------------------------------------------------


class SubQuestion(BaseModel):
    q: str = Field(..., description="The specific sub-question to investigate.")
    sources: list[SourceKind] = Field(
        default_factory=lambda: ["web"],
        description="Which sources to query for this sub-question.",
    )
    why: str = Field(..., description="One sentence: why this sub-question matters to the framing.")
    k: int = Field(default=3, ge=1, le=8, description="How many top results to fetch from each source.")


ExperimentKind = Literal[
    "fetch_pricing",
    "count_demand_signal",
    "compare_repo_growth",
    "gh_search_trend",
]


class ProposedExperiment(BaseModel):
    kind: ExperimentKind
    params: dict = Field(default_factory=dict, description="Kind-specific parameters.")
    why: str = Field(..., description="One sentence: what this experiment tests.")


class InquiryPlan(BaseModel):
    question: str = Field(..., description="The framing question for this iteration.")
    sub_questions: list[SubQuestion] = Field(..., min_length=1, max_length=6)
    proposed_experiments: list[ProposedExperiment] = Field(default_factory=list, max_length=4)


# -------------------------------------------------------------------------
# Extract (per page)
# -------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    quote: str = Field(..., max_length=600, description="A verbatim quote from the page, with no paraphrasing.")
    claim: str = Field(..., max_length=300, description="What the quote actually says, in your words.")
    stance: Literal["supports", "refutes", "neutral"] = Field(
        ...,
        description="Does the evidence support, refute, or sit neutral to the sub-question?",
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="How load-bearing is this evidence (0..1)?")


class EvidenceBatch(BaseModel):
    evidence: list[EvidenceItem] = Field(
        default_factory=list,
        description="Zero or more evidence items. Empty is correct when the page has no specific signal.",
    )


# -------------------------------------------------------------------------
# Synthesize (final)
# -------------------------------------------------------------------------


class FindingOut(BaseModel):
    """The synthesized finding written to the `findings` table.

    Matches the legacy researcher.execute_task output so the evaluation and
    downstream consumers don't change.
    """

    source: Literal["hacker_news", "arxiv", "reddit", "web", "other"]
    url: str | None = None
    title: str
    summary: str = Field(..., max_length=2000)
    relevance_score: float = Field(..., ge=1.0, le=10.0)
    why_it_matters: str = Field(..., max_length=500)
    supports_thesis: bool | None = None


class Synthesis(BaseModel):
    summary: str = Field(..., max_length=1500, description="2-4 sentence overall answer to the framing question.")
    findings: list[FindingOut] = Field(default_factory=list, max_length=8)
    weakest_subquestion_idx: int = Field(
        default=-1,
        description="Index of the sub-question with the least / weakest evidence. -1 if all are well-supported.",
    )
    open_questions: list[str] = Field(default_factory=list, max_length=6, description="Questions still not answered.")


# -------------------------------------------------------------------------
# Gap check (Phase 3 — declared here so the schema lives in one place)
# -------------------------------------------------------------------------


class GapCheck(BaseModel):
    has_gaps: bool
    gaps: list[str] = Field(default_factory=list, max_length=5)
    proposed_followups: list[SubQuestion] = Field(default_factory=list, max_length=4)
    should_iterate: bool
    reason: str = Field(..., max_length=400)


# -------------------------------------------------------------------------
# Experiment interpretation (Phase 3)
# -------------------------------------------------------------------------


class ExperimentInterpretation(BaseModel):
    summary: str = Field(..., max_length=800, description="What the experiment result tells us about the question.")
    bears_on_subquestion_idxs: list[int] = Field(
        default_factory=list, description="Which sub-questions this result speaks to."
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
