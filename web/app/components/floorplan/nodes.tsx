"use client";

// Custom React Flow node components for the floorplan. Node bodies surface the
// at-a-glance live data (the mockup's cards); clicking opens the rich inspector.

import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { AlertTriangle, CheckCircle2, Cpu, Gauge, Library, ShieldCheck, Zap } from "lucide-react";
import { MiniBar, cx } from "../ui";
import { compact } from "../../lib/format";
import { useNodeMeter, type ActivityState } from "./useNodeActivity";
import type { AriadneOverview, DebugCosts, HostStats, KnowledgeStats, MimirPanel, ScoutPanel } from "../../lib/api";
import type { NodeDef } from "./topology";

export type Selected =
  | { kind: "scout"; sourceKind: string; title: string }
  | { kind: "mimir"; title: string }
  | { kind: "library"; title: string }
  | { kind: "gate"; scope: string; title: string }
  | { kind: "ariadne"; title: string }
  | { kind: "queue"; title: string }
  | { kind: "planner"; title: string }
  | { kind: "researcher"; title: string }
  | { kind: "ops"; title: string }
  | { kind: "info"; title: string; sub?: string; description?: string };

export interface FloorNodeData {
  def: NodeDef;
  panel?: ScoutPanel | null;
  series?: number[];
  mimir?: MimirPanel | null;
  stats?: KnowledgeStats | null;
  host?: HostStats | null;
  costs?: DebugCosts | null;
  ariadne?: AriadneOverview | null;
  priority?: string | null;
  activeAt?: number | null; // node's last-active epoch ms (live event stream)
  now?: number; // re-eval tick from useNodeActivity
  activitySeries?: number[]; // bucketed event-rate over the window (live meter)
  onOpen?: (sel: Selected) => void;
  // React Flow v12 requires node data to extend Record<string, unknown>.
  [key: string]: unknown;
}

type FN = Node<FloorNodeData>;

const HANDLE_STYLE = { opacity: 0, width: 7, height: 7, minWidth: 0, minHeight: 0, border: "none", background: "transparent" } as const;

function NodeHandles() {
  return (
    <>
      <Handle id="t" type="target" position={Position.Top} style={HANDLE_STYLE} isConnectable={false} />
      <Handle id="b" type="source" position={Position.Bottom} style={HANDLE_STYLE} isConnectable={false} />
      <Handle id="l" type="target" position={Position.Left} style={HANDLE_STYLE} isConnectable={false} />
      <Handle id="r" type="source" position={Position.Right} style={HANDLE_STYLE} isConnectable={false} />
      {/* top-source handle — lets a return/feedback edge ORIGINATE from the top and arc over the row */}
      <Handle id="ts" type="source" position={Position.Top} style={HANDLE_STYLE} isConnectable={false} />
    </>
  );
}

function IconTile({ icon: Icon, tone = "live" }: { icon?: NodeDef["icon"]; tone?: "live" | "slate" | "violet" }) {
  const cls = tone === "violet" ? "bg-violet-50 text-violet-600" : tone === "slate" ? "bg-slate-100 text-slate-500" : "bg-emerald-50 text-emerald-600";
  return (
    <span className={cx("inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-xl", cls)}>
      {Icon && <Icon className="h-4 w-4" />}
    </span>
  );
}

// --- Activity badge (shared) -------------------------------------------
// One consistent real-time status across every agent node: who's working RIGHT
// NOW (busy, pulsing) vs on-but-quiet (live/idle) vs paused (offline). Replaces
// the old per-node mode/24h labels that never told you who was actually active.
const ACT_META: Record<ActivityState, { dot: string; ring: string; text: string; label: string }> = {
  busy: { dot: "bg-emerald-500 pulse-dot", ring: "border-emerald-200 bg-emerald-50", text: "text-emerald-700", label: "Busy" },
  live: { dot: "bg-emerald-500", ring: "border-emerald-200 bg-emerald-50/70", text: "text-emerald-700", label: "Live" },
  idle: { dot: "bg-slate-300", ring: "border-slate-200 bg-slate-50", text: "text-slate-500", label: "Idle" },
  offline: { dot: "bg-transparent ring-1 ring-inset ring-slate-300", ring: "border-slate-200 bg-white", text: "text-slate-400", label: "Offline" },
};

function ActivityBadge({ state, ago }: { state: ActivityState; ago?: string | null }) {
  const m = ACT_META[state];
  return (
    <span
      title={state === "busy" ? "Working now" : state === "live" ? `Active ${ago ?? "recently"} ago` : state === "idle" ? `Quiet${ago ? ` · ${ago} ago` : ""}` : "Paused"}
      className={cx("inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[9px] font-semibold leading-none", m.ring, m.text)}
    >
      <span className={cx("h-1.5 w-1.5 rounded-full", m.dot)} />
      {m.label}
      {ago && (state === "idle" || state === "live") ? <span className="font-medium text-slate-400">{ago}</span> : null}
    </span>
  );
}

// Busy cards gain a breathing emerald halo so active agents pop on the floorplan.
function busyRing(state: ActivityState): string {
  return state === "busy" ? "busy-halo" : "";
}

// Live activity meter — a lightweight bar-sparkline of an agent's recent event
// rate (newest bar on the right). Fills + brightens during bursts, flat when
// quiet. Pure CSS bars (no chart lib) so ~30 of them can refresh every 2s cheaply.
export function ActivityMeter({ series, state, className = "" }: { series?: number[]; state: ActivityState; className?: string }) {
  const data = series && series.length ? series : new Array(15).fill(0);
  const max = Math.max(1, ...data);
  const on = state === "busy" || state === "live";
  return (
    <div className={cx("flex h-4 items-end gap-[1.5px]", className)} title="recent activity (last ~90s)">
      {data.map((v, i) => (
        <span
          key={i}
          className={cx(
            "flex-1 rounded-[1px] transition-all duration-700 ease-out",
            v > 0 ? (state === "busy" ? "bg-emerald-500" : on ? "bg-emerald-400/70" : "bg-slate-300") : "bg-slate-200/70",
          )}
          style={{ height: `${v > 0 ? Math.max(14, (v / max) * 100) : 8}%` }}
        />
      ))}
    </div>
  );
}

// --- Scout -------------------------------------------------------------
export function ScoutNode({ data }: NodeProps<FN>) {
  const { def, panel } = data;
  const added = panel?.added_today ?? null;
  const inCorpus = panel?.in_corpus ?? null;
  const topics = panel?.last_searched?.topics ?? [];
  const { state, ago, series } = useNodeMeter(data.def.id, true); // scouts are always-on collectors
  return (
    <div className={cx("group flex h-full w-full cursor-pointer flex-col rounded-node border border-emerald-200/70 bg-white/90 p-3 shadow-card backdrop-blur transition hover:border-emerald-300 hover:shadow-panel", busyRing(state))}>
      <NodeHandles />
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <IconTile icon={def.icon} />
          <div className="min-w-0">
            <div className="truncate text-[13px] font-semibold leading-tight text-slate-900">{def.title}</div>
            <div className="truncate text-[10px] text-slate-400">{def.sub}</div>
          </div>
        </div>
        <ActivityBadge state={state} ago={ago} />
      </div>
      <div className="mt-2 flex items-end justify-between">
        <div>
          <div className="text-2xl font-semibold leading-none tabular-nums text-slate-900">{added != null ? compact(added) : "—"}</div>
          <div className="text-[10px] text-slate-400">new today</div>
        </div>
        <div className="text-right text-[10px] text-slate-400">
          {inCorpus != null ? `${compact(inCorpus)} in corpus` : ""}
        </div>
      </div>
      <div className="mt-1">
        <ActivityMeter series={series} state={state} className="h-[26px]" />
      </div>
      {topics.length > 0 && (
        <div className="mt-auto truncate pt-1 text-[9px] text-slate-400" title={topics.join(" · ")}>
          {topics.slice(0, 2).join(" · ")}
        </div>
      )}
    </div>
  );
}

// --- Mimir -------------------------------------------------------------
function PipelineStat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex flex-col items-center rounded-xl bg-slate-50 px-1.5 py-1.5">
      <span className="text-sm font-semibold tabular-nums text-slate-900">{typeof value === "number" ? compact(value) : value}</span>
      <span className="mt-0.5 text-center text-[8.5px] leading-tight text-slate-400">{label}</span>
    </div>
  );
}

export function MimirNode({ data }: NodeProps<FN>) {
  const { def, mimir, stats, priority, onOpen } = data;
  const p = mimir?.pipeline_today;
  const g = mimir?.at_a_glance;
  const nodes = stats?.graph?.status === "ok" ? stats.graph.nodes ?? 0 : null;
  const mix = (mimir?.source_mix ?? []).filter((m) => m.count > 0);
  const { state, ago, series } = useNodeMeter(data.def.id, mimir?.status === "ok");
  return (
    <div className={cx("flex h-full w-full cursor-pointer flex-col rounded-wing border border-emerald-300 bg-white/92 p-3.5 shadow-panel backdrop-blur transition hover:shadow-float", busyRing(state))}>
      <NodeHandles />
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2.5">
          <span className="inline-flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-100 text-emerald-600">
            <ShieldCheck className="h-5 w-5" />
          </span>
          <div>
            <div className="text-[15px] font-semibold leading-tight text-slate-900">{def.title}</div>
            <div className="text-[11px] text-slate-400">{def.sub}</div>
          </div>
        </div>
        <div className="flex flex-col items-end gap-1">
          <ActivityBadge state={state} ago={ago} />
          <ActivityMeter series={series} state={state} className="h-3 w-16" />
        </div>
      </div>

      <div className="mt-3 grid grid-cols-5 gap-1.5">
        <PipelineStat label="Inbox" value={p?.discovered ?? 0} />
        <PipelineStat label="Trust & Verify" value={g?.certified_today ?? 0} />
        <PipelineStat label="Extract" value={p?.parsed ?? 0} />
        <PipelineStat label="Build Graph" value={nodes ?? "—"} />
        <PipelineStat label="Certify / Quar." value={`${compact(p?.ingested ?? 0)}/${compact(p?.quarantined ?? 0)}`} />
      </div>

      {mix.length > 0 && (
        <div className="mt-2 flex h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
          {mix.map((m) => (
            <div key={m.kind} style={{ width: `${m.pct}%`, background: MIX_HEX[m.kind] ?? "#94a3b8" }} title={`${m.kind}: ${m.pct}%`} />
          ))}
        </div>
      )}

      <div className="mt-auto flex items-center justify-between pt-2">
        <span className="truncate rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
          Priority · {priority || "continuous intake"}
        </span>
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onOpen?.({ kind: "gate", scope: "all", title: "Mimir · intake gate" }); }}
          className="shrink-0 rounded-full border border-emerald-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-emerald-700 hover:bg-emerald-50"
        >
          Gate ▸
        </button>
      </div>
    </div>
  );
}

const MIX_HEX: Record<string, string> = { arxiv: "#3b82f6", web: "#10b981", github: "#f59e0b", dataset: "#8b5cf6" };

// --- Storage -----------------------------------------------------------
function sumKinds(stats?: KnowledgeStats | null): number | null {
  const k = stats?.corpus?.documents_by_kind;
  if (!k) return null;
  return Object.values(k).reduce((a, b) => a + b, 0);
}

export function StorageNode({ data }: NodeProps<FN>) {
  const { def, stats, ariadne } = data;
  let live = def.live;
  let value: string | number = "—";
  let sub = def.sub ?? "";
  let delta: string | null = null;
  if (def.storageVariant === "claims") {
    // The Claim Ledger is live once the PI (Ariadne) has framed claims.
    const n = ariadne?.at_a_glance.claims_total ?? null;
    if (n != null) { live = n > 0; value = compact(n); sub = "mission · directions"; }
  } else if (live && stats) {
    if (def.storageVariant === "raw") {
      value = compact(sumKinds(stats) ?? 0);
      delta = stats.corpus?.docs_today ? `+${compact(stats.corpus.docs_today)} (24h)` : null;
    } else if (def.storageVariant === "vector") {
      value = compact(stats.corpus?.chunks_embedded ?? 0);
      sub = "embedded chunks";
    } else if (def.storageVariant === "graph") {
      value = stats.graph?.status === "ok" ? compact(stats.graph.nodes ?? 0) : "—";
      sub = stats.graph?.status === "ok" ? `${compact(stats.graph.papers ?? 0)} papers` : "graph offline";
    }
  }
  return (
    <div className={cx(
      "flex h-full w-full flex-col rounded-node border p-3 backdrop-blur transition",
      live ? "cursor-pointer border-slate-200 bg-white/90 shadow-card hover:shadow-panel" : "border-dashed border-slate-200 bg-white/50",
    )}>
      <NodeHandles />
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <IconTile icon={def.icon} tone={live ? "violet" : "slate"} />
          <div className="text-[12px] font-semibold leading-tight text-slate-800">{def.title}</div>
        </div>
        {!live && <span className="rounded-full border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[9px] font-semibold text-slate-400">Planned</span>}
      </div>
      <div className="mt-2 text-xl font-semibold tabular-nums text-slate-900">{value}</div>
      <div className="text-[10px] text-slate-400">{sub}</div>
      {delta && <div className="mt-0.5 text-[10px] font-medium text-emerald-600">{delta}</div>}
    </div>
  );
}

// --- Ops ---------------------------------------------------------------
function gpuAvgUtil(costs?: DebugCosts | null): number | null {
  const gpus = costs?.power?.gpus;
  if (!gpus || gpus.length === 0) return null;
  return gpus.reduce((a, g) => a + g.util, 0) / gpus.length;
}

function opsAlerts(host?: HostStats | null, costs?: DebugCosts | null, mimir?: MimirPanel | null): string[] {
  const out: string[] = [];
  const gpu = gpuAvgUtil(costs);
  if (gpu != null && gpu > 90) out.push("GPU utilization > 90%");
  if (host?.disk_percent != null && host.disk_percent > 85) out.push(`Storage usage above ${Math.round(host.disk_percent)}%`);
  if (host?.cpu_percent != null && host.cpu_percent > 90) out.push("CPU saturated (> 90%)");
  const quar = mimir?.at_a_glance?.quarantined_today ?? 0;
  if (quar > 200) out.push(`Quarantine spike — ${compact(quar)} today`);
  return out;
}

export function OpsNode({ data }: NodeProps<FN>) {
  const { def, host, costs, mimir } = data;
  const gpu = gpuAvgUtil(costs);
  const spend = costs?.deepseek?.today_cost_usd ?? null;
  const projected = costs?.power?.projected_usd_per_day ?? null;
  const alerts = opsAlerts(host, costs, mimir);
  const decisions = (mimir?.recent_certifications ?? []).slice(0, 3);
  return (
    <div className="flex h-full w-full gap-5 rounded-wing border border-slate-200 bg-white/85 p-4 shadow-card backdrop-blur">
      <NodeHandles />
      {/* identity + gauges */}
      <div className="flex w-[38%] shrink-0 flex-col">
        <div className="flex items-center gap-2.5">
          <span className="inline-flex h-9 w-9 items-center justify-center rounded-xl bg-slate-100 text-slate-500"><Cpu className="h-5 w-5" /></span>
          <div>
            <div className="text-[14px] font-semibold leading-tight text-slate-900">{def.title}</div>
            <div className="text-[11px] text-slate-400">{def.sub}</div>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-x-5 gap-y-2">
          <MiniBar label="CPU" value={host?.cpu_percent ?? 0} valueLabel={host?.cpu_percent != null ? `${Math.round(host.cpu_percent)}%` : "—"} />
          <MiniBar label="Memory" value={host?.memory_percent ?? 0} valueLabel={host?.memory_percent != null ? `${Math.round(host.memory_percent)}%` : "—"} />
          <MiniBar label="GPU" value={gpu ?? 0} valueLabel={gpu != null ? `${Math.round(gpu)}%` : "—"} />
          <div>
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-slate-500">API Spend (24h)</span>
              <span className="font-semibold tabular-nums text-slate-700">{spend != null ? `$${spend.toFixed(2)}` : "—"}</span>
            </div>
            <div className="mt-1 text-[10px] text-slate-400">{projected != null ? `~$${projected.toFixed(2)}/day power` : ""}</div>
          </div>
        </div>
      </div>
      {/* alerts */}
      <div className="flex w-[31%] flex-col border-l border-slate-100 pl-5">
        <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
          <Gauge className="h-3.5 w-3.5" /> Alerts
        </div>
        {alerts.length === 0 ? (
          <div className="flex items-center gap-1.5 text-[11px] text-emerald-600"><CheckCircle2 className="h-3.5 w-3.5" /> All systems nominal</div>
        ) : (
          <ul className="space-y-1">
            {alerts.map((a) => (
              <li key={a} className="flex items-start gap-1.5 text-[11px] text-amber-700">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
                <span>{a}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      {/* recent decisions (live Mimir certifications) */}
      <div className="flex flex-1 flex-col border-l border-slate-100 pl-5">
        <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
          <Zap className="h-3.5 w-3.5" /> Recent Decisions
        </div>
        {decisions.length === 0 ? (
          <div className="text-[11px] text-slate-400">No certifications in the window.</div>
        ) : (
          <ul className="space-y-1">
            {decisions.map((c, i) => (
              <li key={`${c.canonical_key ?? c.arxiv_id ?? c.title ?? i}`} className="flex items-start gap-1.5 text-[11px] text-slate-600">
                <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-500" />
                <span className="line-clamp-1">Certified “{c.title ?? c.arxiv_id ?? c.canonical_key ?? "a source"}”</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// --- Dormant -----------------------------------------------------------
// BUILT agents (Ariadne, Planner, Researchers, Request Queue) render a live card
// with a real-time activity badge + their mode as secondary microcopy. `enabled`
// = the agent's mode is advisory|active (else it shows Offline, not "Planned" —
// it's a built agent that's paused, distinct from the never-built dashed rooms).
function dormantLive(def: NodeDef, ariadne?: AriadneOverview | null):
  { value: string; sub: string; enabled: boolean; mode: string | null } | null {
  if (!ariadne) return null;
  const ag = ariadne.at_a_glance;
  const on = (m: string | null | undefined) => m === "advisory" || m === "active";
  if (def.id === "ariadne") {
    return { value: `${ag.active_directions} directions`, sub: ag.status, enabled: on(ariadne.mode), mode: ariadne.mode };
  }
  if (def.id === "request-queue") {
    const n = ag.acquire_requests_24h ?? 0;
    return { value: `${n} asks`, sub: "acquire · 24h", enabled: true, mode: null };
  }
  if (def.id === "planner") {
    const m = ag.planner_mode ?? "off";
    return { value: `${ag.research_tasks ?? 0} tasks`, sub: on(m) ? `${ag.research_tasks_pending ?? 0} pending` : "paused", enabled: on(m), mode: m };
  }
  if (def.id === "researchers") {
    const m = ag.researcher_mode ?? "off";
    const pending = ag.research_tasks_pending ?? 0;
    return { value: `${ag.research_tasks ?? 0} tasks`, sub: on(m) ? (pending > 0 ? `${pending} investigating` : "findings ready") : "paused", enabled: on(m), mode: m };
  }
  if (def.id === "experiments") {
    const m = ag.experiments_mode ?? "off";
    const running = ag.experiments_running ?? 0;
    return { value: `${ag.experiments_total ?? 0} runs`, sub: on(m) ? (running > 0 ? `${running} in flight` : "idle — awaiting a request") : "paused", enabled: on(m), mode: m };
  }
  return null;
}

export function DormantNode({ data }: NodeProps<FN>) {
  const { def, ariadne } = data;
  const lv = dormantLive(def, ariadne);
  const { state, ago, series } = useNodeMeter(data.def.id, lv?.enabled ?? false);
  if (lv) {
    return (
      <div className={cx("flex h-full w-full cursor-pointer flex-col rounded-node border border-slate-200 bg-white/90 p-3 shadow-card backdrop-blur transition hover:shadow-panel", busyRing(state))}>
        <NodeHandles />
        <div className="flex items-start justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <IconTile icon={def.icon} tone={lv.enabled ? "violet" : "slate"} />
            <div className="min-w-0">
              <div className="truncate text-[12px] font-semibold leading-tight text-slate-800">{def.title}</div>
              <div className="truncate text-[10px] text-slate-400">{def.sub}{lv.mode ? ` · ${lv.mode}` : ""}</div>
            </div>
          </div>
          <ActivityBadge state={state} ago={ago} />
        </div>
        <div className="mt-2 text-xl font-semibold tabular-nums text-slate-900">{lv.value}</div>
        <div className="text-[10px] text-slate-400">{lv.sub}</div>
        <ActivityMeter series={series} state={state} className="mt-auto h-3 pt-1.5" />
      </div>
    );
  }
  return (
    <div className="flex h-full w-full cursor-pointer flex-col rounded-node border border-dashed border-slate-200 bg-white/45 p-3 transition hover:bg-white/70">
      <NodeHandles />
      <div className="flex items-center justify-between">
        <div className="flex min-w-0 items-center gap-2">
          <IconTile icon={def.icon} tone="slate" />
          <div className="min-w-0">
            <div className="truncate text-[12px] font-semibold leading-tight text-slate-500">{def.title}</div>
            <div className="truncate text-[10px] text-slate-400">{def.sub}</div>
          </div>
        </div>
        <span className="shrink-0 rounded-full border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[9px] font-semibold text-slate-400">Planned</span>
      </div>
    </div>
  );
}

// --- Library container -------------------------------------------------
// Decorative box that groups the three live storage cards (which sit on top) as
// one "Library". Non-interactive (the cards open the Library inspector); also
// anchors the Mimir -> Library flow edge.
export function LibraryBoxNode({ data }: NodeProps<FN>) {
  return (
    <div className="relative h-full w-full rounded-wing border border-emerald-200/80 bg-emerald-50/25">
      <NodeHandles />
      <div className="absolute left-4 top-3 flex items-center gap-2">
        <span className="inline-flex h-5 w-5 items-center justify-center rounded-lg bg-emerald-100 text-emerald-600">
          <Library className="h-3 w-3" />
        </span>
        <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-emerald-700">{data.def.title}</span>
        <span className="hidden text-[10px] text-slate-400 md:inline">· {data.def.sub}</span>
      </div>
    </div>
  );
}

// --- Wing backdrop -----------------------------------------------------
export function WingNode({ data }: NodeProps<FN>) {
  return (
    <div className="h-full w-full rounded-[1.75rem] border border-slate-200/70 bg-white/30">
      <span className="absolute left-4 top-3 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-400">
        {data.def.wingLabel}
      </span>
    </div>
  );
}

export const NODE_TYPES = {
  scout: ScoutNode,
  mimir: MimirNode,
  storage: StorageNode,
  ops: OpsNode,
  dormant: DormantNode,
  librarybox: LibraryBoxNode,
  wing: WingNode,
};
