"use client";

// Per-NODE activity, derived live from the shared event stream (sibling to
// useFlowHeat, which lights EDGES). Every bus event is attributed to the node(s)
// that produced it; we keep each node's last-active timestamp and grade it:
//
//   busy    — acted within BUSY_MS (it's working right now)        → pulsing
//   live    — acted within LIVE_MS (on, recently active)            → steady
//   idle    — enabled but quiet beyond LIVE_MS                      → calm
//   offline — disabled (mode off/shadow) or planned                 → hollow
//
// This replaces the old per-node labels that showed MODE ("advisory") or 24h
// totals ("Collecting") — neither of which told you who's actually working now.

import { useEffect, useMemo, useRef, useState } from "react";
import { useSharedEvents } from "../../lib/event-stream";
import type { LabFoundryEvent, StreamMessage } from "../../lib/types";
import { sourceKindOf } from "./inspectors";

export type ActivityState = "busy" | "live" | "idle" | "offline";

export const BUSY_MS = 45_000;
export const LIVE_MS = 10 * 60_000;

export function deriveActivity(enabled: boolean, lastActiveAt: number | null, now: number): ActivityState {
  if (!enabled) return "offline";
  if (lastActiveAt == null) return "idle";
  const age = now - lastActiveAt;
  if (age < BUSY_MS) return "busy";
  if (age < LIVE_MS) return "live";
  return "idle";
}

export function agoLabel(ms: number): string {
  if (ms < 0) return "now";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.round(m / 60)}h`;
}

function asObj(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

// scout source_kind → scout node id; agent name / invocation prefix → agent node id.
const SCOUT_KIND_NODE: Record<string, string> = {
  web: "web", arxiv: "arxiv", paper: "arxiv", github: "github", code: "github", openml: "openml", dataset: "dataset",
};
const AGENT_NODE: Record<string, string> = {
  researcher: "researchers", researchers: "researchers", mimir: "mimir", planner: "planner",
  ariadne: "ariadne", pi: "ariadne", adversary: "critic", critic: "critic",
};

const MIMIR_EVENTS = new Set([
  "document.ingested", "document.parsed", "document.staged", "mimir.ingest_blocked",
  "library.ingest_rejected", "library.trends", "library.sweep_requested", "mimir.certify",
]);
const QUEUE_EVENTS = new Set(["acquire.requested", "acquire.fulfilled", "acquire.rejected", "mimir.ask", "mimir.answered"]);
const ARIADNE_EVENTS = new Set(["ariadne.deliberate", "ariadne.reflect", "claim.created", "claim.invalidated"]);
const RESEARCH_EVENTS = new Set(["task.created", "task.completed", "claim.confidence_changed"]);

// Map one event to the node(s) it lights up.
function nodesForEvent(e: LabFoundryEvent): string[] {
  const t = e.event_type;
  if (t === "source.discovered") {
    const k = sourceKindOf(e);
    const nid = k ? SCOUT_KIND_NODE[k] : null;
    return nid ? [nid] : [];
  }
  if (MIMIR_EVENTS.has(t)) return ["mimir"];
  if (QUEUE_EVENTS.has(t)) return ["mimir", "request-queue"];
  if (ARIADNE_EVENTS.has(t)) return ["ariadne"];
  if (t === "planner.plan") return ["planner"];
  if (RESEARCH_EVENTS.has(t)) return ["researchers"];
  if (t === "step.started" || t === "step.completed") {
    const it = asObj(e.payload).invocation_type;
    const a = typeof it === "string" ? it.split(".")[0] : null;
    const nid = a ? AGENT_NODE[a] : null;
    return nid ? [nid] : [];
  }
  if (t === "agent.stalled" || t === "agent.slow" || t === "agent.broken") {
    const a = asObj(e.payload).agent;
    const nid = typeof a === "string" ? AGENT_NODE[a] : null;
    return nid ? [nid] : [];
  }
  return [];
}

// Rolling activity-rate window — feeds the per-node live meters + the global
// lab heartbeat. Newest bucket on the right.
export const ACT_WINDOW_MS = 90_000;
export const ACT_BUCKETS = 15;

function bucketize(times: number[], now: number, windowMs: number, buckets: number): number[] {
  const out = new Array(buckets).fill(0);
  const span = windowMs / buckets;
  for (const t of times) {
    const age = now - t;
    if (age < 0 || age >= windowMs) continue;
    out[buckets - 1 - Math.floor(age / span)] += 1;
  }
  return out;
}

export interface NodeActivity {
  activeAt: Record<string, number>; // node id → last-active epoch ms
  series: Record<string, number[]>; // node id → bucketed event-rate over the window (live meter)
  total: number[]; // global bucketed event-rate (lab heartbeat)
  rate: number; // events attributed in the window (heartbeat headline)
  now: number; // re-evaluation tick (so busy→live→idle decays without new events)
  connected: boolean;
}

export function useNodeActivity(): NodeActivity {
  const { recent, connected } = useSharedEvents();
  const lastRef = useRef<Record<string, number>>({});
  const timesRef = useRef<Record<string, number[]>>({}); // per-node event timestamps
  const globalRef = useRef<number[]>([]); // all attributed event timestamps
  const seen = useRef<Set<number>>(new Set());
  const [activeAt, setActiveAt] = useState<Record<string, number>>({});
  const [now, setNow] = useState<number>(() => Date.now());

  const events = useMemo(
    () =>
      recent
        .filter((m): m is Extract<StreamMessage, { type: "event" }> => m.type === "event")
        .map((m) => m.event),
    [recent],
  );

  useEffect(() => {
    let changed = false;
    for (const e of events) {
      if (seen.current.has(e.id)) continue;
      seen.current.add(e.id);
      const ts = Date.parse(e.emitted_at) || Date.now();
      const nids = nodesForEvent(e);
      if (nids.length) globalRef.current.push(ts);
      for (const nid of nids) {
        if (!(lastRef.current[nid] >= ts)) {
          lastRef.current[nid] = ts;
          changed = true;
        }
        (timesRef.current[nid] ??= []).push(ts);
      }
    }
    if (changed) setActiveAt({ ...lastRef.current });
  }, [events]);

  // Tick: prune the rolling windows + advance `now` so meters refresh and a node
  // decays busy → live → idle even when the stream goes quiet.
  useEffect(() => {
    const t = setInterval(() => {
      const n = Date.now();
      for (const k of Object.keys(timesRef.current)) {
        timesRef.current[k] = timesRef.current[k].filter((x) => n - x < ACT_WINDOW_MS);
      }
      globalRef.current = globalRef.current.filter((x) => n - x < ACT_WINDOW_MS);
      setNow(n);
    }, 2000);
    return () => clearInterval(t);
  }, []);

  const { series, total, rate } = useMemo(() => {
    const series: Record<string, number[]> = {};
    for (const [nid, times] of Object.entries(timesRef.current)) {
      series[nid] = bucketize(times, now, ACT_WINDOW_MS, ACT_BUCKETS);
    }
    const total = bucketize(globalRef.current, now, ACT_WINDOW_MS, ACT_BUCKETS);
    return { series, total, rate: globalRef.current.filter((t) => now - t < ACT_WINDOW_MS).length };
  }, [now, activeAt]);

  return { activeAt, series, total, rate, now, connected };
}
