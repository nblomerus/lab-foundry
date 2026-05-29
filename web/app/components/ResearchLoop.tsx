"use client";

import { useEffect, useMemo, useState } from "react";
import {
  BrainCircuit, Database, GitBranch, Layers3,
  Search, ShieldCheck, Target, Wallet, type LucideIcon,
} from "lucide-react";
import { api } from "../lib/api";
import { useEventStream } from "../lib/ws";
import type { Claim, Snapshot } from "../lib/types";
import type { PowerSummary } from "./LiveFlow";
import { Badge, Card, cx } from "./ui";

// =========================================================================
// Geometry — polar layout in a square SVG (0..100). Center (50,50).
// angle a: 0 = top, increasing clockwise. r in % of half-extent.
// =========================================================================

const CX = 50, CY = 50;
const HUB_R = 15;     // knowledge core radius
const RING_R = 36;    // stage centres
const RIM_R = 47;     // Quartermaster boundary

function polar(a: number, r: number): { x: number; y: number } {
  const rad = (a * Math.PI) / 180;
  return { x: CX + r * Math.sin(rad), y: CY - r * Math.cos(rad) };
}

// =========================================================================
// Stage model — each maps to an org division.
// =========================================================================

interface Stage {
  id: string;
  label: string;
  division: string;
  icon: LucideIcon;
  roles: string[];          // org_roles that power it
  events: string[];         // event types that pulse it
  angle: number;            // 0 = top, clockwise
}

const STAGES: Stage[] = [
  { id: "question",   label: "Question",   division: "Research & Discovery", icon: GitBranch,   roles: ["planner"],                    events: ["queue.empty", "task.created"],                                            angle: 0 },
  { id: "gather",     label: "Gather",     division: "Research & Discovery", icon: Search,      roles: ["researcher"],                 events: ["task.completed"],                                                          angle: 72 },
  { id: "judge",      label: "Judge",      division: "Quality & Review",     icon: ShieldCheck, roles: ["evaluation", "critic"],       events: ["finding.high_signal", "audit.slop_detected", "thesis.invalidated"],         angle: 144 },
  { id: "synthesise", label: "Synthesise", division: "Leadership",           icon: BrainCircuit,roles: ["pi"],                         events: ["thesis.created", "thesis.invalidated"],                                    angle: 216 },
  { id: "converge",   label: "Converge",   division: "Leadership",           icon: Layers3,     roles: ["phase_adjudicator"],          events: ["thesis.confidence_changed", "phase.transition_proposed"],                  angle: 288 },
];

interface KgStats {
  claims: number; findings: number; verdicts: number;
}

type Selection =
  | { kind: "stage"; stage: Stage }
  | { kind: "hub" }
  | { kind: "claim"; claim: Claim }
  | null;

// =========================================================================
// Top-level
// =========================================================================

export function ResearchLoop({ snapshot, power }: { snapshot: Snapshot; power?: PowerSummary | null }) {
  const { recent, connected } = useEventStream(40);
  const [kg, setKg] = useState<KgStats | null>(null);
  const [sel, setSel] = useState<Selection>(null);

  // Hub counts from the live Neo4j graph (best-effort).
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api.graphStats().then((s) => {
        if (!cancelled && s.status === "ok" && s.nodes) setKg(s.nodes);
      }).catch(() => {});
    load();
    const id = setInterval(load, 8_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Recent event types → which stages/spokes are "hot" right now.
  const hotEvents = useMemo(() => {
    const now = Date.now();
    const set = new Set<string>();
    for (const m of recent) {
      if (m.type !== "event") continue;
      if (now - new Date(m.event.emitted_at).getTime() < 8_000) set.add(m.event.event_type);
    }
    return set;
  }, [recent]);

  const roleByName = useMemo(
    () => new Map(snapshot.org_roles.map((r) => [r.role, r])),
    [snapshot.org_roles],
  );

  function stageActivity(s: Stage): { running: number; today: number; hot: boolean } {
    let running = 0, today = 0;
    for (const role of s.roles) {
      const r = roleByName.get(role);
      running += r?.running_count ?? 0;
      today += r?.runs_today ?? 0;
    }
    const hot = running > 0 || s.events.some((e) => hotEvents.has(e));
    return { running, today, hot };
  }

  const totalRunning = snapshot.org_roles.reduce((a, r) => a + r.running_count, 0);

  // Claims orbiting the hub: high confidence pulled toward the core; size by
  // evidence; spread evenly by index. Active claims only.
  const orbits = useMemo(() => {
    const claims = snapshot.active_claims.slice(0, 12);
    return claims.map((c, i) => {
      const conf = Math.max(0, Math.min(1, c.confidence));
      const r = HUB_R + 4 + (1 - conf) * (RING_R - HUB_R - 9);
      const angle = (360 / Math.max(claims.length, 1)) * i + 18;
      const p = polar(angle, r);
      const size = 1.1 + Math.min(2.2, Math.sqrt(c.finding_count) * 0.35);
      const challenged = c.contradicting_count > c.supporting_count && c.contradicting_count > 0;
      return { claim: c, ...p, size, challenged };
    });
  }, [snapshot.active_claims]);

  const cost = snapshot.cost;
  const capped = cost.cap_reached;

  return (
    <div className="space-y-4">
      {/* Banner */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-3xl border border-slate-200 bg-white/85 p-3 shadow-sm backdrop-blur">
        <div className="flex items-center gap-2 text-xs">
          <span className={cx("inline-block h-2 w-2 rounded-full", totalRunning > 0 ? "bg-emerald-500 animate-pulse" : "bg-slate-300")} />
          <span className="font-semibold text-slate-700">Research loop</span>
          <span className="text-slate-400">·</span>
          <span className="text-slate-500">knowledge at the core · hypotheses orbiting · {connected ? "live" : "reconnecting"}</span>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <Chip label="In flight" value={String(totalRunning)} tone={totalRunning > 0 ? "green" : "default"} />
          <Chip label="Hypotheses" value={String(snapshot.active_claims.length)} tone="blue" />
          <Chip label="Findings 24h" value={String(snapshot.stats.findings_today)} tone="green" />
          <Chip label="Spend" value={`$${cost.total_cost_usd.toFixed(2)}`} tone={capped ? "red" : "default"} />
          <Chip label="Phase" value={snapshot.state.current_phase} tone="amber" />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        {/* The ring */}
        <section className="lg:col-span-8">
          <Card className="p-2">
            <div className="relative mx-auto aspect-square w-full max-w-[680px]">
              <Ring
                stages={STAGES}
                orbits={orbits}
                activity={stageActivity}
                totalRunning={totalRunning}
                capped={capped}
              />
              <StageCards stages={STAGES} activity={stageActivity} sel={sel} onSelect={setSel} />
              <HubCard kg={kg} phase={snapshot.state.current_phase} onSelect={() => setSel({ kind: "hub" })} selected={sel?.kind === "hub"} />
              <OrbitDots orbits={orbits} sel={sel} onSelect={(c) => setSel({ kind: "claim", claim: c })} />
              <RimMeter cost={cost} power={power} />
            </div>
          </Card>
        </section>

        {/* Inspector */}
        <aside className="lg:col-span-4">
          <Inspector sel={sel} activity={stageActivity} kg={kg} snapshot={snapshot} power={power} />
        </aside>
      </div>
    </div>
  );
}

// =========================================================================
// SVG ring: stroke circle, spokes, clockwise particles, orbit guides.
// =========================================================================

function Ring({
  stages, orbits, activity, totalRunning, capped,
}: {
  stages: Stage[];
  orbits: { x: number; y: number }[];
  activity: (s: Stage) => { running: number; today: number; hot: boolean };
  totalRunning: number;
  capped: boolean;
}) {
  // Ring as an SVG circle path (clockwise) for particle animateMotion.
  const ringPath = useMemo(() => {
    const top = polar(0, RING_R);
    // Two arcs make a full circle; sweep-flag 1 = clockwise.
    return `M ${top.x} ${top.y} A ${RING_R} ${RING_R} 0 1 1 ${top.x} ${top.y - 0.01} Z`;
  }, []);

  const particleCount = totalRunning > 0 ? 4 : 2;
  const dur = totalRunning > 0 ? 9 : 16;

  return (
    <svg viewBox="0 0 100 100" className="absolute inset-0 h-full w-full overflow-visible">
      <defs>
        <radialGradient id="hubGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="rgba(16,185,129,0.18)" />
          <stop offset="100%" stopColor="rgba(16,185,129,0)" />
        </radialGradient>
      </defs>

      {/* hub glow */}
      <circle cx={CX} cy={CY} r={HUB_R + 8} fill="url(#hubGlow)" />

      {/* spokes hub → each stage */}
      {stages.map((s) => {
        const p = polar(s.angle, RING_R - 7);
        const h = polar(s.angle, HUB_R + 1);
        const { hot } = activity(s);
        return (
          <line key={s.id} x1={h.x} y1={h.y} x2={p.x} y2={p.y}
            stroke={hot ? "#10b981" : "#cbd5e1"} strokeWidth={hot ? 0.7 : 0.4}
            strokeDasharray="1.2 1.4" opacity={hot ? 0.9 : 0.5} />
        );
      })}

      {/* the loop ring */}
      <circle cx={CX} cy={CY} r={RING_R} fill="none" stroke="#94a3b8" strokeWidth={0.6} opacity={0.5} />

      {/* outer rim (Quartermaster boundary) */}
      <circle cx={CX} cy={CY} r={RIM_R} fill="none"
        stroke={capped ? "#ef4444" : "#cbd5e1"} strokeWidth={capped ? 1.0 : 0.5}
        strokeDasharray="0.6 1.6" opacity={capped ? 0.9 : 0.55} />

      {/* clockwise particles riding the ring */}
      {Array.from({ length: particleCount }).map((_, i) => (
        <circle key={i} r={0.7} fill="#10b981" opacity={0.85}
          style={{ filter: "drop-shadow(0 0 0.8px rgba(16,185,129,0.8))" }}>
          <animateMotion dur={`${dur}s`} repeatCount="indefinite"
            begin={`${(i * dur) / particleCount}s`} path={ringPath} rotate="auto" />
        </circle>
      ))}

      {/* faint orbit guide rings */}
      <circle cx={CX} cy={CY} r={(HUB_R + RING_R) / 2} fill="none" stroke="#e2e8f0" strokeWidth={0.3} opacity={0.6} />
    </svg>
  );
}

// =========================================================================
// HTML overlays — stage cards, hub, orbit dots, rim meter.
// =========================================================================

function atPct(a: number, r: number) {
  const p = polar(a, r);
  return { left: `${p.x}%`, top: `${p.y}%` };
}

function StageCards({
  stages, activity, sel, onSelect,
}: {
  stages: Stage[];
  activity: (s: Stage) => { running: number; today: number; hot: boolean };
  sel: Selection;
  onSelect: (s: Selection) => void;
}) {
  return (
    <>
      {stages.map((s) => {
        const { running, today, hot } = activity(s);
        const Icon = s.icon;
        const selected = sel?.kind === "stage" && sel.stage.id === s.id;
        return (
          <button
            key={s.id}
            type="button"
            onClick={() => onSelect({ kind: "stage", stage: s })}
            className={cx(
              "absolute z-20 flex w-[120px] -translate-x-1/2 -translate-y-1/2 flex-col items-center gap-1 rounded-2xl border bg-white p-2 text-center shadow-sm transition",
              hot ? "border-emerald-300 ring-2 ring-emerald-400/20" : "border-slate-200",
              selected ? "ring-4 ring-emerald-500/20 shadow-lg" : "",
            )}
            style={atPct(s.angle, RING_R)}
          >
            <div className={cx("rounded-xl p-1.5", hot ? "bg-emerald-50" : "bg-slate-50")}>
              <Icon className={cx("h-4 w-4", hot ? "text-emerald-600" : "text-slate-700")} />
            </div>
            <div className="text-[12px] font-semibold leading-tight text-slate-950">{s.label}</div>
            <div className="text-[9px] uppercase tracking-wide text-slate-400">{s.division}</div>
            <Badge tone={running > 0 ? "green" : today > 0 ? "blue" : "default"}>
              {running > 0 ? `${running} live` : today > 0 ? `${today} today` : "idle"}
            </Badge>
          </button>
        );
      })}
    </>
  );
}

function HubCard({
  kg, phase, onSelect, selected,
}: {
  kg: KgStats | null;
  phase: string;
  onSelect: () => void;
  selected: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cx(
        "absolute left-1/2 top-1/2 z-20 flex h-[26%] w-[26%] -translate-x-1/2 -translate-y-1/2 flex-col items-center justify-center gap-0.5 rounded-full border bg-white text-center shadow-md transition",
        selected ? "border-emerald-300 ring-4 ring-emerald-500/20" : "border-slate-200",
      )}
    >
      <Database className="h-4 w-4 text-emerald-600" />
      <div className="text-[11px] font-semibold leading-tight text-slate-950">Knowledge</div>
      <div className="text-[9px] text-slate-500">
        {kg ? `${kg.claims}c · ${kg.findings}f` : "RAG + KG"}
      </div>
      <div className="mt-0.5 rounded-full bg-slate-100 px-1.5 py-0.5 text-[8px] font-semibold uppercase tracking-wide text-slate-500">
        {phase}
      </div>
    </button>
  );
}

function OrbitDots({
  orbits, sel, onSelect,
}: {
  orbits: { claim: Claim; x: number; y: number; size: number; challenged: boolean }[];
  sel: Selection;
  onSelect: (c: Claim) => void;
}) {
  return (
    <>
      {orbits.map((o) => {
        const selected = sel?.kind === "claim" && sel.claim.id === o.claim.id;
        const conf = o.claim.confidence;
        const tone = o.challenged ? "bg-amber-400 border-amber-500" : conf >= 0.5 ? "bg-emerald-400 border-emerald-500" : "bg-slate-300 border-slate-400";
        return (
          <button
            key={o.claim.id}
            type="button"
            title={`C${o.claim.id} · conf ${(conf * 100).toFixed(0)}% · ${o.claim.finding_count} findings`}
            onClick={() => onSelect(o.claim)}
            className={cx(
              "absolute z-10 -translate-x-1/2 -translate-y-1/2 rounded-full border shadow-sm transition hover:scale-125",
              tone,
              selected ? "ring-4 ring-emerald-500/30 scale-125" : "",
            )}
            style={{
              left: `${o.x}%`,
              top: `${o.y}%`,
              width: `${o.size * 6}px`,
              height: `${o.size * 6}px`,
              opacity: 0.55 + conf * 0.45,
            }}
          />
        );
      })}
    </>
  );
}

function RimMeter({ cost, power }: { cost: Snapshot["cost"]; power?: PowerSummary | null }) {
  // Quartermaster as a chip anchored at the top of the rim (honest labels, no
  // fake gauge fill — spend has no fixed cap value to normalise against).
  return (
    <div
      className="absolute left-1/2 top-0 z-20 -translate-x-1/2 -translate-y-1/2"
      style={atPct(0, RIM_R)}
    >
      <div className={cx(
        "flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10px] font-semibold shadow-sm backdrop-blur",
        cost.cap_reached ? "border-red-300 bg-red-50 text-red-700" : "border-slate-200 bg-white/90 text-slate-600",
      )}>
        <Wallet className="h-3 w-3" />
        ${cost.total_cost_usd.toFixed(2)}
        {power && <span className="text-slate-400">· {Math.round(power.total_watts)}W</span>}
        {cost.cap_reached && <span>· CAP</span>}
      </div>
    </div>
  );
}

// =========================================================================
// Inspector
// =========================================================================

function Inspector({
  sel, activity, kg, snapshot, power,
}: {
  sel: Selection;
  activity: (s: Stage) => { running: number; today: number; hot: boolean };
  kg: KgStats | null;
  snapshot: Snapshot;
  power?: PowerSummary | null;
}) {
  if (!sel) {
    return (
      <Card>
        <div className="text-xs uppercase tracking-wider text-slate-400">Inspector</div>
        <p className="mt-2 text-sm text-slate-500">
          Click a stage, the knowledge hub, or an orbiting hypothesis.
        </p>
        <div className="mt-4 space-y-2 text-sm text-slate-600">
          <Legend tone="bg-emerald-400" label="Supported hypothesis (conf ≥ 50%)" />
          <Legend tone="bg-amber-400" label="Under challenge" />
          <Legend tone="bg-slate-300" label="Low confidence / early" />
          <p className="pt-1 text-xs text-slate-400">
            Distance from the core ≈ confidence (closer = stronger). Size ≈ evidence.
          </p>
        </div>
      </Card>
    );
  }

  if (sel.kind === "hub") {
    return (
      <Card>
        <Head icon={Database} title="Knowledge core" sub="RAG corpus + evidence graph" />
        <Rows rows={[
          ["Knowledge graph", kg ? `${kg.claims} claims · ${kg.findings} findings · ${kg.verdicts} verdicts` : "Neo4j (loading)"],
          ["RAG corpus", "papers · media · datasets (planned)"],
          ["Phase", snapshot.state.current_phase],
          ["Read/write by", "every stage of the loop"],
        ]} />
      </Card>
    );
  }

  if (sel.kind === "claim") {
    const c = sel.claim;
    return (
      <Card>
        <Head icon={Target} title={`Hypothesis C${c.id}`} sub={`confidence ${(c.confidence * 100).toFixed(0)}%`} />
        <p className="mb-3 text-sm font-medium leading-snug text-slate-900">{c.claim}</p>
        <Rows rows={[
          ["Status", c.status],
          ["Findings", String(c.finding_count)],
          ["Supporting", String(c.supporting_count)],
          ["Contradicting", String(c.contradicting_count)],
        ]} />
      </Card>
    );
  }

  const s = sel.stage;
  const { running, today } = activity(s);
  const Icon = s.icon;
  return (
    <Card>
      <Head icon={Icon} title={s.label} sub={s.division} />
      <Rows rows={[
        ["Agents", s.roles.map((r) => r.replace(/_/g, " ")).join(", ")],
        ["Running now", String(running)],
        ["Runs today", String(today)],
        ["Reacts to", s.events.join(", ")],
        ...(s.id === "converge" ? [["Resources", power ? `${Math.round(power.total_watts)}W · ${power.gpu_count} GPU` : "—"] as [string, string]] : []),
      ]} />
    </Card>
  );
}

// =========================================================================
// Small bits
// =========================================================================

function Chip({ label, value, tone }: { label: string; value: string; tone: "green" | "amber" | "red" | "blue" | "default" }) {
  return (
    <div className="rounded-2xl bg-slate-50 px-3 py-1.5">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <Badge tone={tone}>{value}</Badge>
    </div>
  );
}

function Head({ icon: Icon, title, sub }: { icon: LucideIcon; title: string; sub: string }) {
  return (
    <div className="mb-3 flex items-center gap-3">
      <div className="rounded-2xl border border-slate-200 bg-slate-50 p-2">
        <Icon className="h-4 w-4 text-slate-700" />
      </div>
      <div>
        <h3 className="text-lg font-semibold tracking-tight text-slate-950">{title}</h3>
        <div className="text-xs text-slate-500">{sub}</div>
      </div>
    </div>
  );
}

function Rows({ rows }: { rows: [string, string][] }) {
  return (
    <div className="space-y-2">
      {rows.map(([k, v], i) => (
        <div key={i} className="rounded-2xl bg-slate-50 px-3 py-2">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{k}</div>
          <div className="mt-0.5 text-sm text-slate-700">{v}</div>
        </div>
      ))}
    </div>
  );
}

function Legend({ tone, label }: { tone: string; label: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className={cx("inline-block h-3 w-3 rounded-full", tone)} />
      <span>{label}</span>
    </div>
  );
}
