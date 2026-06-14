"use client";

// Per-researcher drill-down — one full-stack researcher's profile, the directions they own,
// and every experiment they authored (with realism + failure class). Reached from the roster
// on /researchers or a researcher chip on /experiments.

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Users, RefreshCw, FlaskConical, Compass } from "lucide-react";
import { api, type ResearcherDetail } from "../../lib/api";
import { Badge, Card, SectionTitle, cx } from "../../components/ui";
import { ago } from "../../lib/format";

type Tone = "amber" | "green" | "red" | "blue" | "default";
const MODE_TONE: Record<string, Tone> = { active: "green", advisory: "blue", shadow: "amber", off: "default" };
const REALISM_TONE: Record<string, Tone> = { real: "green", builtin: "amber", synthetic: "red" };
const STATUS_TONE: Record<string, Tone> = { completed: "green", failed: "red", killed: "default", running: "blue", queued: "amber" };

function ExpRow({ e }: { e: ResearcherDetail["experiments"][number] }) {
  return (
    <Link
      href={`/experiments?id=${e.id}`}
      className="block rounded-xl border border-slate-200 bg-white px-3 py-2 transition hover:border-indigo-300 hover:bg-slate-50"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-slate-400">#{e.id}</span>
        <Badge tone={STATUS_TONE[e.status] ?? "default"}>{e.status}</Badge>
        {e.data_realism && <Badge tone={REALISM_TONE[e.data_realism] ?? "default"}>{e.data_realism}{e.realism_mismatch ? " ⚠" : ""}</Badge>}
        {e.failure_class && <Badge tone="red">{e.failure_class}</Badge>}
        {e.requires_gpu && <Badge tone="default">GPU</Badge>}
        <span className="ml-auto text-[11px] text-slate-400">{e.at ? ago(e.at) : ""}</span>
      </div>
      <p className="mt-1 line-clamp-2 text-[13px] leading-snug text-slate-700">{e.hypothesis ?? "(no hypothesis recorded)"}</p>
      {e.claim_statement && <p className="mt-0.5 line-clamp-1 text-[11px] text-slate-400">← {e.claim_statement}</p>}
    </Link>
  );
}

export default function ResearcherDetailPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  const [d, setD] = useState<ResearcherDetail | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    api.researcherDetail(id).then(setD).catch(() => setD(null)).finally(() => setLoading(false));
  }, [id]);
  useEffect(() => {
    load();
    const t = setInterval(load, 12_000);
    return () => clearInterval(t);
  }, [load]);

  if (loading && !d) return <div className="text-sm text-slate-500">Loading researcher…</div>;
  if (!d || d.error) return <div className="text-sm text-slate-500">Researcher not found. <Link href="/researchers" className="text-violet-700 hover:underline">Back to the roster →</Link></div>;

  const completed = d.by_status.completed ?? 0;
  const failed = (d.by_status.failed ?? 0) + (d.by_status.killed ?? 0);

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-6">
        <div>
          <Link href="/researchers" className="text-xs text-slate-400 hover:text-slate-600">← Researchers</Link>
          <div className="mt-1 flex items-center gap-3">
            <Users className="h-7 w-7 text-violet-600" />
            <h1 className="text-3xl font-semibold tracking-tight text-slate-950">{d.name}</h1>
            <Badge tone={MODE_TONE[d.status] ?? "default"}>{d.status}</Badge>
            <Badge tone="blue">{d.specialty}</Badge>
            {d.win_rate != null && <span className="text-sm font-semibold tabular-nums text-slate-700">{d.win_rate}% win</span>}
          </div>
          {d.persona && <p className="mt-2 max-w-2xl text-sm text-slate-500">{d.persona}</p>}
        </div>
        <button
          onClick={load}
          className="inline-flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm hover:bg-slate-50"
        >
          <RefreshCw className={cx("h-4 w-4", loading && "animate-spin")} /> Refresh
        </button>
      </div>

      <Card>
        <SectionTitle icon={FlaskConical} title="Experiment record" subtitle="every run this researcher authored, by outcome" />
        <div className="flex flex-wrap gap-2">
          <Badge tone="green">{completed} completed</Badge>
          <Badge tone={failed ? "red" : "default"}>{failed} failed/killed</Badge>
          {Object.entries(d.by_failure_class).map(([k, n]) => <Badge key={k} tone="amber">{k} · {n}</Badge>)}
        </div>
      </Card>

      <Card>
        <SectionTitle icon={Compass} title={`Directions owned (${d.directions.length})`} subtitle="the research bets assigned to this researcher" />
        {d.directions.length === 0 ? (
          <div className="text-sm text-slate-400">No directions assigned yet.</div>
        ) : (
          <div className="space-y-2">
            {d.directions.map((dir) => (
              <Link
                key={dir.id}
                href={`/ariadne?claim=${dir.id}`}
                className="block rounded-xl border border-slate-200 bg-white px-3 py-2 transition hover:border-indigo-300 hover:bg-slate-50"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs text-slate-400">#{dir.id}</span>
                  <Badge tone={dir.status === "concluded" ? "green" : "default"}>{dir.status}</Badge>
                  {dir.gate && <Badge tone={dir.gate === "approved" ? "green" : "amber"}>{dir.gate}</Badge>}
                  {dir.confidence != null && <span className="ml-auto text-[11px] text-slate-400">confidence {dir.confidence.toFixed(2)}</span>}
                </div>
                <p className="mt-1 text-[13px] leading-snug text-slate-700">{dir.statement}</p>
              </Link>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <SectionTitle icon={FlaskConical} title={`Experiments (${d.experiments.length})`} subtitle="click a run to open its full record on the bench" />
        {d.experiments.length === 0 ? (
          <div className="text-sm text-slate-400">No experiments authored yet.</div>
        ) : (
          <div className="space-y-2">
            {d.experiments.map((e) => <ExpRow key={e.id} e={e} />)}
          </div>
        )}
      </Card>
    </div>
  );
}
