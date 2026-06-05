"use client";

// Composes the genuinely-live knowledge signals (Mimir intake + corpus/graph
// stats + host gauges + the 24h ingest series) into one polled object for the
// top bar, KPI row, and Ops wing. The dormant /snapshot research data is not
// fetched here — this hook is the honest-live surface.

import { useEffect, useState } from "react";
import { api, type HostStats, type KnowledgeStats, type MimirPanel } from "./api";

export interface KnowledgePulse {
  mimir: MimirPanel | null;
  stats: KnowledgeStats | null;
  host: HostStats | null;
  ingestedSeries: number[];
  loading: boolean;
}

export function useKnowledgePulse(intervalMs = 8000): KnowledgePulse {
  const [mimir, setMimir] = useState<MimirPanel | null>(null);
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [host, setHost] = useState<HostStats | null>(null);
  const [ingestedSeries, setIngestedSeries] = useState<number[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [m, s, h, ts] = await Promise.allSettled([
        api.mimirPanel(),
        api.knowledge(),
        api.hostStats(),
        api.timeseries("ingested", { bucket: "hour", points: 24 }),
      ]);
      if (cancelled) return;
      if (m.status === "fulfilled") setMimir(m.value);
      if (s.status === "fulfilled") setStats(s.value);
      if (h.status === "fulfilled") setHost(h.value);
      if (ts.status === "fulfilled") setIngestedSeries(ts.value.points.map((p) => p.value));
      setLoading(false);
    };
    load();
    const id = setInterval(load, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs]);

  return { mimir, stats, host, ingestedSeries, loading };
}
