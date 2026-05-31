"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import {
  ChevronRight, FlaskConical, FileSearch, Layers, ArrowLeft, ExternalLink,
  Search, Brain,
} from "lucide-react";
import {
  api,
  type ResearchTree,
  type ResearchTreeRun,
  type ResearchInquiry,
  type ResearchEvidence,
  type ResearchExperiment,
  type ResearchFinding,
} from "../../../lib/api";
import { Badge, Card, SectionTitle, cx } from "../../../components/ui";

function fmtMs(ms: number | null) {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

function latencyFor(run: ResearchTreeRun | undefined): number | null {
  if (!run || !run.started_at || !run.completed_at) return null;
  return new Date(run.completed_at).getTime() - new Date(run.started_at).getTime();
}

function stanceTone(s: string): "green" | "red" | "default" {
  if (s === "supports") return "green";
  if (s === "refutes") return "red";
  return "default";
}

// Runs that fall in the [thisPlan, nextPlan) id window — the LLM calls that
// belong to one iteration of the loop.
function runsInIteration(
  all: ResearchTreeRun[],
  thisPlanId: number | null,
  nextPlanId: number | null,
): ResearchTreeRun[] {
  if (thisPlanId == null) return [];
  return all.filter((r) => r.id >= thisPlanId && (nextPlanId == null || r.id < nextPlanId));
}

export default function ResearchTreePage() {
  const params = useParams<{ id: string }>();
  const taskId = Number(params.id);
  const [tree, setTree] = useState<ResearchTree | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [attempts, setAttempts] = useState(0);

  const load = useCallback(() => {
    setAttempts((n) => n + 1);
    // Visible in devtools — confirms the client actually fired the request.
    console.log(`[research-tree] fetching /api/debug/research-tree/${taskId}`);
    api.debugResearchTree(taskId)
      .then((t) => { setTree(t); setErr(null);
        console.log(`[research-tree] loaded: inquiries=${t.inquiries.length} evidence=${t.evidence.length} findings=${t.findings.length}`);
      })
      .catch((e) => { setErr(String(e));
        console.error(`[research-tree] fetch failed:`, e);
      });
  }, [taskId]);

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  if (err) {
    return (
      <Card>
        <div className="text-sm font-medium text-red-600">Fetch failed</div>
        <div className="mt-1 text-xs text-slate-600">taskId={String(taskId)} · attempts={attempts}</div>
        <pre className="mt-2 overflow-auto rounded-lg bg-red-50 p-3 text-xs text-red-700">{err}</pre>
        <button
          onClick={load}
          className="mt-3 rounded-full border border-slate-300 bg-white px-3 py-1 text-xs font-medium text-slate-700 hover:border-slate-500"
        >
          Retry
        </button>
      </Card>
    );
  }
  if (!tree) {
    return (
      <Card>
        <div className="text-sm text-slate-700">Loading research tree…</div>
        <div className="mt-1 text-xs text-slate-500">
          fetching <code className="rounded bg-slate-100 px-1 py-0.5">/api/debug/research-tree/{String(taskId)}</code>
          {" · "}attempts={attempts}
        </div>
        <div className="mt-2 text-xs text-slate-400">
          If this sticks here, the browser isn't reaching the dev server. Check VS Code port-forwarding on :8088, or open devtools → Network and look for the request.
        </div>
        <button
          onClick={load}
          className="mt-3 rounded-full border border-slate-300 bg-white px-3 py-1 text-xs font-medium text-slate-700 hover:border-slate-500"
        >
          Try now
        </button>
      </Card>
    );
  }
  if (!tree.task) return <Card><div className="text-sm text-slate-500">Task {taskId} not found.</div></Card>;

  const runsById: Record<number, ResearchTreeRun> = {};
  for (const r of tree.agent_runs) runsById[r.id] = r;
  const allRuns = [...tree.agent_runs].sort((a, b) => a.id - b.id);

  const iterations = [...tree.inquiries].sort((a, b) => a.iteration - b.iteration);

  return (
    <div className="space-y-6">
      <Card>
        <SectionTitle
          icon={Layers}
          title={`Task T${tree.task.id} — research tree`}
          subtitle={tree.task.description}
          action={
            <Link
              href="/debug"
              className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-600 hover:border-slate-400"
            >
              <ArrowLeft className="h-3.5 w-3.5" /> back to debug
            </Link>
          }
        />
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <Badge tone={tree.task.status === "completed" ? "green" : tree.task.status === "failed" ? "red" : "default"}>
            {tree.task.status}
          </Badge>
          {tree.task.claim_id != null && (
            <span>thesis: <span className="font-mono">T{tree.task.claim_id}</span></span>
          )}
          <span>iterations: <span className="font-mono">{iterations.length || 0}</span></span>
          <span>evidence: <span className="font-mono">{tree.evidence.length}</span></span>
          <span>experiments: <span className="font-mono">{tree.experiments.length}</span></span>
          <span>findings: <span className="font-mono">{tree.findings.length}</span></span>
          <span>LLM calls: <span className="font-mono">{allRuns.length}</span></span>
        </div>
      </Card>

      {iterations.length === 0 && (
        <Card>
          <div className="text-sm text-slate-500">
            No inquiries recorded yet. (This task may still be using the legacy researcher,
            or the loop hasn't started.)
          </div>
        </Card>
      )}

      {iterations.map((inq, idx) => {
        const nextPlanId = iterations[idx + 1]?.plan_run_id ?? null;
        const iterRuns = runsInIteration(allRuns, inq.plan_run_id, nextPlanId);
        return (
          <IterationCard
            key={inq.id}
            inquiry={inq}
            evidence={tree.evidence.filter((e) => e.inquiry_id === inq.id)}
            experiments={tree.experiments.filter((e) => e.inquiry_id === inq.id)}
            iterationRuns={iterRuns}
            runsById={runsById}
          />
        );
      })}

      <FindingsCard findings={tree.findings} />
    </div>
  );
}

// -------------------------------------------------------------------------
// Payload viewer — reusable. Shows the PROMPT and OUTPUT for one agent_run.
// -------------------------------------------------------------------------

function RunPayload({ run }: { run: ResearchTreeRun | undefined }) {
  const [open, setOpen] = useState(false);
  if (!run) return <span className="text-xs text-slate-400">(no run recorded)</span>;
  const lat = latencyFor(run);
  return (
    <div className="mt-2 rounded-xl border border-slate-200 bg-slate-50/50">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs"
      >
        <ChevronRight className={cx("h-3.5 w-3.5 text-slate-400 transition-transform", open && "rotate-90")} />
        <Badge tone={run.status === "completed" ? "green" : run.status === "failed" ? "red" : "default"}>
          {run.invocation_type}
        </Badge>
        <span className="font-mono text-slate-500">#{run.id}</span>
        <span className="truncate text-slate-500" style={{ maxWidth: 200 }}>{run.model_name}</span>
        <span className="text-slate-500">{fmtMs(lat)}</span>
        <span className="text-slate-400">
          in {run.input_token_count ?? "—"} · out {run.output_token_count ?? "—"} tok
        </span>
        <span className="ml-auto text-slate-400">{open ? "hide" : "view prompt + output"}</span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-slate-200 p-3">
          {run.error && (
            <div>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-red-600">error</div>
              <pre className="overflow-auto rounded-lg bg-red-50 p-2 text-xs text-red-700">{run.error}</pre>
            </div>
          )}
          <div>
            <div className="mb-1 flex items-center justify-between">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">prompt (what the model saw)</div>
              <CopyBtn text={run.input_summary || ""} />
            </div>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-3 text-[11px] leading-relaxed text-slate-800 ring-1 ring-slate-200">
{run.input_summary || "(empty — this run pre-dates payload capture; re-run for full transparency)"}
            </pre>
          </div>
          <div>
            <div className="mb-1 flex items-center justify-between">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">output (raw JSON the model returned)</div>
              <CopyBtn text={run.output_summary || ""} />
            </div>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-3 text-[11px] leading-relaxed text-slate-800 ring-1 ring-slate-200">
{run.output_summary || "(empty)"}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

function CopyBtn({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1000);
        });
      }}
      className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] text-slate-500 hover:border-slate-400"
    >
      {done ? "copied" : "copy"}
    </button>
  );
}

// -------------------------------------------------------------------------
// Iteration block
// -------------------------------------------------------------------------

function IterationCard({
  inquiry,
  evidence,
  experiments,
  iterationRuns,
  runsById,
}: {
  inquiry: ResearchInquiry;
  evidence: ResearchEvidence[];
  experiments: ResearchExperiment[];
  iterationRuns: ResearchTreeRun[];
  runsById: Record<number, ResearchTreeRun>;
}) {
  const planRun = inquiry.plan_run_id ? runsById[inquiry.plan_run_id] : undefined;
  const synthRun = iterationRuns.find((r) => r.invocation_type === "researcher.synthesize");
  const gapRun   = iterationRuns.find((r) => r.invocation_type === "researcher.gap_check");

  return (
    <Card>
      {/* iteration header + plan */}
      <div className="mb-3">
        <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Iteration {inquiry.iteration}
        </div>
        <div className="mt-1 text-sm font-medium text-slate-900">{inquiry.question}</div>
      </div>
      <details open className="mb-4">
        <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-slate-500">
          <Search className="mr-1 inline h-3.5 w-3.5" /> plan_inquiry
        </summary>
        <RunPayload run={planRun} />
      </details>

      {/* Sub-questions */}
      <div className="space-y-3">
        {inquiry.sub_questions.map((sq, idx) => {
          const sqEv = evidence.filter((e) => e.sub_question_idx === idx);
          return (
            <SubQuestionBlock
              key={idx}
              idx={idx}
              question={sq.q}
              sources={sq.sources}
              why={sq.why}
              evidence={sqEv}
              runsById={runsById}
            />
          );
        })}
      </div>

      {/* Experiments */}
      {(inquiry.proposed_experiments.length > 0 || experiments.length > 0) && (
        <div className="mt-4">
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            <FlaskConical className="h-3.5 w-3.5" /> experiments
          </div>
          <div className="space-y-2">
            {experiments.map((x) => (
              <ExperimentRow key={x.id} exp={x} runsById={runsById} />
            ))}
            {experiments.length === 0 && (
              <div className="text-xs text-slate-400">
                {inquiry.proposed_experiments.length} proposed, none persisted yet.
              </div>
            )}
          </div>
        </div>
      )}

      {/* Synthesize + Gap-check LLM calls */}
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div>
          <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            <Brain className="h-3.5 w-3.5" /> synthesize
          </div>
          <RunPayload run={synthRun} />
        </div>
        <div>
          <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            <Brain className="h-3.5 w-3.5" /> gap_check
          </div>
          <RunPayload run={gapRun} />
        </div>
      </div>
    </Card>
  );
}

// -------------------------------------------------------------------------
// Sub-question block
// -------------------------------------------------------------------------

function SubQuestionBlock({
  idx,
  question,
  sources,
  why,
  evidence,
  runsById,
}: {
  idx: number;
  question: string;
  sources: string[];
  why: string;
  evidence: ResearchEvidence[];
  runsById: Record<number, ResearchTreeRun>;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/60 p-3">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-start gap-3 text-left"
      >
        <ChevronRight
          className={cx("mt-0.5 h-4 w-4 shrink-0 text-slate-400 transition-transform",
            open && "rotate-90")}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-xs font-mono text-slate-400">
            SQ{idx}
            {sources.map((s) => (
              <Badge key={s} tone="default">{s}</Badge>
            ))}
          </div>
          <div className="mt-1 text-sm font-medium text-slate-800">{question}</div>
          <div className="mt-1 text-xs italic text-slate-500">{why}</div>
          <div className="mt-1 text-xs text-slate-500">
            <span className="font-mono">{evidence.length}</span> evidence
          </div>
        </div>
      </button>
      {open && evidence.length > 0 && (
        <div className="mt-3 space-y-2 border-t border-slate-200 pt-3">
          {evidence.map((e) => <EvidenceRow key={e.id} ev={e} runsById={runsById} />)}
        </div>
      )}
    </div>
  );
}

function EvidenceRow({ ev, runsById }: { ev: ResearchEvidence; runsById: Record<number, ResearchTreeRun> }) {
  const run = ev.extract_run_id ? runsById[ev.extract_run_id] : undefined;
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3 text-sm">
      <div className="flex items-center gap-2">
        <Badge tone={stanceTone(ev.stance)}>{ev.stance}</Badge>
        <span className="text-xs text-slate-500">
          conf <span className="font-mono">{Number(ev.confidence).toFixed(2)}</span>
        </span>
        <a
          href={ev.url} target="_blank" rel="noreferrer"
          className="ml-auto inline-flex items-center gap-1 truncate text-xs text-blue-600 hover:underline"
          style={{ maxWidth: 360 }}
        >
          <span className="truncate">{ev.title || ev.url}</span>
          <ExternalLink className="h-3 w-3 shrink-0" />
        </a>
      </div>
      <div className="mt-2 text-slate-800">{ev.claim}</div>
      <blockquote className="mt-2 rounded-lg border-l-2 border-slate-300 bg-slate-50 px-3 py-2 text-xs italic text-slate-600">
        “{ev.quote}”
      </blockquote>
      <RunPayload run={run} />
    </div>
  );
}

// -------------------------------------------------------------------------
// Experiment + finding rows
// -------------------------------------------------------------------------

function ExperimentRow({ exp, runsById }: { exp: ResearchExperiment; runsById: Record<number, ResearchTreeRun> }) {
  const [open, setOpen] = useState(false);
  const interpRun = exp.interpret_run_id ? runsById[exp.interpret_run_id] : undefined;
  const tone: "green" | "red" | "default" =
    exp.status === "completed" ? "green"
      : exp.status === "failed" ? "red" : "default";

  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-3 text-sm">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-start gap-3 text-left">
        <ChevronRight className={cx("mt-0.5 h-4 w-4 shrink-0 text-slate-400 transition-transform", open && "rotate-90")} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs text-slate-700">{exp.kind}</span>
            <Badge tone={tone}>{exp.status}</Badge>
          </div>
          {exp.interpretation && (
            <div className="mt-1 text-slate-700">{exp.interpretation}</div>
          )}
          {exp.error && (
            <div className="mt-1 text-xs text-red-600">{exp.error}</div>
          )}
        </div>
      </button>
      {open && (
        <div className="mt-3 space-y-2 border-t border-slate-200 pt-3 text-xs">
          <div>
            <div className="mb-1 font-semibold text-slate-500">params (input to the dispatch)</div>
            <pre className="overflow-auto rounded-lg bg-slate-50 p-2 text-slate-800">{JSON.stringify(exp.params, null, 2)}</pre>
          </div>
          {exp.result && (
            <div>
              <div className="mb-1 font-semibold text-slate-500">result (output from the experiment runner — pure code, no LLM)</div>
              <pre className="max-h-80 overflow-auto rounded-lg bg-slate-50 p-2 text-slate-800">{JSON.stringify(exp.result, null, 2)}</pre>
            </div>
          )}
          <div>
            <div className="mb-1 font-semibold text-slate-500">interpret call (LLM)</div>
            <RunPayload run={interpRun} />
          </div>
        </div>
      )}
    </div>
  );
}

function FindingsCard({ findings }: { findings: ResearchFinding[] }) {
  if (findings.length === 0) {
    return (
      <Card>
        <SectionTitle icon={FileSearch} title="Synthesized findings" subtitle="None yet — synthesis declined or task not finished." />
      </Card>
    );
  }
  return (
    <Card>
      <SectionTitle icon={FileSearch} title="Synthesized findings"
                    subtitle="What the loop concluded — written to the findings table; the evaluation scores these." />
      <div className="space-y-2">
        {findings.map((f) => (
          <div key={f.id} className="rounded-2xl border border-slate-200 bg-white p-3 text-sm">
            <div className="flex items-center gap-2">
              <Badge tone={f.audit_verdict === "pass" ? "green" : f.audit_verdict === "slop" ? "red" : "default"}>
                {f.audit_verdict || "unaudited"}
              </Badge>
              <Badge tone="default">{f.source || "—"}</Badge>
              <span className="text-xs text-slate-500">
                rel <span className="font-mono">{Number(f.relevance_score).toFixed(1)}</span>
              </span>
              <span className="text-xs text-slate-500">
                supports: <span className="font-mono">{String(f.supports_thesis)}</span>
              </span>
              {f.url && (
                <a href={f.url} target="_blank" rel="noreferrer"
                   className="ml-auto inline-flex items-center gap-1 truncate text-xs text-blue-600 hover:underline"
                   style={{ maxWidth: 360 }}>
                  <span className="truncate">{f.title || f.url}</span>
                  <ExternalLink className="h-3 w-3 shrink-0" />
                </a>
              )}
            </div>
            {f.title && <div className="mt-2 font-medium text-slate-900">{f.title}</div>}
            <div className="mt-1 text-slate-700">{f.summary}</div>
            {f.why_it_matters && (
              <div className="mt-1 text-xs italic text-slate-500">{f.why_it_matters}</div>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}
