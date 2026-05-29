"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { LiveFlow, type PowerSummary } from "../components/LiveFlow";
import type { Snapshot } from "../lib/types";

export default function FlowPage() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [power, setPower] = useState<PowerSummary | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const s = await api.snapshot();
        if (!cancelled) {
          setSnap(s);
          setErr(null);
        }
      } catch (e) {
        if (!cancelled) setErr(String(e));
      }
      // Hardware/budget telemetry for the Quartermaster. Non-fatal: if the
      // debug endpoint is down, the node falls back to budget-only.
      try {
        const costs = await api.debugCosts();
        if (!cancelled && costs?.power) {
          setPower({
            total_watts: costs.power.total_watts,
            gpu_count: costs.power.gpus.length,
            projected_usd_per_day: costs.power.projected_usd_per_day,
          });
        }
      } catch {
        /* leave power null — Quartermaster shows budget only */
      }
    };
    load();
    const id = setInterval(load, 6_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  if (err) {
    return (
      <div className="rounded-3xl border border-red-200 bg-red-50 p-5 text-sm">
        <div className="font-mono text-xs text-red-700">API ERROR</div>
        <div className="mt-1 text-slate-700">{err}</div>
      </div>
    );
  }

  if (!snap) {
    return <div className="text-sm text-slate-500">Loading snapshot…</div>;
  }

  return <LiveFlow snapshot={snap} power={power} />;
}
