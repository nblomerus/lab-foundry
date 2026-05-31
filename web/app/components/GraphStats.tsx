"use client";

import { useEffect, useState } from "react";
import { Database, AlertTriangle, RefreshCw } from "lucide-react";
import { api, type GraphStats } from "../lib/api";
import { Card, SectionTitle } from "./ui";

export default function GraphStatsPanel() {
  const [stats, setStats] = useState<GraphStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.graphStats();
      setStats(data);
      setLastRefresh(new Date());
    } catch (e) {
      setStats({ status: "unavailable", error: "Failed to fetch graph stats" });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, []);

  if (stats?.status === "unavailable") {
    return (
      <Card>
        <div className="flex items-start justify-between">
          <SectionTitle
            icon={Database}
            title="Evidence Graph"
            subtitle="Neo4j integration unavailable"
          />
        </div>
        <div className="flex items-center gap-2 rounded-lg bg-amber-50 p-3 text-amber-700">
          <AlertTriangle className="h-4 w-4 flex-shrink-0" />
          <div className="text-sm">{stats.error || "Neo4j is not running or unavailable"}</div>
        </div>
      </Card>
    );
  }

  const nodes = stats?.nodes;
  const edges = stats?.edges;

  return (
    <Card>
      <div className="flex items-start justify-between">
        <SectionTitle
          icon={Database}
          title="Evidence Graph"
          subtitle="Neo4j knowledge graph: claims, findings, verdicts"
        />
        <button
          onClick={load}
          disabled={loading}
          className="inline-flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {nodes && edges ? (
        <div className="mt-4 grid grid-cols-2 gap-4 md:grid-cols-5">
          <div className="rounded-lg border border-slate-200 p-4">
            <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Claims</div>
            <div className="mt-1 text-2xl font-bold text-slate-900">{nodes.claims.toLocaleString()}</div>
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Findings</div>
            <div className="mt-1 text-2xl font-bold text-slate-900">{nodes.findings.toLocaleString()}</div>
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Verdicts</div>
            <div className="mt-1 text-2xl font-bold text-slate-900">{nodes.verdicts.toLocaleString()}</div>
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Grounds (→)</div>
            <div className="mt-1 text-2xl font-bold text-slate-900">{edges.grounds.toLocaleString()}</div>
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Challenged (→)</div>
            <div className="mt-1 text-2xl font-bold text-slate-900">{edges.challenged.toLocaleString()}</div>
          </div>
        </div>
      ) : (
        <div className="mt-4 h-24 animate-pulse rounded-lg bg-slate-100" />
      )}

      {lastRefresh && (
        <div className="mt-3 text-xs text-slate-400">
          Last updated: {lastRefresh.toLocaleTimeString()}
        </div>
      )}
    </Card>
  );
}
