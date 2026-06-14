"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Users, RefreshCw, Microscope, Search, ShieldCheck, ShieldAlert, Inbox } from "lucide-react";
import { api, type ResearcherOverview, type ResearcherTask, type RosterMember } from "../lib/api";
import { Badge, Card, SectionTitle, cx } from "../components/ui";
import { ago } from "../lib/format";

type Tone = "amber" | "green" | "red" | "blue" | "default";

const MODE_TONE: Record<string, Tone> = { active: "green", advisory: "blue", shadow: "amber", off: "default" };

// disposition → (chip tone, what it means)
const DISP: Record<string, { tone: Tone; label: string }> = {
  supported: { tone: "green", label: "supported" },
  contradicted: { tone: "red", label: "contradicted" },
  corpus_exhausted: { tone: "amber", label: "corpus exhausted → pivot" },
  thin_corpus: { tone: "blue", label: "thin corpus → acquire" },
  needs_experiment: { tone: "default", label: "needs experiment" },
  inconclusive: { tone: "default", label: "inconclusive" },
};

function Stat({ label, value, hint }: { label: string; value: number | string; hint?: string }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className="mt-0.5 text-2xl font-semibold tabular-nums text-slate-900">{value}</div>
      {hint && <div className="text-[11px] text-slate-400">{hint}</div>}
    </div>
  );
}

function Chips({ items, tone = "default" }: { items: string[]; tone?: Tone }) {
  if (!items?.length) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1.5">
      {items.map((it, i) => <Badge key={i} tone={tone}>{it}</Badge>)}
    </div>
  );
}

function FindingCard({ t }: { t: ResearcherTask }) {
  const f = t.finding;
  const d = f?.disposition ? DISP[f.disposition] ?? { tone: "default" as Tone, label: f.disposition } : null;
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-slate-400">T{t.id}</span>
        <Badge tone="default">{t.task_type}</Badge>
        {d ? <Badge tone={d.tone}>{d.label}</Badge> : <Badge tone="amber">{t.status}</Badge>}
        {f?.grounded != null && <span className="text-[11px] text-slate-400">grounded {Math.round(f.grounded * 100)}%</span>}
        {f?.n_evidence != null && <span className="text-[11px] text-slate-400">· {f.n_evidence} passages</span>}
        {f?.confidence_move && (
          <span className="text-[11px] font-medium text-slate-600">
            confidence {f.confidence_move[0].toFixed(2)} → {f.confidence_move[1].toFixed(2)}
          </span>
        )}
        <span className="ml-auto text-xs text-slate-400">{t.at ? ago(t.at) : ""}</span>
      </div>

      <p className="mt-2 text-sm text-slate-700">{t.description}</p>
      {t.direction && <p className="mt-1 text-xs text-slate-400">← {t.direction}</p>}

      {f && (
        <div className="mt-3 space-y-2.5 border-t border-slate-100 pt-3 text-sm">
          {f.summary && <p className="leading-relaxed text-slate-700">{f.summary}</p>}

          {f.queries.length > 0 && (
            <div>
              <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                <Search className="h-3 w-3" /> Searched the Library for
              </div>
              <Chips items={f.queries} tone="blue" />
            </div>
          )}

          {f.key_evidence.length > 0 && (
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Key evidence (cited)</div>
              <ul className="mt-1 space-y-0.5">
                {f.key_evidence.map((e, i) => (
                  <li key={i} className="flex items-start gap-1.5 text-[13px] text-slate-600">
                    <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-500" />
                    <span className="line-clamp-1">{e}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {f.kill_condition_check && (
            <div>
              <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                <ShieldAlert className="h-3 w-3" /> Kill-condition check
              </div>
              <p className="mt-0.5 text-[13px] text-slate-600">{f.kill_condition_check}</p>
            </div>
          )}

          {f.gaps.length > 0 && (
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Gaps</div>
              <Chips items={f.gaps} tone="amber" />
            </div>
          )}

          {f.acquire_queries.length > 0 && (
            <div>
              <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                <Inbox className="h-3 w-3" /> Self-healing acquires{f.acquires_fired != null ? ` (${f.acquires_fired} fired)` : ""}
              </div>
              <Chips items={f.acquire_queries} tone="blue" />
            </div>
          )}

          {f.next_step && (
            <p className="text-[13px] text-slate-500"><span className="font-medium text-slate-600">Next →</span> {f.next_step}</p>
          )}
        </div>
      )}
    </div>
  );
}

// One named full-stack researcher — the roster card that drills into their page.
function RosterCard({ r }: { r: RosterMember }) {
  const tot = r.done + r.failed;
  return (
    <Link
      href={`/researchers/${r.id}`}
      className="block rounded-2xl border border-slate-200 bg-white p-4 transition hover:border-indigo-300 hover:shadow-sm"
    >
      <div className="flex items-center gap-2">
        <span className="text-base font-semibold text-slate-900">{r.name}</span>
        <Badge tone={MODE_TONE[r.status] ?? "default"}>{r.status}</Badge>
        {r.win_rate != null && <span className="ml-auto text-sm font-semibold tabular-nums text-slate-700">{r.win_rate}% win</span>}
      </div>
      <div className="mt-0.5 text-xs text-indigo-600">{r.specialty}</div>
      {r.persona && <p className="mt-2 line-clamp-2 text-[13px] leading-snug text-slate-500">{r.persona}</p>}
      <div className="mt-3 flex flex-wrap gap-1.5">
        <Badge tone="blue">{r.owned_directions} directions</Badge>
        <Badge tone="green">{r.done} done</Badge>
        <Badge tone={r.failed ? "red" : "default"}>{r.failed} failed</Badge>
        <span className="ml-auto text-[11px] text-slate-400">{tot ? `${tot} runs` : "no runs yet"}{r.last_at ? ` · ${ago(r.last_at)}` : ""}</span>
      </div>
    </Link>
  );
}

export default function ResearchersPage() {
  const [ov, setOv] = useState<ResearcherOverview | null>(null);
  const [roster, setRoster] = useState<RosterMember[] | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    api.researcherRoster().then((d) => setRoster(d.researchers)).catch(() => setRoster(null));
    api.researcherOverview(40).then(setOv).catch(() => setOv(null)).finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    load();
    const id = setInterval(load, 12_000);
    return () => clearInterval(id);
  }, [load]);

  if (loading && !ov) return <div className="text-sm text-slate-500">Loading researchers…</div>;
  if (!ov) return <div className="text-sm text-slate-500">Researcher overview unavailable.</div>;

  const completed = ov.by_status.completed ?? 0;
  const running = ov.by_status.running ?? 0;
  const findings = Object.values(ov.by_disposition).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-6">
        <div>
          <div className="flex items-center gap-3">
            <Users className="h-7 w-7 text-violet-600" />
            <h1 className="text-3xl font-semibold tracking-tight text-slate-950">Researchers</h1>
            <Badge tone={MODE_TONE[ov.mode] ?? "default"}>{ov.mode}</Badge>
          </div>
          <p className="mt-2 max-w-2xl text-sm text-slate-500">
            Execute the planner&apos;s tasks against the certified Library — grounded findings that steer each
            direction (supports / contradicts / blocked), and self-healing acquires when the corpus is thin.
            <Link href="/ariadne" className="ml-1 text-violet-700 hover:underline">See the directions →</Link>
          </p>
        </div>
        <button
          onClick={load}
          className="inline-flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm hover:bg-slate-50"
        >
          <RefreshCw className={cx("h-4 w-4", loading && "animate-spin")} /> Refresh
        </button>
      </div>

      {roster && roster.length > 0 && (
        <Card>
          <SectionTitle icon={Users} title="The roster" subtitle="the lab's full-stack researchers — each owns directions end-to-end and authors their own experiments" />
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {roster.map((r) => <RosterCard key={r.id} r={r} />)}
          </div>
        </Card>
      )}

      <Card>
        <SectionTitle icon={Microscope} title="At a glance" subtitle="the research throughput + how the corpus is answering" />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <Stat label="Tasks" value={ov.tasks_total} />
          <Stat label="Completed" value={completed} />
          <Stat label="Investigating" value={running} hint={running ? "in flight" : undefined} />
          <Stat label="Findings" value={findings} />
          <Stat label="Acquires · 24h" value={ov.acquire.fired_24h} hint={`${ov.acquire.outcomes.fulfilled ?? 0} fetched · ${ov.acquire.outcomes.already_have ?? 0} had`} />
        </div>
        {Object.keys(ov.by_disposition).length > 0 && (
          <div className="mt-4 flex flex-wrap gap-2">
            {Object.entries(ov.by_disposition).map(([k, n]) => {
              const cfg = DISP[k] ?? { tone: "default" as Tone, label: k };
              return <Badge key={k} tone={cfg.tone}>{cfg.label} · {n}</Badge>;
            })}
          </div>
        )}
      </Card>

      <Card>
        <SectionTitle icon={Users} title="Findings" subtitle="every task the researcher worked, in full — query, evidence, verdict, and what it steered" />
        {ov.tasks.length === 0 ? (
          <div className="text-sm text-slate-400">No tasks yet — they appear once Ariadne approves a direction and the planner runs.</div>
        ) : (
          <div className="space-y-3">
            {ov.tasks.map((t) => <FindingCard key={t.id} t={t} />)}
          </div>
        )}
      </Card>
    </div>
  );
}
