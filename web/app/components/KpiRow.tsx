"use client";

import Link from "next/link";
import { Inbox, ShieldCheck } from "lucide-react";
import { DeltaBadge, KpiCard } from "./ui";
import { compact } from "../lib/format";
import type { KnowledgePulse } from "../lib/pulse";

// The mockup's 6-card header. Live: Current Mission, New Knowledge 24h,
// Certified Documents. Dormant (muted "Planned" tiles, no fabricated numbers):
// Active Directions / Requests / Experiments — backed by the off research loop.
export function KpiRow({ pulse, mission }: { pulse: KnowledgePulse; mission?: string | null }) {
  const g = pulse.mimir?.at_a_glance;
  const ingestedToday = g?.ingested_today ?? 0;
  const ingestedYday = g?.ingested_yesterday ?? 0;
  const deltaPct = ingestedYday > 0 ? Math.round(((ingestedToday - ingestedYday) / ingestedYday) * 100) : null;
  const certified = g?.certified ?? null;

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-7">
      {/* Current Mission — wide, real company_state.problem_statement */}
      <div className="col-span-2 rounded-card border border-slate-200 bg-white/85 p-4 shadow-card backdrop-blur">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-emerald-600">Current Mission</div>
        <p className="mt-1.5 line-clamp-3 text-sm font-medium leading-snug text-slate-800">
          {mission ?? "Loading the lab's mission…"}
        </p>
        <Link href="/org" className="mt-2 inline-block text-[11px] font-medium text-emerald-700 hover:underline">
          View mission →
        </Link>
      </div>

      <KpiCard
        label="New Knowledge 24h"
        icon={Inbox}
        accent="live"
        value={compact(ingestedToday)}
        delta={deltaPct != null ? <DeltaBadge delta={deltaPct} suffix="%" /> : undefined}
        sparkline={pulse.ingestedSeries}
        sparkTone="live"
        footer={`vs ${compact(ingestedYday)} yesterday`}
      />
      <KpiCard
        label="Certified Documents"
        icon={ShieldCheck}
        accent="live"
        value={compact(certified)}
        footer="in the Library"
      />
      <KpiCard label="Active Directions" planned footer="research workflow" />
      <KpiCard label="Active Requests" planned footer="research workflow" />
      <KpiCard label="Running Experiments" planned footer="research workflow" />
    </div>
  );
}
