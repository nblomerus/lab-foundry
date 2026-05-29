"use client";

import { useEffect, useState } from "react";
import { Layers3 } from "lucide-react";
import { api } from "../lib/api";
import { ClaimsPanel } from "../components/ClaimsPanel";
import { Badge, Card, SectionTitle } from "../components/ui";
import type { Snapshot } from "../lib/types";

export default function ClaimsPage() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  useEffect(() => {
    const load = () => api.snapshot().then(setSnap).catch(() => {});
    load();
    const id = setInterval(load, 8_000);
    return () => clearInterval(id);
  }, []);

  if (!snap) return <div className="text-sm text-slate-500">Loading…</div>;

  return (
    <div className="space-y-6">
      <Card>
        <SectionTitle
          icon={Layers3}
          title={`Claims (${snap.active_claims.length} active, ${snap.killed_claims.length} archived)`}
          subtitle="Born, evolved, killed, or merged. The full history of the company's strategic candidates."
        />
      </Card>

      <ClaimsPanel claims={snap.active_claims} />

      {snap.killed_claims.length > 0 && (
        <Card>
          <SectionTitle
            icon={Layers3}
            title={`Killed / merged (${snap.killed_claims.length})`}
            subtitle="Preserved with their kill reason so the CEO doesn't re-litigate."
          />
          <div className="space-y-2">
            {snap.killed_claims.map((t) => (
              <div key={t.id} className="rounded-3xl border border-slate-200 bg-slate-50 p-4 opacity-80">
                <div className="flex items-baseline justify-between">
                  <span className="text-xs font-semibold text-slate-400">T{t.id}</span>
                  <div className="flex items-center gap-2">
                    <Badge tone={t.status === "merged" ? "blue" : "red"}>{t.status}</Badge>
                    <span className="text-xs text-slate-500">
                      {t.killed_at && new Date(t.killed_at).toLocaleDateString()}
                    </span>
                  </div>
                </div>
                <div className="mt-1 text-sm line-through decoration-slate-300">{t.claim}</div>
                {t.invalidation_reason && (
                  <div className="mt-2 text-xs text-red-700">💀 {t.invalidation_reason}</div>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
