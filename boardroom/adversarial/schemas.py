"""
Adversary loop schemas.

Four stages:
  - AttackPlan          (plan_attack)  — name the weak assumptions worth attacking
  - CounterEvidenceItem (extract_counter per page) — per-quote refutation candidates
  - StressTestInterp    (stress_test)  — interpret experiment result against the thesis
  - AdversaryVerdictOut (judge_verdict, REUSED from handlers.adversary) — the final
    watch/weaken/kill decision

The shape mirrors the researcher loop deliberately: plan → fan-out per
weak point → optional experiment → judge. Reusing that mental model means
the trace DAG renders the same way and the Curator/Router plumbing is the
same; only the prompts and the orchestration differ.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class WeakPoint(BaseModel):
    """A specific assumption the thesis relies on, plus how to attack it."""
    hypothesis: str = Field(
        ...,
        description="A specific claim that would refute (or weaken) the thesis if true. "
                    "Not 'the thesis is wrong' — name a concrete falsifier.",
    )
    search_queries: list[str] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="1-2 search queries likely to surface evidence for the hypothesis. "
                    "Be specific — generic queries return generic noise.",
    )
    sources: list[Literal["web", "hacker_news", "reddit"]] = Field(
        default_factory=lambda: ["web"],
        description="Which sources to query.",
    )


class ExperimentProposal(BaseModel):
    """Optional stress test — a 'do something' that would discriminate."""
    kind: Literal[
        "fetch_pricing", "count_demand_signal",
        "compare_repo_growth", "gh_search_trend",
    ]
    params: dict
    why: str = Field(..., description="What this experiment would tell us about the thesis.")


class AttackPlan(BaseModel):
    """Critic's investigation plan: where the thesis is brittle and how to test."""
    weak_points: list[WeakPoint] = Field(
        ...,
        min_length=1,
        max_length=3,
        description="2-3 specific brittle assumptions the thesis depends on.",
    )
    proposed_experiment: Optional[ExperimentProposal] = Field(
        None,
        description="Optional 'do something' test. Null when no experiment would discriminate.",
    )
    rationale: str = Field(
        ...,
        description="One sentence: what's the single most load-bearing claim in the thesis, "
                    "and which weak point most cleanly attacks it?",
    )


# -------------------------------------------------------------------------
# Counter-evidence extraction (per fetched page)
# -------------------------------------------------------------------------

class CounterEvidenceItem(BaseModel):
    """A verbatim quote from a page that bears on a weak point."""
    quote: str = Field(..., description="Verbatim sentence/phrase from the page. No paraphrasing.")
    claim: str = Field(..., description="What the quote means against the weak point, one sentence.")
    stance: Literal["refutes", "supports", "neutral"] = Field(
        ...,
        description="refutes: backs the WEAK POINT (i.e. refutes the thesis). "
                    "supports: backs the thesis. "
                    "neutral: ambiguous but worth seeing.",
    )
    confidence: float = Field(..., ge=0.0, le=1.0)


class CounterEvidenceBatch(BaseModel):
    """All items extracted from one page. Empty list is the correct answer when
    the page has nothing concrete on the weak point — don't pad."""
    items: list[CounterEvidenceItem] = Field(default_factory=list)


# -------------------------------------------------------------------------
# Stress-test interpretation
# -------------------------------------------------------------------------

class StressTestInterp(BaseModel):
    """Adversarial reading of an experiment result."""
    summary: str = Field(..., description="2-3 sentences: what did the experiment show?")
    bears_against_thesis: bool = Field(
        ...,
        description="Does the result refute or weaken the thesis, even slightly?",
    )
    confidence: float = Field(..., ge=0.0, le=1.0,
                              description="How load-bearing is this result on the verdict?")
