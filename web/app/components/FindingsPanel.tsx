"use client";

import { Eye } from "lucide-react";
import type { Finding } from "../lib/types";
import { Badge, Card, SectionTitle } from "./ui";

function ageString(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.round(ms / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

export function FindingsPanel({ findings }: { findings: Finding[] }) {
  const top = [...findings]
    .filter((f) => f.audit_verdict !== "stale")
    .sort((a, b) => b.relevance_score - a.relevance_score)
    .slice(0, 6);

  return (
    <Card className="lg:col-span-3">
      <SectionTitle icon={Eye} title="High-signal findings" subtitle="Ranked by relevance × audit." />
      {top.length === 0 ? (
        <div className="rounded-3xl border border-dashed border-slate-200 bg-slate-50 p-6 text-sm text-slate-500">
          No findings yet. Researchers in flight.
        </div>
      ) : (
        <div className="space-y-3">
          {top.map((f) => {
            const tone =
              f.audit_verdict === "pass" ? "green"
              : f.audit_verdict === "slop" ? "red"
              : f.audit_verdict === "unclear" ? "amber"
              : "default";
            return (
              <div key={f.id} className="rounded-3xl border border-slate-200 p-4">
                <div className="flex items-center justify-between gap-3">
                  <Badge tone={tone}>Score {f.relevance_score.toFixed(1)}/10</Badge>
                  <span className="text-xs text-slate-400">{ageString(f.created_at)}</span>
                </div>
                <h3 className="mt-3 text-sm font-semibold leading-snug text-slate-950 line-clamp-2">
                  {f.title || "(untitled)"}
                </h3>
                {f.why_it_matters && (
                  <p className="mt-2 text-sm leading-relaxed text-slate-500 line-clamp-3">
                    {f.why_it_matters}
                  </p>
                )}
                <div className="mt-3 flex items-center justify-between text-xs text-slate-400">
                  <span>{f.source || "?"} · T{f.claim_id ?? "—"}</span>
                  {f.url && (
                    <a
                      href={f.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-semibold text-slate-700 hover:text-slate-950"
                    >
                      Open
                    </a>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
