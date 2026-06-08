"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Route, RefreshCw, Search } from "lucide-react";
import { api, type TraceJourneysResponse } from "../../lib/api";
import { Badge, Card, SectionTitle, cx } from "../../components/ui";

type Tone = "amber" | "green" | "red" | "blue" | "default";
const OUTCOME_TONE: Record<string, Tone> = {
  ingested: "green",
  rejected: "red",
  blocked: "amber",
  in_library: "blue",
  pending: "default",
};
const OUTCOMES = ["ingested", "rejected", "blocked", "in_library", "pending"];

function fmt(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export default function JourneysPage() {
  const [data, setData] = useState<TraceJourneysResponse | null>(null);
  const [outcome, setOutcome] = useState<string>("");
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    api
      .traceJourneys({ limit: 200, outcome: outcome || undefined, q: q || undefined })
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [outcome, q]);

  useEffect(() => {
    const t = setTimeout(load, q ? 250 : 0);
    return () => clearTimeout(t);
  }, [load, q]);

  const journeys = data?.journeys ?? [];
  const facets = data?.facets ?? {};

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-6">
        <div>
          <Link href="/trace" className="mb-2 inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-950">
            <ArrowLeft className="h-3 w-3" /> All sessions
          </Link>
          <h1 className="text-3xl font-semibold tracking-tight text-slate-950">Journeys</h1>
          <p className="mt-2 max-w-2xl text-sm text-slate-500">
            Every source that entered the lab, newest first — from the scout that found it to
            its terminal outcome (ingested, rejected, or blocked). Open one to read the full
            event chain with inputs and outputs. No ids needed.
          </p>
        </div>
        <button
          onClick={load}
          className="inline-flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm hover:bg-slate-50"
        >
          <RefreshCw className={cx("h-4 w-4", loading && "animate-spin")} /> Refresh
        </button>
      </div>

      <Card>
        <SectionTitle icon={Route} title="Recent interactions" subtitle={`${journeys.length} shown`} />

        <div className="mb-5 flex flex-wrap items-center gap-2">
          <button
            onClick={() => setOutcome("")}
            className={cx(
              "rounded-2xl border px-3 py-1.5 text-sm",
              outcome === "" ? "border-slate-900 bg-slate-900 text-white" : "border-slate-200 bg-white text-slate-700",
            )}
          >
            all
          </button>
          {OUTCOMES.map((o) => (
            <button
              key={o}
              onClick={() => setOutcome(outcome === o ? "" : o)}
              className={cx(
                "inline-flex items-center gap-1.5 rounded-2xl border px-3 py-1.5 text-sm",
                outcome === o ? "border-slate-900 bg-slate-900 text-white" : "border-slate-200 bg-white text-slate-700",
              )}
            >
              {o} <span className="text-xs opacity-60">{facets[o] ?? 0}</span>
            </button>
          ))}
          <div className="relative ml-auto">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="search title or key"
              className="w-64 rounded-2xl border border-slate-200 bg-white py-1.5 pl-9 pr-3 text-sm"
            />
          </div>
        </div>

        <div className="overflow-hidden rounded-2xl border border-slate-200">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2">Outcome</th>
                <th className="px-3 py-2">Source</th>
                <th className="px-3 py-2">Title</th>
                <th className="px-3 py-2">Started</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {journeys.map((j) => (
                <tr key={j.canonical_key} className="hover:bg-slate-50">
                  <td className="px-3 py-2">
                    <Badge tone={OUTCOME_TONE[j.outcome] ?? "default"}>{j.outcome}</Badge>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-500">{j.source_kind ?? "?"}</td>
                  <td className="px-3 py-2">
                    <Link
                      href={`/trace/journey/${j.doc_id != null ? j.doc_id : j.canonical_key.split("/").map(encodeURIComponent).join("/")}`}
                      className="font-medium text-slate-900 hover:text-blue-700 hover:underline"
                    >
                      {j.title}
                    </Link>
                    <div className="text-xs text-slate-400">{j.outcome_reason}</div>
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-slate-500">{fmt(j.started_at)}</td>
                  <td className="px-3 py-2 text-right">
                    <Link
                      href={`/trace/journey/${j.doc_id != null ? j.doc_id : j.canonical_key.split("/").map(encodeURIComponent).join("/")}`}
                      className="text-xs font-medium text-blue-700 hover:underline"
                    >
                      trace →
                    </Link>
                  </td>
                </tr>
              ))}
              {!loading && journeys.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-8 text-center text-sm text-slate-400">
                    No journeys match.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
