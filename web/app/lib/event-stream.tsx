"use client";

// One shared WebSocket for the whole dashboard. TopBar (health), ActivityFeed,
// and the floorplan all read the same stream instead of each opening their own
// connection. Wrap the dashboard tree in <EventStreamProvider>.

import { createContext, useContext, type ReactNode } from "react";
import { useEventStream } from "./ws";

type Stream = ReturnType<typeof useEventStream>;

const Ctx = createContext<Stream | null>(null);

export function EventStreamProvider({ keep = 80, children }: { keep?: number; children: ReactNode }) {
  const stream = useEventStream(keep);
  return <Ctx.Provider value={stream}>{children}</Ctx.Provider>;
}

export function useSharedEvents(): Stream {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSharedEvents must be used within <EventStreamProvider>");
  return v;
}
