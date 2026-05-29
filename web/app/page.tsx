"use client";

import { useEffect, useState } from "react";
import { api } from "./lib/api";
import { useEventStream } from "./lib/ws";
import type { Snapshot } from "./lib/types";

import { Header } from "./components/Header";
import { StatsGrid } from "./components/StatsGrid";
import { WorkflowLoop } from "./components/WorkflowLoop";
import { DissentPanel } from "./components/DissentPanel";
import { ClaimsPanel } from "./components/ClaimsPanel";
import { TaskQueuePanel } from "./components/TaskQueuePanel";
import { FindingsPanel } from "./components/FindingsPanel";
import { TelemetryPanel } from "./components/TelemetryPanel";
import { SkillPanel } from "./components/SkillPanel";
import { EventStream } from "./components/EventStream";

export default function CommandCenter() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { latest } = useEventStream(1);

  useEffect(() => {
    let cancelled = false;
    const fetchSnap = async () => {
      try {
        const s = await api.snapshot();
        if (!cancelled) { setSnap(s); setErr(null); }
      } catch (e) {
        if (!cancelled) setErr(String(e));
      }
    };
    fetchSnap();
    const id = setInterval(fetchSnap, 8_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    if (!latest || latest.type !== "event") return;
    const t = latest.event.event_type;
    if (
      t === "thesis.invalidated" ||
      t === "phase.transition_proposed" ||
      t === "company.bootstrapped" ||
      t === "thesis.created"
    ) {
      api.snapshot().then(setSnap).catch(() => {});
    }
  }, [latest]);

  if (err) {
    return (
      <div className="rounded-3xl border border-red-200 bg-red-50 p-5 text-sm">
        <div className="font-mono text-xs text-red-700">API ERROR</div>
        <div className="mt-1 text-slate-700">{err}</div>
        <div className="mt-2 text-xs text-slate-500">
          Is the API running? <code className="rounded bg-white px-1 py-0.5">make api</code>
        </div>
      </div>
    );
  }

  if (!snap) {
    return <div className="text-sm text-slate-500">Loading snapshot…</div>;
  }

  const latestEventType =
    latest && latest.type === "event" ? latest.event.event_type : undefined;
  const totalRuns =
    snap.telemetry.reduce((sum, t) => sum + t.runs, 0);
  const savedFindings = snap.recent_findings.length;

  return (
    <div className="space-y-6">
      <Header state={snap.state} />

      <StatsGrid stats={snap.stats} activeClaims={snap.state.active_claim_count} />

      <section className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <WorkflowLoop
          roles={snap.org_roles}
          latestEventType={latestEventType}
          lastActivityAt={snap.stats.last_activity_at}
        />
        <DissentPanel items={snap.dissent} />

        <ClaimsPanel claims={snap.active_claims} />
        <TaskQueuePanel
          taskCounts={snap.task_counts}
          recentRuns={snap.recent_runs}
          langfuseHost={snap.langfuse_host}
        />
        <FindingsPanel findings={snap.recent_findings} />

        <TelemetryPanel
          telemetry={snap.telemetry}
          cost={snap.cost}
          totalRuns={totalRuns}
          savedFindings={savedFindings}
        />
        <SkillPanel lessons={snap.lesson_counts} cost={snap.cost} />

        <EventStream keep={60} />
      </section>
    </div>
  );
}
