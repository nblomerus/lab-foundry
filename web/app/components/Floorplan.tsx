"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import type { LabFoundryEvent, Snapshot, StreamMessage } from "../lib/types";
import { useEventStream } from "../lib/ws";

// =========================================================================
// LabFoundry floorplan — the lab as an architectural blueprint. Each room is
// an agent/subsystem; solid rooms are LIVE, dashed rooms are PLANNED. The
// arrows between rooms animate (particles flowing) — the same live inter-agent
// flow language as the old /flow graph, kept here. Active flows always show a
// slow baseline particle so the lab reads as alive even when the harness is
// between sweeps; a matching live event speeds them up.
// =========================================================================

const VW = 1440;
const VH = 1060;

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

// Layout faithful to the reference blueprint; status reflects what is LIVE now.
const ROOMS: Room[] = [
  // --- COLLECTORS (top-left band) ---
  { id: "web",    x: 66,  y: 96,  w: 188, h: 210, title: "Web Scout",    sub: "Web monitoring & discovery",   active: true,  door: { side: "bottom", at: 0.62 } },
  { id: "arxiv",  x: 258, y: 96,  w: 180, h: 210, title: "arXiv Scout",  sub: "Scientific papers monitoring", active: true,  door: { side: "bottom", at: 0.62 } },
  { id: "github", x: 442, y: 96,  w: 182, h: 210, title: "GitHub Scout", sub: "Code repositories monitoring", active: true,  door: { side: "bottom", at: 0.62 } },
  { id: "openml", x: 628, y: 96,  w: 176, h: 210, title: "OpenML Scout", sub: "ML datasets & benchmarks",     active: false, door: { side: "bottom", at: 0.62 } },
  // --- RESEARCH & DISCOVERY (top-right band) ---
  { id: "ariadne",     x: 808,  y: 96, w: 178, h: 210, title: "Ariadne",     sub: "Principal investigator / scientific direction", active: false, door: { side: "bottom", at: 0.6 } },
  { id: "planner",     x: 990,  y: 96, w: 142, h: 210, title: "Planner",     sub: "Schedules & goals planning",                    active: false, door: { side: "bottom", at: 0.6 } },
  { id: "researchers", x: 1136, y: 96, w: 238, h: 210, title: "Researchers", sub: "Investigate directions & gather findings",      active: false, door: { side: "bottom", at: 0.55 } },
  // --- KNOWLEDGE CORE (centre-left) ---
  { id: "dataset", x: 66,  y: 404, w: 188, h: 164, title: "Dataset Scout", sub: "External data monitoring", active: false, door: { side: "bottom", at: 0.78 } },
  { id: "mimir",   x: 356, y: 384, w: 288, h: 196, title: "Mimir",   sub: "AI Curator of Knowledge",   active: true },
  { id: "library", x: 320, y: 604, w: 348, h: 224, title: "Library", sub: "Queryable research memory", active: true },
  // --- RESEARCH WORKFLOW (right column) ---
  { id: "critic", x: 1190, y: 348, w: 188, h: 150, title: "Critic", sub: "Challenges claims & tests weaknesses", active: false, door: { side: "left", at: 0.4 } },
  { id: "gate",   x: 1190, y: 512, w: 188, h: 150, title: "Gate",   sub: "Promotion review & claim approval",    active: false, door: { side: "left", at: 0.4 } },
  { id: "ops",    x: 1190, y: 700, w: 188, h: 188, title: "Ops",    sub: "Infrastructure, budget & monitoring",  active: false },
  // --- EVALUATION & OUTPUT (bottom-centre) ---
  { id: "experiments", x: 726, y: 700, w: 208, h: 188, title: "Experiments", sub: "Run benchmarks & evaluations", active: false },
  { id: "publication", x: 948, y: 700, w: 222, h: 188, title: "Publication", sub: "Write, assemble & publish",     active: false },
];

const roomById = (id: RoomId): Room => ROOMS.find((r) => r.id === id) as Room;
function anchor(id: RoomId, side: "top" | "bottom" | "left" | "right", t = 0.5): { x: number; y: number } {
  const r = roomById(id);
  if (side === "top")    return { x: r.x + r.w * t, y: r.y };
  if (side === "bottom") return { x: r.x + r.w * t, y: r.y + r.h };
  if (side === "left")   return { x: r.x, y: r.y + r.h * t };
  return { x: r.x + r.w, y: r.y + r.h * t };
}

// --- Flows (the animated arrows between agents) -------------------------
interface Flow {
  id: string;
  d: string;
  active: boolean;
  kind: "intake" | "knowledge" | "seed" | "workflow";
  hotEvents: string[];
}

const MIMIR = roomById("mimir");
function intake(fromId: RoomId, toX: number): string {
  const a = anchor(fromId, "bottom", 0.62);
  const dropY = MIMIR.y - 56;
  return `M ${a.x} ${a.y} C ${a.x} ${a.y + 38}, ${toX} ${dropY - 22}, ${toX} ${MIMIR.y}`;
}

const SCOUT_EVENTS = ["source.discovered", "library.sweep_requested", "library.trends", "document.parsed"];
const LIB_EVENTS = ["document.ingested", "document.parsed"];

const datasetAnchor = anchor("dataset", "right", 0.5);
const mimirBottom = anchor("mimir", "bottom", 0.5);
const libraryTop = anchor("library", "top", 0.5);
const librarySeedIn = anchor("library", "bottom", 0.3);
const libraryRight = anchor("library", "right", 0.4);

const FLOWS: Flow[] = [
  { id: "f-web",    d: intake("web",    MIMIR.x + 70),  active: true,  kind: "intake", hotEvents: SCOUT_EVENTS },
  { id: "f-arxiv",  d: intake("arxiv",  MIMIR.x + 144), active: true,  kind: "intake", hotEvents: SCOUT_EVENTS },
  { id: "f-github", d: intake("github", MIMIR.x + 214), active: true,  kind: "intake", hotEvents: SCOUT_EVENTS },
  { id: "f-openml", d: intake("openml", MIMIR.x + 252), active: false, kind: "intake", hotEvents: [] },
  { id: "f-dataset", active: false, kind: "intake", hotEvents: [], d: `M ${datasetAnchor.x} ${datasetAnchor.y} L ${MIMIR.x} ${datasetAnchor.y}` },
  { id: "f-mimir-lib", active: true, kind: "knowledge", hotEvents: LIB_EVENTS, d: `M ${mimirBottom.x} ${mimirBottom.y} L ${libraryTop.x} ${libraryTop.y}` },
  { id: "f-seed", active: true, kind: "seed", hotEvents: [], d: `M 164 838 C 164 814, 250 ${librarySeedIn.y + 6}, ${librarySeedIn.x} ${librarySeedIn.y}` },
  { id: "f-workflow", active: false, kind: "workflow", hotEvents: [], d: `M ${libraryRight.x} ${libraryRight.y} L 726 ${libraryRight.y}` },
];

// =========================================================================
// Live pulse tracking — which flows are "hot" right now (ported from
// LiveFlow's usePerEdgePulses): a matching event keeps a flow hot for 6s.
// =========================================================================

function useHotFlows(): { hot: Set<string>; connected: boolean } {
  const { recent, connected } = useEventStream(60);
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
      for (const f of FLOWS) {
        if (f.hotEvents.includes(e.event_type)) { expiry.current.set(f.id, now + 6000); changed = true; }
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

  return { hot, connected };
}

// =========================================================================
// Palette + SVG primitives
// =========================================================================

const C = {
  wall: "#3f4753",
  active: "#10b981",
  activeFill: "rgba(16,185,129,0.06)",
  plan: "#9aa3ad",
  seed: "#7c5cd6",
  ink: "#1f2d3d",
  muted: "#5b6b7b",
  faint: "#9aa3ad",
  intake: "#2c5fb8",
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

function RoomBox({ room, phase, activeClaims }: { room: Room; phase: string | null; activeClaims: number | null }) {
  const stroke = room.active ? C.active : C.plan;
  const big = room.id === "mimir" || room.id === "library";
  return (
    <g>
      {room.active && (
        <motion.rect
          x={room.x - 3} y={room.y - 3} width={room.w + 6} height={room.h + 6} rx={16}
          fill="none" stroke={C.active} strokeWidth={2}
          initial={{ opacity: 0.1 }}
          animate={{ opacity: [0.08, 0.3, 0.08] }}
          transition={{ duration: 2.6, repeat: Infinity, ease: "easeInOut" }}
        />
      )}
      <rect
        x={room.x} y={room.y} width={room.w} height={room.h} rx={13}
        fill={room.active ? C.activeFill : "rgba(250,251,252,0.7)"}
        stroke={stroke} strokeWidth={room.active ? 2.4 : 1.6}
        strokeDasharray={room.active ? undefined : "7 5"}
      />
      <DoorArc room={room} />
      <text x={room.x + room.w / 2} y={room.y + (big ? room.h / 2 - 6 : 52)} textAnchor="middle"
        fontSize={big ? 26 : 21} fontWeight={700} fill={room.active ? C.ink : C.muted}>
        {room.title}
      </text>
      <text x={room.x + room.w / 2} y={room.y + (big ? room.h / 2 + 22 : 78)} textAnchor="middle" fontSize={13.5} fill={C.faint}>
        {room.sub}
      </text>
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
    </g>
  );
}

function FlowPath({ flow, hot }: { flow: Flow; hot: boolean }) {
  const color = flow.kind === "seed" ? C.seed : flow.active ? C.intake : C.plan;
  const particleColor = flow.kind === "seed" ? C.seed : C.active;
  const count = !flow.active ? 0 : hot ? 3 : 1;
  const dur = hot ? 1.5 : flow.kind === "seed" ? 5 : 3.4;
  const markerId = flow.kind === "seed" ? "seed" : flow.active ? "active" : "plan";
  return (
    <g>
      <path
        d={flow.d} fill="none" stroke={color}
        strokeWidth={flow.active ? 2.4 : 1.6}
        strokeDasharray={flow.active ? undefined : "6 5"}
        strokeLinecap="round"
        opacity={flow.active ? (hot ? 1 : 0.85) : 0.5}
        markerEnd={`url(#fp-arrow-${markerId})`}
        style={flow.active && hot ? { filter: "drop-shadow(0 0 1.4px rgba(16,185,129,0.55))" } : undefined}
      />
      {Array.from({ length: count }).map((_, i) => (
        <circle key={i} r={i === 0 ? 4.2 : 3} fill={particleColor} opacity={i === 0 ? 1 : 0.7}
          style={{ filter: `drop-shadow(0 0 2px ${particleColor})` }}>
          <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`${(i * dur) / Math.max(count, 1)}s`} path={flow.d} />
        </circle>
      ))}
    </g>
  );
}

function ZoneBracket({ x1, x2, y, label }: { x1: number; x2: number; y: number; label: string }) {
  const cx = (x1 + x2) / 2;
  const labelW = label.length * 9 + 16;
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
// Component
// =========================================================================

export function Floorplan({ snapshot }: { snapshot: Snapshot | null }) {
  const { hot, connected } = useHotFlows();
  const phase = snapshot?.state?.current_phase ?? null;
  const activeClaims = snapshot?.state?.active_claims_count ?? null;
  const liveRooms = ROOMS.filter((r) => r.active).length;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-3xl border border-slate-200 bg-white/85 p-3 shadow-sm backdrop-blur">
        <div className="flex items-center gap-2 text-xs">
          <span className="relative inline-flex h-3 w-3 items-center justify-center">
            <motion.span
              className={connected ? "absolute inline-flex h-full w-full rounded-full bg-emerald-400" : "absolute inline-flex h-full w-full rounded-full bg-slate-300"}
              animate={{ opacity: [0.25, 0.7, 0.25], scale: [1, 1.45, 1] }}
              transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }}
            />
            <span className={connected ? "relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-500" : "relative inline-flex h-1.5 w-1.5 rounded-full bg-slate-400"} />
          </span>
          <span className="font-semibold text-slate-700">Lab floorplan</span>
          <span className="text-slate-400">·</span>
          <span className="text-slate-500">Mimir + the collectors are live; the research workflow is planned · {connected ? "live" : "reconnecting"}</span>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <span className="rounded-2xl bg-emerald-50 px-3 py-1.5 font-medium text-emerald-700">{liveRooms} rooms live</span>
          {phase && <span className="rounded-2xl bg-amber-50 px-3 py-1.5 font-medium text-amber-700">phase · {phase}</span>}
          {activeClaims != null && <span className="rounded-2xl bg-blue-50 px-3 py-1.5 font-medium text-blue-700">{activeClaims} directions</span>}
        </div>
      </div>

      <div className="overflow-hidden rounded-3xl border border-slate-200 bg-white/85 p-3 shadow-sm backdrop-blur">
        <svg viewBox={`0 0 ${VW} ${VH}`} className="h-auto w-full">
          <defs>
            {([["active", C.active], ["plan", C.plan], ["seed", C.seed]] as const).map(([id, col]) => (
              <marker key={id} id={`fp-arrow-${id}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill={col} />
              </marker>
            ))}
          </defs>

          {/* building shell + main entrance */}
          <rect x={48} y={78} width={VW - 96} height={812} rx={10} fill="none" stroke={C.wall} strokeWidth={5} />
          <path d="M 470 890 A 30 30 0 0 1 530 890" fill="none" stroke={C.wall} strokeWidth={2.5} />
          <path d="M 530 890 A 30 30 0 0 1 590 890" fill="none" stroke={C.wall} strokeWidth={2.5} />
          <text x={620} y={886} fontSize={12} letterSpacing="1.2" fill={C.faint}>MAIN ENTRANCE</text>

          <text x={905} y={470} textAnchor="middle" fontSize={17} fontWeight={700} letterSpacing="1.6" fill="#aab2bd">RESEARCH WORKFLOW</text>

          <ZoneBracket x1={66} x2={804} y={70} label="COLLECTORS" />
          <ZoneBracket x1={808} x2={1374} y={70} label="RESEARCH & DISCOVERY" />
          <ZoneBracket x1={726} x2={1170} y={672} label="EVALUATION & OUTPUT" />
          <ZoneBracket x1={300} x2={690} y={848} label="KNOWLEDGE CORE" />

          {/* flows behind rooms */}
          {FLOWS.map((f) => <FlowPath key={f.id} flow={f} hot={hot.has(f.id)} />)}

          {/* rooms */}
          {ROOMS.map((r) => <RoomBox key={r.id} room={r} phase={phase} activeClaims={activeClaims} />)}

          {/* rag-bench base seed chip */}
          <g>
            <rect x={70} y={838} width={188} height={52} rx={12} fill="rgba(124,92,214,0.07)" stroke={C.seed} strokeWidth={1.6} />
            <text x={164} y={860} textAnchor="middle" fontSize={13.5} fontWeight={700} fill="#5a3fa0">rag-bench base</text>
            <text x={164} y={878} textAnchor="middle" fontSize={11.5} fill={C.muted}>21,800 arXiv papers · seed</text>
          </g>

          {/* DATA INTAKE explainer */}
          <g>
            <rect x={66} y={596} width={210} height={96} rx={12} fill="rgba(255,255,255,0.75)" stroke="#e2e8ef" strokeWidth={1} />
            <text x={82} y={622} fontSize={13} fontWeight={700} letterSpacing="0.6" fill={C.intake}>DATA INTAKE</text>
            <text x={82} y={644} fontSize={12.5} fill={C.muted}>Scouts gather + normalize</text>
            <text x={82} y={662} fontSize={12.5} fill={C.muted}>research signals, then hand</text>
            <text x={82} y={680} fontSize={12.5} fill={C.muted}>them to Mimir → Library.</text>
          </g>

          {/* bottom legend */}
          <g>
            <rect x={VW / 2 - 290} y={948} width={580} height={64} rx={14} fill="rgba(255,255,255,0.85)" stroke="#e2e8ef" strokeWidth={1} />
            <rect x={VW / 2 - 262} y={966} width={30} height={28} rx={7} fill={C.activeFill} stroke={C.active} strokeWidth={2.2} />
            <text x={VW / 2 - 222} y={978} fontSize={14} fontWeight={700} fill={C.ink}>Active now</text>
            <text x={VW / 2 - 222} y={996} fontSize={12} fill={C.muted}>Live and operational</text>
            <rect x={VW / 2 + 30} y={966} width={30} height={28} rx={7} fill="none" stroke={C.plan} strokeWidth={1.8} strokeDasharray="5 4" />
            <text x={VW / 2 + 70} y={978} fontSize={14} fontWeight={700} fill={C.ink}>Planned next</text>
            <text x={VW / 2 + 70} y={996} fontSize={12} fill={C.muted}>Coming soon / under development</text>
          </g>
        </svg>
      </div>
    </div>
  );
}
