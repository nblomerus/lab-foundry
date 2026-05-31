"use client";

import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BrainCircuit,
  CheckCircle2,
  CircleDot,
  Database,
  FlaskConical,
  Gauge,
  Library,
  Search,
  ShieldCheck,
  Target,
  Wallet,
  XCircle,
} from "lucide-react";
import type {
  Claim,
  Cost,
  DissentItem,
  Finding,
  OrgRole,
  PhaseTransition,
  Snapshot,
  Stats,
} from "../lib/types";
import { Badge, Card, SectionTitle, cx } from "./ui";

type Tone = "default" | "green" | "amber" | "blue" | "red" | "dark";

// =========================================================================
// Health cards — five top-line org vitals, all derived from the snapshot.
// =========================================================================

interface HealthCard {
  label: string;
  status: string;
  value: string;
  helper: string;
  tone: Tone;
  icon: typeof Target;
}

function buildHealthCards(snap: Snapshot): HealthCard[] {
  const { stats, state, org_roles, cost } = snap;

  // Evidence health — share of audited findings today that cleared the bar.
  const audited = stats.high_signal_today + stats.slop_today;
  const passRate = audited > 0 ? Math.round((stats.high_signal_today / audited) * 100) : null;

  // System health — agents that have run today, out of the known roster.
  const activeAgents = org_roles.filter(
    (r) => r.running_count > 0 || r.runs_today > 0,
  ).length;
  const totalAgents = Math.max(org_roles.length, 1);

  // Attention — failed runs plus any proposed (un-decided) phase transition.
  const pendingTransition = snap.phase_transitions.some(
    (t) => !t.decided_at,
  );
  const attention = stats.failed_runs_today + (pendingTransition ? 1 : 0);

  return [
    {
      label: "Research Progress",
      status: state.active_claims_count > 0 ? "Progressing" : "Starting",
      value: String(state.active_claims_count),
      helper: "Active candidate directions",
      tone: "green",
      icon: Target,
    },
    {
      label: "System Health",
      status: activeAgents >= totalAgents - 1 ? "Healthy" : "Degraded",
      value: `${activeAgents}/${totalAgents}`,
      helper: "Agents active today",
      tone: activeAgents >= totalAgents - 1 ? "green" : "amber",
      icon: Activity,
    },
    {
      label: "Evidence Health",
      status: passRate == null ? "No data" : passRate >= 80 ? "Healthy" : "Watch",
      value: passRate == null ? "—" : `${passRate}%`,
      helper: "Audit pass rate today",
      tone: passRate == null ? "default" : passRate >= 80 ? "green" : "amber",
      icon: ShieldCheck,
    },
    {
      label: "Budget",
      status: cost.cap_reached ? "Cap reached" : "Healthy",
      value: `$${cost.total_cost_usd.toFixed(2)}`,
      helper: "Spend today",
      tone: cost.cap_reached ? "red" : "green",
      icon: Wallet,
    },
    {
      label: "Attention",
      status: attention > 0 ? "Needs review" : "Clear",
      value: String(attention),
      helper: "Items needing a human",
      tone: attention > 0 ? "red" : "green",
      icon: AlertTriangle,
    },
  ];
}

export function HealthCards({ snap }: { snap: Snapshot }) {
  const cards = buildHealthCards(snap);
  return (
    <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
      {cards.map((card) => {
        const Icon = card.icon;
        return (
          <Card key={card.label} className="p-4">
            <div className="flex items-center justify-between">
              <div className="rounded-2xl bg-slate-50 p-2">
                <Icon className="h-4 w-4 text-slate-700" />
              </div>
              <Badge tone={card.tone}>{card.status}</Badge>
            </div>
            <div className="mt-4 text-2xl font-semibold tracking-tight">{card.value}</div>
            <div className="text-sm font-medium text-slate-700">{card.label}</div>
            <div className="mt-1 text-xs text-slate-500">{card.helper}</div>
          </Card>
        );
      })}
    </section>
  );
}

// =========================================================================
// Research Portfolio — active claims as ranked hypotheses.
// =========================================================================

function claimStatus(c: Claim): { label: string; tone: Tone } {
  if (c.status !== "active" && c.status !== "proposed") {
    return { label: c.status, tone: "default" as Tone };
  }
  if (c.contradicting_count > c.supporting_count && c.contradicting_count > 0) {
    return { label: "Under challenge", tone: "amber" };
  }
  if (c.supporting_count >= 5 && c.confidence >= 0.5) {
    return { label: "Strong candidate", tone: "green" };
  }
  if (c.finding_count < 3) {
    return { label: "Needs evidence", tone: "default" };
  }
  return { label: "Promising", tone: "blue" };
}

// Evidence quality (0–10): blends how much supporting evidence exists with how
// contested it is. Honest proxy until per-claim audit scores are wired in.
function evidenceQuality(c: Claim): number {
  const support = c.supporting_count;
  const against = c.contradicting_count;
  const total = support + against;
  const balance = total > 0 ? support / total : 0.5;
  const volume = Math.min(1, c.finding_count / 20);
  return Math.round((0.6 * balance + 0.4 * volume) * 10 * 10) / 10;
}

export function ResearchPortfolio({ claims }: { claims: Claim[] }) {
  const ranked = [...claims]
    .sort((a, b) => evidenceQuality(b) - evidenceQuality(a))
    .slice(0, 5);

  return (
    <Card>
      <SectionTitle
        icon={Library}
        title="Research Portfolio"
        subtitle="Top candidate directions ranked by evidence quality, not confidence."
      />
      {ranked.length === 0 ? (
        <p className="text-sm text-slate-500">No active claims yet — the loop is still casting wide.</p>
      ) : (
        <div className="space-y-4">
          {ranked.map((c) => {
            const q = evidenceQuality(c);
            const status = claimStatus(c);
            return (
              <div key={c.id} className="rounded-3xl border border-slate-200 p-4">
                <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                  <div className="flex-1">
                    <div className="mb-2 flex flex-wrap items-center gap-2">
                      <Badge tone="dark">C{c.id}</Badge>
                      <Badge tone={status.tone}>{status.label}</Badge>
                      {c.contradicting_count > 0 && (
                        <Badge tone={c.contradicting_count > 2 ? "amber" : "default"}>
                          {c.contradicting_count} challenge{c.contradicting_count === 1 ? "" : "s"}
                        </Badge>
                      )}
                    </div>
                    <h3 className="text-base font-semibold leading-snug tracking-tight text-slate-950">
                      {c.claim}
                    </h3>
                    <div className="mt-3 grid grid-cols-3 gap-3">
                      <Metric label="Findings" value={c.finding_count} />
                      <Metric label="Supporting" value={c.supporting_count} />
                      <Metric label="Contradicting" value={c.contradicting_count} />
                    </div>
                  </div>

                  <div className="min-w-[150px] rounded-2xl bg-slate-50 p-3">
                    <div className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                      Evidence quality
                    </div>
                    <div className="mt-1 text-3xl font-semibold text-slate-950">{q}</div>
                    <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-200">
                      <div
                        className="h-full rounded-full bg-slate-950"
                        style={{ width: `${q * 10}%` }}
                      />
                    </div>
                    <div className="mt-2 text-xs text-slate-500">
                      Confidence {(c.confidence * 100).toFixed(0)}%
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// =========================================================================
// Current Organisation State — researching / testing / challenging / waiting.
// =========================================================================

export function CompanyStatePanel({
  claims,
  dissent,
  recentRuns,
  transitions,
}: {
  claims: Claim[];
  dissent: DissentItem[];
  recentRuns: Snapshot["recent_runs"];
  transitions: PhaseTransition[];
}) {
  const researching = claims.slice(0, 3).map((c) => c.claim);

  // "Testing" — distinct invocation types running/just-run, humanized.
  const testing = Array.from(
    new Set(recentRuns.slice(0, 12).map((r) => r.invocation_type)),
  )
    .slice(0, 3)
    .map((t) => t.replace(/[._]/g, " "));

  const challenging = dissent
    .slice(0, 3)
    .map((d) => d.reasoning || d.detail);

  const waiting: string[] = [];
  if (transitions.some((t) => !t.decided_at)) {
    waiting.push("Ratify the proposed phase transition");
  }
  if (claims.length >= 3) {
    waiting.push("Approve the top candidate directions");
  }
  waiting.push("Confirm exploration scope is wide enough");

  const blocks = [
    { title: "Researching", icon: Search, items: researching },
    { title: "Testing", icon: FlaskConical, items: testing },
    { title: "Challenging", icon: ShieldCheck, items: challenging },
    { title: "Waiting", icon: AlertTriangle, items: waiting },
  ];

  return (
    <Card>
      <SectionTitle
        icon={Gauge}
        title="Current Organisation State"
        subtitle="What the organization is doing right now."
      />
      <div className="grid gap-3 sm:grid-cols-2">
        {blocks.map((b) => {
          const Icon = b.icon;
          return (
            <div key={b.title} className="rounded-3xl bg-slate-50 p-4">
              <div className="mb-3 flex items-center gap-2">
                <Icon className="h-4 w-4 text-slate-700" />
                <h3 className="font-semibold text-slate-950">{b.title}</h3>
              </div>
              <div className="space-y-2">
                {b.items.length === 0 ? (
                  <p className="text-sm text-slate-400">Nothing right now.</p>
                ) : (
                  b.items.map((item, i) => (
                    <div key={i} className="flex gap-2 text-sm text-slate-600">
                      <CircleDot className="mt-1 h-3 w-3 shrink-0 text-slate-400" />
                      <span className="line-clamp-2">{item}</span>
                    </div>
                  ))
                )}
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// =========================================================================
// Evidence Health — trust signals as bars.
// =========================================================================

export function EvidenceHealth({
  stats,
  claims,
  findings,
}: {
  stats: Stats;
  claims: Claim[];
  findings: Finding[];
}) {
  const audited = stats.high_signal_today + stats.slop_today;
  const auditPass = audited > 0 ? Math.round((stats.high_signal_today / audited) * 100) : 0;

  // Source diversity — distinct sources across recent findings vs a target of 6.
  const distinctSources = new Set(findings.map((f) => f.source).filter(Boolean)).size;
  const diversity = Math.min(100, Math.round((distinctSources / 6) * 100));

  // Citation coverage — findings carrying a URL.
  const withUrl = findings.filter((f) => f.url).length;
  const citation = findings.length > 0 ? Math.round((withUrl / findings.length) * 100) : 0;

  // Challenge resolution — claims that survived contradiction (supporting still leads).
  const challenged = claims.filter((c) => c.contradicting_count > 0);
  const resolved = challenged.filter((c) => c.supporting_count >= c.contradicting_count).length;
  const resolution = challenged.length > 0 ? Math.round((resolved / challenged.length) * 100) : 100;

  // Evidence coverage — claims that have at least a few findings.
  const grounded = claims.filter((c) => c.finding_count >= 3).length;
  const coverage = claims.length > 0 ? Math.round((grounded / claims.length) * 100) : 0;

  return (
    <Card>
      <SectionTitle
        icon={ShieldCheck}
        title="Evidence Health"
        subtitle="Can we trust the research output?"
      />
      <div className="space-y-3">
        <HealthBar label="Finding audit pass rate" value={auditPass} />
        <HealthBar label="Evidence coverage" value={coverage} />
        <HealthBar label="Source diversity" value={diversity} />
        <HealthBar label="Challenge resolution" value={resolution} />
        <HealthBar label="Citation coverage" value={citation} />
      </div>
    </Card>
  );
}

// =========================================================================
// Agent Workforce — who is working, idle, or stale.
// =========================================================================

function agentStatus(r: OrgRole): { label: string; tone: Tone; helper: string } {
  if (r.running_count > 0) {
    return {
      label: "running",
      tone: "green",
      helper: `${r.running_count} in flight · ${r.runs_today} today`,
    };
  }
  if (r.runs_today > 0) {
    return { label: "active", tone: "blue", helper: `${r.runs_today} runs today` };
  }
  return { label: "idle", tone: "default", helper: "no runs today" };
}

export function AgentWorkforce({ roles }: { roles: OrgRole[] }) {
  const sorted = [...roles].sort((a, b) => b.runs_today - a.runs_today);
  return (
    <Card>
      <SectionTitle
        icon={BrainCircuit}
        title="Agent Workforce"
        subtitle="Who is working, active, or idle."
      />
      <div className="space-y-2">
        {sorted.map((r) => {
          const s = agentStatus(r);
          return (
            <div
              key={r.role}
              className="flex items-center justify-between gap-3 rounded-2xl bg-slate-50 px-3 py-2"
            >
              <div>
                <div className="text-sm font-semibold capitalize text-slate-950">
                  {r.role.replace(/_/g, " ")}
                </div>
                <div className="text-xs text-slate-500">{s.helper}</div>
              </div>
              <Badge tone={s.tone}>{s.label}</Badge>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// =========================================================================
// Budget & Burn — spend tied to output.
// =========================================================================

export function BudgetBurn({
  cost,
  findingsToday,
}: {
  cost: Cost;
  findingsToday: number;
}) {
  const calls =
    cost.reasoning_calls + cost.workhorse_calls + cost.fast_calls + cost.code_calls;
  const perFinding = findingsToday > 0 ? cost.total_cost_usd / findingsToday : null;

  return (
    <Card>
      <SectionTitle
        icon={Wallet}
        title="Budget & Burn"
        subtitle="Spend should track useful research output."
      />
      <div className="grid gap-3">
        <Metric label="Spend today" value={`$${cost.total_cost_usd.toFixed(2)}`} />
        <Metric label="Model calls today" value={calls.toLocaleString()} />
        <Metric
          label="Reasoning / Workhorse / Fast"
          value={`${cost.reasoning_calls} / ${cost.workhorse_calls} / ${cost.fast_calls}`}
        />
        <Metric
          label="Cost per finding today"
          value={perFinding == null ? "—" : `$${perFinding.toFixed(3)}`}
        />
      </div>
    </Card>
  );
}

// =========================================================================
// Attention Queue — what needs a human.
// =========================================================================

interface AttentionItem {
  title: string;
  action: string;
  tone: Tone;
}

export function AttentionQueue({
  stats,
  dissent,
  transitions,
}: {
  stats: Stats;
  dissent: DissentItem[];
  transitions: PhaseTransition[];
}) {
  const items: AttentionItem[] = [];

  const pending = transitions.find((t) => !t.decided_at);
  if (pending) {
    items.push({
      title: `Phase transition proposed: ${pending.from_phase} → ${pending.to_phase}`,
      action: pending.reason || "The system recommends advancing the phase.",
      tone: "blue",
    });
  }

  if (stats.failed_runs_today > 0) {
    items.push({
      title: `${stats.failed_runs_today} failed run${stats.failed_runs_today === 1 ? "" : "s"} today`,
      action: "Check the Trace view for the failing handler and model errors.",
      tone: "red",
    });
  }

  if (stats.slop_today > stats.high_signal_today && stats.slop_today > 0) {
    items.push({
      title: "Evidence quality dipped",
      action: `${stats.slop_today} findings flagged as slop vs ${stats.high_signal_today} high-signal today.`,
      tone: "amber",
    });
  }

  // Surface the most confident critic challenge as a review item.
  const topDissent = [...dissent]
    .filter((d) => d.confidence != null)
    .sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0))[0];
  if (topDissent) {
    items.push({
      title: `Critic challenged C${topDissent.claim_id}`,
      action: topDissent.reasoning || topDissent.detail,
      tone: "amber",
    });
  }

  return (
    <Card>
      <SectionTitle
        icon={AlertTriangle}
        title="Attention Queue"
        subtitle="What needs human attention."
      />
      {items.length === 0 ? (
        <div className="flex items-center gap-2 rounded-2xl bg-emerald-50 p-3 text-sm text-emerald-700">
          <CheckCircle2 className="h-4 w-4" /> Nothing needs a human right now.
        </div>
      ) : (
        <div className="space-y-3">
          {items.slice(0, 4).map((item, i) => (
            <div key={i} className="rounded-3xl border border-slate-200 p-4">
              <div className="font-semibold leading-snug text-slate-950">{item.title}</div>
              <p className="mt-2 line-clamp-3 text-sm leading-6 text-slate-600">{item.action}</p>
              <div className="mt-3">
                <Badge tone={item.tone}>Needs review</Badge>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// =========================================================================
// Mini Evidence Graph — sources → findings → hypotheses, compact.
// =========================================================================

export function MiniEvidenceGraph({
  findings,
  claims,
  dissent,
}: {
  findings: Finding[];
  claims: Claim[];
  dissent: DissentItem[];
}) {
  const sources = Array.from(
    new Set(findings.map((f) => f.source).filter(Boolean)),
  ).slice(0, 4) as string[];

  const findingItems = findings.slice(0, 3).map((f) => {
    const verdict =
      f.audit_verdict === "pass"
        ? "high signal"
        : f.audit_verdict === "slop"
        ? "flagged slop"
        : "unaudited";
    return `F${f.id} · ${verdict}`;
  });

  const claimItems = claims.slice(0, 3).map((c) => `C${c.id}`);

  const topChallenge = dissent[0];

  return (
    <Card>
      <SectionTitle
        icon={Database}
        title="Mini Evidence Graph"
        subtitle="How hypotheses are grounded and challenged."
      />
      <div className="grid items-center gap-4 md:grid-cols-5">
        <GraphNode
          title="Sources"
          icon={Database}
          items={sources.length ? sources : ["—"]}
        />
        <GraphArrow />
        <GraphNode
          title="Findings"
          icon={Search}
          items={findingItems.length ? findingItems : ["—"]}
        />
        <GraphArrow />
        <GraphNode
          title="Hypotheses"
          icon={Target}
          items={claimItems.length ? claimItems : ["—"]}
        />
      </div>

      {topChallenge && (
        <div className="mt-4 rounded-3xl border border-amber-200 bg-amber-50 p-4">
          <div className="flex items-start gap-3">
            <ShieldCheck className="mt-0.5 h-5 w-5 text-amber-700" />
            <div>
              <div className="font-semibold text-slate-950">Current challenge focus</div>
              <p className="mt-1 line-clamp-2 text-sm leading-6 text-slate-600">
                {topChallenge.reasoning || topChallenge.detail}
              </p>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

// =========================================================================
// Small shared helpers
// =========================================================================

function Metric({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="rounded-2xl bg-slate-50 p-3">
      <div className="text-xs font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-1 text-sm font-semibold text-slate-800">{value}</div>
    </div>
  );
}

function HealthBar({ label, value }: { label: string; value: number }) {
  const tone = value >= 80 ? "bg-emerald-500" : value >= 65 ? "bg-amber-500" : "bg-red-500";
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-sm">
        <span className="font-medium text-slate-700">{label}</span>
        <span className="font-semibold text-slate-950">{value}%</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-slate-100">
        <div className={cx("h-full rounded-full", tone)} style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}

function GraphNode({
  title,
  icon: Icon,
  items,
}: {
  title: string;
  icon: typeof Database;
  items: string[];
}) {
  return (
    <div className="rounded-3xl border border-slate-200 bg-slate-50 p-4">
      <div className="mb-3 flex items-center gap-2">
        <Icon className="h-4 w-4 text-slate-700" />
        <div className="font-semibold text-slate-950">{title}</div>
      </div>
      <div className="space-y-2">
        {items.map((item, i) => (
          <div key={i} className="truncate rounded-xl bg-white px-3 py-2 text-sm text-slate-600">
            {item}
          </div>
        ))}
      </div>
    </div>
  );
}

function GraphArrow() {
  return (
    <div className="hidden items-center justify-center md:flex">
      <ArrowRight className="h-6 w-6 text-slate-300" />
    </div>
  );
}
