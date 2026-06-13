"use client";

// The floorplan centerpiece, rebuilt on React Flow: wings + agent/storage/ops
// nodes + activity-gated flow edges, with click-to-open inspectors and chrome
// overlays (flow legend, status legend, zoom controls, live-activity feed).
// Must be rendered inside <EventStreamProvider> (it reads the shared stream).

import { useCallback, useMemo, useState } from "react";
import { ChevronDown, Heart } from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";
import { ReactFlow, ReactFlowProvider, Controls, Panel, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { Snapshot } from "../../lib/types";
import { cx } from "../ui";
import { ActivityFeed } from "../ActivityFeed";
import { NODE_DEFS, EDGE_DEFS, type NodeDef } from "./topology";
import { NODE_TYPES, ActivityMeter, type FloorNodeData, type Selected } from "./nodes";
import { EDGE_TYPES } from "./FlowEdge";
import { type FloorData } from "./useFloorData";
import { NarrationContext, useNarration } from "./narration";
import { useFlowHeat } from "./useFlowHeat";
import { useNodeActivity, NodeActivityContext, BUSY_MS } from "./useNodeActivity";
import { AriadneInspector, GateInspector, LibraryInspector, MimirInspector, OpsInspector, PlannerInspector, QueueInspector, ResearcherInspector, ScoutInspector } from "./inspectors";

function nodeData(def: NodeDef, fd: FloorData, priority: string | null, onOpen: (s: Selected) => void): FloorNodeData {
  // NOTE: no activity/narration fields here — both are read from context per node,
  // so this stays structural and the nodes array reference is stable across event ticks.
  const base: FloorNodeData = { def, onOpen };
  switch (def.type) {
    case "scout":
      return { ...base, panel: fd.scouts[def.sourceKind ?? ""] ?? null, series: fd.scoutSeries[def.sourceKind ?? ""] ?? [] };
    case "mimir":
      return { ...base, mimir: fd.pulse.mimir, stats: fd.pulse.stats, priority };
    case "storage":
      return { ...base, stats: fd.pulse.stats, ariadne: fd.ariadne };
    case "ops":
      return { ...base, host: fd.pulse.host, costs: fd.costs, mimir: fd.pulse.mimir };
    default:
      return { ...base, ariadne: fd.ariadne };
  }
}

function FlowLegend({ connected, working }: { connected: boolean; working: number }) {
  const flows: [string, string][] = [["Intake", "#2c5fb8"], ["Knowledge", "#10b981"], ["Converse", "#8b5cf6"], ["Feedback", "#f59e0b"], ["Planned", "#9aa3ad"]];
  // [label, dot classes, pulse?] — matches the per-node ActivityBadge exactly so
  // the legend reads as the key to what's on the floor.
  const statuses: [string, string, boolean][] = [
    ["Busy", "bg-emerald-500", true],
    ["Live", "bg-emerald-500", false],
    ["Idle", "bg-slate-300", false],
    ["Offline", "bg-transparent ring-1 ring-inset ring-slate-300", false],
  ];
  return (
    <div className="glass-panel rounded-card px-3 py-2.5 text-[10px]">
      {/* at-a-glance: how many agents are working right now */}
      <div className="mb-2 flex items-center gap-1.5">
        <span className={cx("inline-block h-2 w-2 rounded-full", working > 0 ? "bg-emerald-500 pulse-dot" : "bg-slate-300")} />
        <span className="text-[11px] font-semibold tabular-nums text-slate-700">{working}</span>
        <span className="text-slate-400">working now</span>
      </div>
      <div className="mb-1 font-semibold uppercase tracking-wide text-slate-400">Status</div>
      <div className="mb-2 flex flex-wrap gap-x-2.5 gap-y-1">
        {statuses.map(([label, c, pulse]) => (
          <span key={label} className="flex items-center gap-1 text-slate-500">
            <span className={cx("inline-block h-1.5 w-1.5 rounded-full", c, pulse && "pulse-dot")} /> {label}
          </span>
        ))}
      </div>
      <div className="mb-1 font-semibold uppercase tracking-wide text-slate-400">Flows</div>
      <div className="flex flex-col gap-1">
        {flows.map(([label, c]) => (
          <span key={label} className="flex items-center gap-1.5 text-slate-500">
            <span className="inline-block h-0.5 w-5 rounded-full" style={{ background: c }} /> {label}
          </span>
        ))}
      </div>
      <div className="mt-2 flex items-center gap-1 text-slate-400">
        <span className={cx("inline-block h-1.5 w-1.5 rounded-full", connected ? "bg-emerald-500 pulse-dot" : "bg-slate-300")} />
        {connected ? "stream live" : "stream offline"}
      </div>
    </div>
  );
}

// Global lab heartbeat — the whole lab's pulse. The heart beats while events are
// flowing, the meter shows the rate, so you can feel the lab breathing (bursts of
// ingest) vs resting at a glance.
function LabHeartbeat({ total, rate, connected }: { total: number[]; rate: number; connected: boolean }) {
  const beating = connected && rate > 0;
  return (
    <div className="glass-panel flex items-center gap-2.5 rounded-card px-3 py-2">
      <Heart className={cx("h-4 w-4 text-emerald-500", beating && "heartbeat")} fill={beating ? "currentColor" : "none"} strokeWidth={2.2} />
      <div className="flex flex-col">
        <span className="text-[9px] font-semibold uppercase tracking-wider text-slate-400">Lab Activity</span>
        <span className="text-[11px] font-semibold tabular-nums text-slate-700">
          {rate} <span className="font-medium text-slate-400">events · 90s</span>
        </span>
      </div>
      <ActivityMeter series={total} state={beating ? "busy" : "idle"} className="h-5 w-24" />
    </div>
  );
}

function InspectorShell({ title, planned, onClose, children }: { title: string; planned?: boolean; onClose: () => void; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <h3 className="text-lg font-semibold tracking-tight text-slate-950">{title}</h3>
          <span className={cx("rounded-full border px-2 py-0.5 text-[11px] font-semibold", planned ? "border-slate-200 bg-slate-50 text-slate-500" : "border-emerald-200 bg-emerald-50 text-emerald-700")}>
            {planned ? "Planned" : "Live"}
          </span>
        </div>
        <button type="button" onClick={onClose} className="rounded-xl border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50">Close</button>
      </div>
      {children}
    </div>
  );
}

function FloorplanInner({ snapshot, floorData, className }: { snapshot: Snapshot | null; floorData: FloorData; className?: string }) {
  const { hot, connected, events } = useFlowHeat();
  const activity = useNodeActivity();
  const { activeAt, total, rate, now } = activity;
  const [selected, setSelected] = useState<Selected | null>(null);
  const [activityOpen, setActivityOpen] = useState(true);
  const stats = floorData.pulse.stats;

  // "working now" = nodes that emitted an event within the busy window.
  const working = useMemo(() => Object.values(activeAt).filter((t) => now - t < BUSY_MS).length, [activeAt, now]);

  const priority = useMemo(() => {
    const focus = floorData.pulse.mimir?.focus_topics;
    return focus && focus.length ? focus.slice(0, 2).join(", ") : null;
  }, [floorData.pulse.mimir]);

  const onOpen = useCallback((s: Selected) => setSelected(s), []);

  // plain-language narration per node — event bubbles land instantly off the stream;
  // state bubbles refresh with the (event-nudged) polls. Provided via context below.
  const narration = useNarration(floorData.ariadne, floorData.qm);

  const nodes = useMemo<Node<FloorNodeData>[]>(
    () =>
      NODE_DEFS.map((def) => {
        // Backdrops (wings + the Library container) sit behind and don't capture
        // pointer events, so the storage cards on top stay clickable and the
        // canvas still pans over them.
        const backdrop = def.type === "wing" || def.type === "librarybox";
        return {
          id: def.id,
          type: def.type,
          position: { x: def.x, y: def.y },
          style: backdrop ? { width: def.w, height: def.h, pointerEvents: "none" as const } : { width: def.w, height: def.h },
          draggable: false,
          selectable: !backdrop,
          connectable: false,
          zIndex: backdrop ? 0 : 1,
          data: nodeData(def, floorData, priority, onOpen),
        };
      }),
    [floorData, priority, onOpen],
  );

  const edges = useMemo(
    () =>
      EDGE_DEFS.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        sourceHandle: e.sourceHandle,
        targetHandle: e.targetHandle,
        type: "flow",
        data: { kind: e.kind, live: e.live, hot: hot.has(e.id) },
      })),
    [hot],
  );

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node<FloorNodeData>) => {
    const def = node.data?.def;
    if (!def?.inspector) return;
    switch (def.inspector) {
      case "scout": setSelected({ kind: "scout", sourceKind: def.sourceKind ?? "", title: def.title ?? "Scout" }); break;
      case "mimir": setSelected({ kind: "mimir", title: def.title ?? "Mimir" }); break;
      case "library": setSelected({ kind: "library", title: "Library" }); break;
      case "ariadne": setSelected({ kind: "ariadne", title: def.title ?? "Ariadne" }); break;
      case "queue": setSelected({ kind: "queue", title: def.title ?? "Request Queue" }); break;
      case "planner": setSelected({ kind: "planner", title: def.title ?? "Planner" }); break;
      case "researcher": setSelected({ kind: "researcher", title: def.title ?? "Researchers" }); break;
      case "ops": setSelected({ kind: "ops", title: def.title ?? "Ops / Quartermaster" }); break;
      case "info": setSelected({ kind: "info", title: def.title ?? "", sub: def.sub, description: def.description }); break;
      case "gate": setSelected({ kind: "gate", scope: "all", title: def.title ?? "Gate" }); break;
    }
  }, []);

  return (
    <NodeActivityContext.Provider value={activity}>
    <NarrationContext.Provider value={narration}>
    <div className={cx("bg-blueprint relative overflow-hidden rounded-wing border border-slate-200 shadow-card", className ?? "h-[calc(100vh-15rem)] min-h-[620px]")}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        onNodeClick={onNodeClick as never}
        onPaneClick={() => setSelected(null)}
        fitView
        fitViewOptions={{ padding: 0.1 }}
        minZoom={0.3}
        maxZoom={1.6}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        zoomOnScroll={false}
        panOnScroll={false}
        preventScrolling={false}
        panOnDrag
        zoomOnPinch
        elementsSelectable
      >
        <Controls showInteractive={false} position="bottom-left" className="!shadow-panel !rounded-xl" />
        <Panel position="top-left"><FlowLegend connected={connected} working={working} /></Panel>
        <Panel position="top-center"><LabHeartbeat total={total} rate={rate} connected={connected} /></Panel>
        <Panel position="bottom-right">
          <div className={cx("glass-panel flex w-80 flex-col rounded-card p-3", activityOpen && "h-72")}>
            <div className="flex items-center justify-between">
              <button
                type="button"
                onClick={() => setActivityOpen((o) => !o)}
                className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-400 hover:text-slate-600"
              >
                <ChevronDown className={cx("h-3 w-3 transition-transform", !activityOpen && "-rotate-90")} />
                Live Activity
              </button>
              <span className="flex items-center gap-1.5 text-[10px] font-medium text-slate-400">
                <span className={cx("inline-block h-1.5 w-1.5 rounded-full", connected ? "bg-emerald-500 pulse-dot" : "bg-slate-300")} />
                {connected ? "Live" : "Offline"}
              </span>
            </div>
            {activityOpen && <ActivityFeed limit={12} className="mt-2 min-h-0 flex-1" hideHeader />}
          </div>
        </Panel>
      </ReactFlow>

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
            {selected.kind === "gate" ? (
              <GateInspector scope={selected.scope} title={selected.title} onClose={() => setSelected(null)} />
            ) : selected.kind === "mimir" ? (
              <InspectorShell title={selected.title} onClose={() => setSelected(null)}>
                <MimirInspector knowledge={stats} events={events} />
              </InspectorShell>
            ) : selected.kind === "library" ? (
              <InspectorShell title="Library" onClose={() => setSelected(null)}>
                <LibraryInspector knowledge={stats} snapshot={snapshot} />
              </InspectorShell>
            ) : selected.kind === "scout" ? (
              <InspectorShell title={selected.title} onClose={() => setSelected(null)}>
                <ScoutInspector kind={selected.sourceKind} corpus={stats?.corpus} />
              </InspectorShell>
            ) : selected.kind === "ariadne" ? (
              <InspectorShell title={selected.title} onClose={() => setSelected(null)}>
                <AriadneInspector ariadne={floorData.ariadne} />
              </InspectorShell>
            ) : selected.kind === "queue" ? (
              <InspectorShell title={selected.title} onClose={() => setSelected(null)}>
                <QueueInspector />
              </InspectorShell>
            ) : selected.kind === "planner" ? (
              <InspectorShell title={selected.title} onClose={() => setSelected(null)}>
                <PlannerInspector />
              </InspectorShell>
            ) : selected.kind === "researcher" ? (
              <InspectorShell title={selected.title} onClose={() => setSelected(null)}>
                <ResearcherInspector />
              </InspectorShell>
            ) : selected.kind === "ops" ? (
              <InspectorShell title={selected.title} onClose={() => setSelected(null)}>
                <OpsInspector host={floorData.pulse.host} costs={floorData.costs} mimir={floorData.pulse.mimir} />
              </InspectorShell>
            ) : (
              <InspectorShell title={selected.title} planned onClose={() => setSelected(null)}>
                <p className="text-sm leading-snug text-slate-600">{selected.description ?? "Planned — activates with the research workflow."}</p>
              </InspectorShell>
            )}
          </motion.aside>
        )}
      </AnimatePresence>
    </div>
    </NarrationContext.Provider>
    </NodeActivityContext.Provider>
  );
}

export function FloorplanCanvas({ snapshot, floorData, className }: { snapshot: Snapshot | null; floorData: FloorData; className?: string }) {
  return (
    <ReactFlowProvider>
      <FloorplanInner snapshot={snapshot} floorData={floorData} className={className} />
    </ReactFlowProvider>
  );
}
