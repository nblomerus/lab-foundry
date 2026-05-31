"use client";

import { TrendingUp } from "lucide-react";
import type { Cost } from "../lib/types";
import { Badge, Card, SectionTitle, Progress } from "./ui";

export function SkillPanel({
  lessons, cost,
}: { lessons: Record<string, number>; cost: Cost }) {
  const active        = lessons.active        ?? 0;
  const probationary  = lessons.probationary  ?? 0;
  const retired       = lessons.retired       ?? 0;
  const total         = active + probationary;
  const validatedPct  = total > 0 ? Math.round((active / total) * 100) : 0;

  return (
    <Card className="lg:col-span-6">
      <SectionTitle
        icon={TrendingUp}
        title="Skill memory + compute"
        subtitle="Lessons are accumulated from dissent and validated by outcomes."
      />

      <div className="grid gap-3 md:grid-cols-3">
        {[
          { label: "Active",       value: active,       tone: "green" as const },
          { label: "Probationary", value: probationary, tone: "amber" as const },
          { label: "Retired",      value: retired,      tone: "default" as const },
        ].map((s) => (
          <div key={s.label} className="rounded-3xl border border-slate-200 p-4">
            <div className="text-sm font-semibold text-slate-950">{s.label}</div>
            <div className="mt-4 flex items-end justify-between">
              <span className="text-3xl font-semibold tracking-tight">{s.value}</span>
              <Badge tone={s.tone}>{s.label.toLowerCase()}</Badge>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-5 rounded-3xl bg-slate-50 p-4">
        <div className="flex flex-col justify-between gap-3 md:flex-row md:items-center">
          <div className="flex-1">
            <div className="font-semibold text-slate-950">Lessons validated</div>
            <p className="mt-1 text-sm text-slate-500">
              {total === 0
                ? "No lessons yet — first dissent runs haven't produced learnings."
                : `${active} of ${total} candidate lessons survived ≥5 supportive runs.`}
            </p>
            <div className="mt-3"><Progress value={validatedPct} tone="pass" /></div>
          </div>
          <div className="flex w-full max-w-xs flex-col gap-2 md:w-auto">
            {[
              { l: "R-tier", v: cost.reasoning_calls },
              { l: "W-tier", v: cost.workhorse_calls },
              { l: "F-tier", v: cost.fast_calls      },
              { l: "C-tier", v: cost.code_calls      },
            ].map((t) => (
              <div key={t.l} className="flex items-center justify-between rounded-2xl bg-white px-3 py-1.5">
                <span className="text-xs text-slate-500">{t.l}</span>
                <span className="font-mono text-sm">{t.v}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </Card>
  );
}
