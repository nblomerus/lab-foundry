"use client";

import { Activity, Eye, GitBranch, Sparkles, Target } from "lucide-react";
import { type Stats } from "../lib/types";
import { StatTile } from "./ui";

export function StatsGrid({
  stats, activeTheses,
}: { stats: Stats; activeTheses: number }) {
  return (
    <section className="mb-6 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
      <StatTile
        icon={Target}
        value={activeTheses}
        label="active claims"
        helper={activeTheses === 0 ? "none active" : "exploring"}
        helperTone={activeTheses === 0 ? "amber" : "green"}
      />
      <StatTile
        icon={GitBranch}
        value={stats.pending_tasks}
        label="pending tasks"
        helper={stats.running_tasks > 0 ? `${stats.running_tasks} running` : "idle"}
        helperTone={stats.running_tasks > 0 ? "blue" : "default"}
      />
      <StatTile
        icon={Eye}
        value={stats.findings_today}
        label="findings today"
        helper={stats.high_signal_today > 0 ? `${stats.high_signal_today} high signal` : "no high signal"}
        helperTone={stats.high_signal_today > 0 ? "green" : "default"}
      />
      <StatTile
        icon={Activity}
        value={stats.failed_runs_today}
        label="failed runs today"
        helper={stats.failed_runs_today > 0 ? "needs inspection" : "healthy"}
        helperTone={stats.failed_runs_today > 0 ? "amber" : "green"}
      />
    </section>
  );
}
