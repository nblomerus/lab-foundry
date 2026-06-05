import { describe, it, expect } from "vitest";
import { humanizeEvent } from "./activity";
import type { LabFoundryEvent } from "./types";

function ev(event_type: string, payload: Record<string, unknown> = {}, id = 1): LabFoundryEvent {
  return { id, event_type, payload, emitted_at: new Date().toISOString(), target_type: null, target_id: null, status: "emitted" };
}

describe("humanizeEvent", () => {
  it("humanizes a scout discovery with the scout label + title", () => {
    const l = humanizeEvent(ev("source.discovered", { source: { source_kind: "arxiv", title: "Attention Is All You Need" } }));
    expect(l).not.toBeNull();
    expect(l!.text).toContain("arXiv");
    expect(l!.text).toContain("Attention");
    expect(l!.tone).toBe("info");
  });

  it("humanizes a certification", () => {
    const l = humanizeEvent(ev("document.ingested", { title: "A Paper" }));
    expect(l!.text.toLowerCase()).toContain("certified");
    expect(l!.tone).toBe("live");
  });

  it("humanizes a quarantine with its reason", () => {
    const l = humanizeEvent(ev("mimir.ingest_blocked", { reasons: "too thin" }));
    expect(l!.text.toLowerCase()).toContain("quarantined");
    expect(l!.tone).toBe("warn");
  });

  it("drops noise events (no feed line)", () => {
    expect(humanizeEvent(ev("document.parsed"))).toBeNull();
    expect(humanizeEvent(ev("session.started"))).toBeNull();
  });
});
