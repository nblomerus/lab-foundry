"use client";

import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  ReactFlow, ReactFlowProvider, Background, Controls,
  type Node, type Edge, type NodeProps,
  Handle, Position,
} from "@xyflow/react";
import dagre from "dagre";
import {
  ArrowLeft, ExternalLink, Loader2, CheckCircle2, AlertTriangle,
  Network, X, Play, Pencil,
} from "lucide-react";
import {
  api, type TraceSessionDetail, type TraceRun,
  type TraceFallbackAttempt, type BenchOptions,
  type ReplayStepResponse,
} from "../../lib/api";
import { useEventStream } from "../../lib/ws";
import { Badge, Card, SectionTitle, cx } from "../../components/ui";

import "@xyflow/react/dist/style.css";

const NODE_W = 240;
const NODE_H = 96;

const STATUS_BG: Record<string, string> = {
  running: "bg-amber-50 border-amber-300",
  completed: "bg-emerald-50 border-emerald-300",
  failed: "bg-red-50 border-red-300",
};

function fmtMs(ms: number | null) {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

// -------------------------------------------------------------------------
// Custom step node
// -------------------------------------------------------------------------

type StepNodeData = {
  run: TraceRun;
  onClick: () => void;
};

function StepNode({ data }: NodeProps<Node<StepNodeData>>) {
  const r = data.run;
  const StatusIcon =
    r.status === "running" ? Loader2 :
    r.status === "completed" ? CheckCircle2 :
    r.status === "failed" ? AlertTriangle :
    Network;
  return (
    <div
      onClick={data.onClick}
      className={cx(
        "cursor-pointer rounded-2xl border-2 px-3 py-2 shadow-sm transition hover:shadow-md",
        STATUS_BG[r.status] ?? "bg-slate-50 border-slate-300",
      )}
      style={{ width: NODE_W, height: NODE_H }}
    >
      <Handle type="target" position={Position.Left} className="!bg-slate-400" />
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-semibold text-slate-900" title={r.step_name ?? r.invocation_type}>
            {r.step_name ?? r.invocation_type}
          </div>
          <div className="truncate text-[10px] text-slate-500" title={r.model_name}>
            {r.model_name}
          </div>
        </div>
        <StatusIcon
          className={cx(
            "h-4 w-4 shrink-0",
            r.status === "running" && "animate-spin text-amber-600",
            r.status === "completed" && "text-emerald-600",
            r.status === "failed" && "text-red-600",
          )}
        />
      </div>
      <div className="mt-2 flex items-center justify-between text-[10px] text-slate-600">
        <span>
          {r.input_tokens ?? 0} → {r.output_tokens ?? 0} tok
        </span>
        <span>{fmtMs(r.latency_ms)}</span>
      </div>
      {r.fallback_attempts.length > 0 && (
        <div className="mt-1 text-[10px] text-amber-700">
          ↺ {r.fallback_attempts.length} fallback{r.fallback_attempts.length > 1 ? "s" : ""}
        </div>
      )}
      <Handle type="source" position={Position.Right} className="!bg-slate-400" />
    </div>
  );
}

const NODE_TYPES = { step: StepNode };

// -------------------------------------------------------------------------
// Layout with dagre
// -------------------------------------------------------------------------

function layoutWithDagre(runs: TraceRun[], onClick: (r: TraceRun) => void): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 24, ranksep: 60, marginx: 16, marginy: 16 });
  g.setDefaultEdgeLabel(() => ({}));

  for (const r of runs) {
    g.setNode(String(r.id), { width: NODE_W, height: NODE_H });
  }
  const edges: Edge[] = [];
  for (const r of runs) {
    if (r.parent_step_id != null) {
      g.setEdge(String(r.parent_step_id), String(r.id));
      edges.push({
        id: `e-${r.parent_step_id}-${r.id}`,
        source: String(r.parent_step_id),
        target: String(r.id),
        animated: r.status === "running",
        style: { stroke: r.status === "failed" ? "#dc2626" : "#94a3b8", strokeWidth: 1.5 },
      });
    }
  }
  dagre.layout(g);

  const nodes: Node[] = runs.map((r) => {
    const n = g.node(String(r.id));
    return {
      id: String(r.id),
      type: "step",
      position: { x: n.x - NODE_W / 2, y: n.y - NODE_H / 2 },
      data: { run: r, onClick: () => onClick(r) } as StepNodeData,
    };
  });
  return { nodes, edges };
}

// -------------------------------------------------------------------------
// Detail panel
// -------------------------------------------------------------------------

function DetailPanel({
  run, langfuseHost, models, onClose,
}: {
  run: TraceRun;
  langfuseHost: string | null;
  models: BenchOptions["models"];
  onClose: () => void;
}) {
  return (
    <div className="fixed right-0 top-0 z-30 h-full w-full max-w-xl overflow-y-auto border-l border-slate-200 bg-white shadow-xl">
      <div className="sticky top-0 z-10 flex items-center justify-between border-b border-slate-200 bg-white/95 px-5 py-3 backdrop-blur">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-slate-950" title={run.step_name ?? run.invocation_type}>
            {run.step_name ?? run.invocation_type}
          </div>
          <div className="text-xs text-slate-500">
            run #{run.id} · {run.model_tier} · {run.model_name}
          </div>
        </div>
        <button onClick={onClose} className="rounded-xl p-2 text-slate-500 hover:bg-slate-100 hover:text-slate-900">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="space-y-4 px-5 py-4 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            tone={
              run.status === "running" ? "amber" :
              run.status === "completed" ? "green" :
              run.status === "failed" ? "red" : "default"
            }
          >
            {run.status}
          </Badge>
          <span className="text-xs text-slate-500">
            {run.input_tokens ?? 0} → {run.output_tokens ?? 0} tok · {fmtMs(run.latency_ms)}
          </span>
          {langfuseHost && run.langfuse_trace_id && (
            <a
              href={`${langfuseHost}/trace/${run.langfuse_trace_id}`}
              target="_blank" rel="noopener"
              className="ml-auto inline-flex items-center gap-1 text-xs text-blue-700 hover:underline"
            >
              Langfuse <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>

        {run.error && (
          <Card className="border-red-200 bg-red-50 p-3 text-xs text-red-900">
            <div className="mb-1 font-semibold">Error</div>
            <pre className="whitespace-pre-wrap font-mono">{run.error}</pre>
          </Card>
        )}

        {run.fallback_attempts.length > 0 && (
          <div>
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Fallback chain ({run.fallback_attempts.length})
            </div>
            <ul className="space-y-1">
              {run.fallback_attempts.map((a: TraceFallbackAttempt, i) => (
                <li key={i} className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs">
                  <div className="font-mono">
                    {a.provider} · {a.model} <span className="text-slate-500">({a.latency_ms}ms)</span>
                  </div>
                  <div className="mt-0.5 text-amber-900">{a.error}</div>
                </li>
              ))}
            </ul>
          </div>
        )}

        <ReplaySection run={run} models={models} />

        <Section title="Output (raw JSON)">
          <pre className="max-h-80 overflow-auto rounded-xl border border-slate-200 bg-slate-50 p-3 font-mono text-xs">
            {run.output_summary ?? "(none)"}
          </pre>
        </Section>

        <Section title="Input (system prompt)">
          <pre className="max-h-80 overflow-auto rounded-xl border border-slate-200 bg-slate-50 p-3 font-mono text-xs">
            {run.input_summary ?? "(none)"}
          </pre>
        </Section>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</div>
      {children}
    </div>
  );
}

// -------------------------------------------------------------------------
// Replay — frozen prompt + model swap
// -------------------------------------------------------------------------

function ReplaySection({
  run, models,
}: {
  run: TraceRun;
  models: BenchOptions["models"];
}) {
  // Default the target model to the one the original run actually used —
  // first match by model_name wins. Lets the user click Replay immediately
  // for a "is this flaky?" deterministic-rerun test without picking.
  const defaultModelId = useMemo(() => {
    const match = models.find((m) => m.model_name === run.model_name);
    return match?.id ?? models[0]?.id ?? "";
  }, [models, run.model_name]);

  const [open, setOpen] = useState(false);
  const [modelId, setModelId] = useState(defaultModelId);
  const [editPrompt, setEditPrompt] = useState(false);
  const [promptDraft, setPromptDraft] = useState(run.input_summary ?? "");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ReplayStepResponse | null>(null);

  // Re-sync defaults when switching to a different step in the DAG.
  useEffect(() => {
    setModelId(defaultModelId);
    setPromptDraft(run.input_summary ?? "");
    setResult(null);
    setEditPrompt(false);
  }, [run.id, defaultModelId, run.input_summary]);

  const selected = models.find((m) => m.id === modelId);

  async function fire() {
    if (!selected) return;
    setRunning(true);
    setResult(null);
    try {
      const r = await api.replayStep({
        run_id: run.id,
        model: { provider: selected.provider, model_name: selected.model_name },
        prompt_override: editPrompt ? promptDraft : undefined,
      });
      setResult(r);
    } catch (e) {
      setResult({ status: "error", error: String(e) });
    } finally {
      setRunning(false);
    }
  }

  if (!run.input_summary) {
    // Legacy rows pre-Phase-1 have no input_summary; replay can't reconstruct
    // the frozen prompt. Surface it instead of failing silently on click.
    return null;
  }

  return (
    <Card className="border-blue-200 bg-blue-50/40 p-3 text-xs">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between text-left"
      >
        <span className="font-semibold uppercase tracking-wide text-blue-900">
          Replay this step
        </span>
        <span className="text-blue-700">{open ? "−" : "+"}</span>
      </button>

      {open && (
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-slate-600">Model</label>
            <select
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
              className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs"
            >
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.provider} · {m.model_name} ({m.location})
                </option>
              ))}
            </select>
            <button
              onClick={() => setEditPrompt((v) => !v)}
              className={cx(
                "inline-flex items-center gap-1 rounded-lg border px-2 py-1",
                editPrompt
                  ? "border-blue-300 bg-blue-100 text-blue-900"
                  : "border-slate-200 bg-white text-slate-600",
              )}
              title="Hand-edit the system prompt before re-running"
            >
              <Pencil className="h-3 w-3" /> {editPrompt ? "Editing" : "Edit prompt"}
            </button>
            <button
              onClick={fire}
              disabled={running || !selected}
              className="ml-auto inline-flex items-center gap-1 rounded-lg bg-blue-700 px-3 py-1 font-medium text-white hover:bg-blue-800 disabled:opacity-50"
            >
              {running ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}
              Replay
            </button>
          </div>

          {editPrompt && (
            <textarea
              value={promptDraft}
              onChange={(e) => setPromptDraft(e.target.value)}
              rows={10}
              className="w-full rounded-lg border border-blue-200 bg-white p-2 font-mono text-[11px]"
            />
          )}

          {result && <ReplayResult result={result} originalParsed={run.output_summary} />}
        </div>
      )}
    </Card>
  );
}

function ReplayResult({
  result, originalParsed,
}: {
  result: ReplayStepResponse;
  originalParsed: string | null;
}) {
  if (result.error && !result.status) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-2 text-red-900">
        {result.error}
      </div>
    );
  }
  const ok = result.status === "ok";
  const newOutput =
    result.parsed != null
      ? JSON.stringify(result.parsed, null, 2)
      : result.raw ?? "(no output)";
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-slate-600">
        <Badge tone={ok ? "green" : "red"}>{result.status}</Badge>
        <span>
          {result.provider} · {result.model_name}
        </span>
        <span>
          {result.output_tokens ?? 0} out tok · {fmtMs(result.latency_ms ?? null)}
        </span>
        {result.valid === true && <Badge tone="green">schema OK</Badge>}
        {result.valid === false && <Badge tone="amber">schema invalid</Badge>}
        {result.frozen ? <Badge tone="default">frozen</Badge> : <Badge tone="blue">edited</Badge>}
      </div>
      {result.error && (
        <pre className="whitespace-pre-wrap rounded-lg border border-red-200 bg-red-50 p-2 font-mono text-[11px] text-red-900">
          {result.error}
        </pre>
      )}
      {result.validation_error && (
        <pre className="whitespace-pre-wrap rounded-lg border border-amber-200 bg-amber-50 p-2 font-mono text-[11px] text-amber-900">
          {result.validation_error}
        </pre>
      )}
      <div className="grid grid-cols-2 gap-2">
        <div>
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
            Original
          </div>
          <pre className="max-h-72 overflow-auto rounded-lg border border-slate-200 bg-slate-50 p-2 font-mono text-[11px]">
            {originalParsed ?? "(none)"}
          </pre>
        </div>
        <div>
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-blue-700">
            Replay
          </div>
          <pre className="max-h-72 overflow-auto rounded-lg border border-blue-200 bg-blue-50 p-2 font-mono text-[11px]">
            {newOutput}
          </pre>
        </div>
      </div>
    </div>
  );
}

// -------------------------------------------------------------------------
// Page
// -------------------------------------------------------------------------

export default function TraceDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const sessionId = Number(id);

  const [data, setData] = useState<TraceSessionDetail | null>(null);
  const [selected, setSelected] = useState<TraceRun | null>(null);
  const [langfuseHost, setLangfuseHost] = useState<string | null>(null);
  const [models, setModels] = useState<BenchOptions["models"]>([]);
  const { latest } = useEventStream(50);
  // Refetch debouncer: lots of step.* events can fire in a burst (per-page
  // extracts for a research loop) — coalesce into one refetch per 500ms.
  const refetchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(() => {
    api.traceSession(sessionId).then(setData).catch(() => {});
  }, [sessionId]);

  useEffect(() => {
    load();
    api.snapshot().then((s) => setLangfuseHost(s.langfuse_host ?? null)).catch(() => {});
    // Bench options provides the canonical list of provider+model pairs the
    // running router can reach. Reused for the replay model picker so we
    // don't have to maintain a second list.
    api.benchOptions().then((o) => setModels(o.models)).catch(() => {});
  }, [load]);

  // Live updates: filter the WS stream to this session and refetch on any
  // step.* / session.* event tagged with our session_id. Cheaper than parsing
  // payloads and patching node state in place — backend join + dagre relayout
  // is sub-10ms for a 50-node session.
  useEffect(() => {
    if (latest?.type !== "event") return;
    const e = latest.event;
    if (e.session_id !== sessionId) return;
    if (!e.event_type.startsWith("step.") && !e.event_type.startsWith("session.")) return;
    if (refetchTimer.current) clearTimeout(refetchTimer.current);
    refetchTimer.current = setTimeout(load, 500);
  }, [latest, sessionId, load]);

  useEffect(() => () => { if (refetchTimer.current) clearTimeout(refetchTimer.current); }, []);

  const { nodes, edges } = useMemo(() => {
    if (!data?.runs?.length) return { nodes: [], edges: [] };
    return layoutWithDagre(data.runs, setSelected);
  }, [data]);

  if (!data) {
    return (
      <div className="text-sm text-slate-500">Loading session #{sessionId}…</div>
    );
  }
  if (!data.session) {
    return (
      <div>
        <div className="text-sm text-slate-500">Session #{sessionId} not found.</div>
        <Link href="/trace" className="mt-4 inline-flex items-center gap-2 text-sm text-blue-700 hover:underline">
          <ArrowLeft className="h-4 w-4" /> Back to sessions
        </Link>
      </div>
    );
  }

  const s = data.session;
  const tone =
    s.status === "running" ? "amber" :
    s.status === "completed" ? "green" :
    s.status === "failed" ? "red" : "default";

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-6">
        <div>
          <Link href="/trace" className="mb-2 inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-950">
            <ArrowLeft className="h-3 w-3" /> All sessions
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-950">
            {s.handler_name} <span className="font-mono text-slate-400">#{s.id}</span>
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            {s.trigger_event_type ? `Triggered by ${s.trigger_event_type}` : "No trigger event"}
            {s.trigger_target_type && s.trigger_target_id != null && (
              <> · {s.trigger_target_type}#{s.trigger_target_id}</>
            )}
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <Badge tone={tone as "amber" | "green" | "red" | "default"}>{s.status}</Badge>
          {s.mode === "replay" && <Badge tone="blue">replay</Badge>}
          <span className="text-xs text-slate-500">
            {data.runs.length} step{data.runs.length === 1 ? "" : "s"} · {fmtMs(s.latency_ms)}
          </span>
        </div>
      </div>

      {s.error && (
        <Card className="border-red-200 bg-red-50">
          <div className="text-xs font-semibold uppercase tracking-wide text-red-700">Session error</div>
          <pre className="mt-1 whitespace-pre-wrap font-mono text-xs text-red-900">{s.error}</pre>
        </Card>
      )}

      <Card className="p-0">
        <SectionTitle
          icon={Network}
          title="Step DAG"
          subtitle="Click any node for full input / output / fallback chain. Live updates on step.*"
        />
        <div className="h-[640px] w-full border-t border-slate-200" style={{ minHeight: 640 }}>
          {nodes.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center text-sm text-slate-400">
              {s.status === "running" ? (
                <span>Running — waiting for the first model call…</span>
              ) : (
                <>
                  <span className="font-medium text-slate-600">No model calls in this session.</span>
                  <span className="max-w-md">
                    <span className="font-mono">{s.handler_name}</span> ran deterministically — no LLM step
                    (e.g. Mimir&rsquo;s rule-based trust gate).
                    {s.trigger_event_type ? <> Triggered by <span className="font-mono">{s.trigger_event_type}</span>.</> : null}
                  </span>
                  <span className="text-xs text-slate-400">
                    Tip: the sessions list has a &ldquo;with model steps only&rdquo; filter to hide these.
                  </span>
                </>
              )}
            </div>
          ) : (
            <ReactFlowProvider>
              <ReactFlow
                nodes={nodes}
                edges={edges}
                nodeTypes={NODE_TYPES}
                fitView
                fitViewOptions={{ padding: 0.2 }}
                proOptions={{ hideAttribution: true }}
                nodesDraggable={false}
                nodesConnectable={false}
              >
                <Background gap={20} size={1} color="#e2e8f0" />
                <Controls showInteractive={false} />
              </ReactFlow>
            </ReactFlowProvider>
          )}
        </div>
      </Card>

      {selected && (
        <DetailPanel
          run={selected}
          langfuseHost={langfuseHost}
          models={models}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}
