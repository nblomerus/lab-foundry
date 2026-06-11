"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import {
  Compass, Target, TrendingUp, Lightbulb, Flag, RefreshCw, ArrowUpRight, ArrowDownRight, MessagesSquare,
} from "lucide-react";
import {
  api, type AriadneOverview, type AriadneFieldModel, type AriadneScores, type FieldConcept,
  type AriadneConversation,
} from "../lib/api";
import { Badge, Card, SectionTitle, cx } from "../components/ui";
import { ago } from "../lib/format";

type Tone = "amber" | "green" | "red" | "blue" | "default";

const PRIORITY_TONE: Record<string, Tone> = { high: "green", medium: "blue", low: "default" };
const GATE_TONE: Record<string, Tone> = { approved: "green", held: "amber", rejected: "red", pending: "default" };
const MODE_TONE: Record<string, Tone> = { active: "green", advisory: "blue", shadow: "amber", off: "default" };

const DIMS: { key: keyof AriadneScores; label: string }[] = [
  { key: "impact", label: "impact" },
  { key: "novelty", label: "novelty" },
  { key: "differentiation", label: "diff" },
  { key: "paper_potential", label: "paper" },
  { key: "feasibility", label: "feas" },
  { key: "evidence_availability", label: "evid" },
  { key: "reviewer_interest", label: "review" },
  { key: "technical_depth", label: "depth" },
  { key: "cost_efficiency", label: "cost" },
  { key: "lab_alignment", label: "lab-fit" },
];

const TREND: { key: "hot" | "emerging" | "saturated" | "declining"; label: string; dot: string; tone: Tone }[] = [
  { key: "hot", label: "Hot — active, gaining share", dot: "bg-red-500", tone: "red" },
  { key: "emerging", label: "Emerging — new, gaining", dot: "bg-green-500", tone: "green" },
  { key: "saturated", label: "Saturated — well-trodden", dot: "bg-amber-500", tone: "amber" },
  { key: "declining", label: "Declining — cooling", dot: "bg-slate-400", tone: "default" },
];

function Stat({ label, value, hint }: { label: string; value: number | string; hint?: string }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
      <div className="text-2xl font-semibold tracking-tight text-slate-950">{value}</div>
      <div className="text-xs font-medium text-slate-500">{label}</div>
      {hint && <div className="text-[11px] text-slate-400">{hint}</div>}
    </div>
  );
}

function ScoreBars({ scores }: { scores: AriadneScores }) {
  return (
    <div className="mt-2 grid grid-cols-3 gap-x-4 gap-y-1 sm:grid-cols-9">
      {DIMS.map(({ key, label }) => {
        const v = scores[key];
        return (
          <div key={key} className="flex flex-col gap-0.5">
            <div className="flex items-center justify-between text-[10px] text-slate-400">
              <span>{label}</span><span className="font-mono">{v}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
              <div
                className={cx("h-full rounded-full", v >= 4 ? "bg-green-500" : v >= 3 ? "bg-blue-500" : "bg-slate-300")}
                style={{ width: `${(v / 5) * 100}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ConceptChip({ c }: { c: FieldConcept }) {
  const up = c.velocity >= 0;
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-xs">
      <span className="font-medium text-slate-700">{c.name}</span>
      <span className="text-slate-400">{c.total}p</span>
      <span className={cx("inline-flex items-center font-mono", up ? "text-green-600" : "text-red-500")}>
        {up ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
        {Math.abs(Math.round(c.velocity * 100))}%
      </span>
    </span>
  );
}

function Bubble({ who, children }: { who: "Ariadne" | "Mimir"; children: ReactNode }) {
  const isAriadne = who === "Ariadne";
  return (
    <div className={cx("flex", isAriadne ? "justify-end" : "justify-start")}>
      <div className={cx("max-w-[82%] rounded-2xl px-3.5 py-2 text-sm leading-snug",
        isAriadne ? "bg-violet-100 text-violet-950" : "bg-slate-100 text-slate-800")}>
        <div className="mb-1 flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide opacity-60">
          {isAriadne ? <Compass className="h-3 w-3" /> : <MessagesSquare className="h-3 w-3" />}
          {who}
        </div>
        {children}
      </div>
    </div>
  );
}

function ConversationThread({ c }: { c: AriadneConversation }) {
  const isReflection = c.kind === "reflection";
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="mb-2.5 flex items-center gap-2 text-xs text-slate-400">
        <MessagesSquare className="h-3.5 w-3.5 text-violet-500" />
        <Badge tone={isReflection ? "amber" : "blue"}>{c.kind}</Badge>
        {c.at && <span>{ago(c.at)}</span>}
        <Link href={`/trace/${c.session_id}`} className="ml-auto font-medium text-violet-600 hover:underline">
          trace #{c.session_id} →
        </Link>
      </div>
      <div className="space-y-2">
        {c.question && <Bubble who="Ariadne">{c.question}</Bubble>}
        <Bubble who="Mimir">
          {c.answer ?? "(no answer captured)"}
          {c.gaps.length > 0 && (
            <div className="mt-2">
              <div className="text-[10px] font-semibold uppercase tracking-wide opacity-60">Gaps flagged</div>
              <div className="mt-1 flex flex-wrap gap-1">
                {c.gaps.map((gp, i) => (
                  <span key={i} className="rounded-full bg-white/70 px-2 py-0.5 text-[11px] text-slate-600">{gp}</span>
                ))}
              </div>
            </div>
          )}
          {c.citations.length > 0 && (
            <div className="mt-2 text-[11px] opacity-70">cited: {c.citations.join(" · ")}</div>
          )}
        </Bubble>
        {(c.outcome.summary || c.outcome.items.length > 0) && (
          <Bubble who="Ariadne">
            {c.outcome.summary && <div className="font-medium">{c.outcome.label}: {c.outcome.summary}</div>}
            {c.outcome.items.length > 0 && (
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[13px]">
                {c.outcome.items.map((it, i) => <li key={i}>{it}</li>)}
              </ul>
            )}
          </Bubble>
        )}
      </div>
    </div>
  );
}

export default function AriadnePage() {
  const [ov, setOv] = useState<AriadneOverview | null>(null);
  const [fm, setFm] = useState<AriadneFieldModel | null>(null);
  const [convos, setConvos] = useState<AriadneConversation[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    Promise.all([api.ariadneOverview(), api.ariadneFieldModel(), api.ariadneConversations(12)])
      .then(([o, f, c]) => { setOv(o); setFm(f); setConvos(c.conversations); })
      .catch(() => { setOv(null); setFm(null); setConvos([]); })
      .finally(() => setLoading(false));
  }, []);
  useEffect(load, [load]);

  if (loading && !ov) return <div className="text-sm text-slate-500">Loading Ariadne…</div>;
  if (!ov) return <div className="text-sm text-slate-500">Ariadne overview unavailable.</div>;

  const g = ov.at_a_glance;
  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-6">
        <div>
          <div className="flex items-center gap-3">
            <Compass className="h-7 w-7 text-violet-600" />
            <h1 className="text-3xl font-semibold tracking-tight text-slate-950">Ariadne</h1>
            <Badge tone={MODE_TONE[ov.mode] ?? "default"}>{ov.mode}</Badge>
          </div>
          <p className="mt-2 max-w-2xl text-sm text-slate-500">
            Scientific Director &amp; Domain Expert — {g.status}. Frames the mission, scores &amp;
            ranks directions against the field model, and steers the standing agenda.
          </p>
        </div>
        <button
          onClick={load}
          className="inline-flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm hover:bg-slate-50"
        >
          <RefreshCw className={cx("h-4 w-4", loading && "animate-spin")} /> Refresh
        </button>
      </div>

      {/* At a glance */}
      <Card>
        <SectionTitle icon={Compass} title="At a glance" subtitle={g.status} />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Active directions" value={g.active_directions} hint={g.retired_directions ? `${g.retired_directions} retired` : undefined} />
          <Stat label="Claim goals" value={g.claim_goals} />
          <Stat label="Strategic lessons" value={g.lessons} />
          <Stat label="Mode" value={ov.mode} />
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
            <div className="text-xs font-medium text-slate-500">Top priority</div>
            <div className="mt-0.5 text-sm font-medium text-slate-900">{g.top_priority ?? "—"}</div>
          </div>
          <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
            <div className="text-xs font-medium text-slate-500">Focus (hot &amp; emerging)</div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {g.focus.map((f) => <Badge key={f} tone="blue">{f}</Badge>)}
            </div>
          </div>
        </div>
      </Card>

      {/* Mission */}
      {ov.mission && (
        <Card>
          <SectionTitle icon={Flag} title="Research mission" />
          <p className="text-sm leading-relaxed text-slate-700">{ov.mission.statement}</p>
        </Card>
      )}

      {/* Directions + priority gate */}
      <Card>
        <SectionTitle
          icon={Target}
          title="Direction tree"
          subtitle={`ranked by composite · auto-approved gate: ${g.approved}/${g.gate_budget} in active research`}
        />
        <div className="space-y-3">
          {ov.directions.map((d, i) => (
            <div key={d.id} className={cx("rounded-2xl border p-4", d.retired ? "border-slate-200 bg-slate-50 opacity-70" : "border-slate-200 bg-white")}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs text-slate-400">#{i + 1}</span>
                {d.priority && <Badge tone={PRIORITY_TONE[d.priority] ?? "default"}>{d.priority} · {d.composite}</Badge>}
                {!d.scores && <Badge tone="default">unscored</Badge>}
                {d.retired ? <Badge tone="red">retired</Badge> : <Badge tone={GATE_TONE[d.gate] ?? "default"}>{d.gate}</Badge>}
                <span className="font-medium text-slate-900">{d.title}</span>
                <span className="ml-auto text-xs text-slate-400">{d.n_goals} goal{d.n_goals === 1 ? "" : "s"}</span>
              </div>
              {d.statement && <p className="mt-1 text-sm text-slate-600">{d.statement}</p>}
              {d.retired && d.invalidation_reason && (
                <p className="mt-1 text-xs text-red-600">retired: {d.invalidation_reason}</p>
              )}
              {d.scores && <ScoreBars scores={d.scores} />}
            </div>
          ))}
          {ov.directions.length === 0 && <div className="text-sm text-slate-400">No directions framed yet.</div>}
        </div>
      </Card>

      {/* Conversations with Mimir — the back-and-forth behind each deliberation */}
      <Card>
        <SectionTitle
          icon={MessagesSquare}
          title="Conversations with Mimir"
          subtitle="the back-and-forth behind each deliberation — her multi-hop question, Mimir's grounded answer + gaps, the agenda she framed"
        />
        {convos.length === 0 ? (
          <div className="text-sm text-slate-400">
            No captured conversations yet — they appear as Ariadne deliberates (each deliberation opens by asking Mimir a multi-hop question).
          </div>
        ) : (
          <div className="space-y-4">
            {convos.map((c) => <ConversationThread key={c.session_id} c={c} />)}
          </div>
        )}
      </Card>

      {/* Field model */}
      {fm && (
        <Card>
          <SectionTitle
            icon={TrendingUp}
            title="Field model — the AI/ML landscape"
            subtitle={`trend = share shift ${fm.windows.prior}→${fm.windows.recent} · ${fm.counts.hot ?? 0} hot · ${fm.counts.emerging ?? 0} emerging · ${fm.counts.saturated ?? 0} saturated · ${fm.counts.declining ?? 0} declining`}
          />
          <div className="grid gap-4 lg:grid-cols-2">
            {TREND.map((t) => (
              <div key={t.key}>
                <div className="mb-2 flex items-center gap-2">
                  <span className={cx("h-2.5 w-2.5 rounded-full", t.dot)} />
                  <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">{t.label}</span>
                  <span className="text-xs text-slate-400">{fm.counts[t.key] ?? 0}</span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {fm.by_state[t.key].map((c) => <ConceptChip key={`${c.kind}-${c.name}`} c={c} />)}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Lessons */}
      <Card>
        <SectionTitle icon={Lightbulb} title="Strategic lessons" subtitle="distilled by reflection, fed back into framing" />
        {ov.lessons.length === 0 ? (
          <div className="text-sm text-slate-400">No lessons yet — they accrue as Ariadne reflects on the standing agenda.</div>
        ) : (
          <ul className="space-y-2">
            {ov.lessons.map((l, i) => (
              <li key={i} className="flex items-start gap-2 text-sm">
                <Badge tone={l.status === "active" ? "green" : "amber"}>{l.status}</Badge>
                <div>
                  <span className="text-slate-700">{l.lesson}</span>
                  {l.when && <span className="text-slate-400"> · when: {l.when}</span>}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
