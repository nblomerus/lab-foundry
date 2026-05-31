"""
Pydantic response models for the command center API.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CompanyStateOut(BaseModel):
    current_phase: str
    phase_started_at: datetime
    bootstrap_at: datetime
    days_in_phase: int
    days_since_start: int
    problem_statement: str
    stance: str | None
    success_criterion: str | None
    thesis: str | None
    niche: str | None
    audience: str | None
    charter: str | None
    paused: bool
    paused_reason: str | None
    active_claims_count: int
    invalidated_claims_count: int


class ThesisOut(BaseModel):
    id: int
    claim: str
    status: str
    confidence: float
    confidence_prev: float | None
    parent_id: int | None
    created_at: datetime
    updated_at: datetime
    invalidated_at: datetime | None  # Maps from invalidated_at column
    kill_reason: str | None  # Maps from invalidation_reason column
    finding_count: int
    supporting_count: int
    contradicting_count: int


class FindingOut(BaseModel):
    id: int
    task_id: int
    claim_id: int | None
    source: str | None
    url: str | None
    title: str | None
    summary: str
    relevance_score: float
    why_it_matters: str | None
    audit_score: float | None
    audit_verdict: str | None
    supports_thesis: bool | None
    created_at: datetime


class AgentRunOut(BaseModel):
    id: int
    department: str
    invocation_type: str
    model_tier: str
    model_name: str
    started_at: datetime
    completed_at: datetime | None
    status: str
    input_token_count: int | None
    output_token_count: int | None
    output_summary: str | None
    error: str | None
    langfuse_trace_id: str | None = None


class DissentItem(BaseModel):
    kind: str  # 'critic' | 'audit-slop'
    id: int
    claim_id: int
    detail: str  # verdict or audit_verdict
    confidence: float | None
    reasoning: str | None
    created_at: datetime


class PhaseTransitionOut(BaseModel):
    id: int
    from_phase: str
    to_phase: str
    reason: str
    forced: bool
    decided_at: datetime


class EventOut(BaseModel):
    id: int
    event_type: str
    target_type: str | None
    target_id: int | None
    payload: dict
    status: str
    suppression_reason: str | None
    emitted_at: datetime
    consumed_at: datetime | None
    consumed_by_handler: str | None


class LessonOut(BaseModel):
    id: int
    applies_to_invocation: str
    lesson_text: str
    confidence: float
    status: str
    promotion_run_count: int
    contradiction_run_count: int
    created_at: datetime


class CostTrackingOut(BaseModel):
    day: str | None  # ISO date
    reasoning_calls: int
    workhorse_calls: int
    fast_calls: int
    code_calls: int
    total_cost_usd: float
    cap_reached: bool


class OrgRoleOut(BaseModel):
    """One node in the org chart."""

    # 'pi' | 'planner' | 'researcher' | 'evaluation' | 'critic' | 'phase_adjudicator'
    # | 'reflection' | 'knowledge_scout' | 'curator'
    role: str
    running_count: int  # how many invocations currently in flight
    last_run_at: datetime | None
    runs_today: int
    avg_duration_s: float | None


class TelemetryDay(BaseModel):
    day: str  # ISO date
    label: str  # short label e.g. "Mon"
    runs: int
    findings: int
    tokens: int  # in thousands


class TaskCount(BaseModel):
    label: str  # 'pending' | 'running' | 'completed' | 'failed' | 'halted'
    value: int


class StatsOut(BaseModel):
    pending_tasks: int
    running_tasks: int
    findings_today: int
    high_signal_today: int
    slop_today: int
    failed_runs_today: int
    schema_failures_today: int
    # Per-source activity: how many running research tasks currently list each source
    source_hn_in_flight: int
    source_reddit_in_flight: int
    source_web_in_flight: int
    # Newest activity timestamp across runs/events/findings; None if the
    # company has never done anything. Drives the liveness indicator.
    last_activity_at: datetime | None = None


class EdgeActivity(BaseModel):
    """Per-event-type activity stats for the live flow page."""

    event_type: str
    count_last_minute: int
    count_today: int
    last_fired_at: datetime | None


class SnapshotOut(BaseModel):
    state: CompanyStateOut
    active_claims: list[ThesisOut]
    invalidated_claims: list[ThesisOut]
    recent_findings: list[FindingOut]
    recent_runs: list[AgentRunOut]
    dissent: list[DissentItem]
    phase_transitions: list[PhaseTransitionOut]
    org_roles: list[OrgRoleOut]
    cost: CostTrackingOut
    lesson_counts: dict[str, int]
    telemetry: list[TelemetryDay]
    task_counts: list[TaskCount]
    stats: StatsOut
    edge_activity: list[EdgeActivity] = []
    langfuse_host: str | None = None
