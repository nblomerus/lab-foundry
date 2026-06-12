"use client";

// Live data the canvas binds onto nodes: the knowledge pulse (Mimir/stats/host/
// ingest series) plus per-scout panels, per-scout hourly sparklines, the real ops
// cost telemetry (/debug/costs), Ariadne's overview, and the experiment ledger.
// Polled at the dashboard cadence AND event-nudged: a loop event on the stream
// (task/experiment/direction/finding) triggers a debounced immediate refetch, so
// the state-derived narration ("waiting on experiment #92") flips within ~2s of
// the dependency resolving instead of at the next 10s poll.

import { useEffect, useMemo, useRef, useState } from "react";
import { api, type AriadneOverview, type DebugCosts, type QmExperiments, type ScoutPanel } from "../../lib/api";
import { useSharedEvents } from "../../lib/event-stream";
import type { StreamMessage } from "../../lib/types";
import { useKnowledgePulse, type KnowledgePulse } from "../../lib/pulse";

const SCOUT_KINDS = ["web", "arxiv", "github", "openml", "dataset"] as const;

// Events that change what the state bubbles say — worth an immediate refetch.
const NUDGE_EVENTS = new Set([
  "task.created", "task.completed",
  "experiment.requested", "experiment.completed", "experiment.failed",
  "ariadne.deliberate", "ariadne.reflect", "direction.adjudicate", "direction.reopened",
  "claim.created", "claim.invalidated", "finding.synthesize", "acquire.requested", "acquire.fulfilled",
]);

export interface FloorData {
  pulse: KnowledgePulse;
  scouts: Record<string, ScoutPanel | null>;
  scoutSeries: Record<string, number[]>;
  costs: DebugCosts | null;
  ariadne: AriadneOverview | null;
  qm: QmExperiments | null;
}

export function useFloorData(): FloorData {
  const pulse = useKnowledgePulse(10_000);
  const [scouts, setScouts] = useState<Record<string, ScoutPanel | null>>({});
  const [scoutSeries, setScoutSeries] = useState<Record<string, number[]>>({});
  const [costs, setCosts] = useState<DebugCosts | null>(null);
  const [ariadne, setAriadne] = useState<AriadneOverview | null>(null);
  const [qm, setQm] = useState<QmExperiments | null>(null);
  const loadRef = useRef<(() => Promise<void>) | null>(null);

  // Scout panels + ops costs + Ariadne's live state — poll at the dashboard cadence.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const panels = await Promise.all(SCOUT_KINDS.map((k) => api.scoutPanel(k).catch(() => null)));
      const cost = await api.debugCosts().catch(() => null);
      const ari = await api.ariadneOverview().catch(() => null);
      const exps = await api.qmExperiments(12).catch(() => null);
      if (cancelled) return;
      // LAST KNOWN GOOD: a transient API blip (restart, slow poll) must not null out
      // data the dashboard already had — cards were regressing to "—"/"Planned" for a
      // poll cycle. State only moves forward; the next successful poll refreshes it.
      setScouts((prev) => {
        const next: Record<string, ScoutPanel | null> = { ...prev };
        SCOUT_KINDS.forEach((k, i) => { if (panels[i]) next[k] = panels[i]; });
        return next;
      });
      if (cost) setCosts(cost);
      if (ari) setAriadne(ari);
      if (exps) setQm(exps);
    };
    load();
    loadRef.current = load;
    const id = setInterval(load, 10_000);
    return () => { cancelled = true; loadRef.current = null; clearInterval(id); };
  }, []);

  // EVENT NUDGE — a loop event means the waiting graph just changed; refetch now
  // (debounced 1.5s so an event burst costs one round-trip, not one per event).
  const { recent } = useSharedEvents();
  const seenNudge = useRef<Set<number>>(new Set());
  const nudgeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const nudges = useMemo(
    () =>
      recent
        .filter((m): m is Extract<StreamMessage, { type: "event" }> => m.type === "event")
        .map((m) => m.event)
        .filter((e) => NUDGE_EVENTS.has(e.event_type)),
    [recent],
  );
  useEffect(() => {
    let fresh = false;
    for (const e of nudges) {
      if (!seenNudge.current.has(e.id)) {
        seenNudge.current.add(e.id);
        fresh = true;
      }
    }
    if (!fresh || nudgeTimer.current) return;
    nudgeTimer.current = setTimeout(() => {
      nudgeTimer.current = null;
      loadRef.current?.();
    }, 1500);
  }, [nudges]);

  // Scout sparklines (hourly discovered) — slow-moving, refreshed less often.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const series = await Promise.all(
        SCOUT_KINDS.map((k) =>
          api.timeseries("discovered", { kind: k, bucket: "hour", points: 24 })
            .then((r) => r.points.map((p) => p.value))
            .catch(() => [] as number[]),
        ),
      );
      if (cancelled) return;
      const next: Record<string, number[]> = {};
      SCOUT_KINDS.forEach((k, i) => { next[k] = series[i]; });
      setScoutSeries(next);
    };
    load();
    const id = setInterval(load, 60_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  return { pulse, scouts, scoutSeries, costs, ariadne, qm };
}
