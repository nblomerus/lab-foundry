"use client";

import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  BrainCircuit, Database, GitBranch, Layers3,
  Search, ShieldCheck, Target, Wallet, type LucideIcon,
} from "lucide-react";
import { api, type GraphClaim } from "../lib/api";
import { useEventStream } from "../lib/ws";
import type { Claim, Finding, DissentItem, LabFoundryEvent, Snapshot } from "../lib/types";
import { LiveFlow, type PowerSummary } from "./LiveFlow";
import { Badge, Card, cx } from "./ui";

function fmtTime(iso: string): string {
  try { return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
  catch { return "—"; }
}
function ago(iso?: string | null): string {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

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

// Which agent-graph nodes (flow-topology ids) each division spans. Drives the
// focused node-graph drill-down.
const STAGE_FOCUS: Record<string, string[]> = {
  question:   ["planner", "tasks"],
  gather:     ["ingest", "rag", "kg", "web", "researcher", "tasks", "findings"],
  judge:      ["findings", "evaluation", "critic", "claims"],
  synthesise: ["findings", "pi", "claims"],
  converge:   ["claims", "adjudicator", "phase", "pi"],
};
const HUB_FOCUS = ["ingest", "rag", "kg", "researcher"];

interface KgStats {
  claims: number; findings: number; verdicts: number;
}

type Selection =
  | { kind: "stage"; stage: Stage }
  | { kind: "hub" }
  | { kind: "claim"; claim: Claim }
  | null;

// Stable key per selection so AnimatePresence re-runs the reveal on change.
function selectionKey(sel: Selection): string {
  if (!sel) return "none";
  if (sel.kind === "stage") return `stage:${sel.stage.id}`;
  if (sel.kind === "claim") return `claim:${sel.claim.id}`;
  return "hub";
}

// =========================================================================
// Top-level
// =========================================================================

// Graph-mutating events — what the knowledge hub records.
const HUB_EVENTS = [
  "claim.created", "thesis.created", "finding.high_signal",
  "thesis.invalidated", "claim.invalidated", "thesis.confidence_changed",
];

export function ResearchLoop({ snapshot, power }: { snapshot: Snapshot; power?: PowerSummary | null }) {
  const { recent, connected } = useEventStream(60);
  const [kg, setKg] = useState<KgStats | null>(null);
  const [sel, setSel] = useState<Selection>(null);
  const [prefill, setPrefill] = useState<LabFoundryEvent[]>([]);

  // Backlog so the per-node flow isn't empty when the harness is idle.
  useEffect(() => { api.events(80).then(setPrefill).catch(() => {}); }, []);

  // Live + backlog, deduped by id, newest first.
  const events = useMemo(() => {
    const live = recent
      .filter((m): m is Extract<typeof m, { type: "event" }> => m.type === "event")
      .map((m) => m.event);
    const seen = new Set<number>();
    const out: LabFoundryEvent[] = [];
    for (const e of [...live, ...prefill]) {
      if (seen.has(e.id)) continue;
      seen.add(e.id);
      out.push(e);
    }
    return out.sort((a, b) => new Date(b.emitted_at).getTime() - new Date(a.emitted_at).getTime());
  }, [recent, prefill]);

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
          <Card className="overflow-hidden p-2">
            <div
              className="relative mx-auto aspect-square w-full max-w-[680px] rounded-[1.75rem] bg-[radial-gradient(circle_at_50%_50%,_rgba(16,185,129,0.05),_transparent_55%),radial-gradient(circle_at_50%_50%,_#ffffff,_#f6f9fc)]"
              onClick={() => setSel(null)}
            >
              <Ring
                stages={STAGES}
                orbits={orbits}
                activity={stageActivity}
                totalRunning={totalRunning}
                capped={capped}
                sel={sel}
                phase={snapshot.state.current_phase}
              />
              <StageCards stages={STAGES} activity={stageActivity} sel={sel} onSelect={setSel} />
              <HubCard kg={kg} phase={snapshot.state.current_phase} onSelect={() => setSel({ kind: "hub" })} selected={sel?.kind === "hub"} dimmed={sel != null && sel.kind !== "hub"} />
              <OrbitDots orbits={orbits} sel={sel} onSelect={(c) => setSel({ kind: "claim", claim: c })} />
              <RimMeter cost={cost} power={power} />
            </div>
          </Card>
        </section>

        {/* Inspector — animates in when the selection changes (the reveal). */}
        <aside className="lg:col-span-4">
          <AnimatePresence mode="wait">
            <motion.div
              key={selectionKey(sel)}
              initial={{ opacity: 0, x: 12 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -12 }}
              transition={{ duration: 0.18, ease: "easeOut" }}
            >
              <Inspector sel={sel} activity={stageActivity} kg={kg} snapshot={snapshot} power={power} events={events} />
            </motion.div>
          </AnimatePresence>
        </aside>
      </div>

      {/* Drill-down: clicking a division reveals the full agent-node graph,
          focused on that division's nodes — the source→agent interactions. */}
      <AnimatePresence initial={false}>
        {(sel?.kind === "stage" || sel?.kind === "hub") && (
          <motion.div
            key={selectionKey(sel)}
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.25, ease: "easeOut" }}
            className="overflow-hidden"
          >
            <Card>
              <div className="mb-3 flex items-center justify-between">
                <div>
                  <h3 className="text-base font-semibold tracking-tight text-slate-950">
                    Agent interactions ·{" "}
                    {sel.kind === "hub" ? "Knowledge" : sel.stage.label}
                  </h3>
                  <p className="mt-0.5 text-sm text-slate-500">
                    The full pipeline — sources → agents → downstream. Highlighted nodes belong to this division.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setSel(null)}
                  className="rounded-2xl border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
                >
                  Close
                </button>
              </div>
              <LiveFlow
                key={`flow-${selectionKey(sel)}`}
                snapshot={snapshot}
                power={power}
                focusNodeIds={sel.kind === "hub" ? HUB_FOCUS : STAGE_FOCUS[sel.stage.id] ?? []}
              />
            </Card>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// =========================================================================
// SVG ring: stroke circle, spokes, clockwise particles, orbit guides.
// =========================================================================

const PHASE_HUE: Record<string, [string, string]> = {
  exploration: ["#34d399", "#60a5fa"], // emerald → blue
  convergence: ["#60a5fa", "#a78bfa"], // blue → violet
  commitment:  ["#a78bfa", "#f59e0b"], // violet → amber
  execution:   ["#f59e0b", "#10b981"], // amber → emerald
};

function Ring({
  stages, orbits, activity, totalRunning, capped, sel, phase,
}: {
  stages: Stage[];
  orbits: { x: number; y: number }[];
  activity: (s: Stage) => { running: number; today: number; hot: boolean };
  totalRunning: number;
  capped: boolean;
  sel: Selection;
  phase: string;
}) {
  // Ring as an SVG circle path (clockwise) for particle animateMotion.
  const ringPath = useMemo(() => {
    const top = polar(0, RING_R);
    return `M ${top.x} ${top.y} A ${RING_R} ${RING_R} 0 1 1 ${top.x} ${top.y - 0.01} Z`;
  }, []);

  const particleCount = totalRunning > 0 ? 4 : 2;
  const dur = totalRunning > 0 ? 9 : 16;
  const [c0, c1] = PHASE_HUE[phase] ?? PHASE_HUE.exploration;
  const selStageId = sel?.kind === "stage" ? sel.stage.id : null;
  const focusing = sel != null;

  return (
    <svg viewBox="0 0 100 100" className="absolute inset-0 h-full w-full overflow-visible">
      <defs>
        <radialGradient id="hubGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="rgba(16,185,129,0.22)" />
          <stop offset="100%" stopColor="rgba(16,185,129,0)" />
        </radialGradient>
        <linearGradient id="ringGrad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor={c0} />
          <stop offset="100%" stopColor={c1} />
        </linearGradient>
      </defs>

      {/* hub glow */}
      <circle cx={CX} cy={CY} r={HUB_R + 9} fill="url(#hubGlow)" />

      {/* spokes hub → each stage */}
      {stages.map((s) => {
        const p = polar(s.angle, RING_R - 7);
        const h = polar(s.angle, HUB_R + 1);
        const { hot } = activity(s);
        const isSel = selStageId === s.id;
        const active = hot || isSel;
        const dim = focusing && !isSel;
        return (
          <line key={s.id} x1={h.x} y1={h.y} x2={p.x} y2={p.y}
            stroke={active ? "#10b981" : "#cbd5e1"}
            strokeWidth={isSel ? 0.9 : hot ? 0.7 : 0.4}
            strokeDasharray="1.2 1.4"
            opacity={dim ? 0.2 : active ? 0.95 : 0.5} />
        );
      })}

      {/* the loop ring — phase-tinted gradient */}
      <circle cx={CX} cy={CY} r={RING_R} fill="none" stroke="url(#ringGrad)" strokeWidth={1.0} opacity={0.7} />
      <circle cx={CX} cy={CY} r={RING_R} fill="none" stroke="url(#ringGrad)" strokeWidth={3.0} opacity={0.12} />

      {/* outer rim (Quartermaster boundary) */}
      <circle cx={CX} cy={CY} r={RIM_R} fill="none"
        stroke={capped ? "#ef4444" : "#cbd5e1"} strokeWidth={capped ? 1.0 : 0.5}
        strokeDasharray="0.6 1.6" opacity={capped ? 0.9 : 0.5} />

      {/* clockwise particles riding the ring */}
      {Array.from({ length: particleCount }).map((_, i) => (
        <circle key={i} r={i === 0 ? 0.9 : 0.65} fill="#10b981" opacity={0.9}
          style={{ filter: "drop-shadow(0 0 1px rgba(16,185,129,0.85))" }}>
          <animateMotion dur={`${dur}s`} repeatCount="indefinite"
            begin={`${(i * dur) / particleCount}s`} path={ringPath} rotate="auto" />
        </circle>
      ))}

      {/* faint orbit guide ring */}
      <circle cx={CX} cy={CY} r={(HUB_R + RING_R) / 2} fill="none" stroke="#e2e8f0" strokeWidth={0.3} opacity={0.55} />
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
  const focusing = sel != null;
  return (
    <>
      {stages.map((s, i) => {
        const { running, today, hot } = activity(s);
        const Icon = s.icon;
        const selected = sel?.kind === "stage" && sel.stage.id === s.id;
        const dimmed = focusing && !selected;
        const pos = atPct(s.angle, RING_R);
        return (
          <motion.button
            key={s.id}
            type="button"
            onClick={(e) => { e.stopPropagation(); onSelect({ kind: "stage", stage: s }); }}
            initial={{ opacity: 0, scale: 0.85 }}
            animate={{ opacity: dimmed ? 0.4 : 1, scale: selected ? 1.08 : 1 }}
            transition={{ type: "spring", stiffness: 260, damping: 22, delay: i * 0.04 }}
            whileHover={{ scale: selected ? 1.08 : 1.05 }}
            className={cx(
              "absolute z-20 flex w-[124px] -translate-x-1/2 -translate-y-1/2 flex-col items-center gap-1 rounded-2xl border bg-white/95 p-2 text-center shadow-sm backdrop-blur transition-colors",
              hot ? "border-emerald-300" : "border-slate-200",
              selected ? "shadow-xl ring-4 ring-emerald-500/25" : hot ? "ring-2 ring-emerald-400/20" : "",
            )}
            style={{ left: pos.left, top: pos.top }}
          >
            {hot && (
              <motion.span
                className="absolute -inset-1 rounded-[1.1rem] border border-emerald-300"
                animate={{ opacity: [0.15, 0.7, 0.15] }}
                transition={{ duration: 1.6, repeat: Infinity }}
              />
            )}
            <div className={cx("relative rounded-xl p-1.5", hot ? "bg-emerald-50" : "bg-slate-50")}>
              <Icon className={cx("h-4 w-4", hot ? "text-emerald-600" : "text-slate-700")} />
            </div>
            <div className="relative text-[12px] font-semibold leading-tight text-slate-950">{s.label}</div>
            <div className="relative text-[9px] uppercase tracking-wide text-slate-400">{s.division}</div>
            <div className="relative">
              <Badge tone={running > 0 ? "green" : today > 0 ? "blue" : "default"}>
                {running > 0 ? `${running} live` : today > 0 ? `${today} today` : "idle"}
              </Badge>
            </div>
          </motion.button>
        );
      })}
    </>
  );
}

function HubCard({
  kg, phase, onSelect, selected, dimmed,
}: {
  kg: KgStats | null;
  phase: string;
  onSelect: () => void;
  selected: boolean;
  dimmed: boolean;
}) {
  return (
    <motion.button
      type="button"
      onClick={(e) => { e.stopPropagation(); onSelect(); }}
      animate={{ opacity: dimmed ? 0.5 : 1, scale: selected ? 1.06 : 1 }}
      whileHover={{ scale: selected ? 1.06 : 1.04 }}
      transition={{ type: "spring", stiffness: 240, damping: 20 }}
      className={cx(
        "absolute left-1/2 top-1/2 z-20 flex h-[27%] w-[27%] -translate-x-1/2 -translate-y-1/2 flex-col items-center justify-center gap-0.5 rounded-full border text-center shadow-md transition-colors",
        "bg-[radial-gradient(circle_at_50%_35%,_#ffffff,_#ecfdf5)]",
        selected ? "border-emerald-300 ring-4 ring-emerald-500/25" : "border-emerald-200/70",
      )}
    >
      <div className="rounded-xl bg-emerald-50 p-1.5">
        <Database className="h-4 w-4 text-emerald-600" />
      </div>
      <div className="text-[11px] font-semibold leading-tight text-slate-950">Knowledge</div>
      <div className="text-[9px] text-slate-500">
        {kg ? `${kg.claims}c · ${kg.findings}f · ${kg.verdicts}v` : "RAG + KG"}
      </div>
      <div className="mt-0.5 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[8px] font-semibold uppercase tracking-wide text-emerald-700">
        {phase}
      </div>
    </motion.button>
  );
}

function OrbitDots({
  orbits, sel, onSelect,
}: {
  orbits: { claim: Claim; x: number; y: number; size: number; challenged: boolean }[];
  sel: Selection;
  onSelect: (c: Claim) => void;
}) {
  const focusingOther = sel != null && sel.kind !== "claim";
  return (
    <>
      {orbits.map((o) => {
        const selected = sel?.kind === "claim" && sel.claim.id === o.claim.id;
        const conf = o.claim.confidence;
        const tone = o.challenged ? "bg-amber-400 border-amber-500" : conf >= 0.5 ? "bg-emerald-400 border-emerald-500" : "bg-slate-300 border-slate-400";
        const baseOpacity = 0.55 + conf * 0.45;
        return (
          <motion.button
            key={o.claim.id}
            type="button"
            title={`C${o.claim.id} · conf ${(conf * 100).toFixed(0)}% · ${o.claim.finding_count} findings`}
            onClick={(e) => { e.stopPropagation(); onSelect(o.claim); }}
            initial={{ opacity: 0, scale: 0.5 }}
            animate={{ opacity: focusingOther && !selected ? 0.25 : baseOpacity, scale: selected ? 1.4 : 1 }}
            whileHover={{ scale: 1.3, opacity: 1 }}
            transition={{ type: "spring", stiffness: 300, damping: 20 }}
            className={cx(
              "absolute z-10 -translate-x-1/2 -translate-y-1/2 rounded-full border shadow-sm",
              tone,
              selected ? "ring-4 ring-emerald-500/30" : "",
            )}
            style={{
              left: `${o.x}%`,
              top: `${o.y}%`,
              width: `${o.size * 6}px`,
              height: `${o.size * 6}px`,
              filter: selected ? "drop-shadow(0 0 3px rgba(16,185,129,0.7))" : undefined,
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
  sel, activity, kg, snapshot, power, events,
}: {
  sel: Selection;
  activity: (s: Stage) => { running: number; today: number; hot: boolean };
  kg: KgStats | null;
  snapshot: Snapshot;
  power?: PowerSummary | null;
  events: LabFoundryEvent[];
}) {
  if (!sel) {
    return (
      <Card className="max-h-[640px] overflow-y-auto">
        <div className="text-xs uppercase tracking-wider text-slate-400">Inspector</div>
        <p className="mt-2 text-sm text-slate-500">
          Click a stage, the knowledge hub, or an orbiting hypothesis to drill into its flow.
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

  if (sel.kind === "hub") return <HubInspector kg={kg} snapshot={snapshot} events={events} />;
  if (sel.kind === "claim") return <ClaimInspector claim={sel.claim} />;
  return <StageInspector stage={sel.stage} activity={activity} snapshot={snapshot} power={power} events={events} />;
}

// ----- Knowledge hub: sources + graph events + corpus/graph stats -----

function HubInspector({ kg, snapshot, events }: { kg: KgStats | null; snapshot: Snapshot; events: LabFoundryEvent[] }) {
  const graphEvents = events.filter((e) => HUB_EVENTS.includes(e.event_type)).slice(0, 8);
  return (
    <Card className="max-h-[640px] overflow-y-auto">
      <Head icon={Database} title="Knowledge core" sub="RAG corpus + evidence graph" />
      <Rows rows={[
        ["Knowledge graph (Neo4j)", kg ? `${kg.claims} claims · ${kg.findings} findings · ${kg.verdicts} verdicts` : "loading…"],
        ["RAG corpus", "papers · media · datasets (planned)"],
      ]} />

      <SubHead label="Ingestion sources" />
      <div className="flex flex-wrap gap-1.5">
        {[
          { name: "arXiv", live: true },
          { name: "Web / SearXNG", live: true },
          { name: "GitHub repos", live: false },
          { name: "Datasets", live: false },
          { name: "Media", live: false },
        ].map((s) => (
          <span key={s.name} className={cx(
            "rounded-full border px-2 py-0.5 text-xs",
            s.live ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-slate-200 bg-slate-50 text-slate-400",
          )}>
            {s.name}{!s.live && " · planned"}
          </span>
        ))}
      </div>

      <SubHead label="Recent graph events" />
      <EventList events={graphEvents} empty="No graph writes recently." />
    </Card>
  );
}

// ----- Hypothesis: evidence chain + verdicts from the graph -----

function ClaimInspector({ claim }: { claim: Claim }) {
  const [data, setData] = useState<GraphClaim | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.graphClaim(claim.id)
      .then((d) => { if (!cancelled) setData(d); })
      .catch(() => { if (!cancelled) setData(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [claim.id]);

  const evidence = data?.status === "ok" ? data.evidence_chain ?? [] : [];
  const verdicts = data?.status === "ok" ? data.critic_verdicts ?? [] : [];

  return (
    <Card className="max-h-[640px] overflow-y-auto">
      <Head icon={Target} title={`Hypothesis C${claim.id}`} sub={`confidence ${(claim.confidence * 100).toFixed(0)}% · ${claim.status}`} />
      <p className="mb-3 text-sm font-medium leading-snug text-slate-900">{claim.claim}</p>
      <Rows rows={[
        ["Findings", String(claim.finding_count)],
        ["Supporting / Contradicting", `${claim.supporting_count} / ${claim.contradicting_count}`],
      ]} />

      <SubHead label={`Evidence chain${evidence.length ? ` (${evidence.length})` : ""}`} />
      {loading ? (
        <p className="text-sm text-slate-400">Loading from the graph…</p>
      ) : evidence.length === 0 ? (
        <p className="text-sm text-slate-400">No grounded findings in the graph yet.</p>
      ) : (
        <ul className="space-y-1.5">
          {evidence.slice(0, 8).map((f) => (
            <li key={f.finding_id} className="rounded-2xl border border-slate-200 bg-slate-50 p-2 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-slate-500">F{f.finding_id}</span>
                <span className="flex items-center gap-1">
                  {f.source && <Badge tone="default">{f.source}</Badge>}
                  <Badge tone={f.audit_verdict === "pass" ? "green" : f.audit_verdict === "slop" ? "red" : "default"}>
                    rel {f.relevance_score ?? "—"}
                  </Badge>
                </span>
              </div>
              <p className="mt-1 line-clamp-2 text-slate-600">{f.title || f.summary || "(no title)"}</p>
            </li>
          ))}
        </ul>
      )}

      <SubHead label={`Critic challenges${verdicts.length ? ` (${verdicts.length})` : ""}`} />
      {verdicts.length === 0 ? (
        <p className="text-sm text-slate-400">No verdicts recorded.</p>
      ) : (
        <ul className="space-y-1.5">
          {verdicts.slice(0, 5).map((v) => (
            <li key={v.verdict_id} className="rounded-2xl border border-slate-200 bg-slate-50 p-2 text-xs">
              <div className="flex items-center justify-between gap-2">
                <Badge tone={v.action === "kill" ? "red" : v.action === "weaken" ? "amber" : "default"}>
                  {v.action || v.verdict || "verdict"}
                </Badge>
                <span className="text-slate-400">{v.cited_finding_ids?.length ?? 0} cited</span>
              </div>
              {v.reasoning && <p className="mt-1 line-clamp-2 text-slate-600">{v.reasoning}</p>}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// ----- Stage: agents, live stats, recent flow, and what it produced -----

function StageInspector({
  stage, activity, snapshot, power, events,
}: {
  stage: Stage;
  activity: (s: Stage) => { running: number; today: number; hot: boolean };
  snapshot: Snapshot;
  power?: PowerSummary | null;
  events: LabFoundryEvent[];
}) {
  const { running, today } = activity(stage);
  const Icon = stage.icon;
  const stageEvents = events.filter((e) => stage.events.includes(e.event_type)).slice(0, 8);

  return (
    <Card className="max-h-[640px] overflow-y-auto">
      <Head icon={Icon} title={stage.label} sub={stage.division} />
      <Rows rows={[
        ["Agents", stage.roles.map((r) => r.replace(/_/g, " ")).join(", ")],
        ["Running now / today", `${running} / ${today}`],
      ]} />

      {/* Stage-specific outputs */}
      {stage.id === "question" && (
        <>
          <SubHead label="Task queue" />
          <Rows rows={[
            ["Pending / running", `${snapshot.stats.pending_tasks} / ${snapshot.stats.running_tasks}`],
          ]} />
        </>
      )}

      {stage.id === "gather" && (
        <>
          <SubHead label={`Recent findings (${snapshot.stats.findings_today} today)`} />
          <FindingList findings={snapshot.recent_findings.slice(0, 6)} />
          <Rows rows={[["Live web search", `${snapshot.stats.source_web_in_flight} fetch(es) in flight`]]} />
        </>
      )}

      {stage.id === "judge" && (
        <>
          <Rows rows={[["Audit today", `${snapshot.stats.high_signal_today} high-signal · ${snapshot.stats.slop_today} slop`]]} />
          <SubHead label="Recent dissent" />
          <DissentList items={snapshot.dissent.slice(0, 6)} />
        </>
      )}

      {stage.id === "synthesise" && (
        <>
          <SubHead label={`Active hypotheses (${snapshot.active_claims.length})`} />
          <ul className="space-y-1.5">
            {snapshot.active_claims.slice(0, 6).map((c) => (
              <li key={c.id} className="flex items-center justify-between gap-2 rounded-2xl border border-slate-200 bg-slate-50 p-2 text-xs">
                <span className="line-clamp-1 text-slate-700">C{c.id} · {c.claim}</span>
                <Badge tone={c.confidence >= 0.5 ? "green" : "default"}>{(c.confidence * 100).toFixed(0)}%</Badge>
              </li>
            ))}
          </ul>
        </>
      )}

      {stage.id === "converge" && (
        <>
          <Rows rows={[
            ["Phase", `${snapshot.state.current_phase} · day ${snapshot.state.days_in_phase}`],
            ["Resources", power ? `${Math.round(power.total_watts)}W · ${power.gpu_count} GPU` : "—"],
          ]} />
          {snapshot.phase_transitions.length > 0 && (
            <>
              <SubHead label="Phase history" />
              <ul className="space-y-1 text-xs text-slate-600">
                {snapshot.phase_transitions.slice(0, 4).map((t) => (
                  <li key={t.id} className="rounded-2xl bg-slate-50 px-3 py-1.5">
                    {t.from_phase} → {t.to_phase} · {ago(t.decided_at)}{t.forced ? " (forced)" : ""}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}

      <SubHead label="Recent flow" />
      <EventList events={stageEvents} empty="No matching events recently." />
    </Card>
  );
}

// ----- Shared list renderers -----

function EventList({ events, empty }: { events: LabFoundryEvent[]; empty: string }) {
  if (events.length === 0) return <p className="text-sm text-slate-400">{empty}</p>;
  return (
    <ul className="space-y-1">
      {events.map((e) => (
        <li key={e.id} className="flex items-center gap-2 rounded-2xl bg-slate-50 px-3 py-1.5 text-xs">
          <span className="w-14 shrink-0 font-mono text-slate-400">{fmtTime(e.emitted_at)}</span>
          <span className="min-w-0 flex-1 truncate font-mono font-semibold text-slate-700">{e.event_type}</span>
          {e.target_id != null && <span className="font-mono text-[11px] text-slate-400">{e.target_type}#{e.target_id}</span>}
        </li>
      ))}
    </ul>
  );
}

function FindingList({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) return <p className="text-sm text-slate-400">No findings cached.</p>;
  return (
    <ul className="space-y-1.5">
      {findings.map((f) => (
        <li key={f.id} className="rounded-2xl border border-slate-200 bg-slate-50 p-2 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-slate-500">F{f.id}</span>
            <Badge tone={f.audit_verdict === "pass" ? "green" : f.audit_verdict === "slop" ? "red" : "default"}>
              {f.source || "?"} · rel {f.relevance_score}
            </Badge>
          </div>
          <p className="mt-1 line-clamp-2 text-slate-600">{f.title || f.summary}</p>
        </li>
      ))}
    </ul>
  );
}

function DissentList({ items }: { items: DissentItem[] }) {
  if (items.length === 0) return <p className="text-sm text-slate-400">No dissent recorded.</p>;
  return (
    <ul className="space-y-1.5">
      {items.map((d) => (
        <li key={`${d.kind}-${d.id}`} className="rounded-2xl border border-slate-200 bg-slate-50 p-2 text-xs">
          <div className="flex items-center justify-between gap-2">
            <Badge tone={d.kind === "audit-slop" ? "red" : "amber"}>{d.kind}</Badge>
            <span className="font-mono text-slate-400">C{d.claim_id}</span>
          </div>
          <p className="mt-1 line-clamp-2 text-slate-600">{d.reasoning || d.detail}</p>
        </li>
      ))}
    </ul>
  );
}

function SubHead({ label }: { label: string }) {
  return <div className="mb-2 mt-4 text-[10px] font-semibold uppercase tracking-wider text-slate-400">{label}</div>;
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
