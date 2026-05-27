/**
 * Topology of the live agent graph: nodes, edges, and the geometry helpers
 * that turn handle declarations into SVG paths.
 *
 * This file is pure data + pure functions. No React, no DOM, no side effects.
 * Both the renderer and the test harness import from here.
 */

import type { LucideIcon } from "lucide-react";
import {
  Activity, BrainCircuit, Eye, GitBranch, Layers3, Network,
  Search, ShieldCheck, Target, Telescope, TerminalSquare,
} from "lucide-react";

// =========================================================================
// Geometry constants
//
// Node positions are stored in "container percent" space: x and y both run
// 0..100 over the container's width/height. This is what the node CSS
// `left/top` props consume directly.
//
// The SVG that draws edges uses a viewBox whose ASPECT matches the container
// (16:10). In SVG coordinates the canvas is SVG_W × SVG_H wide. To convert
// a node's x from container-% to SVG units, multiply by ASPECT_X.
//
// Why bother: with a square viewBox + preserveAspectRatio="none" (the old
// setup), 1 x-unit and 1 y-unit rendered at different pixel sizes — bezier
// control points and arrowheads got squashed anisotropically. With a matched
// viewBox aspect, x and y units are visually uniform; perpendicular lines
// render perpendicular, and arrows are not stretched ellipses.
// =========================================================================

export const SVG_W = 160;
export const SVG_H = 100;
export const ASPECT_X = SVG_W / 100;   // 1.6: container-% → SVG x units
export const ASPECT_Y = SVG_H / 100;   // 1.0: container-% → SVG y units

// Node dimensions in container percent (used by node CSS width/height).
export const NODE_W = 16;
export const NODE_H = 11;

/**
 * How far past the destination port the arrow extends INTO the node card,
 * in SVG units. The node card has z-20 above the SVG, so the arrow tip is
 * hidden behind the card border — visually it looks like the arrow enters
 * the node, like n8n.
 */
export const ARROW_PENETRATION = 3.0;

/**
 * How far an outside-U route loops past the source/target column, in SVG
 * units. Must be big enough to clear any node sitting between the endpoints
 * in adjacent columns (e.g. phase → ceo has to loop around Theses).
 */
export const OUTSIDE_STUB = 22 * (SVG_W / 100);

export type Side = "left" | "right" | "top" | "bottom";
export type RouteMode = "auto" | "outside-left" | "outside-right";

export interface NodeDef {
  id: string;
  label: string;
  type: "Source" | "Queue" | "Agent" | "Store" | "Critic" | "Phase";
  icon: LucideIcon;
  x: number;
  y: number;
}

export interface EdgeDef {
  id: string;
  from: string;
  fromSide: Side;
  fromOffset?: number;
  to: string;
  toSide: Side;
  toOffset?: number;
  label: string;
  event_type: string;
  route?: RouteMode;
}

// =========================================================================
// Topology
// =========================================================================

export const NODES: NodeDef[] = [
  { id: "hn",          label: "Hacker News",  type: "Source", icon: Search,         x: 9,  y: 16 },
  { id: "reddit",      label: "Reddit",       type: "Source", icon: Network,        x: 9,  y: 38 },
  { id: "web",         label: "Web (DDG)",    type: "Source", icon: Network,        x: 9,  y: 60 },

  { id: "researcher",  label: "Researcher",   type: "Agent",  icon: Search,         x: 29, y: 38 },
  { id: "tasks",       label: "Tasks",        type: "Queue",  icon: TerminalSquare, x: 29, y: 84 },

  { id: "findings",    label: "Findings",     type: "Store",  icon: Eye,            x: 49, y: 38 },
  { id: "auditor",     label: "Auditor",      type: "Critic", icon: ShieldCheck,    x: 49, y: 64 },
  { id: "planner",     label: "Planner",      type: "Agent",  icon: GitBranch,      x: 49, y: 92 },

  { id: "adversary",   label: "Adversary",    type: "Critic", icon: Telescope,      x: 70, y: 16 },
  { id: "theses",      label: "Theses",       type: "Store",  icon: Target,         x: 70, y: 52 },

  { id: "ceo",         label: "CEO",          type: "Agent",  icon: BrainCircuit,   x: 90, y: 16 },
  { id: "adjudicator", label: "Adjudicator",  type: "Agent",  icon: Activity,       x: 90, y: 52 },
  { id: "phase",       label: "Phase",        type: "Phase",  icon: Layers3,        x: 90, y: 84 },
];

export const EDGES: EdgeDef[] = [
  // Sources fan into Researcher.left at three vertical offsets
  { id: "hn-r",      from: "hn",          fromSide: "right",  to: "researcher", toSide: "left",   toOffset: -0.35, label: "stories",         event_type: "_source_hn" },
  { id: "reddit-r",  from: "reddit",      fromSide: "right",  to: "researcher", toSide: "left",                    label: "threads",         event_type: "_source_reddit" },
  { id: "web-r",     from: "web",         fromSide: "right",  to: "researcher", toSide: "left",   toOffset: +0.35, label: "pages",           event_type: "_source_web" },

  // Pipeline
  { id: "tasks-r",   from: "tasks",       fromSide: "top",    to: "researcher", toSide: "bottom",                  label: "claim",           event_type: "task.created" },
  { id: "r-find",    from: "researcher",  fromSide: "right",  to: "findings",   toSide: "left",                    label: "finding",         event_type: "task.completed" },

  // Critics
  { id: "find-aud",  from: "findings",    fromSide: "bottom", to: "auditor",    toSide: "top",                     label: "audit",           event_type: "task.completed" },
  { id: "find-adv",  from: "findings",    fromSide: "top",    to: "adversary",  toSide: "left",                    label: "high signal",     event_type: "finding.high_signal" },

  // Adversary → Theses
  { id: "adv-thes",  from: "adversary",   fromSide: "bottom", to: "theses",     toSide: "top",                     label: "kill / weaken",   event_type: "thesis.invalidated" },

  // CEO ↔ Theses (parallel via ±0.35 offsets)
  { id: "thes-ceo",  from: "theses",      fromSide: "top",    fromOffset: -0.35, to: "ceo",        toSide: "bottom", toOffset: -0.35, label: "invalidated",     event_type: "thesis.invalidated" },
  { id: "ceo-thes",  from: "ceo",         fromSide: "bottom", fromOffset: +0.35, to: "theses",     toSide: "top",    toOffset: +0.35, label: "spawn / charter", event_type: "thesis.created" },

  // Right column flow
  { id: "thes-adj",  from: "theses",      fromSide: "right",  to: "adjudicator", toSide: "left",                    label: "conf changed",    event_type: "thesis.confidence_changed" },
  { id: "adj-phase", from: "adjudicator", fromSide: "bottom", to: "phase",       toSide: "top",                     label: "proposal",        event_type: "phase.transition_proposed" },

  // phase → ceo: long-range. Both nodes sit in the governance column with
  // Adjudicator between them, and the strategic column to the left holds
  // Adversary + Theses stacked at the same y as CEO + Phase respectively.
  // Outside-LEFT would have to thread past Adversary on the way in; route
  // outside-RIGHT instead, looping past the right edge of the canvas where
  // nothing else lives.
  { id: "phase-ceo", from: "phase",       fromSide: "right",  to: "ceo",         toSide: "right",  route: "outside-right", label: "ratify",     event_type: "phase.transition_proposed" },

  // Tasks ↔ Planner (parallel via ±0.35 offsets)
  { id: "tasks-pl",  from: "tasks",       fromSide: "right",  fromOffset: -0.35, to: "planner",    toSide: "left",   toOffset: -0.35, label: "queue empty",     event_type: "queue.empty" },
  { id: "pl-tasks",  from: "planner",     fromSide: "left",   fromOffset: +0.35, to: "tasks",      toSide: "right",  toOffset: +0.35, label: "refill",          event_type: "task.created" },
];

export const ROLE_TO_NODE: Record<string, string> = {
  ceo: "ceo",
  planner: "planner",
  researcher: "researcher",
  auditor: "auditor",
  adversary: "adversary",
  phase_adjudicator: "adjudicator",
};

// =========================================================================
// Geometry helpers — pure functions, fully testable
// =========================================================================

export function nodeById(id: string): NodeDef | undefined {
  return NODES.find((n) => n.id === id);
}

export function handleVec(side: Side): { x: number; y: number } {
  return side === "left"  ? { x: -1, y: 0 }
       : side === "right" ? { x:  1, y: 0 }
       : side === "top"   ? { x:  0, y: -1 }
       :                    { x:  0, y:  1 };
}

/**
 * Center of a node in SVG units (x in 0..SVG_W, y in 0..SVG_H).
 */
export function nodeCenter(node: NodeDef): { x: number; y: number } {
  return { x: node.x * ASPECT_X, y: node.y * ASPECT_Y };
}

/**
 * Half-width / half-height of a node in SVG units.
 */
export function nodeHalfSize(): { halfW: number; halfH: number } {
  return { halfW: (NODE_W * ASPECT_X) / 2, halfH: (NODE_H * ASPECT_Y) / 2 };
}

/**
 * Axis-aligned bounding box of a node in SVG units.
 */
export function nodeBBox(node: NodeDef): { x1: number; y1: number; x2: number; y2: number } {
  const c = nodeCenter(node);
  const { halfW, halfH } = nodeHalfSize();
  return { x1: c.x - halfW, y1: c.y - halfH, x2: c.x + halfW, y2: c.y + halfH };
}

/** Coordinates of a port in SVG units. */
export function handlePoint(node: NodeDef, side: Side, offset = 0): { x: number; y: number } {
  const c = nodeCenter(node);
  const { halfW, halfH } = nodeHalfSize();
  // Offsets are in node-extent units: ±0.5 reaches the corner.
  const offX = offset * NODE_W * ASPECT_X;
  const offY = offset * NODE_H * ASPECT_Y;
  switch (side) {
    case "left":   return { x: c.x - halfW, y: c.y + offY };
    case "right":  return { x: c.x + halfW, y: c.y + offY };
    case "top":    return { x: c.x + offX,  y: c.y - halfH };
    case "bottom": return { x: c.x + offX,  y: c.y + halfH };
  }
}

/**
 * The point INSIDE the destination node where the arrow should actually end.
 * Beyond the port by ARROW_PENETRATION units in the −toV direction (i.e. INTO
 * the node). The path's final segment runs from the port to this point, so
 * the SVG marker (placed at the path's end) lands behind the node card and
 * visually penetrates the border like an n8n edge.
 */
export function penetrationPoint(port: { x: number; y: number }, toSide: Side): { x: number; y: number } {
  const toV = handleVec(toSide);
  return { x: port.x - toV.x * ARROW_PENETRATION, y: port.y - toV.y * ARROW_PENETRATION };
}

/**
 * Build a smooth cubic-bezier path from source handle to target handle,
 * with a short straight segment at the end that extends INTO the destination
 * node by ARROW_PENETRATION units.
 *
 * Invariants:
 *   - path starts exactly at the source port
 *   - path ends exactly at penetrationPoint(target port, toSide)
 *   - the final segment's direction is opposite toV (i.e. INTO the target)
 */
export function buildEdgePath(
  start: { x: number; y: number },
  port:  { x: number; y: number },   // target port (where the curve ends)
  fromSide: Side,
  toSide: Side,
  route: RouteMode = "auto",
): string {
  const tip = penetrationPoint(port, toSide);

  // Outside-U routes stay orthogonal; the curve escapes the column then dives in.
  const outsideStub = OUTSIDE_STUB;
  if (route === "outside-left") {
    const x = Math.min(start.x, port.x) - outsideStub;
    return [`M`, start.x, start.y,
            `Q`, x, start.y, x, (start.y + port.y) / 2,
            `Q`, x, port.y, port.x, port.y,
            `L`, tip.x, tip.y].join(" ");
  }
  if (route === "outside-right") {
    const x = Math.max(start.x, port.x) + outsideStub;
    return [`M`, start.x, start.y,
            `Q`, x, start.y, x, (start.y + port.y) / 2,
            `Q`, x, port.y, port.x, port.y,
            `L`, tip.x, tip.y].join(" ");
  }

  const fromV = handleVec(fromSide);
  const toV   = handleVec(toSide);
  const fromHorizontal = Math.abs(fromV.x) > 0;
  const toHorizontal   = Math.abs(toV.x)   > 0;

  let c1: { x: number; y: number };
  let c2: { x: number; y: number };

  if (fromHorizontal && toHorizontal) {
    const midX = (start.x + port.x) / 2;
    c1 = { x: midX, y: start.y };
    c2 = { x: midX, y: port.y };
  } else if (!fromHorizontal && !toHorizontal) {
    const midY = (start.y + port.y) / 2;
    c1 = { x: start.x, y: midY };
    c2 = { x: port.x,  y: midY };
  } else {
    const dx = Math.abs(port.x - start.x);
    const dy = Math.abs(port.y - start.y);
    // Cap pull at 60% of axis distance so the curve doesn't overshoot or loop.
    const fromPull = Math.max(8, Math.min((fromHorizontal ? dx : dy) * 0.6, 22));
    const toPull   = Math.max(8, Math.min((toHorizontal   ? dx : dy) * 0.6, 22));
    c1 = { x: start.x + fromV.x * fromPull, y: start.y + fromV.y * fromPull };
    c2 = { x: port.x  + toV.x   * toPull,   y: port.y  + toV.y   * toPull };
  }

  return [
    `M`, start.x, start.y,
    `C`, c1.x, c1.y, c2.x, c2.y, port.x, port.y,
    `L`, tip.x, tip.y,
  ].join(" ");
}

/**
 * Midpoint of the visible curve (for placing labels).
 * Uses the bezier t=0.5 evaluation when applicable; for outside-U routes
 * we just use the simple midpoint between endpoints.
 */
export function edgeLabelPoint(
  start: { x: number; y: number },
  port:  { x: number; y: number },
  fromSide: Side,
  toSide: Side,
  route: RouteMode = "auto",
): { x: number; y: number } {
  if (route === "outside-left" || route === "outside-right") {
    const x = route === "outside-left"
      ? Math.min(start.x, port.x) - OUTSIDE_STUB
      : Math.max(start.x, port.x) + OUTSIDE_STUB;
    return { x, y: (start.y + port.y) / 2 };
  }
  const fromV = handleVec(fromSide);
  const toV   = handleVec(toSide);
  const fromHorizontal = Math.abs(fromV.x) > 0;
  const toHorizontal   = Math.abs(toV.x)   > 0;
  let c1: { x: number; y: number };
  let c2: { x: number; y: number };
  if (fromHorizontal && toHorizontal) {
    const midX = (start.x + port.x) / 2;
    c1 = { x: midX, y: start.y };
    c2 = { x: midX, y: port.y };
  } else if (!fromHorizontal && !toHorizontal) {
    const midY = (start.y + port.y) / 2;
    c1 = { x: start.x, y: midY };
    c2 = { x: port.x,  y: midY };
  } else {
    const dx = Math.abs(port.x - start.x);
    const dy = Math.abs(port.y - start.y);
    const fromPull = Math.max(8, Math.min((fromHorizontal ? dx : dy) * 0.6, 22));
    const toPull   = Math.max(8, Math.min((toHorizontal   ? dx : dy) * 0.6, 22));
    c1 = { x: start.x + fromV.x * fromPull, y: start.y + fromV.y * fromPull };
    c2 = { x: port.x  + toV.x   * toPull,   y: port.y  + toV.y   * toPull };
  }
  return {
    x: 0.125 * start.x + 0.375 * c1.x + 0.375 * c2.x + 0.125 * port.x,
    y: 0.125 * start.y + 0.375 * c1.y + 0.375 * c2.y + 0.125 * port.y,
  };
}

/**
 * Evaluate a cubic bezier at parameter t. Used by tests + integrity checks
 * to verify that the path actually ends where it should.
 */
export function evalBezier(
  p0: { x: number; y: number },
  c1: { x: number; y: number },
  c2: { x: number; y: number },
  p3: { x: number; y: number },
  t: number,
): { x: number; y: number } {
  const u = 1 - t;
  return {
    x: u*u*u*p0.x + 3*u*u*t*c1.x + 3*u*t*t*c2.x + t*t*t*p3.x,
    y: u*u*u*p0.y + 3*u*u*t*c1.y + 3*u*t*t*c2.y + t*t*t*p3.y,
  };
}

function evalQuadratic(
  p0: { x: number; y: number },
  c:  { x: number; y: number },
  p1: { x: number; y: number },
  t: number,
): { x: number; y: number } {
  const u = 1 - t;
  return {
    x: u*u*p0.x + 2*u*t*c.x + t*t*p1.x,
    y: u*u*p0.y + 2*u*t*c.y + t*t*p1.y,
  };
}

/**
 * Recompute the same control points buildEdgePath uses, so tests can sample
 * actual rendered points without parsing the SVG string.
 */
export function samplePath(
  start: { x: number; y: number },
  port:  { x: number; y: number },
  fromSide: Side,
  toSide: Side,
  route: RouteMode = "auto",
  steps = 24,
): { x: number; y: number }[] {
  const tip = penetrationPoint(port, toSide);
  const out: { x: number; y: number }[] = [];

  if (route === "outside-left" || route === "outside-right") {
    const x = route === "outside-left"
      ? Math.min(start.x, port.x) - OUTSIDE_STUB
      : Math.max(start.x, port.x) + OUTSIDE_STUB;
    const mid = { x, y: (start.y + port.y) / 2 };
    // Two quadratics: start → mid (control: (x, start.y))
    //                  mid   → port (control: (x, port.y))
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      out.push(evalQuadratic(start, { x, y: start.y }, mid, t));
    }
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      out.push(evalQuadratic(mid, { x, y: port.y }, port, t));
    }
    out.push(tip);
    return out;
  }

  const fromV = handleVec(fromSide);
  const toV   = handleVec(toSide);
  const fromHorizontal = Math.abs(fromV.x) > 0;
  const toHorizontal   = Math.abs(toV.x)   > 0;
  let c1: { x: number; y: number };
  let c2: { x: number; y: number };
  if (fromHorizontal && toHorizontal) {
    const midX = (start.x + port.x) / 2;
    c1 = { x: midX, y: start.y };
    c2 = { x: midX, y: port.y };
  } else if (!fromHorizontal && !toHorizontal) {
    const midY = (start.y + port.y) / 2;
    c1 = { x: start.x, y: midY };
    c2 = { x: port.x,  y: midY };
  } else {
    const dx = Math.abs(port.x - start.x);
    const dy = Math.abs(port.y - start.y);
    const fromPull = Math.max(6, Math.min((fromHorizontal ? dx : dy) * 0.6, 22));
    const toPull   = Math.max(6, Math.min((toHorizontal   ? dx : dy) * 0.6, 22));
    c1 = { x: start.x + fromV.x * fromPull, y: start.y + fromV.y * fromPull };
    c2 = { x: port.x  + toV.x   * toPull,   y: port.y  + toV.y   * toPull };
  }
  for (let i = 0; i <= steps; i++) {
    out.push(evalBezier(start, c1, c2, port, i / steps));
  }
  out.push(tip);
  return out;
}

// =========================================================================
// Integrity checks — assertions about the topology that should always hold
// =========================================================================

export interface Check {
  name: string;
  pass: boolean;
  detail?: string;
}

export function runTopologyChecks(): Check[] {
  const checks: Check[] = [];
  const ids = new Set(NODES.map((n) => n.id));
  const edgeIds = new Set(EDGES.map((e) => e.id));

  checks.push({
    name: "node ids are unique",
    pass: ids.size === NODES.length,
    detail: `${NODES.length} nodes, ${ids.size} unique`,
  });

  checks.push({
    name: "edge ids are unique",
    pass: edgeIds.size === EDGES.length,
    detail: `${EDGES.length} edges, ${edgeIds.size} unique`,
  });

  const missing = EDGES.filter((e) => !ids.has(e.from) || !ids.has(e.to));
  checks.push({
    name: "every edge connects to existing nodes",
    pass: missing.length === 0,
    detail: missing.length ? missing.map((e) => e.id).join(", ") : undefined,
  });

  const validSides: Side[] = ["left", "right", "top", "bottom"];
  const badSides = EDGES.filter((e) => !validSides.includes(e.fromSide) || !validSides.includes(e.toSide));
  checks.push({
    name: "every edge has valid sides",
    pass: badSides.length === 0,
    detail: badSides.length ? badSides.map((e) => e.id).join(", ") : undefined,
  });

  // Bidirectional pairs must have offsets that differ enough to be visually distinct.
  const directionMap = new Map<string, EdgeDef[]>();
  for (const e of EDGES) {
    const key = [e.from, e.to].sort().join("|");
    const arr = directionMap.get(key) ?? [];
    arr.push(e);
    directionMap.set(key, arr);
  }
  const collisions: string[] = [];
  for (const [, pair] of directionMap) {
    if (pair.length < 2) continue;
    for (let i = 0; i < pair.length; i++) {
      for (let j = i + 1; j < pair.length; j++) {
        const a = pair[i], b = pair[j];
        // Same source/target sides AND same offsets → collision
        if (a.fromSide === b.fromSide && a.toSide === b.toSide
            && (a.fromOffset ?? 0) === (b.fromOffset ?? 0)
            && (a.toOffset   ?? 0) === (b.toOffset   ?? 0)) {
          collisions.push(`${a.id} and ${b.id}`);
        }
      }
    }
  }
  checks.push({
    name: "no two edges share both endpoints AND ports",
    pass: collisions.length === 0,
    detail: collisions.length ? collisions.join("; ") : undefined,
  });

  // Path geometry checks — every edge's path should end at penetrationPoint.
  const geomFailures: string[] = [];
  for (const e of EDGES) {
    const from = nodeById(e.from);
    const to   = nodeById(e.to);
    if (!from || !to) continue;
    const start = handlePoint(from, e.fromSide, e.fromOffset ?? 0);
    const port  = handlePoint(to,   e.toSide,   e.toOffset   ?? 0);
    const tip   = penetrationPoint(port, e.toSide);
    const path  = buildEdgePath(start, port, e.fromSide, e.toSide, e.route ?? "auto");
    // Last "L" command should end at tip.
    const tokens = path.split(/\s+/);
    const lastY = parseFloat(tokens[tokens.length - 1]);
    const lastX = parseFloat(tokens[tokens.length - 2]);
    const dx = Math.abs(lastX - tip.x);
    const dy = Math.abs(lastY - tip.y);
    if (dx > 0.001 || dy > 0.001) {
      geomFailures.push(`${e.id} ends at (${lastX.toFixed(2)}, ${lastY.toFixed(2)}) not (${tip.x.toFixed(2)}, ${tip.y.toFixed(2)})`);
    }
  }
  checks.push({
    name: "every edge path ends at penetrationPoint",
    pass: geomFailures.length === 0,
    detail: geomFailures.length ? geomFailures.join("; ") : undefined,
  });

  // For each edge with toSide horizontal, the final segment direction must be horizontal too.
  // For vertical toSide, final segment must be vertical. (Arrow orientation guard.)
  const tangentFailures: string[] = [];
  for (const e of EDGES) {
    const from = nodeById(e.from);
    const to   = nodeById(e.to);
    if (!from || !to) continue;
    const port = handlePoint(to, e.toSide, e.toOffset ?? 0);
    const tip  = penetrationPoint(port, e.toSide);
    const dx = Math.abs(tip.x - port.x);
    const dy = Math.abs(tip.y - port.y);
    const isHorizontalSide = e.toSide === "left" || e.toSide === "right";
    if (isHorizontalSide && dy > 0.001) tangentFailures.push(`${e.id}: expected horizontal final segment`);
    if (!isHorizontalSide && dx > 0.001) tangentFailures.push(`${e.id}: expected vertical final segment`);
  }
  checks.push({
    name: "final segment is perpendicular to target side",
    pass: tangentFailures.length === 0,
    detail: tangentFailures.length ? tangentFailures.join("; ") : undefined,
  });

  // Path-vs-node bbox collision: an edge's sampled curve must not enter the
  // interior of any node it doesn't connect to. (The endpoint nodes are
  // expected to overlap the start/tip points, so they're excluded.)
  // A small inner padding lets the penetration tip sit just inside the
  // destination node without flagging a false collision against neighbors.
  const collisionFailures: string[] = [];
  for (const e of EDGES) {
    const from = nodeById(e.from);
    const to   = nodeById(e.to);
    if (!from || !to) continue;
    const start = handlePoint(from, e.fromSide, e.fromOffset ?? 0);
    const port  = handlePoint(to,   e.toSide,   e.toOffset   ?? 0);
    const samples = samplePath(start, port, e.fromSide, e.toSide, e.route ?? "auto", 32);
    for (const node of NODES) {
      if (node.id === e.from || node.id === e.to) continue;
      const b = nodeBBox(node);
      // Shrink the bbox slightly so an edge grazing a node's outer border
      // isn't counted as "going through" the node.
      const pad = 0.4;
      const inside = samples.some(
        (p) => p.x > b.x1 + pad && p.x < b.x2 - pad && p.y > b.y1 + pad && p.y < b.y2 - pad,
      );
      if (inside) collisionFailures.push(`${e.id} passes through ${node.id}`);
    }
  }
  checks.push({
    name: "no edge path passes through unrelated nodes",
    pass: collisionFailures.length === 0,
    detail: collisionFailures.length ? collisionFailures.join("; ") : undefined,
  });

  return checks;
}
