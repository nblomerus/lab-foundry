"use client";

import { Target } from "lucide-react";
import type { Claim } from "../lib/types";
import { Badge, Card, SectionTitle, Progress } from "./ui";

function deltaPill(t: Claim): { label: string; tone: "green" | "red" | "default" } {
  if (t.confidence_prev == null) return { label: "—", tone: "default" };
  const d = t.confidence - t.confidence_prev;
  if (d > 0.05) return { label: `+${d.toFixed(2)}`, tone: "green" };
  if (d < -0.05) return { label: d.toFixed(2), tone: "red" };
  return { label: "flat", tone: "default" };
}

export function ClaimsPanel({ claims }: { claims: Claim[] }) {
  return (
    <Card className="lg:col-span-5">
      <SectionTitle
        icon={Target}
        title="Active claims"
        subtitle="Candidate claims grounded in evidence. Ranked by confidence."
      />
      {claims.length === 0 ? (
        <div className="rounded-3xl border border-dashed border-slate-200 bg-slate-50 p-6 text-sm text-slate-500">
          No active claims. PI needs to spawn fresh hypotheses.
        </div>
      ) : (
        <div className="space-y-3">
          {claims.map((t) => {
            const d = deltaPill(t);
            const confPct = Math.round(t.confidence * 100);
            const confTone =
              t.confidence >= 0.7 ? "pass"
              : t.confidence >= 0.4 ? "info"
              : "slop";
            return (
              <div key={t.id} className="rounded-3xl border border-slate-200 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs font-semibold text-slate-400">C{t.id}</span>
                      <Badge tone={t.confidence >= 0.7 ? "green" : "default"}>
                        {t.status}
                      </Badge>
                      <Badge tone={d.tone}>{d.label}</Badge>
                    </div>
                    <h3 className="mt-2 font-semibold leading-snug text-slate-950">{t.claim}</h3>
                  </div>
                  <span className="rounded-full bg-slate-100 px-2 py-1 text-xs font-mono text-slate-600">
                    {t.confidence.toFixed(2)}
                  </span>
                </div>
                <div className="mt-2 text-xs text-slate-500">
                  {t.finding_count} findings ·
                  <span className="text-emerald-700"> {t.supporting_count}↑</span> ·
                  <span className="text-red-600"> {t.contradicting_count}↓</span>
                </div>
                <div className="mt-3 flex items-center gap-3">
                  <Progress value={confPct} tone={confTone} />
                  <span className="w-10 text-right text-xs font-semibold text-slate-500">
                    {confPct}%
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
