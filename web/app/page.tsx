"use client";

// Home = the floorplan dashboard. The genuinely-live knowledge substrate is the
// front door; dormant research agents appear as tasteful "Planned" placeholders.
// The old research command center moved to /research (it wakes with the agents).

import { useEffect, useState } from "react";
import { api } from "./lib/api";
import type { Snapshot } from "./lib/types";
import { EventStreamProvider } from "./lib/event-stream";
import { useFloorData } from "./components/floorplan/useFloorData";
import { KpiRow } from "./components/KpiRow";
import { FloorplanCanvas } from "./components/floorplan/FloorplanCanvas";

function Dashboard({ snapshot }: { snapshot: Snapshot | null }) {
  // One fetch of the live knowledge data, shared by the KPI row and the canvas.
  const floorData = useFloorData();
  return (
    <div className="space-y-4">
      <KpiRow pulse={floorData.pulse} mission={snapshot?.state?.problem_statement} />
      <FloorplanCanvas snapshot={snapshot} floorData={floorData} />
    </div>
  );
}

export default function Home() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () => api.snapshot().then((s) => { if (!cancelled) setSnap(s); }).catch(() => {});
    load();
    const id = setInterval(load, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  return (
    <EventStreamProvider>
      <Dashboard snapshot={snap} />
    </EventStreamProvider>
  );
}
