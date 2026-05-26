"use client";

import { useEffect, useState } from "react";
import { Activity } from "lucide-react";
import { useEventStream } from "../lib/ws";
import { api } from "../lib/api";
import type { BoardroomEvent, StreamMessage } from "../lib/types";
import { Badge, Card, SectionTitle } from "./ui";

const EVENT_TONE: Record<string, "red" | "amber" | "green" | "blue" | "default"> = {
  "thesis.invalidated":          "red",
  "audit.slop_detected":         "red",
  "phase.budget_exceeded":       "amber",
  "phase.transition_proposed":   "amber",
  "thesis.created":              "green",
  "finding.high_signal":         "green",
  "thesis.confidence_changed":   "blue",
  "company.bootstrapped":        "blue",
  "task.completed":              "default",
  "task.created":                "default",
  "reflection.requested":        "default",
  "queue.empty":                 "default",
};

function fmtTime(iso: string) {
  return new Date(iso).toLocaleTimeString(undefined, { hour12: false });
}

function isEvent(m: StreamMessage): m is Extract<StreamMessage, { type: "event" }> {
  return m.type === "event";
}

export function EventStream({ keep = 60 }: { keep?: number }) {
  const { recent, connected } = useEventStream(keep);

  // Prefill from the REST events endpoint so the panel isn't empty when the
  // harness is idle. Live pushes from the WebSocket merge on top, deduped by id.
  const [prefill, setPrefill] = useState<BoardroomEvent[]>([]);
  useEffect(() => {
    api.events(keep).then(setPrefill).catch(() => {});
  }, [keep]);

  const live = recent.filter(isEvent).map((m) => m.event);
  const seen = new Set<number>();
  const merged: BoardroomEvent[] = [];
  for (const e of [...live, ...prefill]) {
    if (seen.has(e.id)) continue;
    seen.add(e.id);
    merged.push(e);
    if (merged.length >= keep) break;
  }

  return (
    <Card className="lg:col-span-4">
      <SectionTitle
        icon={Activity}
        title="Event stream"
        subtitle="Postgres NOTIFY → WebSocket. Live."
        action={
          <span className="flex items-center gap-1.5 text-xs text-slate-500">
            <span
              className={
                "inline-block h-1.5 w-1.5 rounded-full " +
                (connected ? "bg-emerald-500 pulse-dot" : "bg-red-500")
              }
            />
            {connected ? "LIVE" : "RECONNECTING"}
          </span>
        }
      />
      <ul className="max-h-[520px] space-y-1 overflow-y-auto pr-1 font-mono text-xs">
        {merged.length === 0 && (
          <li className="text-slate-400">No events yet — harness has nothing to emit.</li>
        )}
        {merged.map((e) => {
          const tone = EVENT_TONE[e.event_type] ?? "default";
          const sub =
            e.target_type && e.target_id != null
              ? `${e.target_type}#${e.target_id}`
              : (e.target_type ?? "");
          return (
            <li
              key={e.id}
              className="flex items-baseline gap-3 rounded px-1 py-0.5 hover:bg-slate-50"
            >
              <span className="text-slate-400">{fmtTime(e.emitted_at)}</span>
              <Badge tone={tone}>{e.event_type}</Badge>
              {sub && <span className="text-slate-400">{sub}</span>}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
