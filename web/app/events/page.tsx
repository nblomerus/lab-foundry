"use client";

import { useEffect, useState } from "react";
import { Activity, TerminalSquare } from "lucide-react";
import { api } from "../lib/api";
import { useEventStream } from "../lib/ws";
import type { LabFoundryEvent, StreamMessage } from "../lib/types";
import { Badge, Card, SectionTitle } from "../components/ui";

const STATUS_TONE: Record<string, "green" | "amber" | "default" | "red"> = {
  consumed:   "green",
  pending:    "amber",
  suppressed: "default",
  failed:     "red",
};

function isEvent(m: StreamMessage): m is Extract<StreamMessage, { type: "event" }> {
  return m.type === "event";
}

export default function EventsPage() {
  const [events, setEvents] = useState<LabFoundryEvent[]>([]);
  const { recent, connected } = useEventStream(200);

  useEffect(() => {
    api.events(200).then(setEvents).catch(() => {});
  }, []);

  useEffect(() => {
    const live = recent.filter(isEvent).map((m) => m.event);
    if (live.length === 0) return;
    setEvents((prev) => {
      const seen = new Set(prev.map((e) => e.id));
      const fresh = live.filter((e) => !seen.has(e.id));
      return [...fresh, ...prev].slice(0, 400);
    });
  }, [recent]);

  return (
    <div className="space-y-6">
      <Card>
        <SectionTitle
          icon={TerminalSquare}
          title={`Events (${events.length})`}
          subtitle="Every state change flows through here. Postgres NOTIFY → harness → WebSocket."
          action={
            <span className="flex items-center gap-1.5 text-xs text-slate-500">
              <span
                className={
                  "h-1.5 w-1.5 rounded-full " +
                  (connected ? "bg-emerald-500 pulse-dot" : "bg-red-500")
                }
              />
              {connected ? "LIVE" : "RECONNECTING"}
            </span>
          }
        />
      </Card>

      <Card>
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-3 py-2">Time</th>
                <th className="px-3 py-2">Type</th>
                <th className="px-3 py-2">Target</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Handler</th>
                <th className="px-3 py-2">Note</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id} className="border-t border-slate-100 hover:bg-slate-50">
                  <td className="px-3 py-1.5 font-mono text-xs text-slate-500">
                    {new Date(e.emitted_at).toLocaleTimeString(undefined, { hour12: false })}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-xs">{e.event_type}</td>
                  <td className="px-3 py-1.5 text-xs text-slate-500">
                    {e.target_type ? `${e.target_type}#${e.target_id}` : "—"}
                  </td>
                  <td className="px-3 py-1.5">
                    <Badge tone={STATUS_TONE[e.status] ?? "default"}>{e.status}</Badge>
                  </td>
                  <td className="px-3 py-1.5 text-xs text-slate-500">
                    {e.consumed_by_handler || "—"}
                  </td>
                  <td className="px-3 py-1.5 text-xs text-slate-500">
                    {e.suppression_reason || ""}
                  </td>
                </tr>
              ))}
              {events.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-3 py-6 text-center text-xs text-slate-400">
                    <Activity className="mx-auto mb-2 h-4 w-4 text-slate-300" />
                    No events yet.
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
