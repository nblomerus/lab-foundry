"use client";

// Live data the canvas binds onto nodes: the knowledge pulse (Mimir/stats/host/
// ingest series) plus per-scout panels, per-scout hourly sparklines, and the
// real ops cost telemetry (/debug/costs). All read-only, polled.

import { useEffect, useState } from "react";
import { api, type AriadneOverview, type DebugCosts, type ScoutPanel } from "../../lib/api";
import { useKnowledgePulse, type KnowledgePulse } from "../../lib/pulse";

const SCOUT_KINDS = ["web", "arxiv", "github", "openml", "dataset"] as const;

export interface FloorData {
  pulse: KnowledgePulse;
  scouts: Record<string, ScoutPanel | null>;
  scoutSeries: Record<string, number[]>;
  costs: DebugCosts | null;
  ariadne: AriadneOverview | null;
}

export function useFloorData(): FloorData {
  const pulse = useKnowledgePulse(10_000);
  const [scouts, setScouts] = useState<Record<string, ScoutPanel | null>>({});
  const [scoutSeries, setScoutSeries] = useState<Record<string, number[]>>({});
  const [costs, setCosts] = useState<DebugCosts | null>(null);
  const [ariadne, setAriadne] = useState<AriadneOverview | null>(null);

  // Scout panels + ops costs + Ariadne's live state — poll at the dashboard cadence.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const panels = await Promise.all(SCOUT_KINDS.map((k) => api.scoutPanel(k).catch(() => null)));
      const cost = await api.debugCosts().catch(() => null);
      const ari = await api.ariadneOverview().catch(() => null);
      if (cancelled) return;
      const next: Record<string, ScoutPanel | null> = {};
      SCOUT_KINDS.forEach((k, i) => { next[k] = panels[i]; });
      setScouts(next);
      setCosts(cost);
      setAriadne(ari);
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

  return { pulse, scouts, scoutSeries, costs, ariadne };
}
