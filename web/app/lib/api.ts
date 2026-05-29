import type { Snapshot, BoardroomEvent, Finding } from "./types";

const API_BASE = "/api";

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} on ${path}`);
  return (await r.json()) as T;
}

async function jpost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} on ${path}`);
  return (await r.json()) as T;
}

// ---- Model bench types ----
export interface BenchTask {
  invocation_type: string;
  tier: string;
  agent: string;
  output_schema: string;
  accepts_thesis: boolean;
  runnable: boolean;
}
export interface BenchModel {
  id: string;
  provider: string;
  model_name: string;
  location: "local" | "cloud";
}
export interface BenchOptions {
  tasks: BenchTask[];
  models: BenchModel[];
  claims: { id: number; claim: string }[];
}
export interface BenchResult {
  provider: string;
  model_name: string;
  status: "pending" | "ok" | "error";
  latency_ms?: number;
  output_tokens?: number;
  parsed?: Record<string, unknown> | null;
  raw?: string | null;
  error?: string;
  valid?: boolean;
  validated?: Record<string, unknown> | null;
  validation_error?: string | null;
}
export interface BenchRunSummary {
  id: number;
  created_at: string;
  invocation_type: string;
  tier: string;
  context_note: string;
  status: string;
  models: { provider: string; model_name: string; status: string; latency_ms?: number; valid?: boolean }[];
}
export interface DebugAgentRun {
  id: number;
  started_at: string | null;
  latency_ms: number | null;
  agent: string;
  invocation_type: string;
  tier: string;
  model_name: string;
  status: string;
  error: string | null;
  output_summary: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  task_id: number | null;
}
export interface DebugResponse {
  runs: DebugAgentRun[];
  facets: { statuses: Record<string, number>; invocation_types: string[] };
}
export interface DebugCosts {
  deepseek: {
    today_cost_usd: number;
    spent: { tracked_since: string | null; spent_tracked_usd: number | null; spent_today_usd: number | null };
    days: { day: string; calls: number; input_tokens: number; output_tokens: number; cost_usd: number }[];
    balance: { total: number; topped_up: number; granted: number; currency: string; available: boolean } | null;
    pricing: { input_per_1m: number; output_per_1m: number };
  };
  power: {
    gpus: { index: number; name: string; watts: number; util: number }[];
    total_watts: number;
    rate_usd_per_kwh: number;
    projected_usd_per_day: number;
  };
}
// ---- Research tree (per-task Debug view) ----
export interface ResearchSubQuestion {
  q: string;
  sources: string[];
  why: string;
  k?: number;
}
export interface ResearchProposedExperiment {
  kind: string;
  params: Record<string, unknown>;
  why: string;
}
export interface ResearchInquiry {
  id: number;
  task_id: number;
  iteration: number;
  question: string;
  sub_questions: ResearchSubQuestion[];
  proposed_experiments: ResearchProposedExperiment[];
  plan_run_id: number | null;
  created_at: string;
}
export interface ResearchEvidence {
  id: number;
  task_id: number;
  inquiry_id: number | null;
  sub_question_idx: number;
  url: string;
  title: string | null;
  quote: string;
  claim: string;
  stance: "supports" | "refutes" | "neutral";
  confidence: number;
  extract_run_id: number | null;
  created_at: string;
}
export interface ResearchExperiment {
  id: number;
  task_id: number;
  inquiry_id: number | null;
  kind: string;
  params: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  status: "pending" | "running" | "completed" | "failed";
  interpretation: string | null;
  interpret_run_id: number | null;
  started_at: string;
  completed_at: string | null;
}
export interface ResearchFinding {
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
  audit_verdict: string | null;
  supports_thesis: boolean | null;
  created_at: string;
}
export interface ResearchTreeRun {
  id: number;
  invocation_type: string;
  model_tier: string;
  model_name: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  input_summary: string | null;   // full prompt body sent to the model
  output_summary: string | null;  // full raw JSON the model returned
  input_token_count: number | null;
  output_token_count: number | null;
  error: string | null;
}
export interface ResearchTree {
  task: { id: number; description: string; status: string; claim_id: number | null; payload: Record<string, unknown>; created_at: string } | null;
  inquiries: ResearchInquiry[];
  evidence: ResearchEvidence[];
  experiments: ResearchExperiment[];
  findings: ResearchFinding[];
  agent_runs: ResearchTreeRun[];
}

// ---- Replay types ----
export interface ReplayOriginal {
  run_id: number;
  step_name: string | null;
  invocation_type: string;
  model_name: string;
  tier: string;
  status: string;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  input_summary: string | null;
  output_summary: string | null;
  parsed: Record<string, unknown> | null;
  error: string | null;
}
export interface ReplayStepResponse {
  error?: string;
  status?: "ok" | "error";
  latency_ms?: number;
  output_tokens?: number;
  provider?: string;
  model_name?: string;
  raw?: string | null;
  parsed?: Record<string, unknown> | null;
  valid?: boolean;
  validated?: Record<string, unknown> | null;
  validation_error?: string | null;
  original?: ReplayOriginal;
  frozen?: boolean;
}

// ---- Trace types ----
export interface TraceSessionSummary {
  id: number;
  handler_name: string;
  status: "running" | "completed" | "failed";
  mode: "live" | "replay";
  started_at: string | null;
  completed_at: string | null;
  latency_ms: number | null;
  error: string | null;
  trigger_event_id: number | null;
  trigger_event_type: string | null;
  trigger_target_type: string | null;
  trigger_target_id: number | null;
  step_count: number;
  failed_steps: number;
  input_tokens: number;
  output_tokens: number;
}
export interface TraceSessionsResponse {
  sessions: TraceSessionSummary[];
  facets: {
    handlers: Record<string, number>;
    statuses: Record<string, number>;
  };
}
export interface TraceFallbackAttempt {
  provider: string;
  model: string;
  error: string;
  latency_ms: number;
}
export interface TraceRun {
  id: number;
  invocation_type: string;
  model_tier: string;
  model_name: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  input_summary: string | null;
  output_summary: string | null;
  error: string | null;
  step_name: string | null;
  parent_step_id: number | null;
  step_order: number | null;
  fallback_attempts: TraceFallbackAttempt[];
  langfuse_trace_id: string | null;
}
export interface TraceSessionDetail {
  session: (TraceSessionSummary & { trigger_payload: Record<string, unknown> | null }) | null;
  runs: TraceRun[];
}

export interface BenchRunResponse {
  error?: string;
  job_id?: string;
  status?: "running" | "done" | "gone";
  invocation_type?: string;
  tier?: string;
  output_schema?: string;
  context_note?: string;
  prompt_tokens?: number;
  prompt_preview?: string;
  results?: BenchResult[];
}
export interface BenchJobResponse {
  error?: string;
  status: "running" | "done" | "gone";
  results?: BenchResult[];
}

export const api = {
  snapshot:  () => jget<Snapshot>("/snapshot"),
  events:    (limit = 100) => jget<BoardroomEvent[]>(`/events?limit=${limit}`),
  findings:  (thesisId: number) => jget<Finding[]>(`/claims/${thesisId}/findings`),
  benchOptions: () => jget<BenchOptions>("/bench/options"),
  benchRun: (body: { invocation_type: string; models: { provider: string; model_name: string }[]; claim_id?: number }) =>
    jpost<BenchRunResponse>("/bench/run", body),
  benchJob: (jobId: string) => jget<BenchJobResponse>(`/bench/jobs/${jobId}`),
  benchRuns: (limit = 30) => jget<{ runs: BenchRunSummary[] }>(`/bench/runs?limit=${limit}`),
  benchRunDetail: (id: number) => jget<BenchRunResponse>(`/bench/runs/${id}`),
  debugAgentRuns: (p: { limit?: number; status?: string; invocation_type?: string } = {}) => {
    const q = new URLSearchParams();
    if (p.limit) q.set("limit", String(p.limit));
    if (p.status) q.set("status", p.status);
    if (p.invocation_type) q.set("invocation_type", p.invocation_type);
    return jget<DebugResponse>(`/debug/agent-runs?${q.toString()}`);
  },
  debugCosts: () => jget<DebugCosts>("/debug/costs"),
  debugResearchTree: (taskId: number) => jget<ResearchTree>(`/debug/research-tree/${taskId}`),
  replayStep: (body: { run_id: number; model: { provider: string; model_name: string }; prompt_override?: string }) =>
    jpost<ReplayStepResponse>("/bench/replay-step", body),
  traceSessions: (p: { limit?: number; handler_name?: string; status?: string; mode?: string } = {}) => {
    const q = new URLSearchParams();
    if (p.limit) q.set("limit", String(p.limit));
    if (p.handler_name) q.set("handler_name", p.handler_name);
    if (p.status) q.set("status", p.status);
    if (p.mode) q.set("mode", p.mode);
    return jget<TraceSessionsResponse>(`/trace/sessions?${q.toString()}`);
  },
  traceSession: (id: number) => jget<TraceSessionDetail>(`/trace/sessions/${id}`),
};
