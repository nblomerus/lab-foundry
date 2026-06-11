"""
Ariadne's structured output — the contract for shadow-mode deliberation.

Deliberately legible (the readiness plan's "every agent output is structured"). The
ClaimGoal fields map 1:1 to the `claim_goals` table (migration 005) so advisory/active
mode can persist them directly; Direction maps to a `claim_kind='direction'` claims row
with per-hypothesis goals. In shadow mode this is produced and printed — never written.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClaimGoal(BaseModel):
    """A per-direction research goal — mirrors the claim_goals table."""

    expectation: str = Field(..., description="what evidence would confirm it")
    kill_condition: str = Field(..., description="what would refute it / when to stop")
    novelty_target: str | None = Field(None, description="why it'd be publishable / not already done")
    next_milestone: str | None = Field(None, description="the concrete next proof")
    priority_hint: str | None = Field(None, description="high | medium | low")


class DecisionScores(BaseModel):
    """Ariadne's decision-framework scores for a direction — each 1 (poor) … 5 (excellent).
    The composite priority is derived deterministically from these (agents.ariadne.scoring),
    so prioritization is principled and inspectable, not a vibe."""

    novelty: int = Field(
        ..., description="how new vs the FIELD MODEL + prior art (EMERGING/under-served → high, SATURATED → low)"
    )
    impact: int = Field(
        ...,
        description="would a CLEAR, falsifiable answer CHANGE how practitioners build ML/AI systems? "
        "5 = flips a real build/deploy decision many practitioners face (e.g. 'use 4-bit not 8-bit', "
        "'GP not XGBoost when calibration matters') or quantifies a real cost/accuracy/latency tradeoff "
        "people pay; 3 = informs a niche / one-team decision; 1 = true-but-inconsequential, changes no "
        "decision. Score the DECISION VALUE of the answer, NOT its novelty or publishability.",
    )
    feasibility: int = Field(..., description="can the lab realistically execute it")
    evidence_availability: int = Field(..., description="is there corpus evidence to ground & test it")
    paper_potential: int = Field(..., description="likelihood of a paper-worthy contribution")
    reviewer_interest: int = Field(..., description="would top-venue reviewers care")
    technical_depth: int = Field(..., description="substance / rigor the direction can sustain")
    differentiation: int = Field(..., description="distinct from saturated, well-trodden work")
    cost_efficiency: int = Field(..., description="resource efficiency — 5 = cheap/light, 1 = very costly")
    lab_alignment: int = Field(
        ..., description="fit with the lab's strengths (hybrid retrieval, trust gate, reasoning graph)"
    )
    rationale: str = Field(
        "", description="one line justifying the scores, citing field-model trend states where relevant"
    )


class Direction(BaseModel):
    """A novelty unit: 'attack X via approach Y', grounded in retrieved prior art."""

    title: str
    statement: str = Field(..., description="the direction as a falsifiable research bet")
    stakes: str = Field(
        ...,
        description="why this MATTERS: the real DECISION a clear answer changes, WHO acts on it (a named "
        "practitioner / system-builder / the field resolving a tension), and what it saves or settles. "
        "NOT 'X is important' — name a concrete actor AND a concrete decision. This is the paper's 'so what'.",
    )
    novelty_rationale: str = Field(..., description="why it's novel, grounded in the prior art shown (name the gap)")
    grounded_in: list[str] = Field(
        default_factory=list,
        description="EXACT paper titles from the prior art shown that justify the novelty claim "
        "— real retrieved evidence, never invented",
    )
    scores: DecisionScores | None = Field(None, description="decision-framework scores (all 9 dimensions, 1–5)")
    claim_goals: list[ClaimGoal] = Field(default_factory=list)
    kill_conditions: list[str] = Field(default_factory=list)
    reviewer_risks: list[str] = Field(
        default_factory=list, description="weak eval, baselines, LLM-judge bias, reproducibility…"
    )


class AcquireRequest(BaseModel):
    """A request for Mimir to fetch a SPECIFIC paper that's likely MISSING — not a topic.
    The corpus already covers topics comprehensively (the scouts pull the newest papers on
    everything), so a broad topic just returns 'already have'. Only a specific foundational or
    cross-domain paper is worth a fetch. Under-explored GAPS become DIRECTIONS, not requests."""

    paper: str = Field(
        ..., description="a SPECIFIC paper — its exact title, or an arxiv id — you believe is missing & important"
    )
    arxiv_id: str | None = Field(
        None, description="the arxiv id if you know it (e.g. 2406.12345) — the most precise fetch"
    )
    why: str


class AriadneOutput(BaseModel):
    mission_frame: str = Field(..., description="the research mission framed from the seed problem")
    directions: list[Direction] = Field(..., description="3-5 candidate directions")
    novelty_risks: list[str] = Field(default_factory=list, description="portfolio-level novelty/saturation risks")
    requests: list[AcquireRequest] = Field(default_factory=list, description="evidence Ariadne wants Mimir to acquire")
    reflection: str = Field(..., description="what is uncertain; what would change the priorities")


# ── Reflect & Steer (the continuous-loop / feedback half) ──────────────────────

ASSESSMENTS = ("advance", "reprioritize", "pivot", "retire")


class DirectionVerdict(BaseModel):
    """Reflection's verdict on ONE standing direction — referenced by its real claim id."""

    claim_id: int = Field(..., description="the id of an EXISTING standing direction (never invented)")
    assessment: str = Field(..., description="advance | reprioritize | pivot | retire")
    reason: str = Field(..., description="grounded in field-model shift, goal/lifecycle state, or evidence")
    new_priority: str | None = Field(None, description="high|medium|low — set when reprioritizing/pivoting")


class StrategicLesson(BaseModel):
    """A generalizable lesson for future deliberations — lands probationary in the lessons table."""

    lesson: str = Field(..., description="the generalizable insight (imperative, reusable)")
    rationale: str = Field("", description="why — what pattern it generalizes")
    applies_when: str | None = Field(None, description="the condition under which it applies")


class ReflectionOutput(BaseModel):
    portfolio_assessment: str = Field(..., description="the standing agenda read against the CURRENT landscape")
    verdicts: list[DirectionVerdict] = Field(
        default_factory=list, description="one per standing direction worth steering"
    )
    lessons: list[StrategicLesson] = Field(default_factory=list, description="strategic lessons to carry forward")
    reprioritized_focus: str = Field(..., description="what the lab should emphasize next round")
