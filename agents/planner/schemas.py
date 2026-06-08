"""
Planner loop schemas.

Three stages:
  - StateAssessment   (assess_state)   — per-thesis evidence gaps + portfolio shape
  - PlannedTasks      (propose_tasks)  — reused from agents.planner.handler
  - CritiquedTasks    (critique)       — self-review; emit the final list

The PlannedTask shape is unchanged so the commit-to-DB transaction at the
end works identically to the legacy path. Only the *path* to that list
gets longer (assess → propose → critique) — and that's the point. A 3-step
deliberation traces in /trace as three nodes, so a bad task batch can be
diagnosed by reading the assessment + the critique alongside the final
list.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Re-export the existing PlannedTask / PlannedTasks shape so callers can
# import everything from one place without circular imports.
from agents.planner.handler import PlannedTask, PlannedTasks  # noqa: F401


class ThesisGap(BaseModel):
    """One thesis's evidence gap + suggested next angle."""

    thesis_id: int
    evidence_gap: str = Field(
        ...,
        description="One sentence — what's missing from this thesis's evidence? "
        "Be specific: 'no quantitative comparison vs alternatives' beats 'needs more research'.",
    )
    suggested_task_type: Literal["disambiguate", "falsify", "deepen", "compare"] = Field(
        ...,
        description="Best-fit task type for the gap. disambiguate for under-evidenced, "
        "falsify for high-confidence theses, deepen for promising angles, "
        "compare for convergence-phase pairs.",
    )
    priority_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="0=skip this batch · 0.5=normal · 1=highest priority work next.",
    )


class StateAssessment(BaseModel):
    """Output of assess_state: where the portfolio's gaps are right now."""

    thesis_gaps: list[ThesisGap] = Field(
        ...,
        min_length=1,
        description="One entry per active thesis. Include even thesis with priority_score=0 "
        "so the propose step sees the full portfolio.",
    )
    portfolio_notes: str = Field(
        ...,
        description="1-2 sentences on portfolio shape: where it's over-concentrated, where there are blind spots.",
    )
    target_task_count: int = Field(
        ...,
        ge=4,
        le=16,
        description="How many tasks total this batch should produce. "
        "More for under-evidenced portfolios, fewer when most theses are saturated.",
    )


class CritiquedTasks(BaseModel):
    """Final post-critique task list. The propose step's output is the input;
    critique decides which to keep, drop, or edit, and emits the final list."""

    final_tasks: list[PlannedTask] = Field(
        ...,
        max_length=16,
        description="The list that will actually be committed to the tasks table. "
        "May be a subset of the proposal, or include edits.",
    )
    changes_summary: str = Field(
        ...,
        min_length=10,
        description="2-4 sentences: what was kept, what was removed, what was edited, why.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How confident is the planner that this final list is right? "
        "Low confidence is honest signal — the swarm will run them anyway, "
        "but trace observers can spot weak batches.",
    )


# ── Direction → research tasks (Stage 2: Ariadne's approved directions) ──────


class ResearchTask(BaseModel):
    """One executable research task for the Researcher, decomposed from a direction."""

    title: str
    description: str = Field(..., description="the concrete instruction the Researcher executes")
    task_type: str = Field("analyze", description="survey|analyze|compare|reproduce|falsify|deepen")
    rationale: str = Field("", description="which goal/expectation this task advances")
    priority: str = Field("medium", description="high | medium | low")


class DirectionPlan(BaseModel):
    claim_id: int = Field(..., description="the approved direction this plan decomposes")
    tasks: list[ResearchTask] = Field(default_factory=list)


class PlanOutput(BaseModel):
    plans: list[DirectionPlan] = Field(default_factory=list)
    notes: str = Field("", description="planning notes / sequencing")
