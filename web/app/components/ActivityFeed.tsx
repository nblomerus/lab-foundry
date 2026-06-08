"use client";

import { useMemo } from "react";
import { useSharedEvents } from "../lib/event-stream";
import { humanizeEvent, type ActivityLine } from "../lib/activity";
import { ago } from "../lib/format";
import { cx, type Accent } from "./ui";
import type { StreamMessage } from "../lib/types";

const TONE_TEXT: Record<Accent, string> = {
  live: "text-emerald-600", info: "text-blue-600", warn: "text-amber-600",
  danger: "text-red-500", idle: "text-slate-400", slate: "text-slate-500",
};

// Humanized live feed over the shared WS stream — zero backend.
export function ActivityFeed({ limit = 16, className = "", hideHeader = false }: { limit?: number; className?: string; hideHeader?: boolean }) {
  const { recent, connected } = useSharedEvents();
  const lines = useMemo<ActivityLine[]>(
    () =>
      recent
        .filter((m): m is Extract<StreamMessage, { type: "event" }> => m.type === "event")
        .map((m) => humanizeEvent(m.event))
        .filter((l): l is ActivityLine => l != null)
        .slice(0, limit),
    [recent, limit],
  );

  return (
    <div className={cx("flex flex-col", className)}>
      {!hideHeader && (
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Live Activity</span>
          <span className="flex items-center gap-1.5 text-[10px] font-medium text-slate-400">
            <span className={cx("inline-block h-1.5 w-1.5 rounded-full", connected ? "bg-emerald-500 pulse-dot" : "bg-slate-300")} />
            {connected ? "Live" : "Offline"}
          </span>
        </div>
      )}
      <ul className="flex-1 space-y-1 overflow-y-auto pr-1">
        {lines.length === 0 ? (
          <li className="px-1 py-2 text-xs text-slate-400">Waiting for events…</li>
        ) : (
          lines.map((l) => {
            const Icon = l.icon;
            return (
              <li key={l.id} className="rise-in flex items-start gap-2 rounded-xl px-1.5 py-1.5 hover:bg-slate-50">
                <Icon className={cx("mt-0.5 h-3.5 w-3.5 shrink-0", TONE_TEXT[l.tone])} />
                <span className="min-w-0 flex-1 text-xs leading-snug text-slate-600">{l.text}</span>
                <span className="shrink-0 text-[10px] tabular-nums text-slate-400">{ago(l.at)}</span>
              </li>
            );
          })
        )}
      </ul>
    </div>
  );
}
