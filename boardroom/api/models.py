"""
Pydantic response models for the command center API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class CompanyStateOut(BaseModel):
    current_phase: str
    phase_started_at: datetime
    bootstrap_at: datetime
    deadline: datetime
    days_in_phase: int
    days_remaining: int
    problem_statement: str
    stance: Optional[str]
    success_criterion: Optional[str]
    thesis: Optional[str]
    niche: Optional[str]
    audience: Optional[str]
    charter: Optional[str]
    paused: bool
    paused_reason: Optional[str]
    active_thesis_count: int
    killed_thesis_count: int


class ThesisOut(BaseModel):
    id: int
    claim: str
    status: str
    confidence: float
    confidence_prev: Optional[float]
    parent_id: Optional[int]
    created_at: datetime
    updated_at: datetime
    killed_at: Optional[datetime]
    kill_reason: Optional[str]
    finding_count: int
    supporting_count: int
    contradicting_count: int


class FindingOut(BaseModel):
    id: int
    task_id: int
    thesis_id: Optional[int]
    source: Optional[str]
    url: Optional[str]
    title: Optional[str]
    summary: str
    relevance_score: float
    why_it_matters: Optional[str]
    audit_score: Optional[float]
    audit_verdict: Optional[str]
    supports_thesis: Optional[bool]
    created_at: datetime


class AgentRunOut(BaseModel):
    id: int
    department: str
    invocation_type: str
    model_tier: str
    model_name: str
    started_at: datetime
    completed_at: Optional[datetime]
    status: str
    input_token_count: Optional[int]
    output_token_count: Optional[int]
    output_summary: Optional[str]
    error: Optional[str]
    langfuse_trace_id: Optional[str] = None


class DissentItem(BaseModel):
    kind: str                   # 'adversary' | 'audit-slop'
    id: int
    thesis_id: int
    detail: str                 # verdict or audit_verdict
    confidence: Optional[float]
    reasoning: Optional[str]
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
    target_type: Optional[str]
    target_id: Optional[int]
    payload: dict
    status: str
    suppression_reason: Optional[str]
    emitted_at: datetime
    consumed_at: Optional[datetime]
    consumed_by_handler: Optional[str]


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
    day: Optional[str]          # ISO date
    reasoning_calls: int
    workhorse_calls: int
    fast_calls: int
    code_calls: int
    total_cost_usd: float
    cap_reached: bool


class OrgRoleOut(BaseModel):
    """One node in the org chart."""
    role: str                   # 'ceo' | 'planner' | 'researcher' | 'auditor' | 'adversary' | 'phase_adjudicator' | 'reflection'
    running_count: int          # how many invocations currently in flight
    last_run_at: Optional[datetime]
    runs_today: int
    avg_duration_s: Optional[float]


class TelemetryDay(BaseModel):
    day: str            # ISO date
    label: str          # short label e.g. "Mon"
    runs: int
    findings: int
    tokens: int         # in thousands


class TaskCount(BaseModel):
    label: str          # 'pending' | 'running' | 'completed' | 'failed' | 'halted'
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


class EdgeActivity(BaseModel):
    """Per-event-type activity stats for the live flow page."""
    event_type: str
    count_last_minute: int
    count_today: int
    last_fired_at: Optional[datetime]


class SnapshotOut(BaseModel):
    state: CompanyStateOut
    active_theses: list[ThesisOut]
    killed_theses: list[ThesisOut]
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
    langfuse_host: Optional[str] = None
