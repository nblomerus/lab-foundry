"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Network, RefreshCw, AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { api, type TraceSessionSummary, type TraceSessionsResponse } from "../lib/api";
import { useEventStream } from "../lib/ws";
import { Badge, Card, SectionTitle, cx } from "../components/ui";

const STATUS_TONE: Record<string, "amber" | "green" | "red" | "default"> = {
  running: "amber", completed: "green", failed: "red",
};

function fmtMs(ms: number | null) {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}
function ago(iso: string | null) {
  if (!iso) return "";
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export default function TracePage() {
  const [data, setData] = useState<TraceSessionsResponse | null>(null);
  const [handler, setHandler] = useState<string>("");
  const [status, setStatus] = useState<string>("");
  const [mode, setMode] = useState<string>("");
  const { latest } = useEventStream(20);

  const load = useCallback(() => {
    api
      .traceSessions({
        limit: 100,
        handler_name: handler || undefined,
        status: status || undefined,
        mode: mode || undefined,
      })
      .then(setData)
      .catch(() => {});
  }, [handler, status, mode]);

  useEffect(() => {
    load();
  }, [load]);

  // Refetch when a session.* event arrives — keeps the list honest without
  // a poll. step.* events are also useful (a running row's counters tick)
  // but we don't refetch on every step to avoid hammering during a busy run.
  useEffect(() => {
    if (latest?.type !== "event") return;
    if (latest.event.event_type.startsWith("session.")) load();
  }, [latest, load]);

  const sessions = data?.sessions ?? [];

  const handlers = useMemo(() => {
    const m = data?.facets.handlers ?? {};
    return Object.keys(m).sort();
  }, [data]);

  return (
    <main className="mx-auto max-w-7xl space-y-8 px-6 py-10 xl:pl-28">
      <div className="flex items-end justify-between gap-6">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-slate-950">Trace</h1>
          <p className="mt-2 max-w-2xl text-sm text-slate-500">
            Every handler invocation lights up as a session. Open one to see
            the step-by-step DAG of model calls — live, in flight, with full
            input / output / fallback chain.
          </p>
        </div>
        <button
          onClick={load}
          className="inline-flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm hover:bg-slate-50"
        >
          <RefreshCw className="h-4 w-4" /> Refresh
        </button>
      </div>

      <Card>
        <SectionTitle
          icon={Network}
          title="Sessions"
          subtitle={`${sessions.length} shown · live updates on session.start / completed / failed`}
        />
        <div className="mb-5 flex flex-wrap items-center gap-2">
          <select
            value={handler}
            onChange={(e) => setHandler(e.target.value)}
            className="rounded-2xl border border-slate-200 bg-white px-3 py-1.5 text-sm"
          >
            <option value="">All handlers</option>
            {handlers.map((h) => (
              <option key={h} value={h}>
                {h} · {data?.facets.handlers[h] ?? 0}
              </option>
            ))}
          </select>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="rounded-2xl border border-slate-200 bg-white px-3 py-1.5 text-sm"
          >
            <option value="">Any status</option>
            <option value="running">running</option>
            <option value="completed">completed</option>
            <option value="failed">failed</option>
          </select>
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            className="rounded-2xl border border-slate-200 bg-white px-3 py-1.5 text-sm"
          >
            <option value="">Live + replay</option>
            <option value="live">live only</option>
            <option value="replay">replay only</option>
          </select>
        </div>

        <div className="overflow-hidden rounded-2xl border border-slate-200">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2">#</th>
                <th className="px-3 py-2">Handler</th>
                <th className="px-3 py-2">Trigger</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Mode</th>
                <th className="px-3 py-2">Steps</th>
                <th className="px-3 py-2">Tokens</th>
                <th className="px-3 py-2">Latency</th>
                <th className="px-3 py-2">Started</th>
              </tr>
            </thead>
            <tbody>
              {sessions.length === 0 ? (
                <tr>
                  <td colSpan={9} className="px-3 py-6 text-center text-slate-400">
                    No sessions yet. Fire a research task or wait for the next
                    handler trigger.
                  </td>
                </tr>
              ) : (
                sessions.map((s) => <SessionRow key={s.id} s={s} />)
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </main>
  );
}

function SessionRow({ s }: { s: TraceSessionSummary }) {
  const tone = STATUS_TONE[s.status] ?? "default";
  const StatusIcon =
    s.status === "running" ? Loader2 :
    s.status === "completed" ? CheckCircle2 :
    s.status === "failed" ? AlertTriangle :
    Network;

  const trigger =
    s.trigger_event_type
      ? `${s.trigger_event_type}${s.trigger_target_id ? ` · ${s.trigger_target_type}#${s.trigger_target_id}` : ""}`
      : "—";

  return (
    <tr className="border-t border-slate-100 hover:bg-slate-50">
      <td className="px-3 py-2 font-mono text-xs text-slate-500">
        <Link href={`/trace/${s.id}`} className="hover:text-slate-950 hover:underline">
          #{s.id}
        </Link>
      </td>
      <td className="px-3 py-2 font-medium text-slate-900">
        <Link href={`/trace/${s.id}`} className="hover:underline">
          {s.handler_name}
        </Link>
      </td>
      <td className="px-3 py-2 text-slate-600">{trigger}</td>
      <td className="px-3 py-2">
        <Badge tone={tone}>
          <StatusIcon className={cx("mr-1 h-3 w-3", s.status === "running" && "animate-spin")} />
          {s.status}
        </Badge>
      </td>
      <td className="px-3 py-2">
        {s.mode === "replay" ? <Badge tone="blue">replay</Badge> : <span className="text-slate-400">live</span>}
      </td>
      <td className="px-3 py-2 text-slate-700">
        {s.step_count}
        {s.failed_steps > 0 && (
          <span className="ml-1 text-red-600">({s.failed_steps} failed)</span>
        )}
      </td>
      <td className="px-3 py-2 text-slate-600">
        {s.input_tokens + s.output_tokens > 0
          ? `${s.input_tokens.toLocaleString()} → ${s.output_tokens.toLocaleString()}`
          : "—"}
      </td>
      <td className="px-3 py-2 text-slate-600">{fmtMs(s.latency_ms)}</td>
      <td className="px-3 py-2 text-slate-500">{ago(s.started_at)}</td>
    </tr>
  );
}
