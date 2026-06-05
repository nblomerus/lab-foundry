"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useEventStream } from "../lib/ws";
import type { Snapshot } from "../lib/types";

import { Header } from "../components/Header";
import { AskPanel } from "../components/AskPanel";
import {
  HealthCards,
  ResearchPortfolio,
  CompanyStatePanel,
  EvidenceHealth,
  AgentWorkforce,
  BudgetBurn,
  AttentionQueue,
  MiniEvidenceGraph,
} from "../components/Overview";

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

  return (
    <div className="space-y-5">
      <Header state={snap.state} />

      <HealthCards snap={snap} />

      <main className="grid grid-cols-1 gap-5 lg:grid-cols-12">
        {/* Left column — the research portfolio is the headline. */}
        <section className="lg:col-span-7">
          <ResearchPortfolio claims={snap.active_claims} />
        </section>

        {/* Right column — ask, then live org state. */}
        <section className="space-y-5 lg:col-span-5">
          <AskPanel snap={snap} />
          <CompanyStatePanel
            claims={snap.active_claims}
            dissent={snap.dissent}
            recentRuns={snap.recent_runs}
            transitions={snap.phase_transitions}
          />
          <EvidenceHealth
            stats={snap.stats}
            claims={snap.active_claims}
            findings={snap.recent_findings}
          />
        </section>

        {/* Three-up: workforce, budget, attention. */}
        <section className="lg:col-span-4">
          <AgentWorkforce roles={snap.org_roles} />
        </section>
        <section className="lg:col-span-4">
          <BudgetBurn cost={snap.cost} findingsToday={snap.stats.findings_today} />
        </section>
        <section className="lg:col-span-4">
          <AttentionQueue
            stats={snap.stats}
            dissent={snap.dissent}
            transitions={snap.phase_transitions}
          />
        </section>

        {/* Full-width evidence graph. */}
        <section className="lg:col-span-12">
          <MiniEvidenceGraph
            findings={snap.recent_findings}
            claims={snap.active_claims}
            dissent={snap.dissent}
          />
        </section>
      </main>
    </div>
  );
}
