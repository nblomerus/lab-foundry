"use client";

import { ShieldCheck, Clock3 } from "lucide-react";
import type { DissentItem } from "../lib/types";
import { Badge, Card, SectionTitle } from "./ui";

function fmtTime(s: string) {
  return new Date(s).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function DissentPanel({ items }: { items: DissentItem[] }) {
  return (
    <Card className="lg:col-span-4">
      <SectionTitle
        icon={ShieldCheck}
        title="Recent dissent"
        subtitle="Adversary verdicts + Auditor slop flags. The loop's brakes."
      />

      {items.length === 0 ? (
        <div className="rounded-3xl border border-emerald-200 bg-emerald-50 p-4">
          <div className="flex items-center justify-between gap-3">
            <Badge tone="green">No dissent yet</Badge>
            <Clock3 className="h-4 w-4 text-emerald-700" />
          </div>
          <p className="mt-3 text-sm text-slate-600">
            Auditor and Adversary are running but haven't flagged anything significant. Watch this space — Zechner's warning lives here.
          </p>
        </div>
      ) : (
        <ul className="max-h-[420px] space-y-2 overflow-y-auto pr-1">
          {items.map((d) => {
            const detail = (d.detail || "?").toUpperCase();
            const tone =
              detail === "KILL" || detail === "SLOP" ? "red"
              : detail === "WEAKEN" ? "amber"
              : "default";
            return (
              <li
                key={`${d.kind}-${d.id}`}
                className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2"
              >
                <div className="flex items-center gap-2 text-xs">
                  <Badge tone={tone}>{detail}</Badge>
                  <span className="text-slate-400">{d.kind}</span>
                  <span className="text-slate-500">on T{d.claim_id}</span>
                  {d.confidence != null && (
                    <span className="ml-auto font-mono text-slate-400">
                      {d.confidence.toFixed(2)}
                    </span>
                  )}
                  <span className="text-slate-400">{fmtTime(d.created_at)}</span>
                </div>
                {d.reasoning && (
                  <div className="mt-1 line-clamp-3 text-xs text-slate-500">
                    {d.reasoning}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
