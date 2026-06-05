"use client";

// Activity-gated edge heat: an edge's particles travel only while a matching
// live event arrived in the last 6s (generalized from the original
// useFloorplanLive, now driven by the shared event stream + EDGE_DEFS).

import { useEffect, useMemo, useRef, useState } from "react";
import { useSharedEvents } from "../../lib/event-stream";
import type { LabFoundryEvent, StreamMessage } from "../../lib/types";
import { sourceKindOf } from "./inspectors";
import { EDGE_DEFS } from "./topology";

export function useFlowHeat(): { hot: Set<string>; connected: boolean; events: LabFoundryEvent[] } {
  const { recent, connected } = useSharedEvents();
  const [hot, setHot] = useState<Set<string>>(new Set());
  const expiry = useRef<Map<string, number>>(new Map());
  const seen = useRef<Set<number>>(new Set());

  const events = useMemo(
    () =>
      recent
        .filter((m): m is Extract<StreamMessage, { type: "event" }> => m.type === "event")
        .map((m) => m.event),
    [recent],
  );

  useEffect(() => {
    const now = Date.now();
    let changed = false;
    for (const e of events) {
      if (seen.current.has(e.id)) continue;
      seen.current.add(e.id);
      const ek = sourceKindOf(e);
      for (const f of EDGE_DEFS) {
        if (!f.hotEvents.includes(e.event_type)) continue;
        if (f.sourceKind && ek !== f.sourceKind) continue;
        expiry.current.set(f.id, now + 6000);
        changed = true;
      }
    }
    if (changed) setHot(new Set(expiry.current.keys()));
  }, [events]);

  useEffect(() => {
    const t = setInterval(() => {
      const now = Date.now();
      let changed = false;
      for (const [id, exp] of expiry.current) if (exp <= now) { expiry.current.delete(id); changed = true; }
      if (changed) setHot(new Set(expiry.current.keys()));
    }, 500);
    return () => clearInterval(t);
  }, []);

  return { hot, connected, events };
}
