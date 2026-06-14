"use client";

// Experiments — the lab's bench notebook, live.
//
// Left: the run ledger (polled every 5s) — status, direction, hypothesis, budgets.
// Right: the selected run's full record — the hypothesis and the direction it serves,
// the result numbers, the researcher's interpretation + first-person lab note (the
// REASONING), provenance (seed / code hash / image digest), the actual code, and a
// kill switch for runaway runs. Running/queued rows keep refreshing so results appear
// the moment the sandbox prints them.

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, QmExperimentDetail, QmExperiments } from "../lib/api";


const STATUS_TONE: Record<string, string> = {
  running: "border-sky-200 bg-sky-50 text-sky-700",
  queued: "border-amber-200 bg-amber-50 text-amber-700",
  completed: "border-emerald-200 bg-emerald-50 text-emerald-700",
  failed: "border-rose-200 bg-rose-50 text-rose-600",
  killed: "border-slate-300 bg-slate-100 text-slate-500",
};

function StatusPill({ status }: { status: string }) {
  return (
    <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${STATUS_TONE[status] ?? "border-slate-200 bg-slate-50 text-slate-500"}`}>
      {status}
    </span>
  );
}

const REALISM_TONE: Record<string, string> = {
  real: "border-emerald-200 bg-emerald-50 text-emerald-700",
  builtin: "border-amber-200 bg-amber-50 text-amber-700",
  synthetic: "border-rose-200 bg-rose-50 text-rose-600",
};

// Whether the run's data was REAL, a sklearn builtin, or synthesized — the real-research signal.
function RealismPill({ realism, mismatch }: { realism?: string | null; mismatch?: boolean | null }) {
  if (!realism) return null;
  return (
    <span
      className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${REALISM_TONE[realism] ?? "border-slate-200 bg-slate-50 text-slate-500"}`}
      title={mismatch ? "plan named a real dataset but the run used synthetic — mismatch" : `data: ${realism}`}
    >
      {realism}{mismatch ? " ⚠" : ""}
    </span>
  );
}

const FAILURE_TONE: Record<string, string> = {
  env_missing_lib: "border-fuchsia-200 bg-fuchsia-50 text-fuchsia-700",
  network_attempt: "border-rose-200 bg-rose-50 text-rose-600",
  serialization: "border-orange-200 bg-orange-50 text-orange-700",
  timeout: "border-amber-200 bg-amber-50 text-amber-700",
  no_result: "border-slate-300 bg-slate-100 text-slate-500",
  infeasible: "border-violet-200 bg-violet-50 text-violet-700",
  genuine_bug: "border-red-200 bg-red-50 text-red-700",
};

// Why a run failed (session-loop classification) — the triage signal.
function FailurePill({ fc }: { fc?: string | null }) {
  if (!fc) return null;
  return (
    <span
      className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${FAILURE_TONE[fc] ?? "border-slate-200 bg-slate-50 text-slate-500"}`}
      title={`failure class: ${fc}`}
    >
      {fc}
    </span>
  );
}

// The researcher who authored the run — a chip that drills into their page.
function ResearcherChip({ id, name }: { id?: number | null; name?: string | null }) {
  if (!name) return null;
  const body = (
    <span className="shrink-0 rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-700">
      {name}
    </span>
  );
  return id ? (
    <Link href={`/researchers/${id}`} onClick={(e) => e.stopPropagation()} className="hover:opacity-80">
      {body}
    </Link>
  ) : (
    body
  );
}

function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">{title}</div>
      {children}
    </div>
  );
}

export default function ExperimentsPage() {
  const [ledger, setLedger] = useState<QmExperiments | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<QmExperimentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refreshLedger = useCallback(async () => {
    try {
      setLedger(await api.qmExperiments(80));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const refreshDetail = useCallback(async (id: number) => {
    try {
      setDetail(await api.qmExperimentDetail(id));
    } catch {
      /* transient — next poll retries */
    }
  }, []);

  useEffect(() => {
    refreshLedger();
    const t = setInterval(refreshLedger, 5000);
    return () => clearInterval(t);
  }, [refreshLedger]);

  useEffect(() => {
    if (selectedId == null) return;
    refreshDetail(selectedId);
    // keep the open record live — results/notes land the moment the run settles
    const t = setInterval(() => refreshDetail(selectedId), 5000);
    return () => clearInterval(t);
  }, [selectedId, refreshDetail]);

  // deep-link: /experiments?id=<n> (from a researcher's experiment list) preselects that run
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("id");
    if (q) setSelectedId(Number(q));
  }, []);

  // default-select the newest run once the ledger arrives
  useEffect(() => {
    if (selectedId == null && ledger?.experiments?.length) setSelectedId(ledger.experiments[0].id);
  }, [ledger, selectedId]);

  const counts = ledger?.by_status ?? {};
  const rows = ledger?.experiments ?? [];
  const liveNow = (counts["running"] ?? 0) + (counts["queued"] ?? 0);

  const kill = useMemo(
    () => async (id: number) => {
      if (!window.confirm(`Kill experiment #${id}? The container is terminated immediately.`)) return;
      await api.qmKillExperiment(id);
      refreshLedger();
      refreshDetail(id);
    },
    [refreshLedger, refreshDetail],
  );

  return (
    <main className="mx-auto max-w-[1680px] px-4 pb-10 pt-4">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold text-slate-800">Experiments</h1>
        <span className="text-[12px] text-slate-400">the lab’s bench notebook — designs, runs, results, and the researcher’s reasoning</span>
        <div className="ml-auto flex items-center gap-2 text-[11px]">
          {["running", "queued", "completed", "failed", "killed"].map((s) => (
            <span key={s} className={`rounded-full border px-2 py-0.5 font-semibold ${STATUS_TONE[s]}`}>
              {counts[s] ?? 0} {s}
            </span>
          ))}
          <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-slate-500">QM: {ledger?.mode ?? "…"}</span>
        </div>
      </div>

      {error && <div className="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] text-rose-600">API unreachable: {error}</div>}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[460px_1fr]">
        {/* ── run ledger ── */}
        <div className="max-h-[78vh] overflow-y-auto rounded-xl border border-slate-200 bg-white/80 shadow-sm">
          {rows.length === 0 && <div className="p-6 text-center text-[12px] text-slate-400">no experiments yet — the coverage driver fires one per approved direction</div>}
          <ul className="divide-y divide-slate-100">
            {rows.map((r) => (
              <li
                key={r.id}
                onClick={() => setSelectedId(r.id)}
                className={`cursor-pointer px-3 py-2.5 transition hover:bg-slate-50 ${selectedId === r.id ? "bg-violet-50/60" : ""}`}
              >
                <div className="flex items-center gap-2">
                  <span className="text-[12px] font-semibold tabular-nums text-slate-700">#{r.id}</span>
                  <StatusPill status={r.status} />
                  <ResearcherChip id={r.researcher_id} name={r.researcher_name} />
                  <RealismPill realism={r.data_realism} mismatch={r.realism_mismatch} />
                  <FailurePill fc={r.failure_class} />
                  {r.requires_gpu && <span className="rounded border border-violet-200 bg-violet-50 px-1 text-[9px] font-semibold text-violet-600">GPU</span>}
                  <span className="ml-auto text-[10px] text-slate-400">{ago(r.at)}</span>
                </div>
                <div className="mt-1 line-clamp-2 text-[12px] leading-snug text-slate-700">{r.hypothesis ?? "(no hypothesis recorded)"}</div>
                {r.claim_statement && (
                  <div className="mt-0.5 line-clamp-1 text-[10px] text-slate-400">
                    direction #{r.claim_id}: {r.claim_statement}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>

        {/* ── selected run record ── */}
        <div className="max-h-[78vh] overflow-y-auto rounded-xl border border-slate-200 bg-white/80 p-4 shadow-sm">
          {!detail ? (
            <div className="p-8 text-center text-[12px] text-slate-400">select a run</div>
          ) : (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[15px] font-semibold text-slate-800">Experiment #{detail.id}</span>
                <StatusPill status={detail.status} />
                <ResearcherChip id={detail.researcher_id} name={detail.researcher_name} />
                <RealismPill realism={detail.data_realism} mismatch={detail.realism_mismatch} />
                <FailurePill fc={detail.failure_class} />
                {(detail.status === "running" || detail.status === "queued") && (
                  <button onClick={() => kill(detail.id)} className="rounded-md border border-rose-200 bg-rose-50 px-2 py-0.5 text-[11px] font-semibold text-rose-600 hover:bg-rose-100">
                    kill
                  </button>
                )}
                <span className="ml-auto text-[11px] text-slate-400">
                  {detail.duration_s != null ? `ran ${detail.duration_s}s` : detail.started_at ? `started ${ago(detail.started_at)}` : "not started"}
                  {" · "}budget {detail.wall_clock_budget_s ?? "—"}s / {detail.mem_budget_mb ?? "—"}MB
                </span>
              </div>

              {detail.claim_statement && (
                <Section title={`Direction #${detail.claim_id} · confidence ${detail.claim_confidence ?? "—"}`}>
                  <p className="text-[13px] leading-snug text-slate-700">{detail.claim_statement}</p>
                </Section>
              )}

              <Section title="Hypothesis under test">
                <p className="text-[13px] font-medium leading-snug text-slate-800">{detail.hypothesis ?? "—"}</p>
                {detail.dataset_plan && <p className="mt-1 text-[11px] text-slate-500">data: {detail.dataset_plan}</p>}
              </Section>

              {detail.result != null && (
                <Section title="Result (the script’s JSON output)">
                  <pre className="max-h-56 overflow-auto rounded-lg border border-emerald-100 bg-emerald-50/50 p-3 text-[11px] leading-relaxed text-slate-700">
                    {JSON.stringify(detail.result, null, 2)}
                  </pre>
                </Section>
              )}

              {(detail.error || detail.kill_reason) && (
                <Section title="Failure">
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-rose-100 bg-rose-50/60 p-3 text-[11px] text-rose-700">
                    {detail.kill_reason ? `killed: ${detail.kill_reason}\n` : ""}{detail.error ?? ""}
                  </pre>
                </Section>
              )}

              {detail.interpretation && (
                <Section title="Interpretation (honest read of the numbers)">
                  <p className="rounded-lg border border-slate-100 bg-slate-50/70 p-3 text-[12px] leading-relaxed text-slate-700">{detail.interpretation}</p>
                </Section>
              )}

              {detail.researcher_notes && (
                <Section title="Lab note — the researcher’s reasoning">
                  <p className="whitespace-pre-wrap rounded-lg border border-violet-100 bg-violet-50/40 p-3 text-[12px] leading-relaxed text-slate-700">{detail.researcher_notes}</p>
                </Section>
              )}

              {detail.provenance && Object.keys(detail.provenance).length > 0 && (
                <Section title="Provenance (reproducibility record)">
                  <div className="flex flex-wrap gap-2 text-[10px] text-slate-500">
                    {Object.entries(detail.provenance).map(([k, v]) => (
                      <span key={k} className="rounded border border-slate-200 bg-white px-1.5 py-0.5">
                        {k}: {String(v).slice(0, 60)}
                      </span>
                    ))}
                  </div>
                </Section>
              )}

              {detail.code && (
                <Section title="Code (final working script)">
                  <pre className="max-h-[420px] overflow-auto rounded-lg border border-slate-200 bg-slate-900 p-3 text-[11px] leading-relaxed text-slate-100">
                    {detail.code}
                  </pre>
                </Section>
              )}
            </div>
          )}
        </div>
      </div>

      <p className="mt-3 text-[10px] text-slate-400">
        polling every 5s{liveNow > 0 ? ` — ${liveNow} run(s) live now` : ""} · results appear the moment the sandbox prints them · kill terminates the container immediately
      </p>
    </main>
  );
}
