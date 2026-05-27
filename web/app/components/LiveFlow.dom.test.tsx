// @vitest-environment jsdom
//
// Render-layer integration tests. The pure-geometry tests in
// flow-topology.test.ts prove the math is right; these tests prove the
// React component actually USES the math correctly to position nodes and
// draw edge paths.
//
// The bug class this is here to catch: someone sets `transform:
// translate(-50%,-50%)` in a node's inline style to center it on (node.x,
// node.y), but framer-motion's `animate` prop owns the transform property
// and silently overwrites it — every node ends up shifted by half its size
// from where the SVG paths expect. The tests below derive the rendered
// center from `left + width/2` (the layout system), not from `transform`,
// so they fail the moment the rendered position diverges from the math.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, cleanup } from "@testing-library/react";

import { LiveFlow } from "./LiveFlow";
import {
  ASPECT_X, ASPECT_Y,
  EDGES, NODES,
  handlePoint, penetrationPoint,
} from "../lib/flow-topology";
import type { Snapshot } from "../lib/types";

// --------------------------------------------------------------------------
// Mock the WebSocket hook so the component doesn't try to open a real WS
// connection in jsdom.
// --------------------------------------------------------------------------

vi.mock("../lib/ws", () => ({
  useEventStream: () => ({ latest: null, recent: [], connected: false }),
}));

// --------------------------------------------------------------------------
// A minimal snapshot that satisfies the LiveFlow consumers. Numeric counters
// are zero so no per-source perma-pulse fires, keeping the test
// deterministic.
// --------------------------------------------------------------------------

const fakeSnapshot: Snapshot = {
  state: {
    current_phase: "exploration",
    phase_started_at: "2026-05-01T00:00:00Z",
    bootstrap_at: "2026-05-01T00:00:00Z",
    deadline: "2026-06-01T00:00:00Z",
    days_in_phase: 1,
    days_remaining: 29,
    problem_statement: "test",
    stance: null,
    success_criterion: null,
    thesis: null,
    niche: null,
    audience: null,
    charter: null,
    paused: false,
    paused_reason: null,
    active_thesis_count: 0,
    killed_thesis_count: 0,
  },
  active_theses: [],
  killed_theses: [],
  recent_findings: [],
  recent_runs: [],
  dissent: [],
  phase_transitions: [],
  org_roles: [],
  cost: {
    day: "2026-05-27",
    reasoning_calls: 0, workhorse_calls: 0, fast_calls: 0, code_calls: 0,
    total_cost_usd: 0, cap_reached: false,
  },
  lesson_counts: {},
  telemetry: [],
  task_counts: [],
  stats: {
    pending_tasks: 0, running_tasks: 0,
    findings_today: 0, high_signal_today: 0, slop_today: 0,
    failed_runs_today: 0, schema_failures_today: 0,
    source_hn_in_flight: 0, source_reddit_in_flight: 0, source_web_in_flight: 0,
    last_activity_at: null,
  },
  edge_activity: [],
  langfuse_host: null,
};

beforeEach(() => {
  cleanup();
});

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

function pct(s: string): number {
  return parseFloat(s.replace("%", ""));
}

function firstMoveTo(d: string): { x: number; y: number } {
  const t = d.trim().split(/\s+/);
  return { x: parseFloat(t[1]), y: parseFloat(t[2]) };
}

function lastPoint(d: string): { x: number; y: number } {
  const t = d.trim().split(/\s+/);
  return { x: parseFloat(t[t.length - 2]), y: parseFloat(t[t.length - 1]) };
}

function close(a: number, b: number, eps = 1e-3): boolean {
  return Math.abs(a - b) <= eps;
}

// --------------------------------------------------------------------------
// Node rendering: visible center must equal (node.x%, node.y%) using
// LAYOUT properties (left/top/width/height), never transform.
// --------------------------------------------------------------------------

describe("rendered node positioning", () => {
  it("each node's visible center is at (node.x%, node.y%) per its layout box", () => {
    const { container } = render(<LiveFlow snapshot={fakeSnapshot} />);
    for (const node of NODES) {
      const el = container.querySelector(`[data-node-id="${node.id}"]`) as HTMLElement | null;
      expect(el, `node ${node.id} not rendered`).not.toBeNull();
      if (!el) continue;

      const left   = pct(el.style.left);
      const top    = pct(el.style.top);
      const width  = pct(el.style.width);
      const height = pct(el.style.height);

      // Visible center derived from the LAYOUT — does not depend on
      // transform. If someone tries to position with transform:translate
      // and framer-motion clobbers it, this assertion fails.
      const centerX = left + width / 2;
      const centerY = top + height / 2;

      expect(close(centerX, node.x, 1e-3),
        `${node.id} centerX: layout box centers at ${centerX}%, expected ${node.x}%`).toBe(true);
      expect(close(centerY, node.y, 1e-3),
        `${node.id} centerY: layout box centers at ${centerY}%, expected ${node.y}%`).toBe(true);
    }
  });

  it("no node positions itself via `transform: translate(...)` (would be eaten by framer-motion)", () => {
    const { container } = render(<LiveFlow snapshot={fakeSnapshot} />);
    for (const node of NODES) {
      const el = container.querySelector(`[data-node-id="${node.id}"]`) as HTMLElement | null;
      if (!el) continue;

      const transform = el.getAttribute("style")?.match(/transform:\s*([^;]+)/)?.[1] ?? "";
      // We tolerate framer-motion's own transforms (it sets scale/translateX
      // for animation), but we must not be relying on a transform-only
      // centering trick that framer-motion would overwrite. Inline-style
      // translate(-50%, -50%) is the trap — guard against it explicitly.
      expect(transform).not.toMatch(/translate\([-\d.]+%\s*,\s*[-\d.]+%/);
    }
  });
});

// --------------------------------------------------------------------------
// Edge rendering: each SVG path must START on its source node's bbox face
// in CSS-percent space, and END inside its target node's bbox in CSS-percent
// space. Catches mismatches between SVG coords (160×100) and CSS layout
// (container percent) — the previous class of bug that produced "arrows
// floating into the void".
// --------------------------------------------------------------------------

describe("rendered edge endpoints align with rendered node positions", () => {
  it("every edge path's first point maps to its source node's visible bbox face", () => {
    const { container } = render(<LiveFlow snapshot={fakeSnapshot} />);
    for (const e of EDGES) {
      const pathEl = container.querySelector(`[data-edge-path="${e.id}"]`) as SVGPathElement | null;
      expect(pathEl, `edge path ${e.id} not rendered`).not.toBeNull();
      if (!pathEl) continue;

      const d = pathEl.getAttribute("d") ?? "";
      const start = firstMoveTo(d);

      // Path is in SVG coords. Convert to CSS percent.
      const cssX = start.x / ASPECT_X;
      const cssY = start.y / ASPECT_Y;

      // Find the source node's rendered bbox in CSS percent.
      const fromEl = container.querySelector(`[data-node-id="${e.from}"]`) as HTMLElement | null;
      expect(fromEl, `source node ${e.from} not rendered`).not.toBeNull();
      if (!fromEl) continue;
      const left   = pct(fromEl.style.left);
      const top    = pct(fromEl.style.top);
      const width  = pct(fromEl.style.width);
      const height = pct(fromEl.style.height);

      // First point must sit on one of the four faces of the rendered bbox.
      const onLeft   = close(cssX, left);
      const onRight  = close(cssX, left + width);
      const onTop    = close(cssY, top);
      const onBottom = close(cssY, top + height);
      expect(onLeft || onRight || onTop || onBottom,
        `${e.id}: path starts at (${cssX.toFixed(2)}%, ${cssY.toFixed(2)}%) but ${e.from} bbox is left=${left}% top=${top}% w=${width}% h=${height}%`).toBe(true);
    }
  });

  it("every edge path's last point is INSIDE its target node's visible bbox", () => {
    const { container } = render(<LiveFlow snapshot={fakeSnapshot} />);
    for (const e of EDGES) {
      const pathEl = container.querySelector(`[data-edge-path="${e.id}"]`) as SVGPathElement | null;
      if (!pathEl) continue;
      const d = pathEl.getAttribute("d") ?? "";
      const tip = lastPoint(d);
      const cssX = tip.x / ASPECT_X;
      const cssY = tip.y / ASPECT_Y;

      const toEl = container.querySelector(`[data-node-id="${e.to}"]`) as HTMLElement | null;
      if (!toEl) continue;
      const left   = pct(toEl.style.left);
      const top    = pct(toEl.style.top);
      const width  = pct(toEl.style.width);
      const height = pct(toEl.style.height);

      const inside =
        cssX > left - 1e-3 && cssX < left + width  + 1e-3 &&
        cssY > top  - 1e-3 && cssY < top  + height + 1e-3;
      expect(inside,
        `${e.id}: tip at (${cssX.toFixed(2)}%, ${cssY.toFixed(2)}%) not in ${e.to} bbox left=${left}% top=${top}% w=${width}% h=${height}%`).toBe(true);
    }
  });

  it("every edge path's first point matches handlePoint() for its source", () => {
    // Sanity: the rendered path's start equals what the topology module
    // says it should be. Decouples this assertion from the bbox check above.
    const { container } = render(<LiveFlow snapshot={fakeSnapshot} />);
    for (const e of EDGES) {
      const pathEl = container.querySelector(`[data-edge-path="${e.id}"]`) as SVGPathElement | null;
      if (!pathEl) continue;
      const d = pathEl.getAttribute("d") ?? "";
      const start = firstMoveTo(d);

      const expected = handlePoint(NODES.find((n) => n.id === e.from)!, e.fromSide, e.fromOffset ?? 0);
      expect(close(start.x, expected.x, 1e-3),
        `${e.id} start.x: rendered ${start.x}, expected ${expected.x}`).toBe(true);
      expect(close(start.y, expected.y, 1e-3),
        `${e.id} start.y: rendered ${start.y}, expected ${expected.y}`).toBe(true);
    }
  });

  it("every edge path's last point matches penetrationPoint(port, toSide)", () => {
    const { container } = render(<LiveFlow snapshot={fakeSnapshot} />);
    for (const e of EDGES) {
      const pathEl = container.querySelector(`[data-edge-path="${e.id}"]`) as SVGPathElement | null;
      if (!pathEl) continue;
      const d = pathEl.getAttribute("d") ?? "";
      const tip = lastPoint(d);
      const node = NODES.find((n) => n.id === e.to)!;
      const port = handlePoint(node, e.toSide, e.toOffset ?? 0);
      const expected = penetrationPoint(port, e.toSide);
      expect(close(tip.x, expected.x, 1e-3),
        `${e.id} tip.x: rendered ${tip.x}, expected ${expected.x}`).toBe(true);
      expect(close(tip.y, expected.y, 1e-3),
        `${e.id} tip.y: rendered ${tip.y}, expected ${expected.y}`).toBe(true);
    }
  });
});
