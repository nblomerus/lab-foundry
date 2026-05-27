"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  CircleDot, Maximize2, Minus, PanelRightOpen, Plus,
} from "lucide-react";

import { api } from "../lib/api";
import type {
  BoardroomEvent, EdgeActivity, Snapshot, StreamMessage,
} from "../lib/types";
import { useEventStream } from "../lib/ws";
import { Badge, Card, cx } from "./ui";

import {
  ARROW_PENETRATION, ASPECT_X, ASPECT_Y, EDGES, NODES, NODE_H, NODE_W, ROLE_TO_NODE,
  type EdgeDef, type NodeDef, type Side,
  buildEdgePath, edgeLabelPoint, handlePoint, handleVec, nodeById,
  penetrationPoint, runTopologyChecks,
} from "../lib/flow-topology";

// =========================================================================
// Status derivation
// =========================================================================

type Tone = "running" | "active" | "queued" | "warn" | "blocked" | "idle";

interface NodeStatus {
  tone: Tone;
  badge: string;
  current: string;
  details: string[];
}

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function ago(iso?: string | null): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  return `${h}h ago`;
}

function deriveNodeStatus(node: NodeDef, snap: Snapshot): NodeStatus {
  const role = snap.org_roles.find((r) => r.role === (ROLE_TO_NODE[node.id] ?? ""));

  if (["researcher", "auditor", "adversary", "ceo", "planner", "adjudicator"].includes(node.id)) {
    const running = (role?.running_count ?? 0) > 0;
    const today = role?.runs_today ?? 0;
    return {
      tone: running ? "running" : today > 0 ? "active" : "idle",
      badge: running ? `${role!.running_count} in flight` : today > 0 ? `${today} today` : "idle",
      current: role?.last_run_at ? `last ${ago(role.last_run_at)}` : "no runs",
      details: [
        running ? `${role!.running_count} currently in flight` : "0 in flight",
        `${today} runs today`,
        role?.avg_duration_s != null ? `avg ${role.avg_duration_s.toFixed(1)}s per run` : "no timing yet",
        role?.last_run_at ? `last run @ ${fmtTime(role.last_run_at)}` : "never run",
      ],
    };
  }

  if (node.id === "tasks") {
    const p = snap.stats.pending_tasks, r = snap.stats.running_tasks;
    const completed = snap.task_counts.find((t) => t.label === "completed")?.value ?? 0;
    const failed = snap.task_counts.find((t) => t.label === "failed")?.value ?? 0;
    return {
      tone: r > 0 ? "running" : p > 0 ? "queued" : "idle",
      badge: `${p} pending`,
      current: `${r} claimed · ${completed} done · ${failed} failed`,
      details: [`${p} pending`, `${r} claimed`, `${completed} completed`, `${failed} failed`],
    };
  }

  if (node.id === "findings") {
    const t = snap.stats.findings_today;
    const h = snap.stats.high_signal_today;
    const s = snap.stats.slop_today;
    return {
      tone: t > 0 ? "active" : "idle",
      badge: `${t} today`,
      current: `${h} high-signal · ${s} slop`,
      details: [`${t} findings (24h)`, `${h} audit=pass & rel≥8`, `${s} flagged slop`, `${snap.recent_findings.length} in cache`],
    };
  }

  if (node.id === "theses") {
    const a = snap.state.active_thesis_count;
    const k = snap.state.killed_thesis_count;
    const top = snap.active_theses[0];
    return {
      tone: a > 0 ? "active" : "warn",
      badge: `${a} active`,
      current: top ? `top T${top.id} conf ${top.confidence.toFixed(2)}` : "no theses",
      details: [
        `${a} active · ${k} killed/merged`,
        top ? `top T${top.id}: conf ${top.confidence.toFixed(2)}` : "no active",
        top ? `"${top.claim.slice(0, 60)}${top.claim.length > 60 ? "…" : ""}"` : "",
      ].filter(Boolean),
    };
  }

  if (node.id === "phase") {
    return {
      tone: snap.state.paused ? "blocked" : snap.state.current_phase === "execution" ? "active" : "warn",
      badge: snap.state.current_phase,
      current: `day ${snap.state.days_in_phase} · ${snap.state.days_remaining}d left`,
      details: [
        `phase: ${snap.state.current_phase}`,
        `day ${snap.state.days_in_phase}`,
        `${snap.state.days_remaining}d to deadline`,
        snap.state.paused ? `PAUSED: ${snap.state.paused_reason ?? ""}` : "running",
      ],
    };
  }

  const inFlight = node.id === "hn"     ? snap.stats.source_hn_in_flight
                 : node.id === "reddit" ? snap.stats.source_reddit_in_flight
                 :                        snap.stats.source_web_in_flight;
  const endpoint = node.id === "hn"     ? "hn.algolia.com"
                 : node.id === "reddit" ? "reddit.com"
                 :                        "duckduckgo.com";
  return {
    tone: inFlight > 0 ? "running" : "idle",
    badge: inFlight > 0 ? `${inFlight} live` : "idle",
    current: inFlight > 0 ? "polling now" : "no live calls",
    details: [`endpoint: ${endpoint}`, `${inFlight} tasks fetching now`, inFlight > 0 ? "live" : "waiting"],
  };
}

// =========================================================================
// Port style — DOM coordinates for the port indicator dots
// =========================================================================

function portStyle(side: Side, offset = 0): React.CSSProperties {
  const pct = (off: number) => `${50 + off * 100}%`;
  switch (side) {
    case "left":   return { left: "0%",        top: pct(offset), transform: "translate(-50%, -50%)" };
    case "right":  return { left: "100%",      top: pct(offset), transform: "translate(-50%, -50%)" };
    case "top":    return { left: pct(offset), top: "0%",        transform: "translate(-50%, -50%)" };
    case "bottom": return { left: pct(offset), top: "100%",      transform: "translate(-50%, -50%)" };
  }
}

// =========================================================================
// Pulse tracking
// =========================================================================

function usePerEdgePulses(): { pulses: Map<string, number>; recent: BoardroomEvent[] } {
  const { recent } = useEventStream(60);
  const [pulses, setPulses] = useState<Map<string, number>>(new Map());
  const seen = useRef<Set<number>>(new Set());

  useEffect(() => {
    const now = Date.now();
    const next = new Map(pulses);
    let changed = false;
    for (const [id, exp] of next) if (exp <= now) { next.delete(id); changed = true; }
    for (const msg of recent) {
      if (msg.type !== "event" || seen.current.has(msg.event.id)) continue;
      seen.current.add(msg.event.id);
      for (const e of EDGES.filter((e) => e.event_type === msg.event.event_type)) {
        next.set(e.id, now + 6000);
        changed = true;
      }
    }
    if (changed) setPulses(next);
  }, [recent, pulses]);

  useEffect(() => {
    const t = setInterval(() => {
      const now = Date.now();
      setPulses((p) => {
        let changed = false;
        const next = new Map(p);
        for (const [id, exp] of next) if (exp <= now) { next.delete(id); changed = true; }
        return changed ? next : p;
      });
    }, 250);
    return () => clearInterval(t);
  }, []);

  const events = recent
    .filter((m): m is Extract<StreamMessage, { type: "event" }> => m.type === "event")
    .map((m) => m.event);
  return { pulses, recent: events };
}

// =========================================================================
// Visual primitives
// =========================================================================

function toneStyles(tone: Tone): { border: string; bg: string; dot: string; badge: "green" | "amber" | "red" | "blue" | "default" } {
  switch (tone) {
    case "running":  return { border: "border-emerald-300", bg: "bg-emerald-50",    dot: "text-emerald-500", badge: "green" };
    case "active":   return { border: "border-emerald-200", bg: "bg-emerald-50/60", dot: "text-emerald-500", badge: "green" };
    case "warn":     return { border: "border-amber-300",   bg: "bg-amber-50",      dot: "text-amber-500",   badge: "amber" };
    case "queued":   return { border: "border-blue-300",    bg: "bg-blue-50",       dot: "text-blue-500",    badge: "blue" };
    case "blocked":  return { border: "border-red-300",     bg: "bg-red-50",        dot: "text-red-500",     badge: "red" };
    default:         return { border: "border-slate-200",   bg: "bg-white",         dot: "text-slate-400",   badge: "default" };
  }
}

function NodePort({ side, offset, hot }: { side: Side; offset: number; hot: boolean }) {
  return (
    <span
      className={cx(
        "absolute z-30 rounded-full border border-white shadow transition-all",
        hot
          ? "h-2.5 w-2.5 bg-emerald-500 ring-4 ring-emerald-400/25"
          : "h-2 w-2 bg-slate-300",
      )}
      style={portStyle(side, offset)}
    />
  );
}

function FlowNode({
  node, status, selected, hot, ringConnected, ports, onSelect,
}: {
  node: NodeDef;
  status: NodeStatus;
  selected: boolean;
  hot: boolean;
  ringConnected: boolean;
  ports: { side: Side; offset: number; hot: boolean }[];
  onSelect: () => void;
}) {
  const Icon = node.icon;
  const t = toneStyles(status.tone);
  const active = selected || hot;

  return (
    <motion.button
      type="button"
      onClick={onSelect}
      data-node-id={node.id}
      initial={{ opacity: 0, y: 6, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: active ? 1.03 : 1 }}
      whileHover={{ scale: 1.05 }}
      transition={{ type: "spring", stiffness: 260, damping: 22 }}
      className={cx(
        "absolute z-20 flex flex-col justify-center rounded-2xl border bg-white p-2.5 text-left shadow-sm transition",
        t.border, t.bg,
        active ? "shadow-lg ring-4 ring-emerald-500/15" : "",
        ringConnected && !active ? "ring-2 ring-slate-400/40" : "",
      )}
      style={{
        // Position by top-left corner so the node's CENTER lands at
        // (node.x%, node.y%). We can't use `transform: translate(-50%,-50%)`
        // because framer-motion's animate/whileHover overwrites `transform`
        // with its own scale/y values, dropping the translate. Pre-computing
        // the corner keeps the center aligned with handlePoint() in SVG.
        // The render-layer test in LiveFlow.dom.test.tsx enforces this.
        left: `${node.x - NODE_W / 2}%`,
        top:  `${node.y - NODE_H / 2}%`,
        width: `${NODE_W}%`,
        height: `${NODE_H}%`,
      }}
    >
      {status.tone === "running" && (
        <motion.span
          className="absolute -inset-1 rounded-[1.2rem] border border-emerald-300"
          animate={{ opacity: [0.15, 0.75, 0.15] }}
          transition={{ duration: 1.4, repeat: Infinity }}
        />
      )}
      <div className="relative flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <div className="shrink-0 rounded-xl bg-white p-1.5 shadow-sm">
            <Icon className="h-3.5 w-3.5 text-slate-800" />
          </div>
          <div className="min-w-0">
            <div className="text-[12.5px] font-semibold leading-tight text-slate-950">{node.label}</div>
            <div className="text-[9px] font-bold uppercase tracking-wide text-slate-400">{node.type}</div>
          </div>
        </div>
        <CircleDot className={cx("mt-0.5 h-3 w-3 shrink-0", t.dot)} />
      </div>
      <div className="relative mt-1.5"><Badge tone={t.badge}>{status.badge}</Badge></div>
      <div className="relative mt-1 truncate text-[10px] text-slate-500">{status.current}</div>

      {ports.map((p, i) => (
        <NodePort key={`${p.side}-${p.offset}-${i}`} side={p.side} offset={p.offset} hot={p.hot} />
      ))}
    </motion.button>
  );
}

function FlowDefs() {
  // markerUnits="userSpaceOnUse" → marker dimensions are absolute SVG units,
  // not multiplied by strokeWidth. This keeps arrow heads the same physical
  // size regardless of how thick or thin we draw the edge below them.
  const Arrow = ({ id, color, size }: { id: string; color: string; size: number }) => (
    <marker
      id={id}
      markerWidth={size}
      markerHeight={size}
      refX={size}
      refY={size / 2}
      orient="auto"
      markerUnits="userSpaceOnUse"
    >
      <path d={`M 0 0 L ${size} ${size / 2} L 0 ${size} Z`} fill={color} />
    </marker>
  );
  return (
    <defs>
      <Arrow id="arrow-dim"  color="#94a3b8" size={2.4} />
      <Arrow id="arrow-mid"  color="#334155" size={2.8} />
      <Arrow id="arrow-hot"  color="#10b981" size={2.8} />
      <Arrow id="arrow-conn" color="#475569" size={2.6} />
    </defs>
  );
}

function FlowEdge({
  edge, hot, selected, connected, rate, onSelect,
}: {
  edge: EdgeDef;
  hot: boolean;
  selected: boolean;
  connected: boolean;
  /** Events per minute over the last minute. Drives continuous particle flow. */
  rate: number;
  onSelect: () => void;
}) {
  const from = nodeById(edge.from);
  const to   = nodeById(edge.to);
  if (!from || !to) return null;

  const start = handlePoint(from, edge.fromSide, edge.fromOffset ?? 0);
  const port  = handlePoint(to,   edge.toSide,   edge.toOffset   ?? 0);
  const path  = buildEdgePath(start, port, edge.fromSide, edge.toSide, edge.route ?? "auto");

  const warm = rate > 0;
  let stroke = "#94a3b8";
  let marker = "arrow-dim";
  let opacity = 0.55;
  let weight  = 0.5;
  if (connected) { stroke = "#475569"; marker = "arrow-conn"; opacity = 0.85; weight = 0.7; }
  if (warm)      { stroke = "#10b981"; marker = "arrow-hot";  opacity = 0.9;  weight = 0.7; }
  if (selected)  { stroke = "#334155"; marker = "arrow-mid";  opacity = 1;    weight = 0.9; }
  if (hot)       { stroke = "#10b981"; marker = "arrow-hot";  opacity = 1;    weight = 1.0; }

  // Particle flow: continuous when warm, bursty when hot.
  //   rate ≥ 10 → 3 fast particles
  //   rate ≥ 3  → 2 medium particles
  //   rate > 0  → 1 slow particle
  //   hot only  → 2 medium burst (was the old behavior)
  let particleCount = 0;
  let particleDur = 2;
  if (warm) {
    particleCount = rate >= 10 ? 3 : rate >= 3 ? 2 : 1;
    particleDur   = rate >= 10 ? 1.2 : rate >= 3 ? 2.0 : 3.2;
  } else if (hot) {
    particleCount = 2;
    particleDur   = 1.8;
  }

  return (
    <g
      onClick={onSelect}
      className="cursor-pointer"
      data-edge
      data-edge-id={edge.id}
      data-from={edge.from}
      data-to={edge.to}
    >
      <path
        d={path}
        data-edge-path={edge.id}
        fill="none"
        stroke={stroke}
        strokeWidth={weight}
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity={opacity}
        markerEnd={`url(#${marker})`}
        style={(hot || warm) ? { filter: "drop-shadow(0 0 0.9px rgba(16,185,129,0.55))" } : undefined}
      />
      <path d={path} fill="none" stroke="transparent" strokeWidth="3" />
      {particleCount > 0 && Array.from({ length: particleCount }).map((_, i) => (
        <circle
          key={i}
          r={i === 0 ? 0.85 : 0.55}
          fill={i === 0 ? "#10b981" : "#34d399"}
          opacity={i === 0 ? 1 : 0.75}
          style={i === 0 ? { filter: "drop-shadow(0 0 0.9px rgba(16,185,129,0.85))" } : undefined}
        >
          <animateMotion
            dur={`${particleDur}s`}
            repeatCount="indefinite"
            begin={`${(i * particleDur) / particleCount}s`}
            path={path}
          />
        </circle>
      ))}
    </g>
  );
}

function EdgeLabel({
  edge, hot, selected, onSelect,
}: {
  edge: EdgeDef;
  hot: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  if (!hot && !selected) return null;
  const from = nodeById(edge.from);
  const to   = nodeById(edge.to);
  if (!from || !to) return null;
  const start = handlePoint(from, edge.fromSide, edge.fromOffset ?? 0);
  const port  = handlePoint(to,   edge.toSide,   edge.toOffset   ?? 0);
  const mid   = edgeLabelPoint(start, port, edge.fromSide, edge.toSide, edge.route ?? "auto");
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cx(
        "pointer-events-auto absolute z-30 max-w-[110px] -translate-x-1/2 -translate-y-1/2 truncate rounded-full border px-2 py-0.5 text-[10px] font-semibold shadow-sm backdrop-blur",
        hot ? "border-emerald-300 bg-emerald-50 text-emerald-700"
            : "border-slate-300 bg-white text-slate-600",
      )}
      style={{ left: `${mid.x / ASPECT_X}%`, top: `${mid.y / ASPECT_Y}%` }}
    >
      {edge.label}
    </button>
  );
}

// =========================================================================
// Inspector
// =========================================================================

type Selection =
  | { kind: "node"; node: NodeDef; status: NodeStatus }
  | { kind: "edge"; edge: EdgeDef; pulsing: boolean; activity: EdgeActivity | null };

function Inspector({ selection }: { selection: Selection | null }) {
  if (!selection) {
    return (
      <Card>
        <div className="text-xs uppercase tracking-wider text-slate-400">Inspector</div>
        <p className="mt-2 text-sm text-slate-500">Click any node or edge.</p>
      </Card>
    );
  }
  if (selection.kind === "edge") {
    const f = nodeById(selection.edge.from);
    const t = nodeById(selection.edge.to);
    const act = selection.activity;
    const isPseudo = selection.edge.event_type.startsWith("_");
    return (
      <Card>
        <div className="mb-3 flex items-start justify-between">
          <div>
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
              <PanelRightOpen className="h-4 w-4" /> Edge
            </div>
            <h3 className="mt-2 text-lg font-semibold tracking-tight text-slate-950">
              {f?.label} → {t?.label}
            </h3>
            <div className="mt-1 text-xs text-slate-500">{selection.edge.label}</div>
          </div>
          <Badge tone={selection.pulsing ? "green" : "default"}>{selection.pulsing ? "active" : "idle"}</Badge>
        </div>
        <div className="space-y-3">
          {isPseudo ? (
            <Info title="Driven by" text="per-source in-flight task counter (not an event)" />
          ) : (
            <>
              <Info title="Event" text={<span className="font-mono text-xs">{selection.edge.event_type}</span>} />
              <div className="grid grid-cols-2 gap-2">
                <Stat label="Last fired"  value={act?.last_fired_at ? ago(act.last_fired_at) : "never"} />
                <Stat label="Last minute" value={String(act?.count_last_minute ?? 0)} />
                <Stat label="Today (24h)" value={String(act?.count_today ?? 0)} />
                <Stat label="At"          value={act?.last_fired_at ? fmtTime(act.last_fired_at) : "—"} />
              </div>
            </>
          )}
        </div>
      </Card>
    );
  }

  const { node, status } = selection;
  return (
    <Card>
      <div className="mb-3 flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            <PanelRightOpen className="h-4 w-4" /> Node
          </div>
          <h3 className="mt-2 text-lg font-semibold tracking-tight text-slate-950">{node.label}</h3>
          <div className="mt-0.5 text-xs text-slate-500">{node.type}</div>
        </div>
        <Badge tone={toneStyles(status.tone).badge}>{status.badge}</Badge>
      </div>
      <div className="space-y-2">
        {status.details.map((d, i) => (
          <div key={i} className="flex items-start gap-2 rounded-2xl bg-slate-50 px-3 py-2 text-sm text-slate-700">
            <CircleDot className="mt-0.5 h-3 w-3 shrink-0 text-slate-400" />
            <span className="break-words">{d}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

function Info({ title, text }: { title: string; text: React.ReactNode }) {
  return (
    <div className="rounded-2xl bg-slate-50 p-3">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{title}</div>
      <div className="mt-0.5 text-sm leading-6 text-slate-700">{text}</div>
    </div>
  );
}
function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl bg-slate-50 px-3 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-0.5 text-sm font-mono text-slate-700">{value}</div>
    </div>
  );
}

// =========================================================================
// Event stream
// =========================================================================

function EventStream({
  recent, onHover, onPick, connected,
}: {
  recent: BoardroomEvent[];
  onHover: (eventType: string | null) => void;
  onPick: (eventType: string) => void;
  connected: boolean;
}) {
  const [prefill, setPrefill] = useState<BoardroomEvent[]>([]);
  useEffect(() => { api.events(40).then(setPrefill).catch(() => {}); }, []);

  const seen = new Set<number>();
  const merged: BoardroomEvent[] = [];
  for (const e of [...recent, ...prefill]) {
    if (seen.has(e.id)) continue;
    seen.add(e.id);
    merged.push(e);
    if (merged.length >= 12) break;
  }

  return (
    <Card>
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold tracking-tight text-slate-950">Live event stream</h2>
          <p className="mt-0.5 text-sm text-slate-500">Hover a row to highlight its edge. Click to pin selection.</p>
        </div>
        <span className="flex items-center gap-1.5 text-xs text-slate-500">
          <span className={cx("inline-block h-1.5 w-1.5 rounded-full", connected ? "bg-emerald-500 animate-pulse" : "bg-red-500")} />
          {connected ? "LIVE" : "RECONNECTING"}
        </span>
      </div>
      {merged.length === 0 ? (
        <p className="text-sm text-slate-400">No events yet.</p>
      ) : (
        <div className="grid gap-2 md:grid-cols-2">
          {merged.map((e) => {
            const willPulse = EDGES.some((x) => x.event_type === e.event_type);
            return (
              <button
                key={e.id}
                type="button"
                onMouseEnter={() => willPulse && onHover(e.event_type)}
                onMouseLeave={() => onHover(null)}
                onClick={() => willPulse && onPick(e.event_type)}
                className="flex items-center gap-3 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-left transition hover:border-emerald-200 hover:bg-emerald-50/40"
              >
                <span className="w-16 shrink-0 font-mono text-xs text-slate-400">{fmtTime(e.emitted_at)}</span>
                <span className={cx("h-2 w-2 rounded-full", willPulse ? "bg-emerald-500" : "bg-slate-300")} />
                <span className="min-w-0 flex-1 truncate font-mono text-xs font-semibold text-slate-700">{e.event_type}</span>
                <span className="font-mono text-[11px] text-slate-400">{e.target_type ? `${e.target_type}#${e.target_id}` : ""}</span>
              </button>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// =========================================================================
// Integrity panel — runtime self-test
// =========================================================================

function IntegrityPanel() {
  const checks = useMemo(() => runTopologyChecks(), []);
  const passed = checks.filter((c) => c.pass).length;
  const allOk = passed === checks.length;
  // Default collapsed when all checks pass — the panel is most useful when
  // something is wrong. Expand by default if any check fails so the user
  // notices.
  const [open, setOpen] = useState(!allOk);
  return (
    <Card>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between text-left"
        aria-expanded={open}
        aria-controls="integrity-checks-body"
      >
        <div>
          <h2 className="text-base font-semibold tracking-tight text-slate-950">Integrity checks</h2>
          <p className="mt-0.5 text-sm text-slate-500">
            Topology + path geometry invariants{open ? " — verified on mount." : "."}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={allOk ? "green" : "red"}>{passed}/{checks.length}</Badge>
          <span
            className={cx(
              "inline-block text-slate-400 transition-transform",
              open ? "rotate-180" : "rotate-0",
            )}
            aria-hidden
          >▾</span>
        </div>
      </button>
      {open && (
        <ul id="integrity-checks-body" className="mt-3 space-y-1.5">
          {checks.map((c) => (
            <li key={c.name} className="flex items-start gap-2 rounded-2xl bg-slate-50 px-3 py-2 text-sm">
              <span className={cx(
                "mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[10px] font-bold text-white",
                c.pass ? "bg-emerald-500" : "bg-red-500",
              )}>{c.pass ? "✓" : "✗"}</span>
              <div className="min-w-0">
                <div className="font-medium text-slate-800">{c.name}</div>
                {c.detail && !c.pass && (
                  <div className="mt-0.5 truncate font-mono text-[11px] text-red-600">{c.detail}</div>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// =========================================================================
// Zoom + pan
// =========================================================================

function ZoomableCanvas({ children }: { children: React.ReactNode }) {
  const [scale, setScale] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const dragging = useRef(false);
  const lastMouse = useRef({ x: 0, y: 0 });
  const clamp = (s: number) => Math.max(0.55, Math.min(2.5, s));

  const onWheel = useCallback((e: React.WheelEvent) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    setScale((s) => clamp(s + (e.deltaY > 0 ? -0.08 : 0.08)));
  }, []);
  const onMouseDown = useCallback((e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest("button, a, [data-edge]")) return;
    dragging.current = true;
    lastMouse.current = { x: e.clientX, y: e.clientY };
  }, []);
  const onMouseMove = useCallback((e: React.MouseEvent) => {
    if (!dragging.current) return;
    const dx = e.clientX - lastMouse.current.x;
    const dy = e.clientY - lastMouse.current.y;
    lastMouse.current = { x: e.clientX, y: e.clientY };
    setPan((p) => ({ x: p.x + dx, y: p.y + dy }));
  }, []);
  const onMouseUp = useCallback(() => { dragging.current = false; }, []);
  const reset = () => { setScale(1); setPan({ x: 0, y: 0 }); };

  return (
    <div
      className="relative aspect-[16/10] min-h-[560px] w-full overflow-hidden rounded-[2rem] border border-slate-200 bg-[radial-gradient(circle_at_20%_22%,_rgba(16,185,129,.06),_transparent_22%),radial-gradient(circle_at_80%_28%,_rgba(245,158,11,.06),_transparent_22%),linear-gradient(135deg,_#ffffff,_#f8fafc)] shadow-sm select-none"
      onWheel={onWheel}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={onMouseUp}
      style={{ cursor: dragging.current ? "grabbing" : "grab" }}
    >
      <div className="absolute inset-0 origin-top-left" style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})` }}>
        {children}
      </div>
      <div className="absolute right-3 top-3 z-30 flex flex-col gap-1 rounded-2xl border border-slate-200 bg-white/90 p-1 shadow-sm backdrop-blur">
        <button type="button" onClick={(e) => { e.stopPropagation(); setScale((s) => clamp(s + 0.15)); }} className="rounded-xl p-1.5 hover:bg-slate-100" title="Zoom in"><Plus className="h-4 w-4 text-slate-700" /></button>
        <button type="button" onClick={(e) => { e.stopPropagation(); setScale((s) => clamp(s - 0.15)); }} className="rounded-xl p-1.5 hover:bg-slate-100" title="Zoom out"><Minus className="h-4 w-4 text-slate-700" /></button>
        <button type="button" onClick={(e) => { e.stopPropagation(); reset(); }} className="rounded-xl p-1.5 hover:bg-slate-100" title="Fit"><Maximize2 className="h-4 w-4 text-slate-700" /></button>
      </div>
      <div className="pointer-events-none absolute bottom-3 right-3 z-30 rounded-full border border-slate-200 bg-white/90 px-2 py-0.5 text-[10px] font-mono text-slate-500">
        {Math.round(scale * 100)}% · drag to pan · ⌘/Ctrl + scroll
      </div>
    </div>
  );
}

// =========================================================================
// Top-level
// =========================================================================

function isSourcePulse(edge: EdgeDef, snap: Snapshot): boolean {
  if (edge.event_type === "_source_hn")     return snap.stats.source_hn_in_flight > 0;
  if (edge.event_type === "_source_reddit") return snap.stats.source_reddit_in_flight > 0;
  if (edge.event_type === "_source_web")    return snap.stats.source_web_in_flight > 0;
  return false;
}

export function LiveFlow({ snapshot }: { snapshot: Snapshot }) {
  const statuses = useMemo(
    () => Object.fromEntries(NODES.map((n) => [n.id, deriveNodeStatus(n, snapshot)])),
    [snapshot],
  ) as Record<string, NodeStatus>;
  const activityByEvent = useMemo(
    () => new Map(snapshot.edge_activity.map((a) => [a.event_type, a])),
    [snapshot.edge_activity],
  );
  const { pulses, recent } = usePerEdgePulses();
  const { connected } = useEventStream(1);

  const [hoverEventType, setHoverEventType] = useState<string | null>(null);

  // Edges only pulse "hot" when there's an actual event (or hover). Live
  // in-flight source counters surface on the source NODES (badge + port
  // halo), not on the edge — otherwise source edges would be perma-hot
  // and drown out the rest of the graph.
  const isEdgeHot = useCallback((e: EdgeDef): boolean =>
    pulses.has(e.id) ||
    (hoverEventType != null && e.event_type === hoverEventType),
    [pulses, hoverEventType],
  );
  const isSourceLive = useCallback(
    (e: EdgeDef): boolean => isSourcePulse(e, snapshot),
    [snapshot],
  );

  // Per-edge throughput → continuous particle flow rate (events / minute).
  // Pseudo-events for sources (_source_*) report in-flight worker count via
  // snapshot.stats; convert to an equivalent "events/min" so the visual
  // language is consistent across real and pseudo edges.
  const edgeRates = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of EDGES) {
      if (e.event_type === "_source_hn") {
        m.set(e.id, snapshot.stats.source_hn_in_flight * 4);
      } else if (e.event_type === "_source_reddit") {
        m.set(e.id, snapshot.stats.source_reddit_in_flight * 4);
      } else if (e.event_type === "_source_web") {
        m.set(e.id, snapshot.stats.source_web_in_flight * 4);
      } else {
        const a = activityByEvent.get(e.event_type);
        m.set(e.id, a?.count_last_minute ?? 0);
      }
    }
    return m;
  }, [snapshot.stats, activityByEvent]);

  const [selection, setSelection] = useState<Selection | null>(() => {
    const n = nodeById("researcher")!;
    return { kind: "node", node: n, status: statuses["researcher"] };
  });

  useEffect(() => {
    setSelection((prev) => {
      if (!prev) return prev;
      if (prev.kind === "node") return { kind: "node", node: prev.node, status: statuses[prev.node.id] };
      const act = activityByEvent.get(prev.edge.event_type) ?? null;
      return { kind: "edge", edge: prev.edge, pulsing: isEdgeHot(prev.edge), activity: act };
    });
  }, [statuses, activityByEvent, pulses, hoverEventType, isEdgeHot]);

  const connectedEdgeIds = useMemo(() => {
    if (!selection || selection.kind !== "node") return new Set<string>();
    return new Set(EDGES.filter((e) => e.from === selection.node.id || e.to === selection.node.id).map((e) => e.id));
  }, [selection]);

  const portsByNode = useMemo(() => {
    const map = new Map<string, { side: Side; offset: number; hot: boolean }[]>();
    for (const e of EDGES) {
      const fOff = e.fromOffset ?? 0;
      const tOff = e.toOffset ?? 0;
      const isSel = selection?.kind === "edge" && selection.edge.id === e.id;
      const edgeHot = isEdgeHot(e) || isSel || connectedEdgeIds.has(e.id);
      // Source-in-flight lights up the source side's port only.
      const sourceLive = isSourceLive(e);
      const pushPort = (id: string, side: Side, offset: number, hot: boolean) => {
        const arr = map.get(id) ?? [];
        const existing = arr.find((p) => p.side === side && Math.abs(p.offset - offset) < 0.01);
        if (existing) existing.hot = existing.hot || hot;
        else arr.push({ side, offset, hot });
        map.set(id, arr);
      };
      pushPort(e.from, e.fromSide, fOff, edgeHot || sourceLive);
      pushPort(e.to, e.toSide, tOff, edgeHot);
    }
    return map;
  }, [pulses, hoverEventType, snapshot, selection, connectedEdgeIds, isEdgeHot, isSourceLive]);

  const ringConnectedNodes = useMemo(() => {
    if (!selection || selection.kind !== "edge") return new Set<string>();
    return new Set([selection.edge.from, selection.edge.to]);
  }, [selection]);

  const inFlight = snapshot.org_roles.reduce((s, r) => s + r.running_count, 0);
  const totalRate = useMemo(
    () => Array.from(edgeRates.values()).reduce((a, b) => a + b, 0),
    [edgeRates],
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-3xl border border-slate-200 bg-white/85 p-3 shadow-sm backdrop-blur">
        <div className="flex items-center gap-2 text-xs">
          <Heartbeat active={inFlight > 0 || totalRate > 0} />
          <span className="font-semibold text-slate-700">Live agent graph</span>
          <span className="text-slate-400">·</span>
          <span className="text-slate-500">edges pulse green from port → port when an event fires</span>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <T label="In flight"      value={String(inFlight)}                        tone={inFlight > 0 ? "green" : "default"} />
          <T label="Pending"        value={String(snapshot.stats.pending_tasks)}    tone={snapshot.stats.pending_tasks > 0 ? "blue" : "default"} />
          <T label="Findings 24h"   value={String(snapshot.stats.findings_today)}   tone="green" />
          <T label="High signal"    value={String(snapshot.stats.high_signal_today)} tone="green" />
          <T label="Slop"           value={String(snapshot.stats.slop_today)}       tone={snapshot.stats.slop_today > 0 ? "red" : "default"} />
          <T label="Phase"          value={snapshot.state.current_phase}            tone="amber" />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <section className="lg:col-span-9">
          <ZoomableCanvas>
            <div className="absolute inset-0 bg-[radial-gradient(rgba(148,163,184,.18)_1px,_transparent_1px)] bg-[size:24px_24px]" />

            <div className="pointer-events-none absolute inset-x-0 top-3 z-10 grid grid-cols-5 px-3 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
              <div className="text-center">Sources</div>
              <div className="text-center">Pipeline</div>
              <div className="text-center">Findings &amp; critics</div>
              <div className="text-center">Strategic</div>
              <div className="text-center">Governance</div>
            </div>

            <svg className="absolute inset-0 h-full w-full overflow-visible" viewBox="0 0 160 100" preserveAspectRatio="none">
              <FlowDefs />
              {[19, 39, 59.5, 80].map((cssPct) => {
                const x = cssPct * 1.6;
                return (
                  <line key={cssPct} x1={x} y1="6" x2={x} y2="98" stroke="#cbd5e1" strokeWidth="0.2" strokeDasharray="0.8 1.2" opacity="0.45" />
                );
              })}
              {EDGES.map((edge) => (
                <FlowEdge
                  key={edge.id}
                  edge={edge}
                  hot={isEdgeHot(edge)}
                  selected={selection?.kind === "edge" && selection.edge.id === edge.id}
                  connected={connectedEdgeIds.has(edge.id)}
                  rate={edgeRates.get(edge.id) ?? 0}
                  onSelect={() => setSelection({
                    kind: "edge",
                    edge,
                    pulsing: isEdgeHot(edge),
                    activity: activityByEvent.get(edge.event_type) ?? null,
                  })}
                />
              ))}
            </svg>

            {EDGES.map((edge) => (
              <EdgeLabel
                key={`l-${edge.id}`}
                edge={edge}
                hot={isEdgeHot(edge)}
                selected={selection?.kind === "edge" && selection.edge.id === edge.id}
                onSelect={() => setSelection({
                  kind: "edge",
                  edge,
                  pulsing: isEdgeHot(edge),
                  activity: activityByEvent.get(edge.event_type) ?? null,
                })}
              />
            ))}

            {NODES.map((node) => {
              const isSelected = selection?.kind === "node" && selection.node.id === node.id;
              const ports = portsByNode.get(node.id) ?? [];
              const isHot = ports.some((p) => p.hot);
              return (
                <FlowNode
                  key={node.id}
                  node={node}
                  status={statuses[node.id]}
                  selected={isSelected}
                  hot={isHot}
                  ringConnected={ringConnectedNodes.has(node.id)}
                  ports={ports}
                  onSelect={() => setSelection({ kind: "node", node, status: statuses[node.id] })}
                />
              );
            })}
          </ZoomableCanvas>
        </section>

        <aside className="lg:col-span-3 space-y-4">
          <Inspector selection={selection} />
          <IntegrityPanel />
        </aside>

        <section className="lg:col-span-12">
          <EventStream
            recent={recent}
            connected={connected}
            onHover={(et) => setHoverEventType(et)}
            onPick={(et) => {
              const edge = EDGES.find((e) => e.event_type === et);
              if (!edge) return;
              setSelection({
                kind: "edge",
                edge,
                pulsing: isEdgeHot(edge),
                activity: activityByEvent.get(edge.event_type) ?? null,
              });
              setHoverEventType(et);
              setTimeout(() => setHoverEventType((cur) => (cur === et ? null : cur)), 2500);
            }}
          />
        </section>
      </div>
    </div>
  );
}

function T({ label, value, tone }: { label: string; value: string; tone: "green" | "amber" | "red" | "blue" | "default" }) {
  // Animate purely-numeric values so the user can see counters tick in real
  // time when the snapshot refreshes. Non-numeric labels (e.g. "exploration")
  // pass through unchanged.
  const numeric = /^\d+$/.test(value);
  const rendered = numeric ? <AnimatedNumber value={parseInt(value, 10)} /> : value;
  return (
    <div className="rounded-2xl bg-slate-50 px-3 py-1.5">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <Badge tone={tone}>{rendered}</Badge>
    </div>
  );
}

function Heartbeat({ active }: { active: boolean }) {
  // A subtle pulsing dot — green when the system has activity, slate when
  // idle. Pulse rate is identical in both states; what changes is whether
  // it lights up. This is the "is the system alive" indicator.
  return (
    <span className="relative inline-flex h-3 w-3 items-center justify-center" aria-label={active ? "live" : "idle"}>
      <motion.span
        className={cx(
          "absolute inline-flex h-full w-full rounded-full",
          active ? "bg-emerald-400" : "bg-slate-300",
        )}
        animate={{ opacity: [0.25, 0.7, 0.25], scale: [1, 1.45, 1] }}
        transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }}
      />
      <span
        className={cx(
          "relative inline-flex h-1.5 w-1.5 rounded-full",
          active ? "bg-emerald-500" : "bg-slate-400",
        )}
      />
    </span>
  );
}

function AnimatedNumber({ value, durationMs = 600 }: { value: number; durationMs?: number }) {
  const [display, setDisplay] = useState(value);
  const fromRef = useRef(value);
  useEffect(() => {
    if (display === value) return;
    fromRef.current = display;
    const start = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplay(Math.round(fromRef.current + (value - fromRef.current) * eased));
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, durationMs]);
  return <span className="tabular-nums">{display.toLocaleString()}</span>;
}
