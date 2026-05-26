"use client";

import { useEffect, useState } from "react";
import { Layers3 } from "lucide-react";
import { api } from "../lib/api";
import { WorkflowLoop } from "../components/WorkflowLoop";
import { EventStream } from "../components/EventStream";
import { Card, SectionTitle } from "../components/ui";
import type { Snapshot } from "../lib/types";

export default function OrgPage() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  useEffect(() => {
    const load = () => api.snapshot().then(setSnap).catch(() => {});
    load();
    const id = setInterval(load, 5_000);
    return () => clearInterval(id);
  }, []);

  if (!snap) return <div className="text-sm text-slate-500">Loading…</div>;

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
      <WorkflowLoop roles={snap.org_roles} />
      <EventStream keep={80} />

      <Card className="lg:col-span-12">
        <SectionTitle
          icon={Layers3}
          title="Recent agent runs"
          subtitle="Every invocation, with its model tier and latency."
        />
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-3 py-2">Started</th>
                <th className="px-3 py-2">Invocation</th>
                <th className="px-3 py-2">Model</th>
                <th className="px-3 py-2">Tier</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Tokens</th>
              </tr>
            </thead>
            <tbody>
              {snap.recent_runs.map((r) => (
                <tr key={r.id} className="border-t border-slate-100">
                  <td className="px-3 py-1.5 font-mono text-xs text-slate-500">
                    {new Date(r.started_at).toLocaleTimeString(undefined, { hour12: false })}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-xs">{r.invocation_type}</td>
                  <td className="px-3 py-1.5 text-xs text-slate-500">{r.model_name}</td>
                  <td className="px-3 py-1.5 font-mono text-xs">{r.model_tier}</td>
                  <td className="px-3 py-1.5 font-mono text-xs">
                    {r.status === "completed" ? "✓"
                      : r.status === "running" ? "⏳"
                      : r.status === "failed" ? "✗"
                      : r.status}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-xs text-slate-500">
                    {r.input_token_count ?? "—"} / {r.output_token_count ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
