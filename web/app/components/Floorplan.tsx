"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Boxes, CheckCircle2, Clock, Compass, Database, Download, FileCode, FileText, Github, Globe, Network, Search, ShieldAlert, ShieldCheck } from "lucide-react";
import { api, type CorpusHit, type GatePanel, type KnowledgeStats, type MimirPanel, type RecentIngest, type ScoutPanel } from "../lib/api";
import { useEventStream } from "../lib/ws";
import type { LabFoundryEvent, Snapshot, StreamMessage } from "../lib/types";

// =========================================================================
// LabFoundry floorplan — the lab as an architectural blueprint. Each room is
// an agent/subsystem; solid rooms are LIVE, dashed rooms are PLANNED. The
// arrows between rooms animate (particles flowing). Click any room to open an
// inspector showing what that agent is doing right now (live events + corpus
// stats; the Library panel can search the corpus).
// =========================================================================

const VW = 1500;
const VH = 1100;

type RoomId =
  | "web" | "arxiv" | "github" | "openml"
  | "ariadne" | "planner" | "researchers"
  | "dataset" | "mimir" | "library"
  | "critic" | "gate" | "ops" | "experiments" | "publication";

interface Room {
  id: RoomId;
  x: number; y: number; w: number; h: number;
  title: string;
  sub: string;
  active: boolean;
  door?: { side: "bottom" | "left" | "right"; at: number };
}

// Layout — generous, even gaps; a real divider between the two top wings;
// breathing room above the south wall. Status reflects what is LIVE now.
const ROOMS: Room[] = [
  // --- COLLECTORS (north-west wing) ---
  { id: "web",    x: 60,  y: 96, w: 186, h: 202, title: "Web Scout",    sub: "Web monitoring & discovery",   active: true,  door: { side: "bottom", at: 0.62 } },
  { id: "arxiv",  x: 252, y: 96, w: 180, h: 202, title: "arXiv Scout",  sub: "Scientific papers monitoring", active: true,  door: { side: "bottom", at: 0.62 } },
  { id: "github", x: 438, y: 96, w: 182, h: 202, title: "GitHub Scout", sub: "Code repositories monitoring", active: true,  door: { side: "bottom", at: 0.62 } },
  { id: "openml", x: 626, y: 96, w: 180, h: 202, title: "OpenML Scout", sub: "ML datasets & benchmarks",     active: false, door: { side: "bottom", at: 0.62 } },
  // --- RESEARCH & DISCOVERY (north-east wing) ---
  { id: "ariadne",     x: 852,  y: 96, w: 188, h: 202, title: "Ariadne",     sub: "Principal investigator / scientific direction", active: false, door: { side: "bottom", at: 0.6 } },
  { id: "planner",     x: 1046, y: 96, w: 144, h: 202, title: "Planner",     sub: "Schedules & goals planning",                    active: false, door: { side: "bottom", at: 0.6 } },
  { id: "researchers", x: 1196, y: 96, w: 224, h: 202, title: "Researchers", sub: "Investigate directions & gather findings",      active: false, door: { side: "bottom", at: 0.55 } },
  // --- KNOWLEDGE CORE (centre-west) ---
  { id: "dataset", x: 60,  y: 404, w: 188, h: 160, title: "Dataset Scout", sub: "External data monitoring", active: true, door: { side: "bottom", at: 0.78 } },
  { id: "mimir",   x: 360, y: 388, w: 288, h: 196, title: "Mimir",   sub: "AI Curator of Knowledge",   active: true },
  { id: "library", x: 324, y: 612, w: 348, h: 222, title: "Library", sub: "Queryable research memory", active: true },
  // --- RESEARCH WORKFLOW (east column) ---
  { id: "critic", x: 1238, y: 346, w: 188, h: 150, title: "Critic", sub: "Challenges claims & tests weaknesses", active: false, door: { side: "left", at: 0.4 } },
  { id: "gate",   x: 1238, y: 512, w: 188, h: 150, title: "Gate",   sub: "Promotion review & claim approval",    active: false, door: { side: "left", at: 0.4 } },
  { id: "ops",    x: 1238, y: 698, w: 188, h: 174, title: "Ops",    sub: "Infrastructure, budget & monitoring",  active: false },
  // --- EVALUATION & OUTPUT (south-centre) ---
  { id: "experiments", x: 742, y: 698, w: 216, h: 174, title: "Experiments", sub: "Run benchmarks & evaluations", active: false },
  { id: "publication", x: 964, y: 698, w: 226, h: 174, title: "Publication", sub: "Write, assemble & publish",     active: false },
];

const roomById = (id: RoomId): Room => ROOMS.find((r) => r.id === id) as Room;
function anchor(id: RoomId, side: "top" | "bottom" | "left" | "right", t = 0.5): { x: number; y: number } {
  const r = roomById(id);
  if (side === "top")    return { x: r.x + r.w * t, y: r.y };
  if (side === "bottom") return { x: r.x + r.w * t, y: r.y + r.h };
  if (side === "left")   return { x: r.x, y: r.y + r.h * t };
  return { x: r.x + r.w, y: r.y + r.h * t };
}

interface RoomInfo {
  what: string;
  events: string[];
  sourceKind?: string;
  role?: string;
  gate?: string;
  triggers?: string;   // prose: what wakes this agent
  emits?: string[];    // event types it produces
  pipeline?: string;   // prose: its place in the flow
}
const ROOM_INFO: Record<RoomId, RoomInfo> = {
  web:    { what: "Monitors the open web (via SearXNG) for relevant material and hands new sources to Mimir.", events: ["source.discovered"], sourceKind: "web",
    triggers: "a scheduled discovery sweep.", emits: ["source.discovered"], pipeline: "Open web → Mimir → Library." },
  arxiv:  { what: "Watches arXiv for new scientific papers and hands them to Mimir to ingest.", events: ["source.discovered"], sourceKind: "arxiv",
    triggers: "a scheduled discovery sweep.", emits: ["source.discovered"], pipeline: "arXiv → Mimir → Library." },
  github: { what: "Tracks code repositories on GitHub and surfaces them to Mimir, capturing each repo's license.", events: ["source.discovered"], sourceKind: "github",
    triggers: "a scheduled discovery sweep.", emits: ["source.discovered"], pipeline: "GitHub → Mimir → Library." },
  openml: { what: "Will monitor OpenML for ML datasets & benchmarks and feed them to Mimir.", events: [], gate: "scout not built yet",
    triggers: "planned scout — will monitor OpenML datasets.", emits: ["source.discovered"], pipeline: "OpenML → Mimir." },
  ariadne:     { what: "The Principal Investigator — frames research directions and decides what to pursue or kill.", events: ["claim.created", "claim.confidence_changed", "phase.transition_proposed", "phase.budget_exceeded"], role: "pi", gate: "research workflow (KNOWLEDGE_CORE_ONLY)",
    triggers: "exploration kickoff; a claim's confidence shifts or is invalidated; a phase budget is exceeded.", emits: ["claim.created", "phase.transition_proposed"], pipeline: "Sets direction → Planner → Researchers → Critic → Gate." },
  planner:     { what: "Turns research directions into concrete, falsifiable tasks.", events: ["queue.empty", "task.created"], role: "planner", gate: "research workflow",
    triggers: "the task queue empties (queue.empty).", emits: ["task.created"], pipeline: "Ariadne's direction → tasks → Researchers." },
  researchers: { what: "Investigate directions, gather evidence from the Library, and produce findings. Reads the Library and can ask Mimir to acquire a missing source.", events: ["task.created", "task.completed", "finding.high_signal", "acquire.requested"], role: "researcher", gate: "research workflow",
    triggers: "a task is created (task.created).", emits: ["finding.high_signal", "task.completed", "acquire.requested"], pipeline: "Planner's tasks → evidence from the Library → findings → Critic." },
  dataset: { what: "Tracks the AI/ML data landscape on the HuggingFace datasets hub, follows emerging trends, and hands newly discovered datasets to Mimir.", events: ["source.discovered", "library.trends"], sourceKind: "dataset",
    triggers: "a scheduled discovery sweep.", emits: ["source.discovered", "library.trends"], pipeline: "HuggingFace datasets → Mimir → Library." },
  mimir:   { what: "The Warden — one agent ingests every source and gates its trust (deterministic ladder + an LLM tie-breaker), then certifies it into the Library or quarantines it.", events: ["source.discovered", "document.parsed", "document.ingested", "mimir.ingest_blocked", "library.sweep_requested", "library.trends", "acquire.requested", "acquire.fulfilled", "acquire.rejected"],
    triggers: "source.discovered / acquire.requested / a scheduled sweep.", emits: ["document.ingested", "mimir.ingest_blocked", "library.trends"], pipeline: "Scouts & acquisitions → trust gate → Library." },
  library: { what: "The queryable research memory — certified documents in a pgvector (768-d) corpus plus a Neo4j knowledge graph.", events: ["document.ingested"],
    triggers: "Mimir certifies a document (document.ingested).", emits: ["document.ingested"], pipeline: "Mimir's certified docs → corpus + graph → Researchers query it." },
  critic:  { what: "Will challenge claims and probe their weaknesses before they advance.", events: ["finding.high_signal", "claim.invalidated"], role: "critic", gate: "research workflow",
    triggers: "a high-signal finding (finding.high_signal).", emits: ["claim.invalidated"], pipeline: "Researchers' findings → challenge → Gate." },
  gate:    { what: "Will run the promotion gate — approve, hold, reject, or merge a claim.", events: ["claim.confidence_changed"], gate: "research workflow",
    triggers: "a claim's confidence crosses a promotion threshold.", emits: [], pipeline: "Critic-tested claims → promote / hold / reject / merge." },
  ops:     { what: "Will watch infrastructure, budget and spend.", events: ["phase.budget_exceeded"], gate: "research workflow",
    triggers: "a phase budget is exceeded; infra/cost watch.", emits: [], pipeline: "Watches the whole lab — budget, spend, infrastructure." },
  experiments: { what: "Will run benchmarks and evaluations against the corpus.", events: [], gate: "planned",
    triggers: "planned — will run benchmarks against the corpus.", emits: [], pipeline: "Gate-approved claims → experiments → Evaluation." },
  publication: { what: "Will assemble and publish the lab's findings.", events: [], gate: "planned",
    triggers: "planned — will assemble and publish findings.", emits: [], pipeline: "Validated claims → write-up → publish." },
};

// Each scout's cumulative footprint in the corpus, keyed by documents_by_kind.
const SCOUT_KIND: Record<"web" | "arxiv" | "github" | "dataset", string> = {
  web: "web", arxiv: "paper", github: "code", dataset: "dataset",
};

// --- Flows (the animated arrows between agents) -------------------------
interface Flow { id: string; d: string; active: boolean; kind: "intake" | "knowledge" | "seed" | "workflow"; hotEvents: string[]; sourceKind?: string }
const MIMIR = roomById("mimir");
function intake(fromId: RoomId, toX: number): string {
  const a = anchor(fromId, "bottom", 0.62);
  const dropY = MIMIR.y - 54;
  return `M ${a.x} ${a.y} C ${a.x} ${a.y + 36}, ${toX} ${dropY - 22}, ${toX} ${MIMIR.y}`;
}
const SCOUT_EVENTS = ["source.discovered", "library.sweep_requested", "library.trends", "document.parsed"];
const LIB_EVENTS = ["document.ingested", "document.parsed"];
const datasetAnchor = anchor("dataset", "right", 0.5);
const mimirBottom = anchor("mimir", "bottom", 0.5);
const libraryTop = anchor("library", "top", 0.5);
const libraryRight = anchor("library", "right", 0.4);

const FLOWS: Flow[] = [
  { id: "f-web",    d: intake("web",    MIMIR.x + 72),  active: true,  kind: "intake", hotEvents: SCOUT_EVENTS, sourceKind: "web" },
  { id: "f-arxiv",  d: intake("arxiv",  MIMIR.x + 144), active: true,  kind: "intake", hotEvents: SCOUT_EVENTS, sourceKind: "arxiv" },
  { id: "f-github", d: intake("github", MIMIR.x + 216), active: true,  kind: "intake", hotEvents: SCOUT_EVENTS, sourceKind: "github" },
  { id: "f-openml", d: intake("openml", MIMIR.x + 252), active: false, kind: "intake", hotEvents: [] },
  { id: "f-dataset", active: true, kind: "intake", hotEvents: SCOUT_EVENTS, sourceKind: "dataset", d: `M ${datasetAnchor.x} ${datasetAnchor.y} L ${MIMIR.x} ${datasetAnchor.y}` },
  { id: "f-mimir-lib", active: true, kind: "knowledge", hotEvents: LIB_EVENTS, d: `M ${mimirBottom.x} ${mimirBottom.y} L ${libraryTop.x} ${libraryTop.y}` },
  { id: "f-workflow", active: false, kind: "workflow", hotEvents: [], d: `M ${libraryRight.x} ${libraryRight.y} L 742 ${libraryRight.y}` },
];

// =========================================================================
// Live data
// =========================================================================

function useFloorplanLive(): { hot: Set<string>; connected: boolean; events: LabFoundryEvent[] } {
  const { recent, connected } = useEventStream(80);
  const [hot, setHot] = useState<Set<string>>(new Set());
  const expiry = useRef<Map<string, number>>(new Map());
  const seen = useRef<Set<number>>(new Set());

  const events = useMemo(
    () =>
      recent
        .filter((m): m is Extract<StreamMessage, { type: "event" }> => m.type === "event")
        .map((m) => m.event as LabFoundryEvent),
    [recent],
  );

  useEffect(() => {
    const now = Date.now();
    let changed = false;
    for (const e of events) {
      if (seen.current.has(e.id)) continue;
      seen.current.add(e.id);
      const ek = sourceKindOf(e);
      for (const f of FLOWS) {
        if (!f.hotEvents.includes(e.event_type)) continue;
        // Scout flows carry a sourceKind: only heat them when the event's
        // source matches that scout's kind, so the animation tracks each
        // scout's own panel (which filters to the same source_kind).
        if (f.sourceKind && ek !== f.sourceKind) continue;
        expiry.current.set(f.id, now + 6000);
        changed = true;
      }
    }
    if (changed) setHot(new Set(expiry.current.keys()));
  }, [events]);

  useEffect(() => {
    const t = setInterval(() => {
      const now = Date.now();
      let changed = false;
      for (const [id, exp] of expiry.current) if (exp <= now) { expiry.current.delete(id); changed = true; }
      if (changed) setHot(new Set(expiry.current.keys()));
    }, 500);
    return () => clearInterval(t);
  }, []);

  return { hot, connected, events };
}

function fmtTime(iso: string): string {
  try { return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
  catch { return "—"; }
}
function ago(iso?: string | null): string {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function sourceKindOf(e: LabFoundryEvent): string | null {
  const src = (e.payload as { source?: { source_kind?: unknown } } | null | undefined)?.source;
  const k = src?.source_kind;
  return typeof k === "string" ? k : null;
}
// SVG <text> doesn't wrap; greedily split a subtitle into lines that fit the room.
function wrapText(text: string, maxChars: number): string[] {
  const words = text.split(" ");
  const lines: string[] = [];
  let cur = "";
  for (const w of words) {
    if (!cur) cur = w;
    else if ((cur + " " + w).length <= maxChars) cur += " " + w;
    else { lines.push(cur); cur = w; }
  }
  if (cur) lines.push(cur);
  return lines;
}
function reasonOf(e: LabFoundryEvent): string {
  const r = (e.payload as { reasons?: unknown } | null | undefined)?.reasons;
  return typeof r === "string" ? r : "blocked";
}

// =========================================================================
// Palette + SVG primitives
// =========================================================================

const C = {
  wall: "#3f4753", active: "#10b981", activeFill: "rgba(16,185,129,0.06)",
  plan: "#9aa3ad", seed: "#7c5cd6", ink: "#1f2d3d", muted: "#5b6b7b", faint: "#9aa3ad", intake: "#2c5fb8",
};
const TIER_COLORS: Record<string, string> = {
  peer_reviewed: "bg-emerald-500", preprint: "bg-emerald-400", official_repo: "bg-teal-400",
  web_reputable: "bg-blue-400", web_unknown: "bg-slate-300", user_asserted: "bg-violet-300", quarantined: "bg-red-300",
};

function DoorArc({ room }: { room: Room }) {
  if (!room.door) return null;
  const r = 26;
  if (room.door.side === "bottom") {
    const x = room.x + room.w * room.door.at;
    const y = room.y + room.h;
    return <path d={`M ${x - r} ${y} A ${r} ${r} 0 0 0 ${x + r} ${y}`} fill="none" stroke={C.wall} strokeWidth={2} opacity={0.5} />;
  }
  const y = room.y + room.h * room.door.at;
  const x = room.door.side === "left" ? room.x : room.x + room.w;
  const dir = room.door.side === "left" ? 1 : -1;
  return <path d={`M ${x} ${y - r} A ${r} ${r} 0 0 0 ${x + dir * r} ${y + r}`} fill="none" stroke={C.wall} strokeWidth={2} opacity={0.5} />;
}

function RoomBox({ room, phase, activeClaims, selected, onSelect }: {
  room: Room; phase: string | null; activeClaims: number | null; selected: boolean; onSelect: () => void;
}) {
  const stroke = room.active ? C.active : C.plan;
  const big = room.id === "mimir" || room.id === "library";
  return (
    <g style={{ cursor: "pointer" }} onClick={(e) => { e.stopPropagation(); onSelect(); }}>
      {room.active && (
        <motion.rect
          x={room.x - 3} y={room.y - 3} width={room.w + 6} height={room.h + 6} rx={16}
          fill="none" stroke={C.active} strokeWidth={2}
          initial={{ opacity: 0.1 }} animate={{ opacity: [0.08, 0.3, 0.08] }}
          transition={{ duration: 2.6, repeat: Infinity, ease: "easeInOut" }}
        />
      )}
      {selected && (
        <rect x={room.x - 7} y={room.y - 7} width={room.w + 14} height={room.h + 14} rx={18}
          fill="none" stroke={room.active ? C.active : C.intake} strokeWidth={3} opacity={0.9} />
      )}
      <rect
        x={room.x} y={room.y} width={room.w} height={room.h} rx={13}
        fill={room.active ? C.activeFill : "rgba(250,251,252,0.7)"}
        stroke={stroke} strokeWidth={room.active ? 2.4 : 1.6}
        strokeDasharray={room.active ? undefined : "7 5"}
      />
      <DoorArc room={room} />
      <text x={room.x + room.w / 2} y={room.y + (big ? room.h / 2 - 6 : 50)} textAnchor="middle"
        fontSize={big ? 26 : 21} fontWeight={700} fill={room.active ? C.ink : C.muted}>{room.title}</text>
      {(big ? [room.sub] : wrapText(room.sub, Math.max(12, Math.floor((room.w - 22) / 6.6)))).slice(0, 3).map((ln, i) => (
        <text key={i} x={room.x + room.w / 2} y={room.y + (big ? room.h / 2 + 22 : 74) + i * 16} textAnchor="middle" fontSize={13.5} fill={C.faint}>{ln}</text>
      ))}
      {room.id === "mimir" && phase && (
        <g>
          <rect x={room.x + room.w / 2 - 56} y={room.y + room.h - 44} width={112} height={24} rx={12} fill="rgba(16,185,129,0.1)" stroke={C.active} strokeWidth={1} />
          <text x={room.x + room.w / 2} y={room.y + room.h - 27} textAnchor="middle" fontSize={12} fontWeight={600} fill="#0f9b6e">phase · {phase}</text>
        </g>
      )}
      {room.id === "library" && activeClaims != null && (
        <text x={room.x + room.w / 2} y={room.y + room.h - 24} textAnchor="middle" fontSize={12.5} fill={C.muted}>
          {activeClaims} active research direction{activeClaims === 1 ? "" : "s"}
        </text>
      )}
      {!room.active && (
        <g>
          <rect x={room.x + room.w / 2 - 46} y={room.y + room.h - 44} width={92} height={22} rx={11} fill="none" stroke={C.plan} strokeWidth={1} strokeDasharray="4 3" />
          <text x={room.x + room.w / 2} y={room.y + room.h - 28} textAnchor="middle" fontSize={11.5} fill={C.faint}>Coming soon</text>
        </g>
      )}
      <circle cx={room.x + room.w - 16} cy={room.y + 16} r={4} fill={room.active ? C.active : C.faint} opacity={0.6} />
    </g>
  );
}

function FlowPath({ flow, hot }: { flow: Flow; hot: boolean }) {
  const color = flow.kind === "seed" ? C.seed : flow.active ? C.intake : C.plan;
  const particleColor = flow.kind === "seed" ? C.seed : C.active;
  const markerId = flow.kind === "seed" ? "seed" : flow.active ? "active" : "plan";
  // Motion is activity-gated: particles only travel while the flow is HOT (a
  // matching live event arrived recently). When cold, only the static path
  // renders — no <animateMotion>, so the lab is still unless something happens.
  const active = flow.active && hot;
  const count = active ? 3 : 0;
  const dur = 1.5;
  return (
    <g>
      <path d={flow.d} fill="none" stroke={color}
        strokeWidth={flow.active ? 2.4 : 1.6} strokeDasharray={flow.active ? undefined : "6 5"} strokeLinecap="round"
        style={{
          opacity: flow.active ? (hot ? 1 : 0.85) : 0.5,
          filter: active ? "drop-shadow(0 0 1.4px rgba(16,185,129,0.55))" : "none",
          transition: "opacity 0.5s ease, filter 0.5s ease",
        }}
        markerEnd={`url(#fp-arrow-${markerId})`} />
      {Array.from({ length: count }).map((_, i) => (
        // key includes `hot` so the particles mount fresh on each hot→cold→hot
        // transition; the <animateMotion> then begins cleanly from t=0 rather
        // than resuming a stale animation.
        <circle key={`hot-${i}`} r={i === 0 ? 4.2 : 3} fill={particleColor} opacity={i === 0 ? 1 : 0.7} style={{ filter: `drop-shadow(0 0 2px ${particleColor})` }}>
          <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`${(i * dur) / count}s`} path={flow.d} />
        </circle>
      ))}
    </g>
  );
}

function ZoneBracket({ x1, x2, y, label }: { x1: number; x2: number; y: number; label: string }) {
  const cx = (x1 + x2) / 2;
  const labelW = label.length * 9 + 18;
  const gapL = cx - labelW / 2, gapR = cx + labelW / 2;
  return (
    <g>
      <path d={`M ${x1} ${y + 10} L ${x1} ${y} L ${gapL} ${y}`} fill="none" stroke={C.faint} strokeWidth={1.4} />
      <path d={`M ${gapR} ${y} L ${x2} ${y} L ${x2} ${y + 10}`} fill="none" stroke={C.faint} strokeWidth={1.4} />
      <text x={cx} y={y + 5} textAnchor="middle" fontSize={14} fontWeight={700} letterSpacing="1.6" fill={C.muted}>{label}</text>
    </g>
  );
}

// =========================================================================
// Inspector primitives
// =========================================================================

function StatTile({ label, value, tone = "slate" }: { label: string; value: string | number; tone?: "slate" | "emerald" | "blue" | "violet" | "red" | "amber" }) {
  const tones: Record<string, string> = {
    slate: "bg-slate-50 text-slate-800", emerald: "bg-emerald-50 text-emerald-700", blue: "bg-blue-50 text-blue-700",
    violet: "bg-violet-50 text-violet-700", red: "bg-red-50 text-red-700", amber: "bg-amber-50 text-amber-700",
  };
  return (
    <div className={`rounded-2xl px-3 py-2 ${tones[tone]}`}>
      <div className="text-[10px] font-semibold uppercase tracking-wide opacity-70">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tabular-nums">{typeof value === "number" ? value.toLocaleString() : value}</div>
    </div>
  );
}

function TierBars({ tiers }: { tiers: Record<string, number> }) {
  const order = ["peer_reviewed", "preprint", "official_repo", "web_reputable", "web_unknown", "user_asserted", "quarantined"];
  const total = Object.values(tiers).reduce((a, b) => a + b, 0) || 1;
  const present = order.filter((t) => tiers[t]);
  return (
    <div className="space-y-1.5">
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
        {present.map((t) => <div key={t} className={TIER_COLORS[t] ?? "bg-slate-300"} style={{ width: `${(tiers[t] / total) * 100}%` }} title={`${t}: ${tiers[t]}`} />)}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-500">
        {present.map((t) => (
          <span key={t} className="flex items-center gap-1">
            <span className={`inline-block h-2 w-2 rounded-full ${TIER_COLORS[t] ?? "bg-slate-300"}`} />
            {t.replace(/_/g, " ")} · {tiers[t].toLocaleString()}
          </span>
        ))}
      </div>
    </div>
  );
}

function SubHead({ label }: { label: string }) {
  return <div className="mb-1.5 mt-4 text-[10px] font-semibold uppercase tracking-wider text-slate-400">{label}</div>;
}

function EventRows({ events, empty }: { events: LabFoundryEvent[]; empty: string }) {
  if (events.length === 0) return <p className="text-sm text-slate-400">{empty}</p>;
  return (
    <ul className="space-y-1">
      {events.map((e) => (
        <li key={e.id} className="flex items-center gap-2 rounded-xl bg-slate-50 px-2.5 py-1.5 text-xs">
          <span className="w-14 shrink-0 font-mono text-slate-400">{fmtTime(e.emitted_at)}</span>
          <span className="min-w-0 flex-1 truncate font-mono font-semibold text-slate-700">{e.event_type}</span>
          {e.target_id != null && <span className="font-mono text-[10px] text-slate-400">{e.target_type}#{e.target_id}</span>}
        </li>
      ))}
    </ul>
  );
}

// --- Library corpus search (inline) -------------------------------------

function CorpusSearch() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<CorpusHit[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = async () => {
    const query = q.trim();
    if (!query) return;
    setLoading(true); setErr(null);
    try {
      const res = await api.corpusSearch(query, 6);
      setHits(res.hits);
      if (res.status !== "ok") setErr(res.error ?? "search unavailable");
    } catch (e) { setErr(String(e)); setHits([]); }
    finally { setLoading(false); }
  };

  return (
    <div>
      <SubHead label="Search the corpus" />
      <div className="flex gap-2">
        <div className="relative min-w-0 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") run(); }}
            placeholder="e.g. mixture-of-experts routing"
            className="w-full rounded-xl border border-slate-200 bg-white py-2 pl-9 pr-3 text-sm focus:border-emerald-300 focus:outline-none"
          />
        </div>
        <button type="button" onClick={run} disabled={loading || !q.trim()}
          className="shrink-0 rounded-xl bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-50">
          {loading ? "…" : "Search"}
        </button>
      </div>
      <p className="mt-1.5 text-[11px] text-slate-400">Search across titles, abstracts, code, entities, and full text.</p>
      {err && <p className="mt-2 text-xs text-red-500">{err}</p>}
      {hits && hits.length === 0 && !err && <p className="mt-2 text-sm text-slate-400">No matches.</p>}
      {hits && hits.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {hits.map((h) => (
            <li key={`${h.document_id}-${h.score}`} className="rounded-2xl border border-slate-200 bg-slate-50 p-2.5 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-1.5">
                  <span className={`inline-block h-2 w-2 rounded-full ${TIER_COLORS[h.trust_tier] ?? "bg-slate-300"}`} />
                  <span className="text-slate-400">{h.trust_tier.replace(/_/g, " ")}</span>
                </span>
                <span className="font-mono text-slate-400">score {h.score.toFixed(2)}</span>
              </div>
              {h.source_url ? (
                <a href={h.source_url} target="_blank" rel="noreferrer" className="mt-1 block font-medium text-emerald-700 hover:underline line-clamp-2">
                  {h.title || h.source_url}
                </a>
              ) : (
                <div className="mt-1 font-medium text-slate-700 line-clamp-2">{h.title || `doc #${h.document_id}`}</div>
              )}
              <p className="mt-1 line-clamp-2 text-slate-500">{h.snippet}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// =========================================================================
// Rich Library inspector pieces
// =========================================================================

const KIND_HEX: Record<string, string> = {
  paper: "#3b82f6", web: "#10b981", code: "#f59e0b", dataset: "#8b5cf6",
  media: "#ec4899", note: "#64748b", log: "#94a3b8",
};
const TIER_HEX: Record<string, string> = {
  peer_reviewed: "#059669", preprint: "#10b981", official_repo: "#14b8a6",
  web_reputable: "#3b82f6", web_unknown: "#94a3b8", user_asserted: "#a78bfa", quarantined: "#f87171",
};
const TILE_ACCENT: Record<string, string> = {
  slate: "bg-slate-100 text-slate-500", emerald: "bg-emerald-50 text-emerald-600", blue: "bg-blue-50 text-blue-600",
  violet: "bg-violet-50 text-violet-600", amber: "bg-amber-50 text-amber-600",
};

function StatCard({ icon: Icon, label, value, sub, subGreen = false, accent = "slate" }: {
  icon: typeof FileText; label: string; value: string | number; sub?: string; subGreen?: boolean; accent?: string;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-3">
      <span className={`inline-flex rounded-lg p-1.5 ${TILE_ACCENT[accent] ?? TILE_ACCENT.slate}`}><Icon className="h-4 w-4" /></span>
      <div className="mt-2 text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-0.5 text-xl font-semibold tabular-nums text-slate-900">{typeof value === "number" ? value.toLocaleString() : value}</div>
      {sub && <div className={`mt-0.5 text-[11px] ${subGreen ? "text-emerald-600" : "text-slate-400"}`}>{sub}</div>}
    </div>
  );
}

function Composition({ kinds }: { kinds: Record<string, number> }) {
  const entries = Object.entries(kinds).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((s, [, v]) => s + v, 0) || 1;
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap gap-x-5 gap-y-3">
        {entries.map(([k, v]) => {
          const pct = (v / total) * 100;
          const hex = KIND_HEX[k] ?? "#94a3b8";
          return (
            <div key={k} className="min-w-[84px] flex-1">
              <div className="text-[10px] font-semibold uppercase tracking-wide" style={{ color: hex }}>{k}</div>
              <div className="mt-0.5 text-sm font-semibold tabular-nums text-slate-800">
                {v.toLocaleString()} <span className="text-[11px] font-normal text-slate-400">({pct.toFixed(0)}%)</span>
              </div>
              <div className="mt-1 h-1.5 w-full rounded-full bg-slate-100">
                <div className="h-full rounded-full" style={{ width: `${Math.max(pct, 3)}%`, background: hex }} />
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-3 text-[11px] text-slate-400">Total {total.toLocaleString()} items</div>
    </div>
  );
}

function TrustBreakdown({ tiers }: { tiers: Record<string, number> }) {
  const order = ["peer_reviewed", "preprint", "official_repo", "web_reputable", "web_unknown", "user_asserted", "quarantined"];
  const total = Object.values(tiers).reduce((a, b) => a + b, 0) || 1;
  const present = order.filter((t) => tiers[t]);
  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-slate-100">
        {present.map((t) => <div key={t} style={{ width: `${(tiers[t] / total) * 100}%`, background: TIER_HEX[t] ?? "#94a3b8" }} title={`${t}: ${tiers[t]}`} />)}
      </div>
      <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
        {present.map((t) => (
          <span key={t} className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: TIER_HEX[t] ?? "#94a3b8" }} />
            <span className="text-slate-600">{t.replace(/_/g, " ")}</span>
            <span className="text-slate-400">{tiers[t].toLocaleString()} ({((tiers[t] / total) * 100).toFixed(1)}%)</span>
          </span>
        ))}
      </div>
      <div className="mt-1.5 text-right text-[11px] text-slate-400">Total {total.toLocaleString()} items</div>
    </div>
  );
}

function KgTile({ label, value, caption, color }: { label: string; value: string | number; caption: string; color: string }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-3">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-0.5 text-xl font-semibold tabular-nums" style={{ color }}>{typeof value === "number" ? value.toLocaleString() : value}</div>
      <div className="mt-0.5 text-[11px] text-slate-400">{caption}</div>
    </div>
  );
}

const SOURCE_META: Record<string, { icon: typeof FileText; bg: string; fg: string }> = {
  arxiv:  { icon: FileText, bg: "#fee2e2", fg: "#dc2626" },
  github: { icon: Github,   bg: "#f1f5f9", fg: "#0f172a" },
  web:    { icon: Globe,    bg: "#ecfdf5", fg: "#059669" },
  doi:    { icon: FileText, bg: "#eef2ff", fg: "#4f46e5" },
  dataset:{ icon: Database, bg: "#f5f3ff", fg: "#7c3aed" },
  openml: { icon: Database, bg: "#f5f3ff", fg: "#7c3aed" },
  code:   { icon: FileCode, bg: "#fffbeb", fg: "#d97706" },
};

function IngestRow({ it }: { it: RecentIngest }) {
  const src = SOURCE_META[it.source_kind] ?? SOURCE_META.web;
  const Icon = src.icon;
  const isNew = it.at ? Date.now() - new Date(it.at).getTime() < 3_600_000 : false;
  const meta = it.arxiv_id ? `arXiv:${it.arxiv_id}` : it.source_kind;
  const blocked = it.status === "blocked" || it.status === "quarantined";
  return (
    <li className="flex items-start gap-2.5 rounded-2xl border border-slate-200 bg-white p-2.5">
      <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg" style={{ background: src.bg, color: src.fg }}>
        <Icon className="h-4 w-4" />
      </span>
      <div className="min-w-0 flex-1">
        {it.source_url ? (
          <a href={it.source_url} target="_blank" rel="noreferrer" className="block truncate text-sm font-medium text-slate-800 hover:text-emerald-700">{it.title || `doc #${it.id}`}</a>
        ) : (
          <div className="truncate text-sm font-medium text-slate-800">{it.title || `doc #${it.id}`}</div>
        )}
        <div className="mt-0.5 truncate text-[11px] text-slate-400">{meta}{it.at ? ` · Ingested ${ago(it.at)}` : ""}</div>
      </div>
      {blocked
        ? <span className="shrink-0 rounded-full border border-red-200 bg-red-50 px-2 py-0.5 text-[10px] font-semibold text-red-600">Blocked</span>
        : isNew && <span className="shrink-0 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">New</span>}
    </li>
  );
}

function LatestIngests() {
  const [data, setData] = useState<{ today: number; items: RecentIngest[] } | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () => api.recentIngests(8).then((d) => { if (!cancelled) setData({ today: d.today, items: d.items }); }).catch(() => {});
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);
  const items = (data?.items ?? []).slice(0, 5);
  return (
    <div>
      <div className="mb-1.5 mt-4 flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Latest ingests</span>
        <a href="/events" className="text-[11px] font-medium text-emerald-700 hover:underline">View all</a>
      </div>
      {items.length === 0 ? (
        <p className="text-sm text-slate-400">No ingests yet.</p>
      ) : (
        <ul className="space-y-1.5">{items.map((it) => <IngestRow key={it.id} it={it} />)}</ul>
      )}
      {data && <p className="mt-2 text-[11px] text-slate-400">Showing {items.length} of {data.today} ingests today</p>}
    </div>
  );
}

function LibraryInspector({ knowledge, snapshot }: { knowledge: KnowledgeStats | null; snapshot: Snapshot | null }) {
  const corpus = knowledge?.corpus;
  const graph = knowledge?.graph;
  const memory = knowledge?.memory;
  const totalDocs = corpus ? Object.values(corpus.documents_by_kind).reduce((a, b) => a + b, 0) : 0;
  const embedPct = corpus && corpus.chunks > 0 ? (corpus.chunks_embedded / corpus.chunks) * 100 : 0;
  const directions = snapshot?.state?.active_claims_count ?? memory?.claims ?? 0;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2">
        <StatCard icon={FileText} label="Documents" value={totalDocs} accent="emerald"
          sub={corpus && corpus.docs_today > 0 ? `+${corpus.docs_today.toLocaleString()} today` : "in the corpus"} subGreen={!!corpus && corpus.docs_today > 0} />
        <StatCard icon={Boxes} label="Embedded chunks" value={corpus?.chunks_embedded ?? 0} accent="blue"
          sub={corpus ? `${embedPct.toFixed(embedPct >= 99.95 ? 0 : 1)}% embedded` : undefined} />
        <StatCard icon={Network} label="Graph entities" value={graph?.status === "ok" ? (graph.nodes ?? 0) : "—"} accent="violet"
          sub={graph?.status === "ok" ? `${(graph.papers ?? 0).toLocaleString()} papers · Neo4j` : "graph offline"} />
        <StatCard icon={Compass} label="Active directions" value={directions} accent="amber"
          sub={snapshot?.state?.current_phase ? `phase · ${snapshot.state.current_phase}` : "research agenda"} />
      </div>

      {corpus && Object.keys(corpus.documents_by_kind).length > 0 && (
        <div><SubHead label="Corpus composition" /><Composition kinds={corpus.documents_by_kind} /></div>
      )}

      {corpus && Object.keys(corpus.docs_by_trust_tier).length > 0 && (
        <div><SubHead label="Trust tier breakdown" /><TrustBreakdown tiers={corpus.docs_by_trust_tier} /></div>
      )}

      <div>
        <SubHead label="Knowledge graph (structured memory)" />
        <div className="grid grid-cols-2 gap-2">
          <KgTile label="KG papers" value={graph?.status === "ok" ? (graph.papers ?? 0) : "—"} caption="in graph" color="#7c3aed" />
          <KgTile label="Datasets" value={(graph?.datasets ?? corpus?.datasets) ?? 0} caption="in graph" color="#0f766e" />
          <KgTile label="Experiments" value={memory?.experiments ?? 0} caption="runs" color="#d97706" />
          <KgTile label="Claims" value={memory?.claims ?? 0} caption="research directions" color="#059669" />
        </div>
      </div>

      <CorpusSearch />
      <LatestIngests />
    </div>
  );
}

// =========================================================================
// Rich Mimir (Warden) inspector — backed by GET /knowledge/mimir
// =========================================================================

const LADDER_LABEL: Record<string, string> = {
  official_repo: "official repo", web_reputable: "web reputable", web_unknown: "web unknown",
};
const MIX_LABEL: Record<string, string> = { arxiv: "arXiv", web: "Web", github: "GitHub", dataset: "Dataset" };
const MIX_HEX: Record<string, string> = { arxiv: "#3b82f6", web: "#10b981", github: "#f59e0b", dataset: "#8b5cf6" };
const REQ_BADGE: Record<string, string> = {
  requested: "border-violet-200 bg-violet-50 text-violet-700",
  fulfilled: "border-emerald-200 bg-emerald-50 text-emerald-700",
  rejected: "border-red-200 bg-red-50 text-red-600",
};

function FunnelStep({ label, value, last = false }: { label: string; value: number; last?: boolean }) {
  return (
    <div>
      <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-3 py-1.5">
        <span className="text-xs text-slate-600">{label}</span>
        <span className="text-sm font-semibold tabular-nums text-slate-900">{value.toLocaleString()}</span>
      </div>
      {!last && <div className="mx-auto my-0.5 h-2 w-px bg-slate-200" />}
    </div>
  );
}

function MimirWardenPanel({ panel }: { panel: MimirPanel }) {
  const g = panel.at_a_glance;
  const ingestDelta = g.ingested_today - g.ingested_yesterday;
  const ladder = Object.entries(panel.trust_ladder).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  const mix = panel.source_mix.filter((m) => m.count > 0);

  return (
    <div className="space-y-4">
      {/* Warden banner */}
      <div className="flex items-start gap-2.5 rounded-2xl border border-emerald-200 bg-emerald-50/70 p-3">
        <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-emerald-100 text-emerald-600">
          <ShieldCheck className="h-4 w-4" />
        </span>
        <p className="text-xs leading-snug text-emerald-900">
          <span className="font-semibold">Mimir is the Warden of Knowledge.</span>{" "}
          It ingests research from across the lab, evaluates trust, and certifies reliable content into the Library — or quarantines it.
        </p>
      </div>

      {/* At a glance */}
      <div>
        <SubHead label="At a glance" />
        <div className="grid grid-cols-2 gap-2">
          <StatCard icon={ShieldCheck} label="Certified" value={g.certified} accent="emerald"
            sub={`+${g.certified_today.toLocaleString()} today`} subGreen={g.certified_today > 0} />
          <StatCard icon={ShieldAlert} label="Quarantined" value={g.quarantined} accent="amber"
            sub={`+${g.quarantined_today.toLocaleString()} today`} />
          <StatCard icon={Clock} label="Pending review" value={g.pending} accent="amber" />
          <StatCard icon={Download} label="Ingested today" value={g.ingested_today} accent="blue"
            sub={`${ingestDelta >= 0 ? "+" : ""}${ingestDelta.toLocaleString()} vs yesterday`} />
        </div>
      </div>

      {/* Trust ladder · Intake pipeline · Source mix */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <div className="lg:col-span-3">
          <SubHead label="Trust ladder" />
          {ladder.length === 0 ? (
            <p className="text-sm text-slate-400">No certified sources yet.</p>
          ) : (
            <ul className="space-y-1">
              {ladder.map(([tier, count]) => (
                <li key={tier} className="flex items-center gap-2 text-[11px]">
                  <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: TIER_HEX[tier] ?? "#94a3b8" }} />
                  <span className="flex-1 text-slate-600">{LADDER_LABEL[tier] ?? tier.replace(/_/g, " ")}</span>
                  <span className="font-semibold tabular-nums text-slate-700">{count.toLocaleString()}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="lg:col-span-3">
          <SubHead label="Intake pipeline (today)" />
          <FunnelStep label="Discovered" value={panel.pipeline_today.discovered} />
          <FunnelStep label="Parsed" value={panel.pipeline_today.parsed} />
          <FunnelStep label="Ingested" value={panel.pipeline_today.ingested} />
          <FunnelStep label="Quarantined" value={panel.pipeline_today.quarantined} last />
        </div>

        <div className="lg:col-span-3">
          <SubHead label="Source mix" />
          {mix.length === 0 ? (
            <p className="text-sm text-slate-400">No sources yet.</p>
          ) : (
            <div className="space-y-1.5">
              {mix.map((m) => {
                const hex = MIX_HEX[m.kind] ?? "#94a3b8";
                return (
                  <div key={m.kind}>
                    <div className="flex items-center justify-between text-[11px]">
                      <span className="text-slate-600">{MIX_LABEL[m.kind] ?? m.kind}</span>
                      <span className="text-slate-400">{m.count.toLocaleString()} · {m.pct}%</span>
                    </div>
                    <div className="mt-0.5 h-1.5 w-full rounded-full bg-slate-100">
                      <div className="h-full rounded-full" style={{ width: `${Math.max(m.pct, 2)}%`, background: hex }} />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Recent certifications */}
      <div>
        <SubHead label="Recent certifications" />
        {panel.recent_certifications.length === 0 ? (
          <p className="text-sm text-slate-400">No certifications in the live window.</p>
        ) : (
          <ul className="space-y-1.5">
            {panel.recent_certifications.map((c, i) => {
              const badge = c.arxiv_id ? `arXiv:${c.arxiv_id}` : c.canonical_key ?? c.source_kind;
              return (
                <li key={`${c.canonical_key ?? c.arxiv_id ?? c.title ?? "cert"}-${i}`} className="flex items-start gap-2 rounded-2xl border border-slate-200 bg-white p-2.5">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                  <div className="min-w-0 flex-1">
                    <div className="line-clamp-1 text-sm font-medium text-slate-800">{c.title ?? badge}</div>
                    <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-slate-400">
                      <span className="truncate rounded-full bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">{badge}</span>
                      <span>{ago(c.at)}</span>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* Requests */}
      <div>
        <SubHead label="Requests" />
        {panel.requests.length === 0 ? (
          <p className="text-sm text-slate-400">No acquire requests yet — agents request sources here once the research workflow is live.</p>
        ) : (
          <ul className="space-y-1.5">
            {panel.requests.map((r, i) => (
              <li key={`${r.requester}-${i}`} className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white p-2.5 text-xs">
                <span className="shrink-0 font-medium text-slate-700">{r.requester}</span>
                <span className="min-w-0 flex-1 truncate text-slate-500">{r.ask ?? "—"}</span>
                <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${REQ_BADGE[r.status] ?? "border-slate-200 bg-slate-50 text-slate-500"}`}>{r.status}</span>
                <span className="shrink-0 text-[10px] text-slate-400">{ago(r.at)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function MimirInspector({ knowledge, roomEvents, blocked }: {
  knowledge: KnowledgeStats | null; roomEvents: LabFoundryEvent[]; blocked: LabFoundryEvent[];
}) {
  const [panel, setPanel] = useState<MimirPanel | null>(null);
  const [loading, setLoading] = useState(true);
  const corpus = knowledge?.corpus;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.mimirPanel()
      .then((p) => { if (!cancelled) setPanel(p); })
      .catch(() => { if (!cancelled) setPanel(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  if (panel && panel.status === "ok") return <MimirWardenPanel panel={panel} />;

  // Fallback: corpus-based tiles (also the loading view) ------------------
  return (
    <div className="space-y-3">
      {loading
        ? <p className="text-sm text-slate-400">Loading Warden stats…</p>
        : <p className="text-sm text-slate-400">Live stats unavailable.</p>}
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="Today" value={corpus?.docs_today ?? 0} tone="blue" />
        <StatTile label="Certified" value={corpus?.by_status?.certified ?? 0} tone="emerald" />
        <StatTile label="Quarantined" value={corpus?.by_status?.quarantined ?? 0} tone="red" />
      </div>
      {corpus && Object.keys(corpus.docs_by_trust_tier).length > 0 && (
        <div><SubHead label="Trust ladder" /><TierBars tiers={corpus.docs_by_trust_tier} /></div>
      )}
      <div>
        <SubHead label={`Recently blocked${blocked.length ? ` (${blocked.length})` : ""}`} />
        {blocked.length === 0 ? (
          <p className="text-sm text-slate-400">Nothing blocked in the live window.</p>
        ) : (
          <ul className="space-y-1.5">
            {blocked.map((e) => (
              <li key={e.id} className="rounded-2xl border border-red-100 bg-red-50/60 p-2 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-red-600">doc #{e.target_id}</span>
                  <span className="text-slate-400">{fmtTime(e.emitted_at)}</span>
                </div>
                <p className="mt-0.5 line-clamp-2 text-slate-600">{reasonOf(e)}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <SubHead label="Recent ingest activity" />
        <EventRows events={roomEvents} empty="Quiet right now — Mimir sweeps on a schedule." />
      </div>
    </div>
  );
}

// =========================================================================
// Scout inspector — durable recent findings, backed by GET /knowledge/scout
// =========================================================================

type ScoutItem = ScoutPanel["recent"][number];

function arxivAbsUrl(id: string): string {
  return `https://arxiv.org/abs/${id}`;
}

function ScoutRow({ kind, it }: { kind: string; it: ScoutItem }) {
  const at = <span className="ml-2 shrink-0 text-[10px] text-slate-400">{ago(it.at)}</span>;

  if (kind === "arxiv") {
    return (
      <li className="rounded-2xl border border-slate-200 bg-white p-2 text-xs">
        <div className="flex items-start justify-between gap-2">
          <div className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{it.title ?? "Untitled paper"}</div>
          {at}
        </div>
        {it.arxiv_id && (
          <a href={arxivAbsUrl(it.arxiv_id)} target="_blank" rel="noreferrer"
            className="mt-1 inline-block rounded-full bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-emerald-700 hover:underline">
            arXiv:{it.arxiv_id}
          </a>
        )}
        {it.snippet && <p className="mt-1 line-clamp-2 text-slate-500">{it.snippet}</p>}
      </li>
    );
  }

  if (kind === "web") {
    return (
      <li className="rounded-2xl border border-slate-200 bg-white p-2 text-xs">
        <div className="flex items-start justify-between gap-2">
          {it.source_url ? (
            <a href={it.source_url} target="_blank" rel="noreferrer" className="line-clamp-2 min-w-0 flex-1 font-medium text-emerald-700 hover:underline">
              {it.title || it.source_url}
            </a>
          ) : (
            <div className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{it.title || "Untitled"}</div>
          )}
          {at}
        </div>
        {it.source_url && <div className="mt-0.5 truncate text-[10px] text-slate-400">{it.source_url}</div>}
        {it.snippet && <p className="mt-1 line-clamp-2 text-slate-500">{it.snippet}</p>}
      </li>
    );
  }

  if (kind === "github") {
    const repo = it.canonical_key || it.title || "repository";
    const href = it.source_url || (it.canonical_key ? `https://github.com/${it.canonical_key}` : null);
    return (
      <li className="rounded-2xl border border-slate-200 bg-white p-2 text-xs">
        <div className="flex items-start justify-between gap-2">
          {href ? (
            <a href={href} target="_blank" rel="noreferrer" className="line-clamp-2 min-w-0 flex-1 font-medium text-emerald-700 hover:underline">{repo}</a>
          ) : (
            <div className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{repo}</div>
          )}
          {at}
        </div>
        {it.snippet && <p className="mt-1 line-clamp-2 text-slate-500">{it.snippet}</p>}
      </li>
    );
  }

  // dataset
  const id = it.canonical_key || it.title || "dataset";
  return (
    <li className="rounded-2xl border border-slate-200 bg-white p-2 text-xs">
      <div className="flex items-start justify-between gap-2">
        {it.source_url ? (
          <a href={it.source_url} target="_blank" rel="noreferrer" className="line-clamp-2 min-w-0 flex-1 font-medium text-emerald-700 hover:underline">{id}</a>
        ) : (
          <div className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{id}</div>
        )}
        {at}
      </div>
      {it.snippet && <p className="mt-1 line-clamp-2 text-slate-500">{it.snippet}</p>}
    </li>
  );
}

function ScoutPanelView({ kind, panel }: { kind: string; panel: ScoutPanel }) {
  const topics = panel.last_searched?.topics ?? [];
  const recentTitle = kind === "dataset" ? "Top datasets · recently surfaced" : "Recently found";
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="In corpus" value={panel.in_corpus} tone="violet" />
        <StatTile label="Added today" value={panel.added_today} tone="emerald" />
        <StatTile label="Last searched" value={panel.last_searched?.at ? ago(panel.last_searched.at) : "—"} tone="slate" />
      </div>

      {topics.length > 0 && (
        <div>
          <SubHead label="Last searched" />
          <div className="flex flex-wrap gap-1.5">
            {topics.map((t, i) => (
              <span key={`${t}-${i}`} className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">{t}</span>
            ))}
          </div>
        </div>
      )}

      <div>
        <SubHead label={recentTitle} />
        {panel.recent.length === 0 ? (
          <p className="text-sm text-slate-400">No items yet — this scout hasn&apos;t surfaced anything into the corpus.</p>
        ) : (
          <ul className="space-y-1.5">
            {panel.recent.map((it, i) => (
              <ScoutRow key={`${it.canonical_key ?? it.arxiv_id ?? it.source_url ?? it.title ?? "item"}-${i}`} kind={kind} it={it} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function ScoutInspector({ kind, corpus }: { kind: string; corpus: KnowledgeStats["corpus"] | undefined }) {
  const [panel, setPanel] = useState<ScoutPanel | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setPanel(null);
    api.scoutPanel(kind)
      .then((p) => { if (!cancelled) setPanel(p); })
      .catch(() => { if (!cancelled) setPanel(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [kind]);

  if (panel && panel.status === "ok") return <ScoutPanelView kind={kind} panel={panel} />;

  // Fallback: corpus-based count tiles (also the loading view) -------------
  return (
    <div className="space-y-3">
      {loading
        ? <p className="text-sm text-slate-400">Loading…</p>
        : <p className="text-sm text-slate-400">Live stats unavailable.</p>}
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="In corpus" value={corpus?.documents_by_kind?.[SCOUT_KIND[kind as keyof typeof SCOUT_KIND]] ?? 0} tone="violet" />
        <StatTile label="Added today" value="—" tone="emerald" />
        <StatTile label="Last searched" value="—" tone="slate" />
      </div>
    </div>
  );
}

function RoomInspector({ roomId, snapshot, knowledge, events, onClose }: {
  roomId: RoomId; snapshot: Snapshot | null; knowledge: KnowledgeStats | null; events: LabFoundryEvent[]; onClose: () => void;
}) {
  const room = roomById(roomId);
  const info = ROOM_INFO[roomId];
  const roomEvents = useMemo(() => {
    let es = events.filter((e) => info.events.includes(e.event_type));
    if (info.sourceKind) es = es.filter((e) => { const k = sourceKindOf(e); return k == null || k === info.sourceKind; });
    return es.slice(0, 8);
  }, [events, info]);
  const blocked = useMemo(() => events.filter((e) => e.event_type === "mimir.ingest_blocked").slice(0, 6), [events]);
  const corpus = knowledge?.corpus;

  return (
    <div>
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-lg font-semibold tracking-tight text-slate-950">{room.title}</h3>
            <span className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${room.active ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-slate-200 bg-slate-50 text-slate-500"}`}>
              {room.active ? "Live" : "Planned"}
            </span>
          </div>
          <div className="mt-0.5 text-xs text-slate-500">{room.sub}</div>
        </div>
        <button type="button" onClick={onClose} className="rounded-xl border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50">Close</button>
      </div>

      <p className="mb-3 text-sm leading-snug text-slate-700">{info.what}</p>

      {roomId === "mimir" && <MimirInspector knowledge={knowledge} roomEvents={roomEvents} blocked={blocked} />}

      {roomId === "library" && <LibraryInspector knowledge={knowledge} snapshot={snapshot} />}

      {(roomId === "web" || roomId === "arxiv" || roomId === "github" || roomId === "dataset") && (
        <ScoutInspector kind={info.sourceKind ?? roomId} corpus={corpus} />
      )}

      {!room.active && (
        <p className="text-xs text-slate-400">
          {info.gate?.includes("research workflow")
            ? "Dormant — activates with the research workflow."
            : "Planned — not built yet."}
        </p>
      )}
    </div>
  );
}

// =========================================================================
// The Gate inspector — the main entrance; what's admitted vs turned away,
// with reasons. Backed by GET /knowledge/gate.
// =========================================================================

const GATE_BADGE: Record<string, string> = {
  trust: "border-amber-200 bg-amber-50 text-amber-700",
  quality: "border-violet-200 bg-violet-50 text-violet-700",
};

function GateInspector({ onClose }: { onClose: () => void }) {
  const [panel, setPanel] = useState<GatePanel | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.gatePanel()
      .then((p) => { if (!cancelled) setPanel(p); })
      .catch(() => { if (!cancelled) setPanel(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const header = (
    <div className="mb-3 flex items-start justify-between gap-2">
      <div>
        <div className="flex items-center gap-2">
          <h3 className="text-lg font-semibold tracking-tight text-slate-950">The Gate</h3>
          <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold text-emerald-700">Live</span>
        </div>
        <div className="mt-0.5 text-xs text-slate-500">Every source clears trust + quality here</div>
      </div>
      <button type="button" onClick={onClose} className="rounded-xl border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50">Close</button>
    </div>
  );

  if (loading) {
    return <div>{header}<p className="text-sm text-slate-400">Loading gate stats…</p></div>;
  }
  if (!panel || panel.status !== "ok") {
    return <div>{header}<p className="text-sm text-slate-400">Gate stats unavailable.</p></div>;
  }

  const t = panel.today;
  return (
    <div>
      {header}

      <p className="mb-3 text-sm leading-snug text-slate-700">
        Two gates guard the Library — <span className="font-semibold text-amber-700">TRUST</span> (source credibility) and{" "}
        <span className="font-semibold text-violet-700">QUALITY</span> (real, substantial content). Both must pass.
      </p>

      <SubHead label="At a glance" />
      <div className="grid grid-cols-2 gap-2">
        <StatTile label="Admitted" value={t.admitted} tone="emerald" />
        <StatTile label="Blocked · trust" value={t.blocked_trust} tone="amber" />
        <StatTile label="Rejected · quality" value={t.rejected_quality} tone="slate" />
        <StatTile label="Quarantined" value={panel.quarantined} tone="red" />
      </div>
      <div className="mt-2 text-[11px] text-slate-400">Discovered today · {t.discovered.toLocaleString()}</div>

      <SubHead label="Turned away (why)" />
      {panel.turned_away.length === 0 ? (
        <p className="text-sm text-slate-400">Nothing turned away in the recent window.</p>
      ) : (
        <ul className="space-y-1.5">
          {panel.turned_away.map((r, i) => {
            const label = r.title || r.url || r.source_kind || "source";
            const linkable = !!r.url && (r.source_kind === "web" || r.source_kind === "github" || r.source_kind === "dataset");
            return (
              <li key={`${r.url ?? r.title ?? r.source_kind ?? "ta"}-${i}`} className="rounded-2xl border border-slate-200 bg-white p-2.5 text-xs">
                <div className="flex items-start justify-between gap-2">
                  <span className="flex min-w-0 flex-1 items-start gap-2">
                    <span className={`mt-0.5 shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold ${GATE_BADGE[r.gate] ?? GATE_BADGE.quality}`}>{r.gate}</span>
                    {linkable && r.url ? (
                      <a href={r.url} target="_blank" rel="noreferrer" className="line-clamp-2 min-w-0 flex-1 font-medium text-emerald-700 hover:underline">{label}</a>
                    ) : (
                      <span className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{label}</span>
                    )}
                  </span>
                  <span className="shrink-0 text-[10px] text-slate-400">{ago(r.at)}</span>
                </div>
                {r.source_kind && <div className="mt-0.5 text-[10px] uppercase tracking-wide text-slate-400">{r.source_kind}</div>}
                <p className="mt-1 line-clamp-2 text-slate-500">{r.reason}</p>
              </li>
            );
          })}
        </ul>
      )}

      <SubHead label="Recently admitted" />
      {panel.admitted.length === 0 ? (
        <p className="text-sm text-slate-400">Nothing admitted in the recent window.</p>
      ) : (
        <ul className="space-y-1.5">
          {panel.admitted.map((a, i) => {
            const badge = a.arxiv_id ? `arXiv:${a.arxiv_id}` : a.canonical_key ?? a.source_kind;
            return (
              <li key={`${a.canonical_key ?? a.arxiv_id ?? a.title ?? "adm"}-${i}`} className="flex items-start gap-2 rounded-2xl border border-slate-200 bg-white p-2.5 text-xs">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                <div className="min-w-0 flex-1">
                  <div className="line-clamp-1 text-sm font-medium text-slate-800">{a.title ?? badge}</div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-slate-400">
                    {a.trust_tier && (
                      <span className="flex items-center gap-1">
                        <span className={`inline-block h-2 w-2 rounded-full ${TIER_COLORS[a.trust_tier] ?? "bg-slate-300"}`} />
                        {a.trust_tier.replace(/_/g, " ")}
                      </span>
                    )}
                    <span className="truncate rounded-full bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">{badge}</span>
                    <span>{ago(a.at)}</span>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// =========================================================================
// Component
// =========================================================================

export function Floorplan({ snapshot }: { snapshot: Snapshot | null }) {
  const { hot, connected, events } = useFloorplanLive();
  const [selected, setSelected] = useState<RoomId | "entrance" | null>(null);
  const [knowledge, setKnowledge] = useState<KnowledgeStats | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => api.knowledge().then((k) => { if (!cancelled) setKnowledge(k); }).catch(() => {});
    load();
    const id = setInterval(load, 8_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const phase = snapshot?.state?.current_phase ?? null;
  const activeClaims = snapshot?.state?.active_claims_count ?? null;
  const liveRooms = ROOMS.filter((r) => r.active).length;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-3xl border border-slate-200 bg-white/85 px-5 py-4 shadow-sm backdrop-blur">
        <div className="flex items-center gap-2 text-sm">
          <span className="relative inline-flex h-3 w-3 items-center justify-center">
            <motion.span className={connected ? "absolute inline-flex h-full w-full rounded-full bg-emerald-400" : "absolute inline-flex h-full w-full rounded-full bg-slate-300"}
              animate={{ opacity: [0.25, 0.7, 0.25], scale: [1, 1.45, 1] }} transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }} />
            <span className={connected ? "relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-500" : "relative inline-flex h-1.5 w-1.5 rounded-full bg-slate-400"} />
          </span>
          <span className="font-semibold text-slate-800">Lab floorplan</span>
          <span className="hidden text-slate-400 sm:inline">·</span>
          <span className="hidden text-slate-500 sm:inline">Click any room to see what it&apos;s doing · {connected ? "live" : "reconnecting"}</span>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <span className="rounded-2xl bg-emerald-50 px-3 py-1.5 font-medium text-emerald-700">{liveRooms} rooms live</span>
          {phase && <span className="rounded-2xl bg-amber-50 px-3 py-1.5 font-medium text-amber-700">phase · {phase}</span>}
          {activeClaims != null && <span className="rounded-2xl bg-blue-50 px-3 py-1.5 font-medium text-blue-700">{activeClaims} directions</span>}
        </div>
      </div>

      <div className="relative overflow-hidden rounded-3xl border border-slate-200 bg-gradient-to-br from-white to-slate-50/60 p-6 shadow-sm backdrop-blur sm:p-10">
        <svg viewBox={`0 0 ${VW} ${VH}`} className="h-auto w-full" onClick={() => setSelected(null)}>
          <defs>
            {([["active", C.active], ["plan", C.plan], ["seed", C.seed]] as const).map(([id, col]) => (
              <marker key={id} id={`fp-arrow-${id}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill={col} />
              </marker>
            ))}
          </defs>

          <rect x={16} y={48} width={1468} height={872} rx={16} fill="none" stroke={C.wall} strokeWidth={5} />
          {/* divider wall between the two north wings */}
          <line x1={829} y1={48} x2={829} y2={300} stroke={C.wall} strokeWidth={3} opacity={0.5} />
          {/* main entrance (south wall, under the knowledge core) — clickable: opens the Gate drill-down */}
          <g
            style={{ cursor: "pointer" }}
            role="button"
            aria-label="Open the Gate — what's admitted vs turned away, with reasons"
            onClick={(e) => { e.stopPropagation(); setSelected("entrance"); }}
          >
            {selected === "entrance" && (
              <rect x={426} y={884} width={278} height={58} rx={12}
                fill="none" stroke={C.active} strokeWidth={3} opacity={0.9} />
            )}
            {(() => {
              const arcStroke = selected === "entrance" ? C.active : C.wall;
              const arcW = selected === "entrance" ? 3 : 2.5;
              return (
                <>
                  <path d="M 438 920 A 30 30 0 0 1 498 920" fill="none" stroke={arcStroke} strokeWidth={arcW} />
                  <path d="M 498 920 A 30 30 0 0 1 558 920" fill="none" stroke={arcStroke} strokeWidth={arcW} />
                </>
              );
            })()}
            <text x={588} y={916} fontSize={12} letterSpacing="1.2" fill={selected === "entrance" ? C.active : C.faint}>MAIN ENTRANCE</text>
            <text x={588} y={932} fontSize={10} letterSpacing="0.8" fill={selected === "entrance" ? "#0f9b6e" : C.faint} opacity={0.85}>the Gate · click to inspect</text>
            {/* transparent hotspot covering the arcs + label */}
            <rect x={430} y={888} width={270} height={50} fill="transparent" />
          </g>
          <text x={946} y={476} textAnchor="middle" fontSize={17} fontWeight={700} letterSpacing="1.6" fill="#aab2bd">RESEARCH WORKFLOW</text>

          <ZoneBracket x1={60} x2={806} y={36} label="COLLECTORS" />
          <ZoneBracket x1={852} x2={1420} y={36} label="RESEARCH & DISCOVERY" />
          <ZoneBracket x1={742} x2={1190} y={676} label="EVALUATION & OUTPUT" />
          <ZoneBracket x1={300} x2={700} y={858} label="KNOWLEDGE CORE" />

          {FLOWS.map((f) => <FlowPath key={f.id} flow={f} hot={hot.has(f.id)} />)}
          {ROOMS.map((r) => <RoomBox key={r.id} room={r} phase={phase} activeClaims={activeClaims} selected={selected === r.id} onSelect={() => setSelected(r.id)} />)}

          <g>
            <rect x={60} y={590} width={210} height={98} rx={12} fill="rgba(255,255,255,0.8)" stroke="#e2e8ef" strokeWidth={1} />
            <text x={78} y={616} fontSize={13} fontWeight={700} letterSpacing="0.6" fill={C.intake}>DATA INTAKE</text>
            <text x={78} y={638} fontSize={12.5} fill={C.muted}>Scouts gather + normalize</text>
            <text x={78} y={656} fontSize={12.5} fill={C.muted}>research signals, then hand</text>
            <text x={78} y={674} fontSize={12.5} fill={C.muted}>them to Mimir → Library.</text>
          </g>

          <g>
            <rect x={VW / 2 - 300} y={982} width={600} height={66} rx={14} fill="rgba(255,255,255,0.9)" stroke="#e2e8ef" strokeWidth={1} />
            <rect x={VW / 2 - 270} y={1001} width={30} height={28} rx={7} fill={C.activeFill} stroke={C.active} strokeWidth={2.2} />
            <text x={VW / 2 - 228} y={1013} fontSize={14} fontWeight={700} fill={C.ink}>Active now</text>
            <text x={VW / 2 - 228} y={1031} fontSize={12} fill={C.muted}>Live and operational</text>
            <rect x={VW / 2 + 36} y={1001} width={30} height={28} rx={7} fill="none" stroke={C.plan} strokeWidth={1.8} strokeDasharray="5 4" />
            <text x={VW / 2 + 78} y={1013} fontSize={14} fontWeight={700} fill={C.ink}>Planned next</text>
            <text x={VW / 2 + 78} y={1031} fontSize={12} fill={C.muted}>Coming soon / under development</text>
          </g>
        </svg>

        <AnimatePresence>
          {selected && (
            <motion.aside
              key="inspector"
              initial={{ x: "100%", opacity: 0.6 }}
              animate={{ x: 0, opacity: 1 }}
              exit={{ x: "100%", opacity: 0.4 }}
              transition={{ type: "spring", stiffness: 280, damping: 32 }}
              className="absolute right-0 top-0 z-20 h-full w-full max-w-[440px] overflow-y-auto border-l border-slate-200 bg-white/95 p-5 shadow-2xl backdrop-blur"
            >
              {selected === "entrance" ? (
                <GateInspector onClose={() => setSelected(null)} />
              ) : (
                <RoomInspector roomId={selected} snapshot={snapshot} knowledge={knowledge} events={events} onClose={() => setSelected(null)} />
              )}
            </motion.aside>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
