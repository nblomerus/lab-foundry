"use client";

// Live data the canvas binds onto nodes: the knowledge pulse (Mimir/stats/host/
// ingest series) plus per-scout panels, per-scout hourly sparklines, and the
// real ops cost telemetry (/debug/costs). All read-only, polled.

import { useEffect, useState } from "react";
import { api, type AriadneOverview, type DebugCosts, type QmExperiments, type ScoutPanel } from "../../lib/api";
import { useKnowledgePulse, type KnowledgePulse } from "../../lib/pulse";

const SCOUT_KINDS = ["web", "arxiv", "github", "openml", "dataset"] as const;

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
    const id = setInterval(load, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

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
