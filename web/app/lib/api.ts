import type { Snapshot, LabFoundryEvent, Finding, QueryResponse } from "./types";

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
export interface TraceJourneyStep {
  at: string | null;
  kind: string;
  label: string;
  detail: string;
  status?: string | null;
  event_id?: number | null;
  session_id?: number | null;
  payload?: Record<string, unknown> | null;
}
export interface TraceJourneySubject {
  canonical_key: string;
  source_kind: string | null;
  title: string;
  doc_id: number | null;
  trust_tier: string | null;
  trust_state: string | null;
  status: string | null;
  queryable: boolean;
  ingested_at: string | null;
  outcome: string;
  outcome_reason: string;
}
export interface TraceJourney {
  subject: TraceJourneySubject | null;
  steps: TraceJourneyStep[];
}
export interface TraceJourneyListItem {
  canonical_key: string;
  source_kind: string | null;
  title: string;
  doc_id: number | null;
  started_at: string | null;
  ended_at: string | null;
  outcome: string;
  outcome_reason: string;
}
export interface TraceJourneysResponse {
  journeys: TraceJourneyListItem[];
  facets: Record<string, number>;
  total: number;
}
export interface AriadneScores {
  novelty: number; feasibility: number; evidence_availability: number; paper_potential: number;
  reviewer_interest: number; technical_depth: number; differentiation: number;
  cost_efficiency: number; lab_alignment: number;
}
export interface AriadneDirection {
  id: number; title: string; statement: string; status: string; retired: boolean;
  invalidation_reason: string | null; composite: number | null; priority: string | null;
  rationale: string | null; scores: AriadneScores | null; n_goals: number; gate: string;
}
export interface AriadneLesson { lesson: string; status: string; when: string | null; created_at: string | null }
export interface AriadneOverview {
  mode: string;
  at_a_glance: {
    active_directions: number; retired_directions: number; claim_goals: number; lessons: number;
    top_priority: string | null; focus: string[]; status: string;
    approved: number; gate_budget: number;
    claims_total: number; acquire_requests_24h: number;
    planner_mode: string; researcher_mode: string;
    research_tasks: number; research_tasks_pending: number;
    experiments_mode: string; quartermaster_mode: string;
    experiments_running: number; experiments_total: number;
  };
  mission: { id: number; statement: string; framed_at: string } | null;
  directions: AriadneDirection[];
  lessons: AriadneLesson[];
}
export interface AcquireRequestRow {
  requester: string | null; subject: string; why: string | null; at: string | null;
  request_status: string; outcome: string; reason: string | null; document_id: number | null;
}
export interface QueueHealth {
  pending: number;                          // requests still awaiting a Mimir reply (depth)
  oldest_pending_age_seconds: number | null; // how long the oldest unanswered ask has waited (lag)
  requested_1h: number;
  resolved_1h: number;
}
export interface AriadneRequests { requests: AcquireRequestRow[]; counts: Record<string, number>; health?: QueueHealth }
export interface AriadneConversation {
  session_id: number;
  at: string | null;
  kind: "deliberation" | "reflection";
  question: string | null;      // Ariadne's multi-hop question to Mimir
  answer: string | null;        // Mimir's synthesized answer
  citations: string[];
  gaps: string[];               // thinly-covered areas Mimir flagged
  outcome: {
    label: string;              // "Framed" (deliberation) | "Steered" (reflection)
    summary: string | null;     // mission framed / reprioritized focus
    items: string[];            // direction titles / per-direction verdicts
  };
}
export interface PlannerTask {
  id: number; task_type: string; description: string; status: string;
  direction: string | null; at: string | null;
}
export interface ResearcherFinding {
  verdict: string | null;
  disposition: string | null;            // supported|contradicted|corpus_exhausted|thin_corpus|needs_experiment|inconclusive
  grounded: number | null;               // fraction of cited evidence that resolves to real papers
  summary: string | null;
  key_evidence: string[];
  kill_condition_check: string | null;
  gaps: string[];
  acquire_queries: string[];
  next_step: string | null;
  queries: string[];                     // the topical corpus queries the researcher formulated
  n_evidence: number | null;
  confidence_move: [number, number] | null;  // [from, to] if the finding moved the direction
  acquires_fired: number | null;
}
export interface ResearcherTask {
  id: number; task_type: string; status: string; description: string;
  claim_id: number | null; direction: string | null; at: string | null;
  finding: ResearcherFinding | null;
}
export interface ResearcherOverview {
  mode: string;
  tasks_total: number;
  by_status: Record<string, number>;
  by_disposition: Record<string, number>;
  acquire: { fired_24h: number; replied: number; outcomes: Record<string, number>; pending: number };
  tasks: ResearcherTask[];
}
export interface PlannerPanel {
  mode: string;
  tasks_total: number;
  by_status: Record<string, number>;
  awaiting_plan: number;        // approved directions still awaiting a plan (the planner's backlog)
  last_plan_at: string | null;
  tasks: PlannerTask[];
}
export interface FieldConcept { kind: string; name: string; total: number; recent: number; prior: number; velocity: number }
export interface AriadneFieldModel {
  windows: { recent: string | null; prior: string | null };
  counts: Record<string, number>;
  by_state: { hot: FieldConcept[]; emerging: FieldConcept[]; saturated: FieldConcept[]; declining: FieldConcept[] };
}
export interface GraphStats {
  status: "ok" | "unavailable";
  nodes?: { claims: number; findings: number; verdicts: number };
  edges?: { grounds: number; challenged: number; cited_by: number };
  error?: string;
}
export interface GraphEvidence {
  finding_id: number;
  source: string | null;
  url: string | null;
  title: string | null;
  summary: string | null;
  relevance_score: number | null;
  supports_claim: boolean | null;
  audit_verdict: string | null;
}
export interface GraphVerdict {
  verdict_id: number;
  verdict: string | null;
  confidence: number | null;
  reasoning: string | null;
  action: string | null;
  created_at: string | null;
  cited_finding_ids: number[];
}
export interface GraphClaim {
  status: "ok" | "unavailable";
  claim_id: number;
  evidence_chain?: GraphEvidence[];
  critic_verdicts?: GraphVerdict[];
  error?: string;
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

export interface KnowledgeStats {
  corpus: {
    status: string;
    documents_by_kind: Record<string, number>;
    docs_by_trust_tier: Record<string, number>;
    by_status: Record<string, number>;
    chunks: number;
    chunks_embedded: number;
    datasets: number;
    docs_today: number;
  };
  graph: {
    status: string;
    nodes?: number;
    papers?: number;
    datasets?: number;
    citations?: number;
    error?: string;
  };
  memory?: { claims: number; experiments: number };
}

export interface RecentIngest {
  id: number;
  title: string | null;
  source_kind: string;
  arxiv_id: string | null;
  source_url: string | null;
  status: string;
  at: string | null;
}
export interface RecentIngests {
  status: string;
  today: number;
  items: RecentIngest[];
  error?: string;
}

export interface MimirPanel {
  status: "ok" | "planned" | "error";
  at_a_glance: { certified: number; certified_today: number; quarantined: number; quarantined_today: number; pending: number; ingested_today: number; ingested_yesterday: number };
  trust_ladder: Record<string, number>;               // e.g. { preprint, official_repo, web_reputable, web_unknown, quarantined }
  pipeline_today: { discovered: number; parsed: number; ingested: number; quarantined: number };
  source_mix: { kind: string; count: number; pct: number }[];   // kinds: arxiv, web, github, dataset
  focus_topics?: string[];                                       // the lab's global research agenda (latest library.trends)
  recent_certifications: { title: string | null; source_kind: string; arxiv_id: string | null; canonical_key: string | null; at: string | null }[];
  requests: { requester: string; ask: string | null; status: string; at: string | null }[];
}

export interface GatePanel {
  status: "ok" | "planned" | "error";
  scope: string;
  in_corpus: number;
  today: { admitted: number; blocked_trust: number; rejected_quality: number; discovered: number };
  quarantined: number;
  turned_away: { gate: "trust" | "quality"; title: string | null; url: string | null; source_kind: string | null; reason: string; at: string | null }[];
  admitted: { title: string | null; source_kind: string; arxiv_id: string | null; canonical_key: string | null; trust_tier: string | null; at: string | null }[];
}

export interface ScoutPanel {
  status: "ok" | "planned" | "error";
  source_kind: string;
  in_corpus: number;
  added_today: number;
  last_searched: { topics: string[]; at: string | null };
  recent: { title: string | null; source_url: string | null; arxiv_id: string | null; canonical_key: string | null; status: string; snippet: string | null; at: string | null }[];
}

export interface CorpusHit {
  document_id: number;
  title: string | null;
  source_url: string | null;
  trust_tier: string;
  score: number;
  snippet: string;
}
export interface CorpusSearchResult {
  status: string;
  query: string;
  hits: CorpusHit[];
  error?: string;
}

// ---- Activity timeseries (sparklines + 24h deltas) ----
export type TimeseriesMetric = "discovered" | "parsed" | "ingested" | "certified" | "quarantined";
export interface TimeseriesPoint { t: string; value: number }
export interface TimeseriesResult {
  status: string;
  metric: string;
  kind: string | null;
  bucket: "hour" | "day";
  points: TimeseriesPoint[];
  error?: string;
}

// ---- Host / ops gauges ----
export interface HostStats {
  status: "ok" | "unavailable";
  cpu_percent?: number;
  cpu_count?: number;
  load_avg?: number[];
  memory_percent?: number;
  memory_used_gb?: number;
  memory_total_gb?: number;
  disk_percent?: number;
  disk_used_gb?: number;
  disk_total_gb?: number;
  error?: string;
}

// ---- Agent Lab ----
export interface AgentModeInput { name: string; label: string; placeholder?: string }
export interface AgentMode {
  key: string;
  label: string;
  kind: "llm" | "mimir";
  inputs: AgentModeInput[];
  action?: string;
  invocation_type?: string;
  tier?: string;
  model?: string;
  output_schema?: string | null;
  runnable?: boolean;
  emits?: string | null;
  needs_claim?: boolean;
  note?: string;
}
export interface AgentDef { id: string; label: string; role: string; status: string; what: string; modes: AgentMode[]; has_suite?: boolean }
export interface AgentCatalog { agents: AgentDef[]; claims: { id: number; claim: string }[] }
export interface SuiteCaseMeta { id: string; label: string; question: string; expect: string; gap: boolean }
export interface SuiteCaseResult extends SuiteCaseMeta {
  status: "pass" | "fail" | "gap" | "error";
  actual?: string;
  explanation?: string;
  note?: string;
}
export interface AgentRunResult {
  status: string;
  error?: string;
  kind?: "llm" | "mimir" | "collectors";
  dry_run?: boolean;
  live?: boolean;
  invocation_type?: string;
  tier?: string;
  model?: string;
  context_note?: string;
  prompt_tokens?: number;
  prompt_preview?: string;
  latency_ms?: number;
  output_tokens?: number;
  parsed?: unknown;
  raw?: string | null;
  valid?: boolean;
  validated?: unknown;
  validation_error?: string | null;
  would_emit?: string | null;
  action?: string;
  result?: Record<string, unknown>;
  note?: string;
}

export interface QmExperiment {
  id: number;
  kind: string;
  status: string;
  claim_id?: number | null;
  hypothesis?: string | null;
  requires_gpu?: boolean | null;
  gpu_mem_mb?: number | null;
  priority?: number | null;
  wall_clock_budget_s?: number | null;
  mem_budget_mb?: number | null;
  iterations?: number | null;
  kill_reason?: string | null;
  error?: string | null;
  interpretation?: string | null;
  researcher_notes?: string | null;
  ingested_doc_id?: number | null;
  at?: string | null;
}
export interface QmExperiments {
  mode: string;
  by_status: Record<string, number>;
  running: number;
  queued: number;
  experiments: QmExperiment[];
}

export const api = {
  snapshot:  () => jget<Snapshot>("/snapshot"),
  qmExperiments: (limit = 50) => jget<QmExperiments>(`/quartermaster/experiments?limit=${limit}`),
  qmKillExperiment: (id: number) => jpost<{ killed: number }>(`/quartermaster/experiments/${id}/kill`, {}),
  knowledge: () => jget<KnowledgeStats>("/knowledge/stats"),
  recentIngests: (limit = 8) => jget<RecentIngests>(`/knowledge/recent?limit=${limit}`),
  mimirPanel: () => jget<MimirPanel>("/knowledge/mimir"),
  scoutPanel: (kind: string) => jget<ScoutPanel>(`/knowledge/scout?kind=${encodeURIComponent(kind)}`),
  gatePanel: (kind?: string) => jget<GatePanel>(kind ? `/knowledge/gate?kind=${encodeURIComponent(kind)}` : "/knowledge/gate"),
  corpusSearch: (q: string, k = 6) => jget<CorpusSearchResult>(`/knowledge/search?q=${encodeURIComponent(q)}&k=${k}`),
  timeseries: (metric: TimeseriesMetric, opts: { kind?: string; bucket?: "hour" | "day"; points?: number } = {}) => {
    const q = new URLSearchParams({ metric });
    if (opts.kind) q.set("kind", opts.kind);
    if (opts.bucket) q.set("bucket", opts.bucket);
    if (opts.points) q.set("points", String(opts.points));
    return jget<TimeseriesResult>(`/knowledge/timeseries?${q.toString()}`);
  },
  hostStats: () => jget<HostStats>("/ops/host"),
  agentCatalog: () => jget<AgentCatalog>("/agentlab/agents"),
  agentRun: (body: { agent: string; mode: string; claim_id?: number | null; inputs?: Record<string, string> }) =>
    jpost<AgentRunResult>("/agentlab/run", body),
  agentSuite: (agent: string) => jget<{ agent: string; cases: SuiteCaseMeta[] }>(`/agentlab/suite?agent=${agent}`),
  agentSuiteRun: (agent: string) => jpost<{ agent: string; results: SuiteCaseResult[] }>("/agentlab/suite/run", { agent }),
  events:    (limit = 100) => jget<LabFoundryEvent[]>(`/events?limit=${limit}`),
  findings:  (thesisId: number) => jget<Finding[]>(`/claims/${thesisId}/findings`),
  query:     (body: { query: string; context_window?: number; include_sources?: boolean }) =>
    jpost<QueryResponse>("/query", body),
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
  traceSessions: (
    p: { limit?: number; handler_name?: string; status?: string; mode?: string; min_steps?: number } = {},
  ) => {
    const q = new URLSearchParams();
    if (p.limit) q.set("limit", String(p.limit));
    if (p.handler_name) q.set("handler_name", p.handler_name);
    if (p.status) q.set("status", p.status);
    if (p.mode) q.set("mode", p.mode);
    if (p.min_steps) q.set("min_steps", String(p.min_steps));
    return jget<TraceSessionsResponse>(`/trace/sessions?${q.toString()}`);
  },
  traceSession: (id: number) => jget<TraceSessionDetail>(`/trace/sessions/${id}`),
  traceJourney: (ref: string) =>
    jget<TraceJourney>(`/trace/journey/${ref.split("/").map(encodeURIComponent).join("/")}`),
  ariadneOverview: () => jget<AriadneOverview>("/ariadne/overview"),
  ariadneFieldModel: () => jget<AriadneFieldModel>("/ariadne/field-model"),
  ariadneRequests: (limit = 15) => jget<AriadneRequests>(`/ariadne/requests?limit=${limit}`),
  ariadneConversations: (limit = 12) =>
    jget<{ conversations: AriadneConversation[] }>(`/ariadne/conversations?limit=${limit}`),
  ariadnePlanner: () => jget<PlannerPanel>("/ariadne/planner"),
  researcherOverview: (limit = 30) => jget<ResearcherOverview>(`/researcher/overview?limit=${limit}`),
  ariadneGate: (claimId: number, decision: string, note?: string) =>
    jpost<{ ok: boolean; error?: string; budget_full?: boolean; decision?: string }>(
      `/ariadne/gate/${claimId}`, { decision, note }),
  traceJourneys: (p: { limit?: number; outcome?: string; kind?: string; q?: string } = {}) => {
    const qs = new URLSearchParams();
    if (p.limit) qs.set("limit", String(p.limit));
    if (p.outcome) qs.set("outcome", p.outcome);
    if (p.kind) qs.set("kind", p.kind);
    if (p.q) qs.set("q", p.q);
    const s = qs.toString();
    return jget<TraceJourneysResponse>(`/trace/journeys${s ? `?${s}` : ""}`);
  },
  graphStats: () => jget<GraphStats>("/trace/graph/stats"),
  graphClaim: (id: number) => jget<GraphClaim>(`/trace/graph/claim/${id}`),
};
