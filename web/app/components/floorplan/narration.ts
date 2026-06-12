"use client";

// Narration — the floorplan's speech bubbles: what is RUNNING WHERE, WHO is WAITING
// FOR WHAT, and what just HAPPENED — in plain sentences anyone can read.
//
// Two layers, merged per node:
//   * STATE bubbles (composeNarration) — durable running/waiting facts composed from
//     the same polls the cards use, so a bubble can never disagree with its card.
//     They persist exactly as long as the state they describe.
//   * EVENT bubbles (eventBubble) — transient happenings straight off the live event
//     stream ("arXiv scout found '…'", "synthesizing direction #84 into a finding"),
//     shown the instant they occur and faded after a short TTL. While alive, an event
//     bubble overrides the state bubble on its node.
//
// Narration is provided through NarrationContext (the useNodeActivity pattern): the
// React-Flow nodes array stays STRUCTURALLY STABLE — bubbles re-render per node, not
// per canvas. Events also nudge the state polls (see useFloorData) so "waiting on
// experiment #92" flips within ~2s of experiment.completed, not at the next 10s poll.

import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useSharedEvents } from "../../lib/event-stream";
import type { AriadneOverview, QmExperiment, QmExperiments } from "../../lib/api";
import type { LabFoundryEvent, StreamMessage } from "../../lib/types";
import { sourceKindOf } from "./inspectors";

export interface Bubble {
  kind: "running" | "waiting" | "reading";
  text: string;
}

const SCOUT_NODE: Record<string, string> = {
  web: "web", arxiv: "arxiv", paper: "arxiv", github: "github", code: "github", openml: "openml", dataset: "dataset",
};

function elapsed(iso?: string | null): string {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return s < 120 ? `${Math.round(s)}s` : `${Math.round(s / 60)}m`;
}

function clip(s: string | null | undefined, n: number): string {
  if (!s) return "";
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function asObj(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

// ── STATE layer ────────────────────────────────────────────────────────────────

export function composeNarration(ariadne: AriadneOverview | null, qm: QmExperiments | null): Record<string, Bubble> {
  const out: Record<string, Bubble> = {};
  const ag = ariadne?.at_a_glance;
  const running: QmExperiment[] = (qm?.experiments ?? []).filter((e) => e.status === "running");
  const queued: QmExperiment[] = (qm?.experiments ?? []).filter((e) => e.status === "queued");
  const on = (m?: string | null) => m === "advisory" || m === "active";

  // Experiments — what is running WHERE (lane + container), on which direction, for how long.
  if (running.length > 0) {
    const e = running[0];
    const lane = e.requires_gpu ? "GPU" : "CPU";
    const where = e.worker ? ` · ${e.worker}` : "";
    const more = running.length > 1 ? ` (+${running.length - 1} more)` : "";
    out["experiments"] = {
      kind: "running",
      text: `#${e.id} running on ${lane}${where} (${elapsed(e.started_at)}) — “${clip(e.hypothesis, 90)}” · direction #${e.claim_id}${more}`,
    };
  } else if (queued.length > 0) {
    const e = queued[0];
    out["experiments"] = {
      kind: "waiting",
      text: `#${e.id} queued for a ${e.requires_gpu ? "GPU" : "CPU"} slot — “${clip(e.hypothesis, 80)}” · direction #${e.claim_id}`,
    };
  }

  if (ag) {
    const tasksOpen = (ag.research_tasks_pending ?? 0) + (ag.research_tasks_running ?? 0);
    const expBusy = running.length > 0 || queued.length > 0;
    const firstBusy = running[0] ?? queued[0];

    // Researchers — investigating, or naming exactly what they wait on.
    if ((ag.research_tasks_running ?? 0) > 0) {
      out["researchers"] = { kind: "running", text: `investigating ${ag.research_tasks_running} task(s) against the Library` };
    } else if ((ag.research_tasks_pending ?? 0) > 0) {
      out["researchers"] = { kind: "waiting", text: `${ag.research_tasks_pending} task(s) queued — picking up next` };
    } else if (expBusy && firstBusy) {
      out["researchers"] = { kind: "waiting", text: `waiting on experiment #${firstBusy.id} (direction #${firstBusy.claim_id}) to interpret` };
    } else if (on(ag.researcher_mode)) {
      out["researchers"] = { kind: "waiting", text: "waiting for the planner's next tasks" };
    }

    // Planner — supply state.
    if (on(ag.planner_mode) && tasksOpen === 0) {
      out["planner"] = expBusy
        ? { kind: "waiting", text: "all directions planned — waiting on experiment results" }
        : (ag.approved ?? 0) > 0
          ? { kind: "waiting", text: "approved directions all worked — waiting for the next gap or approval" }
          : { kind: "waiting", text: "waiting for gate approvals to plan against" };
    }

    // Ariadne — steering vs waiting vs exhausted.
    if ((ag.active_directions ?? 0) === 0) {
      out["ariadne"] = { kind: "waiting", text: "agenda exhausted — a fresh deliberation will fire on the cooldown" };
    } else if (expBusy && firstBusy) {
      out["ariadne"] = { kind: "waiting", text: `waiting on direction #${firstBusy.claim_id}'s evidence before steering` };
    } else if (ag.status) {
      out["ariadne"] = { kind: "reading", text: `${ag.status} — ${ag.active_directions} direction(s), ${ag.findings ?? 0} finding(s) so far` };
    }

    // Gate — slots + what it waits for.
    const approved = ag.approved ?? 0;
    const budget = ag.gate_budget ?? 0;
    if (budget > 0) {
      out["gate-promotion"] =
        approved < budget
          ? { kind: "waiting", text: `${approved}/${budget} slots used — waiting for adjudicated 'pass' candidates` }
          : { kind: "reading", text: `gate budget full (${approved}/${budget}) — directions in flight` };
    }

    // Critic — has it ever had anything to challenge?
    if (on(ag.critic_mode)) {
      out["critic"] =
        (ag.critic_verdicts ?? 0) === 0
          ? { kind: "waiting", text: "waiting for a high-signal finding to challenge" }
          : { kind: "reading", text: `${ag.critic_verdicts} verdict(s) issued — watching for the next high-signal finding` };
    }

    // Request queue — acquisitions in flight with Mimir.
    if ((ag.acquire_pending ?? 0) > 0) {
      out["request-queue"] = { kind: "running", text: `${ag.acquire_pending} acquisition(s) being adjudicated by Mimir` };
    }
  }

  return out;
}

// ── EVENT layer ────────────────────────────────────────────────────────────────

const EVENT_TTL_MS = 12_000;
const LONG_TTL_MS = 90_000; // deliberation/synthesis-style work that runs for minutes

interface TransientBubble {
  nodeId: string;
  bubble: Bubble;
  ttlMs: number;
}

const ALL_SCOUTS = ["web", "arxiv", "github", "openml", "dataset"];

export function eventBubbles(e: LabFoundryEvent): TransientBubble[] {
  const t = e.event_type;
  const p = asObj(e.payload);
  switch (t) {
    case "source.discovered": {
      const k = sourceKindOf(e);
      const nid = k ? SCOUT_NODE[k] : null;
      const title = asObj(p.source).title;
      return nid
        ? [{ nodeId: nid, bubble: { kind: "running", text: `found “${clip(String(title ?? "a new source"), 70)}”` }, ttlMs: EVENT_TTL_MS }]
        : [];
    }
    case "document.ingested":
      return [{
        nodeId: "mimir",
        bubble: { kind: "running", text: `certified a ${p.kind ?? "document"} into the Library (${p.trust_tier ?? "tiered"})` },
        ttlMs: EVENT_TTL_MS,
      }];
    case "library.sweep_requested": {
      // The briefing the user asked to SEE: Mimir dispatches the sweep, and every scout
      // gets told what to look for (the sweep's topics fan out to all of them).
      const topicList = Array.isArray(p.topics) ? (p.topics as unknown[]).map(String) : [];
      const topics = topicList.slice(0, 2).join(" · ");
      const claim = p.claim_id ? ` (for direction #${p.claim_id})` : "";
      const out: TransientBubble[] = [
        {
          nodeId: "mimir",
          bubble: { kind: "running", text: topics ? `briefing the scouts${claim}: ${clip(topics, 60)}` : "briefing the scouts for a field sweep" },
          ttlMs: LONG_TTL_MS,
        },
      ];
      for (const sc of ALL_SCOUTS) {
        out.push({
          nodeId: sc,
          bubble: { kind: "running", text: topics ? `told to look for: ${clip(topics, 60)}` : "told to sweep the field" },
          ttlMs: LONG_TTL_MS,
        });
      }
      return out;
    }
    case "library.sweep_settled":
      return [{
        nodeId: "mimir",
        bubble: { kind: "reading", text: `sweep settled — scanned ${p.scanned ?? "?"}, ${p.discovered ?? 0} genuinely new` },
        ttlMs: EVENT_TTL_MS,
      }];
    case "acquire.requested":
      return [{
        nodeId: "request-queue",
        bubble: { kind: "running", text: `fetching: “${clip(String(p.query ?? p.paper ?? "a source"), 70)}”${p.claim_id ? ` · direction #${p.claim_id}` : ""}` },
        ttlMs: EVENT_TTL_MS,
      }];
    case "acquire.fulfilled":
      return [{ nodeId: "request-queue", bubble: { kind: "reading", text: `shelved “${clip(String(p.title ?? p.query ?? "a source"), 70)}”` }, ttlMs: EVENT_TTL_MS }];
    case "task.created":
      return [{ nodeId: "researchers", bubble: { kind: "running", text: `picked up task #${e.target_id ?? "?"}` }, ttlMs: EVENT_TTL_MS }];
    case "task.completed":
      return [{ nodeId: "researchers", bubble: { kind: "reading", text: `finished task #${e.target_id ?? "?"} — feeding the direction` }, ttlMs: EVENT_TTL_MS }];
    case "experiment.requested":
      return [{
        nodeId: "experiments",
        bubble: { kind: "running", text: `designing an experiment for direction #${p.claim_id ?? "?"}` },
        ttlMs: LONG_TTL_MS,
      }];
    case "experiment.completed":
      return [{
        nodeId: "experiments",
        bubble: { kind: "reading", text: `run #${p.experiment_id ?? "?"} finished — interpreting the numbers` },
        ttlMs: EVENT_TTL_MS,
      }];
    case "experiment.failed":
      return [{
        nodeId: "experiments",
        bubble: { kind: "waiting", text: `run #${p.experiment_id ?? "?"} failed — recording the failure as data` },
        ttlMs: EVENT_TTL_MS,
      }];
    case "finding.synthesize":
      return [{
        nodeId: "ariadne",
        bubble: { kind: "running", text: `synthesizing direction #${p.claim_id ?? "?"} into a finding (${p.experiment_count ?? "?"} experiments)` },
        ttlMs: LONG_TTL_MS,
      }];
    case "ariadne.deliberate":
      return [{ nodeId: "ariadne", bubble: { kind: "running", text: "re-framing the research agenda from the current field…" }, ttlMs: LONG_TTL_MS }];
    case "ariadne.reflect":
      return [{ nodeId: "ariadne", bubble: { kind: "running", text: "reflecting on the standing agenda…" }, ttlMs: LONG_TTL_MS }];
    case "direction.adjudicate":
      return [{ nodeId: "gate-promotion", bubble: { kind: "running", text: "adjudicating proposed directions against prior art" }, ttlMs: LONG_TTL_MS }];
    case "direction.reopened":
      return [{
        nodeId: "ariadne",
        bubble: { kind: "reading", text: `reopened direction #${p.claim_id ?? "?"} — ${p.new_matching_docs ?? "new"} on-topic papers arrived` },
        ttlMs: EVENT_TTL_MS,
      }];
    case "finding.high_signal":
      return [{ nodeId: "critic", bubble: { kind: "running", text: `challenging a high-signal finding on direction #${e.target_id ?? "?"}` }, ttlMs: LONG_TTL_MS }];
    default:
      return [];
  }
}

// ── merge + context ────────────────────────────────────────────────────────────

export function useNarration(ariadne: AriadneOverview | null, qm: QmExperiments | null): Record<string, Bubble> {
  const baseline = useMemo(() => composeNarration(ariadne, qm), [ariadne, qm]);
  const { recent } = useSharedEvents();
  const seen = useRef<Set<number>>(new Set());
  const transientRef = useRef<Record<string, { bubble: Bubble; until: number }>>({});
  const [transient, setTransient] = useState<Record<string, Bubble>>({});

  const events = useMemo(
    () =>
      recent
        .filter((m): m is Extract<StreamMessage, { type: "event" }> => m.type === "event")
        .map((m) => m.event),
    [recent],
  );

  useEffect(() => {
    let changed = false;
    const now = Date.now();
    for (const e of events) {
      if (seen.current.has(e.id)) continue;
      seen.current.add(e.id);
      for (const tb of eventBubbles(e)) {
        transientRef.current[tb.nodeId] = { bubble: tb.bubble, until: now + tb.ttlMs };
        changed = true;
      }
    }
    if (changed) {
      setTransient(Object.fromEntries(Object.entries(transientRef.current).map(([k, v]) => [k, v.bubble])));
    }
  }, [events]);

  // TTL pruning — fade transient bubbles back to the state layer.
  useEffect(() => {
    const t = setInterval(() => {
      const now = Date.now();
      let changed = false;
      for (const [k, v] of Object.entries(transientRef.current)) {
        if (v.until <= now) {
          delete transientRef.current[k];
          changed = true;
        }
      }
      if (changed) {
        setTransient(Object.fromEntries(Object.entries(transientRef.current).map(([k, v]) => [k, v.bubble])));
      }
    }, 3000);
    return () => clearInterval(t);
  }, []);

  return useMemo(() => ({ ...baseline, ...transient }), [baseline, transient]);
}

export const NarrationContext = createContext<Record<string, Bubble>>({});

export function useNodeBubble(nodeId: string): Bubble | null {
  return useContext(NarrationContext)[nodeId] ?? null;
}
