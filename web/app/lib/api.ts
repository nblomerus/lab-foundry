import type { Snapshot, BoardroomEvent, Finding } from "./types";

const API_BASE = "/api";

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} on ${path}`);
  return (await r.json()) as T;
}

export const api = {
  snapshot:  () => jget<Snapshot>("/snapshot"),
  events:    (limit = 100) => jget<BoardroomEvent[]>(`/events?limit=${limit}`),
  findings:  (thesisId: number) => jget<Finding[]>(`/theses/${thesisId}/findings`),
};
