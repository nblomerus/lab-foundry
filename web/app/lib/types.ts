// Frontend mirrors of boardroom.api.models. Keep these in sync by hand;
// when they drift, regenerate from the OpenAPI schema (FastAPI exposes /docs).

export type Phase = "exploration" | "convergence" | "commitment" | "execution";

export interface CompanyState {
  current_phase: Phase;
  phase_started_at: string;
  bootstrap_at: string;
  deadline: string;
  days_in_phase: number;
  days_remaining: number;
  problem_statement: string;
  stance: string | null;
  success_criterion: string | null;
  thesis: string | null;
  niche: string | null;
  audience: string | null;
  charter: string | null;
  paused: boolean;
  paused_reason: string | null;
  active_claim_count: number;
  invalidated_claim_count: number;
}

export interface Claim {
  id: number;
  claim: string;
  status: string;
  confidence: number;
  confidence_prev: number | null;
  parent_id: number | null;
  created_at: string;
  updated_at: string;
  killed_at: string | null;
  invalidation_reason: string | null;
  finding_count: number;
  supporting_count: number;
  contradicting_count: number;
}

export interface Finding {
  id: number;
  task_id: number;
  claim_id: number | null;
  source: string | null;
  url: string | null;
  title: string | null;
  summary: string;
  relevance_score: number;
  why_it_matters: string | null;
  audit_score: number | null;
  audit_verdict: "pass" | "slop" | "unclear" | "stale" | null;
  supports_thesis: boolean | null;
  created_at: string;
}

export interface AgentRun {
  id: number;
  department: string;
  invocation_type: string;
  model_tier: string;
  model_name: string;
  started_at: string;
  completed_at: string | null;
  status: "running" | "completed" | "failed" | string;
  input_token_count: number | null;
  output_token_count: number | null;
  output_summary: string | null;
  error: string | null;
  langfuse_trace_id: string | null;
}

export interface DissentItem {
  kind: "adversary" | "audit-slop";
  id: number;
  claim_id: number;
  detail: string;
  confidence: number | null;
  reasoning: string | null;
  created_at: string;
}

export interface PhaseTransition {
  id: number;
  from_phase: Phase;
  to_phase: Phase;
  reason: string;
  forced: boolean;
  decided_at: string;
}

export interface OrgRole {
  role: string;
  running_count: number;
  last_run_at: string | null;
  runs_today: number;
  avg_duration_s: number | null;
}

export interface Cost {
  day: string | null;
  reasoning_calls: number;
  workhorse_calls: number;
  fast_calls: number;
  code_calls: number;
  total_cost_usd: number;
  cap_reached: boolean;
}

export interface TelemetryDay {
  day: string;
  label: string;
  runs: number;
  findings: number;
  tokens: number;
}

export interface TaskCount {
  label: string;
  value: number;
}

export interface Stats {
  pending_tasks: number;
  running_tasks: number;
  findings_today: number;
  high_signal_today: number;
  slop_today: number;
  failed_runs_today: number;
  schema_failures_today: number;
  source_hn_in_flight: number;
  source_reddit_in_flight: number;
  source_web_in_flight: number;
  last_activity_at: string | null;
}

export interface EdgeActivity {
  event_type: string;
  count_last_minute: number;
  count_today: number;
  last_fired_at: string | null;
}

export interface Snapshot {
  state: CompanyState;
  active_claims: Claim[];
  killed_claims: Claim[];
  recent_findings: Finding[];
  recent_runs: AgentRun[];
  dissent: DissentItem[];
  phase_transitions: PhaseTransition[];
  org_roles: OrgRole[];
  cost: Cost;
  lesson_counts: Record<string, number>;
  telemetry: TelemetryDay[];
  task_counts: TaskCount[];
  stats: Stats;
  edge_activity: EdgeActivity[];
  langfuse_host: string | null;
}

export interface BoardroomEvent {
  id: number;
  event_type: string;
  target_type: string | null;
  target_id: number | null;
  session_id?: number | null;
  payload: Record<string, unknown>;
  status: string;
  suppression_reason?: string | null;
  emitted_at: string;
  consumed_at?: string | null;
  consumed_by_handler?: string | null;
}

export type StreamMessage =
  | { type: "hello" }
  | {
      type: "event";
      event: BoardroomEvent;
      thesis?: Claim;
      task?: unknown;
      finding?: Finding;
      company_state?: CompanyState;
    };
