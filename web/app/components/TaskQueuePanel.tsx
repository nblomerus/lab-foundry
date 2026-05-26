"use client";

import { TerminalSquare } from "lucide-react";
import {
  Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { AgentRun, TaskCount } from "../lib/types";
import { Badge, Card, SectionTitle } from "./ui";

const STATUS_TONE: Record<string, "default" | "blue" | "green" | "amber" | "red"> = {
  running:    "blue",
  pending:    "default",
  completed:  "green",
  failed:     "red",
  halted:     "amber",
};

export function TaskQueuePanel({
  taskCounts, recentRuns,
}: { taskCounts: TaskCount[]; recentRuns: AgentRun[] }) {
  const chartData = taskCounts.map((t) => ({
    label: t.label.charAt(0).toUpperCase() + t.label.slice(1),
    value: t.value,
  }));

  return (
    <Card className="lg:col-span-4">
      <SectionTitle icon={TerminalSquare} title="Task queue" subtitle="Operational substrate." />
      <div className="mb-4 h-40">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={chartData} margin={{ top: 8, right: 0, bottom: 0, left: -24 }}>
            <XAxis dataKey="label" tickLine={false} axisLine={false} fontSize={11} />
            <YAxis tickLine={false} axisLine={false} fontSize={11} />
            <Tooltip cursor={{ fill: "rgba(15,23,42,0.05)" }} />
            <Bar dataKey="value" radius={[10, 10, 0, 0]} fill="#0f172a" />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <ul className="space-y-2">
        {recentRuns.slice(0, 6).map((r) => (
          <li
            key={r.id}
            className="flex items-center justify-between gap-3 rounded-2xl bg-slate-50 p-3"
          >
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-xs">
                <span className="font-mono text-slate-400">R{r.id}</span>
                <span className="text-slate-400">{r.model_tier}</span>
              </div>
              <div className="truncate text-sm font-medium text-slate-800">{r.invocation_type}</div>
            </div>
            <Badge tone={STATUS_TONE[r.status] ?? "default"}>{r.status}</Badge>
          </li>
        ))}
        {recentRuns.length === 0 && (
          <li className="text-xs text-slate-400">No runs yet.</li>
        )}
      </ul>
    </Card>
  );
}
