"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Floorplan } from "../components/Floorplan";
import type { Snapshot } from "../lib/types";

export default function FloorplanPage() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const s = await api.snapshot();
        if (!cancelled) { setSnap(s); setErr(null); }
      } catch (e) {
        if (!cancelled) setErr(String(e));
      }
    };
    load();
    const id = setInterval(load, 6_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (err) {
    return (
      <div className="rounded-3xl border border-red-200 bg-red-50 p-5 text-sm">
        <div className="font-mono text-xs text-red-700">API ERROR</div>
        <div className="mt-1 text-slate-700">{err}</div>
      </div>
    );
  }

  // The floorplan renders fine with a null snapshot (it just omits live badges),
  // so we don't gate on loading — the building is drawn immediately.
  return <Floorplan snapshot={snap} />;
}
