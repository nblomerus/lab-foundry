"use client";

import { useEffect, useMemo, useState, useCallback } from "react";
import { FlaskConical, Play, ChevronDown, ChevronRight, Cpu, Cloud, Loader2, History } from "lucide-react";
import { api, type BenchOptions, type BenchModel, type BenchRunResponse, type BenchRunSummary } from "../lib/api";
import { Badge, Card, SectionTitle, cx } from "../components/ui";

const TIER_TONE: Record<string, "blue" | "amber" | "green" | "red" | "default"> = {
  reasoning: "red", workhorse: "amber", fast: "blue", code: "green",
};

function fmtMs(ms: number) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

export default function BenchPage() {
  const [opts, setOpts] = useState<BenchOptions | null>(null);
  const [task, setTask] = useState<string>("");
  const [thesisId, setThesisId] = useState<number | undefined>(undefined);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [running, setRunning] = useState(false);
  const [res, setRes] = useState<BenchRunResponse | null>(null);
  const [showPrompt, setShowPrompt] = useState(false);
  const [history, setHistory] = useState<BenchRunSummary[]>([]);

  const loadHistory = useCallback(() => {
    api.benchRuns(20).then((d) => setHistory(d.runs)).catch(() => {});
  }, []);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  useEffect(() => {
    api.benchOptions().then((o) => {
      setOpts(o);
      const firstRunnable = o.tasks.find((t) => t.runnable);
      if (firstRunnable) setTask(firstRunnable.invocation_type);
      // default pick: one capable local + one cloud, if present
      const def = new Set<string>();
      const local = o.models.find((m) => m.location === "local");
      const cloud = o.models.find((m) => m.location === "cloud");
      if (local) def.add(local.id);
      if (cloud) def.add(cloud.id);
      setPicked(def);
    }).catch(() => {});
  }, []);

  const currentTask = useMemo(
    () => opts?.tasks.find((t) => t.invocation_type === task),
    [opts, task],
  );

  function toggle(id: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  async function run() {
    if (!opts || !task || picked.size === 0) return;
    setRunning(true);
    setRes(null);
    const models = opts.models.filter((m) => picked.has(m.id))
      .map((m) => ({ provider: m.provider, model_name: m.model_name }));
    try {
      const start = await api.benchRun({
        invocation_type: task,
        models,
        claim_id: currentTask?.accepts_thesis ? thesisId : undefined,
      });
      setRes(start);
      if (start.error || !start.job_id) {
        setRunning(false);
        return;
      }
      const jobId = start.job_id;
      // Poll until every model has landed. Each request is short, so the
      // dev proxy is happy and results stream in as they finish.
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise((r) => setTimeout(r, 1500));
        const j = await api.benchJob(jobId);
        setRes((prev) => (prev ? { ...prev, results: j.results ?? prev.results } : prev));
        if (j.status !== "running") break;
      }
    } catch (e) {
      setRes({ error: String(e) });
    } finally {
      setRunning(false);
      loadHistory();
    }
  }

  async function loadSaved(id: number) {
    setRunning(false);
    try {
      const detail = await api.benchRunDetail(id);
      setRes(detail);
      if (detail.invocation_type) setTask(detail.invocation_type);
    } catch (e) {
      setRes({ error: String(e) });
    }
  }

  if (!opts) return <div className="text-sm text-slate-500">Loading…</div>;

  const local = opts.models.filter((m) => m.location === "local");
  const cloud = opts.models.filter((m) => m.location === "cloud");

  return (
    <div className="space-y-6">
      <Card>
        <SectionTitle
          icon={FlaskConical}
          title="Model Bench"
          subtitle="Run any agent's real template task across models, side by side. Read-only — it builds the exact prompt the harness would and does not touch the running organisation or its budget."
        />
      </Card>

      {/* Controls */}
      <Card>
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_auto]">
          <div className="space-y-4">
            {/* Task picker */}
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-slate-500">
                Template task
              </label>
              <select
                value={task}
                onChange={(e) => setTask(e.target.value)}
                className="w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm shadow-sm focus:border-slate-400 focus:outline-none"
              >
                {opts.tasks.map((t) => (
                  <option key={t.invocation_type} value={t.invocation_type} disabled={!t.runnable}>
                    {t.invocation_type} [{t.tier}]{t.runnable ? "" : " — not wired"}
                  </option>
                ))}
              </select>
              {currentTask && (
                <div className="mt-2 flex items-center gap-2 text-xs text-slate-500">
                  <Badge tone={TIER_TONE[currentTask.tier] ?? "default"}>{currentTask.tier}</Badge>
                  <span>→ {currentTask.output_schema}</span>
                </div>
              )}
            </div>

            {/* Thesis picker (only for thesis-scoped tasks) */}
            {currentTask?.accepts_thesis && (
              <div>
                <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-slate-500">
                  Context thesis
                </label>
                <select
                  value={thesisId ?? ""}
                  onChange={(e) => setThesisId(e.target.value ? Number(e.target.value) : undefined)}
                  className="w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm shadow-sm focus:border-slate-400 focus:outline-none"
                >
                  <option value="">(auto — first active thesis)</option>
                  {opts.claims.map((t) => (
                    <option key={t.id} value={t.id}>T{t.id}: {t.claim.slice(0, 70)}</option>
                  ))}
                </select>
              </div>
            )}

            {/* Model multi-select */}
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-slate-500">
                Models to compare ({picked.size})
              </label>
              <div className="space-y-2">
                <ModelGroup icon={Cpu} label="Local (Ollama)" models={local} picked={picked} onToggle={toggle} />
                <ModelGroup icon={Cloud} label="Cloud" models={cloud} picked={picked} onToggle={toggle} />
              </div>
            </div>
          </div>

          <div className="flex items-start">
            <button
              onClick={run}
              disabled={running || picked.size === 0 || !currentTask?.runnable}
              className={cx(
                "inline-flex items-center gap-2 rounded-2xl px-5 py-2.5 text-sm font-semibold shadow-sm transition",
                running || picked.size === 0 || !currentTask?.runnable
                  ? "cursor-not-allowed bg-slate-100 text-slate-400"
                  : "bg-slate-950 text-white hover:bg-slate-800",
              )}
            >
              <Play className="h-4 w-4" />
              {running ? "Running…" : "Run comparison"}
            </button>
          </div>
        </div>
      </Card>

      {/* Results */}
      {res?.error && (
        <Card className="border-red-200 bg-red-50/60">
          <div className="text-sm text-red-700">⚠ {res.error}</div>
        </Card>
      )}

      {res && !res.error && (
        <>
          <Card>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
              <span><span className="font-semibold text-slate-700">{res.invocation_type}</span></span>
              <span>· context: {res.context_note}</span>
              <span>· prompt: {res.prompt_tokens} tokens</span>
              <button
                onClick={() => setShowPrompt((s) => !s)}
                className="inline-flex items-center gap-1 text-slate-600 hover:text-slate-900"
              >
                {showPrompt ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                {showPrompt ? "hide" : "show"} prompt
              </button>
            </div>
            {showPrompt && (
              <pre className="mt-3 max-h-96 overflow-auto rounded-2xl bg-slate-950 p-4 text-xs leading-relaxed text-slate-100">
                {res.prompt_preview}
              </pre>
            )}
          </Card>

          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {res.results?.map((r) => (
              <Card
                key={`${r.provider}:${r.model_name}`}
                className={cx(r.status === "error" ? "border-red-200" : "", r.status === "pending" ? "opacity-90" : "")}
              >
                <div className="mb-3 flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-slate-900" title={r.model_name}>
                      {r.model_name}
                    </div>
                    <div className="mt-0.5 flex items-center gap-1.5">
                      <Badge tone={r.provider === "ollama" ? "blue" : "green"}>{r.provider}</Badge>
                      {r.status === "ok" && (
                        <span title={r.validation_error || undefined}>
                          <Badge tone={r.valid ? "green" : "red"}>{r.valid ? "schema ✓" : "schema ✗"}</Badge>
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="text-right text-xs text-slate-500">
                    {r.status === "pending" ? (
                      <span className="inline-flex items-center gap-1 text-slate-400">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" /> running
                      </span>
                    ) : (
                      <>
                        <div className={cx("font-semibold", (r.latency_ms ?? 0) > 30000 ? "text-amber-600" : "text-slate-700")}>
                          {fmtMs(r.latency_ms ?? 0)}
                        </div>
                        {r.output_tokens != null && <div>{r.output_tokens} tok</div>}
                      </>
                    )}
                  </div>
                </div>
                {r.status === "pending" && <div className="h-40 rounded-2xl bg-slate-100 animate-pulse" />}
                {r.status === "ok" && (
                  <pre className="max-h-[28rem] overflow-auto rounded-2xl bg-slate-50 p-3 text-xs leading-relaxed text-slate-800">
                    {r.parsed ? JSON.stringify(r.parsed, null, 2) : (r.raw || "(empty)")}
                  </pre>
                )}
                {r.status === "error" && (
                  <div className="rounded-2xl bg-red-50 p-3 text-xs text-red-700">{r.error}</div>
                )}
              </Card>
            ))}
          </div>
        </>
      )}

      {/* Saved runs — persisted, retrievable */}
      {history.length > 0 && (
        <Card>
          <SectionTitle
            icon={History}
            title={`Saved comparisons (${history.length})`}
            subtitle="Every run is persisted. Click one to reload its results."
          />
          <div className="space-y-1.5">
            {history.map((h) => (
              <button
                key={h.id}
                onClick={() => loadSaved(h.id)}
                className="flex w-full items-center gap-3 rounded-2xl border border-slate-100 px-3 py-2 text-left text-xs transition hover:border-slate-300 hover:bg-slate-50"
              >
                <span className="font-mono text-slate-400">#{h.id}</span>
                <span className="font-medium text-slate-700">{h.invocation_type}</span>
                <span className="truncate text-slate-400">{h.context_note}</span>
                <span className="ml-auto flex shrink-0 items-center gap-1">
                  {h.models.map((m, i) => (
                    <span
                      key={i}
                      title={`${m.model_name} — ${m.status}`}
                      className={cx("inline-block h-2 w-2 rounded-full",
                        m.status === "ok" ? "bg-emerald-500" : m.status === "error" ? "bg-red-400" : "bg-slate-300")}
                    />
                  ))}
                </span>
                <span className="shrink-0 text-slate-400">{new Date(h.created_at).toLocaleTimeString()}</span>
              </button>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

function ModelGroup({
  icon: Icon, label, models, picked, onToggle,
}: {
  icon: typeof Cpu;
  label: string;
  models: BenchModel[];
  picked: Set<string>;
  onToggle: (id: string) => void;
}) {
  if (models.length === 0) return null;
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-slate-400">
        <Icon className="h-3.5 w-3.5" /> {label}
      </div>
      <div className="flex flex-wrap gap-2">
        {models.map((m) => {
          const on = picked.has(m.id);
          return (
            <button
              key={m.id}
              onClick={() => onToggle(m.id)}
              className={cx(
                "rounded-full border px-3 py-1 text-xs font-medium transition",
                on
                  ? "border-slate-900 bg-slate-900 text-white"
                  : "border-slate-200 bg-white text-slate-600 hover:border-slate-400",
              )}
            >
              {m.model_name}
            </button>
          );
        })}
      </div>
    </div>
  );
}
