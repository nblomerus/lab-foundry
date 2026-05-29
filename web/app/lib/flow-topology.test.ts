import { describe, it, expect } from "vitest";
import {
  ARROW_PENETRATION,
  ASPECT_X,
  ASPECT_Y,
  EDGES,
  NODES,
  NODE_H,
  NODE_W,
  ROLE_TO_NODE,
  SVG_H,
  SVG_W,
  buildEdgePath,
  edgeLabelPoint,
  evalBezier,
  handlePoint,
  handleVec,
  nodeBBox,
  nodeById,
  nodeCenter,
  penetrationPoint,
  runTopologyChecks,
  samplePath,
} from "./flow-topology";
import type { EdgeDef, Side } from "./flow-topology";

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

const SIDES: Side[] = ["left", "right", "top", "bottom"];

function lastTwoTokens(path: string): { x: number; y: number } {
  const t = path.trim().split(/\s+/);
  return { x: parseFloat(t[t.length - 2]), y: parseFloat(t[t.length - 1]) };
}

function firstPoint(path: string): { x: number; y: number } {
  const t = path.trim().split(/\s+/);
  return { x: parseFloat(t[1]), y: parseFloat(t[2]) };
}

function close(a: number, b: number, eps = 1e-6) {
  return Math.abs(a - b) <= eps;
}

// --------------------------------------------------------------------------
// Coordinate system invariants
// --------------------------------------------------------------------------

describe("coordinate system", () => {
  it("SVG_W / SVG_H matches ASPECT_X / ASPECT_Y", () => {
    expect(SVG_W / 100).toBeCloseTo(ASPECT_X);
    expect(SVG_H / 100).toBeCloseTo(ASPECT_Y);
  });

  it("ASPECT_X equals viewBox aspect (uniform pixel-units guarantee)", () => {
    // For perpendicular angles in viewBox to render perpendicular in pixels,
    // the SVG aspect must match the container aspect (16:10).
    expect(SVG_W / SVG_H).toBeCloseTo(1.6);
  });

  it("every node center sits inside the SVG canvas", () => {
    for (const n of NODES) {
      const c = nodeCenter(n);
      expect(c.x).toBeGreaterThanOrEqual(0);
      expect(c.x).toBeLessThanOrEqual(SVG_W);
      expect(c.y).toBeGreaterThanOrEqual(0);
      expect(c.y).toBeLessThanOrEqual(SVG_H);
    }
  });

  it("no two node bboxes overlap", () => {
    for (let i = 0; i < NODES.length; i++) {
      for (let j = i + 1; j < NODES.length; j++) {
        const a = nodeBBox(NODES[i]);
        const b = nodeBBox(NODES[j]);
        const overlap = a.x1 < b.x2 && a.x2 > b.x1 && a.y1 < b.y2 && a.y2 > b.y1;
        expect(overlap, `${NODES[i].id} overlaps ${NODES[j].id}`).toBe(false);
      }
    }
  });
});

// --------------------------------------------------------------------------
// Topology data integrity
// --------------------------------------------------------------------------

describe("topology data", () => {
  it("has unique node ids", () => {
    const ids = NODES.map((n) => n.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("has unique edge ids", () => {
    const ids = EDGES.map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("every edge references existing nodes", () => {
    const ids = new Set(NODES.map((n) => n.id));
    for (const e of EDGES) {
      expect(ids.has(e.from), `edge ${e.id}.from=${e.from} missing`).toBe(true);
      expect(ids.has(e.to), `edge ${e.id}.to=${e.to} missing`).toBe(true);
    }
  });

  it("every edge uses valid sides", () => {
    for (const e of EDGES) {
      expect(SIDES).toContain(e.fromSide);
      expect(SIDES).toContain(e.toSide);
    }
  });

  it("ROLE_TO_NODE entries point at existing nodes", () => {
    const ids = new Set(NODES.map((n) => n.id));
    for (const [role, id] of Object.entries(ROLE_TO_NODE)) {
      expect(ids.has(id), `role ${role} → ${id} missing`).toBe(true);
    }
  });

  it("no two edges share both endpoints AND both ports", () => {
    const seen = new Map<string, EdgeDef>();
    for (const e of EDGES) {
      const k = [
        e.from, e.fromSide, e.fromOffset ?? 0,
        e.to,   e.toSide,   e.toOffset   ?? 0,
      ].join("|");
      const prior = seen.get(k);
      expect(prior, `collision: ${e.id} matches ${prior?.id}`).toBeUndefined();
      seen.set(k, e);
    }
  });
});

// --------------------------------------------------------------------------
// Geometry helpers
// --------------------------------------------------------------------------

describe("handleVec", () => {
  it("returns unit vectors", () => {
    expect(handleVec("left")).toEqual({ x: -1, y: 0 });
    expect(handleVec("right")).toEqual({ x: 1, y: 0 });
    expect(handleVec("top")).toEqual({ x: 0, y: -1 });
    expect(handleVec("bottom")).toEqual({ x: 0, y: 1 });
  });
});

describe("handlePoint", () => {
  // A test node at container-% center (50, 50). In SVG units that's
  // (50 * ASPECT_X, 50 * ASPECT_Y) = (80, 50).
  const node = {
    id: "x", label: "x", type: "Agent" as const, icon: null as any, x: 50, y: 50,
  };
  const halfW = (NODE_W * ASPECT_X) / 2;
  const halfH = (NODE_H * ASPECT_Y) / 2;

  it("places left/right ports at the node edge in SVG units", () => {
    expect(handlePoint(node, "left")).toEqual({ x: 80 - halfW, y: 50 });
    expect(handlePoint(node, "right")).toEqual({ x: 80 + halfW, y: 50 });
  });

  it("places top/bottom ports at the node edge in SVG units", () => {
    expect(handlePoint(node, "top")).toEqual({ x: 80, y: 50 - halfH });
    expect(handlePoint(node, "bottom")).toEqual({ x: 80, y: 50 + halfH });
  });

  it("applies offset proportional to node extent in matching axis", () => {
    const lp = handlePoint(node, "left", 0.5);
    expect(lp.x).toBeCloseTo(80 - halfW);
    expect(lp.y).toBeCloseTo(50 + 0.5 * NODE_H * ASPECT_Y);
  });
});

describe("penetrationPoint", () => {
  it("moves the point INTO the node by ARROW_PENETRATION", () => {
    expect(penetrationPoint({ x: 10, y: 5 }, "left")).toEqual({ x: 10 + ARROW_PENETRATION, y: 5 });
    expect(penetrationPoint({ x: 10, y: 5 }, "right")).toEqual({ x: 10 - ARROW_PENETRATION, y: 5 });
    expect(penetrationPoint({ x: 10, y: 5 }, "top")).toEqual({ x: 10, y: 5 + ARROW_PENETRATION });
    expect(penetrationPoint({ x: 10, y: 5 }, "bottom")).toEqual({ x: 10, y: 5 - ARROW_PENETRATION });
  });
});

// --------------------------------------------------------------------------
// buildEdgePath invariants
// --------------------------------------------------------------------------

describe("buildEdgePath", () => {
  it("starts exactly at the source port", () => {
    const path = buildEdgePath({ x: 10, y: 20 }, { x: 80, y: 20 }, "right", "left");
    expect(firstPoint(path)).toEqual({ x: 10, y: 20 });
  });

  it("ends exactly at penetrationPoint(target port)", () => {
    const start = { x: 10, y: 20 };
    const port  = { x: 80, y: 20 };
    const path = buildEdgePath(start, port, "right", "left");
    const tip = penetrationPoint(port, "left");
    const end = lastTwoTokens(path);
    expect(close(end.x, tip.x)).toBe(true);
    expect(close(end.y, tip.y)).toBe(true);
  });

  it("final L-segment direction matches handleVec inversion", () => {
    for (const toSide of SIDES) {
      const start = { x: 10, y: 10 };
      const port  = { x: 80, y: 80 };
      const path = buildEdgePath(start, port, "right", toSide);
      const tokens = path.trim().split(/\s+/);
      const tipX = parseFloat(tokens[tokens.length - 2]);
      const tipY = parseFloat(tokens[tokens.length - 1]);
      const beforeTipX = parseFloat(tokens[tokens.length - 5]);
      const beforeTipY = parseFloat(tokens[tokens.length - 4]);
      const segDx = tipX - beforeTipX;
      const segDy = tipY - beforeTipY;
      const expected = handleVec(toSide);
      if (Math.abs(expected.x) > 0) {
        expect(Math.sign(segDx)).toBe(-Math.sign(expected.x));
        expect(close(segDy, 0)).toBe(true);
      } else {
        expect(Math.sign(segDy)).toBe(-Math.sign(expected.y));
        expect(close(segDx, 0)).toBe(true);
      }
    }
  });

  it("outside-left route loops out past the left side", () => {
    const start = { x: 90, y: 80 };
    const port  = { x: 90, y: 20 };
    const path = buildEdgePath(start, port, "left", "left", "outside-left");
    expect(path.startsWith("M ")).toBe(true);
    expect(path).toContain("Q ");
    const end = lastTwoTokens(path);
    const tip = penetrationPoint(port, "left");
    expect(close(end.x, tip.x)).toBe(true);
    expect(close(end.y, tip.y)).toBe(true);
  });

  it("for every real edge, path ends at the correct penetration tip", () => {
    for (const e of EDGES) {
      const from = nodeById(e.from)!;
      const to   = nodeById(e.to)!;
      const start = handlePoint(from, e.fromSide, e.fromOffset ?? 0);
      const port  = handlePoint(to,   e.toSide,   e.toOffset   ?? 0);
      const tip   = penetrationPoint(port, e.toSide);
      const path  = buildEdgePath(start, port, e.fromSide, e.toSide, e.route ?? "auto");
      const end   = lastTwoTokens(path);
      expect(close(end.x, tip.x), `${e.id} x: ${end.x} ≠ ${tip.x}`).toBe(true);
      expect(close(end.y, tip.y), `${e.id} y: ${end.y} ≠ ${tip.y}`).toBe(true);
    }
  });
});

// --------------------------------------------------------------------------
// Endpoint correctness — every arrow must visibly start at its source node
// and end inside its target node. These are the invariants the screenshot
// review keeps catching by eye: "this arrow doesn't land on the right node".
// --------------------------------------------------------------------------

describe("edge endpoint correctness", () => {
  it("every edge's path starts exactly at handlePoint(fromNode, fromSide, fromOffset)", () => {
    for (const e of EDGES) {
      const from = nodeById(e.from)!;
      const to   = nodeById(e.to)!;
      const expectedStart = handlePoint(from, e.fromSide, e.fromOffset ?? 0);
      const port = handlePoint(to, e.toSide, e.toOffset ?? 0);
      const path = buildEdgePath(expectedStart, port, e.fromSide, e.toSide, e.route ?? "auto");
      const actual = firstPoint(path);
      expect(close(actual.x, expectedStart.x),
        `${e.id} start x: ${actual.x} ≠ ${expectedStart.x}`).toBe(true);
      expect(close(actual.y, expectedStart.y),
        `${e.id} start y: ${actual.y} ≠ ${expectedStart.y}`).toBe(true);
    }
  });

  it("every edge starts on the boundary of its source node", () => {
    // The port has to sit ON the source bbox (one coordinate equals one
    // bbox edge). Otherwise the arrow visibly "floats" off the node.
    for (const e of EDGES) {
      const from = nodeById(e.from)!;
      const start = handlePoint(from, e.fromSide, e.fromOffset ?? 0);
      const b = nodeBBox(from);
      const onLeft   = Math.abs(start.x - b.x1) < 1e-6;
      const onRight  = Math.abs(start.x - b.x2) < 1e-6;
      const onTop    = Math.abs(start.y - b.y1) < 1e-6;
      const onBottom = Math.abs(start.y - b.y2) < 1e-6;
      expect(onLeft || onRight || onTop || onBottom,
        `${e.id} start (${start.x}, ${start.y}) not on ${from.id} bbox ${JSON.stringify(b)}`).toBe(true);
    }
  });

  it("every edge's start point is between the source node's bbox edges", () => {
    // Not just touching the bbox — must be within the bbox span so the port
    // is on the FACE of the node, not floating off its corner.
    for (const e of EDGES) {
      const from = nodeById(e.from)!;
      const start = handlePoint(from, e.fromSide, e.fromOffset ?? 0);
      const b = nodeBBox(from);
      expect(start.x).toBeGreaterThanOrEqual(b.x1 - 1e-6);
      expect(start.x).toBeLessThanOrEqual(b.x2 + 1e-6);
      expect(start.y).toBeGreaterThanOrEqual(b.y1 - 1e-6);
      expect(start.y).toBeLessThanOrEqual(b.y2 + 1e-6);
    }
  });

  it("every edge's start sits on the SIDE specified by fromSide", () => {
    // fromSide=left → start.x equals bbox.x1, etc.
    for (const e of EDGES) {
      const from = nodeById(e.from)!;
      const start = handlePoint(from, e.fromSide, e.fromOffset ?? 0);
      const b = nodeBBox(from);
      const expected =
        e.fromSide === "left"   ? close(start.x, b.x1) :
        e.fromSide === "right"  ? close(start.x, b.x2) :
        e.fromSide === "top"    ? close(start.y, b.y1) :
                                  close(start.y, b.y2);
      expect(expected,
        `${e.id} fromSide=${e.fromSide} but start=(${start.x}, ${start.y}) doesn't match bbox face`).toBe(true);
    }
  });

  it("every edge's tip lands INSIDE the target node's bbox", () => {
    // The penetration tip is what the arrow marker sits at. If the tip is
    // outside the target node, the arrow visibly floats short of the node.
    for (const e of EDGES) {
      const to = nodeById(e.to)!;
      const port = handlePoint(to, e.toSide, e.toOffset ?? 0);
      const tip = penetrationPoint(port, e.toSide);
      const b = nodeBBox(to);
      expect(tip.x).toBeGreaterThan(b.x1 - 1e-6);
      expect(tip.x).toBeLessThan(b.x2 + 1e-6);
      expect(tip.y).toBeGreaterThan(b.y1 - 1e-6);
      expect(tip.y).toBeLessThan(b.y2 + 1e-6);
    }
  });

  it("every edge's port sits on the SIDE specified by toSide", () => {
    for (const e of EDGES) {
      const to = nodeById(e.to)!;
      const port = handlePoint(to, e.toSide, e.toOffset ?? 0);
      const b = nodeBBox(to);
      const expected =
        e.toSide === "left"   ? close(port.x, b.x1) :
        e.toSide === "right"  ? close(port.x, b.x2) :
        e.toSide === "top"    ? close(port.y, b.y1) :
                                close(port.y, b.y2);
      expect(expected,
        `${e.id} toSide=${e.toSide} but port=(${port.x}, ${port.y}) doesn't match bbox face`).toBe(true);
    }
  });

  it("offset stays within the node — port never leaves the bbox face", () => {
    // toOffset / fromOffset multipliers must keep the port on the node's
    // face. |offset| ≤ 0.5 means within the node's half-extent.
    for (const e of EDGES) {
      expect(Math.abs(e.fromOffset ?? 0)).toBeLessThanOrEqual(0.5);
      expect(Math.abs(e.toOffset ?? 0)).toBeLessThanOrEqual(0.5);
    }
  });

  it("every real edge: start ↔ source bbox and tip ↔ target bbox (end-to-end)", () => {
    // Belt-and-braces: roundtrip the full edge as it would render, and
    // verify both endpoints belong to the right nodes.
    for (const e of EDGES) {
      const from = nodeById(e.from)!;
      const to   = nodeById(e.to)!;
      const start = handlePoint(from, e.fromSide, e.fromOffset ?? 0);
      const port  = handlePoint(to,   e.toSide,   e.toOffset   ?? 0);
      const path  = buildEdgePath(start, port, e.fromSide, e.toSide, e.route ?? "auto");

      const firstP = firstPoint(path);
      const tip    = lastTwoTokens(path);

      const fromBox = nodeBBox(from);
      const toBox   = nodeBBox(to);

      // First point sits on source bbox face
      const onFromFace = (
        Math.abs(firstP.x - fromBox.x1) < 1e-6 ||
        Math.abs(firstP.x - fromBox.x2) < 1e-6 ||
        Math.abs(firstP.y - fromBox.y1) < 1e-6 ||
        Math.abs(firstP.y - fromBox.y2) < 1e-6
      );
      expect(onFromFace, `${e.id}: path start not on ${e.from} bbox face`).toBe(true);

      // Tip sits inside target bbox interior
      const inTo = tip.x > toBox.x1 - 1e-6 && tip.x < toBox.x2 + 1e-6
                && tip.y > toBox.y1 - 1e-6 && tip.y < toBox.y2 + 1e-6;
      expect(inTo, `${e.id}: tip (${tip.x},${tip.y}) not in ${e.to} bbox`).toBe(true);
    }
  });
});

// --------------------------------------------------------------------------
// Path sampler ↔ buildEdgePath consistency
// --------------------------------------------------------------------------

describe("samplePath", () => {
  it("starts at the source port and ends at the penetration tip", () => {
    const start = { x: 10, y: 20 };
    const port  = { x: 90, y: 60 };
    const samples = samplePath(start, port, "right", "left");
    expect(close(samples[0].x, start.x)).toBe(true);
    expect(close(samples[0].y, start.y)).toBe(true);
    const last = samples[samples.length - 1];
    const tip = penetrationPoint(port, "left");
    expect(close(last.x, tip.x)).toBe(true);
    expect(close(last.y, tip.y)).toBe(true);
  });

  it("for every real edge, sampled curve doesn't pass through unrelated nodes", () => {
    // This is the test that catches "arrow crosses through a different node".
    for (const e of EDGES) {
      const from = nodeById(e.from)!;
      const to   = nodeById(e.to)!;
      const start = handlePoint(from, e.fromSide, e.fromOffset ?? 0);
      const port  = handlePoint(to,   e.toSide,   e.toOffset   ?? 0);
      const samples = samplePath(start, port, e.fromSide, e.toSide, e.route ?? "auto", 32);
      for (const node of NODES) {
        if (node.id === e.from || node.id === e.to) continue;
        const b = nodeBBox(node);
        const pad = 0.4;
        for (const p of samples) {
          const inside = p.x > b.x1 + pad && p.x < b.x2 - pad
                      && p.y > b.y1 + pad && p.y < b.y2 - pad;
          expect(inside, `edge ${e.id} sample (${p.x.toFixed(2)},${p.y.toFixed(2)}) inside ${node.id}`).toBe(false);
        }
      }
    }
  });
});

// --------------------------------------------------------------------------
// evalBezier
// --------------------------------------------------------------------------

describe("evalBezier", () => {
  it("returns p0 at t=0 and p3 at t=1", () => {
    const p0 = { x: 0, y: 0 };
    const p3 = { x: 100, y: 50 };
    const c1 = { x: 25, y: 0 };
    const c2 = { x: 75, y: 50 };
    expect(evalBezier(p0, c1, c2, p3, 0)).toEqual(p0);
    expect(evalBezier(p0, c1, c2, p3, 1)).toEqual(p3);
  });
});

// --------------------------------------------------------------------------
// edgeLabelPoint
// --------------------------------------------------------------------------

describe("edgeLabelPoint", () => {
  it("sits between endpoints for a flat horizontal connection", () => {
    const start = { x: 10, y: 20 };
    const port  = { x: 80, y: 20 };
    const pt = edgeLabelPoint(start, port, "right", "left");
    expect(pt.x).toBeGreaterThan(start.x);
    expect(pt.x).toBeLessThan(port.x);
    expect(close(pt.y, 20)).toBe(true);
  });

  it("places outside-left labels to the left of both endpoints", () => {
    const start = { x: 90, y: 80 };
    const port  = { x: 90, y: 20 };
    const pt = edgeLabelPoint(start, port, "left", "left", "outside-left");
    expect(pt.x).toBeLessThan(Math.min(start.x, port.x));
  });
});

// --------------------------------------------------------------------------
// Bidirectional pairs must be visually distinct
// --------------------------------------------------------------------------

describe("bidirectional edge offsets", () => {
  it("CEO ↔ Claims pair uses ±0.35 offsets", () => {
    const a = EDGES.find((e) => e.id === "thes-ceo")!;
    const b = EDGES.find((e) => e.id === "ceo-thes")!;
    expect(Math.abs((a.fromOffset ?? 0) - (b.toOffset ?? 0))).toBeGreaterThanOrEqual(0.3);
  });

  it("Tasks ↔ Planner pair uses ±0.35 offsets", () => {
    const a = EDGES.find((e) => e.id === "tasks-pl")!;
    const b = EDGES.find((e) => e.id === "pl-tasks")!;
    expect(Math.abs((a.fromOffset ?? 0) - (b.toOffset ?? 0))).toBeGreaterThanOrEqual(0.3);
  });
});

// --------------------------------------------------------------------------
// Meta-assertion
// --------------------------------------------------------------------------

describe("runTopologyChecks", () => {
  it("every check passes for the current topology", () => {
    const checks = runTopologyChecks();
    const failures = checks.filter((c) => !c.pass);
    if (failures.length > 0) {
      const msg = failures.map((c) => `${c.name}: ${c.detail ?? "(no detail)"}`).join("\n");
      throw new Error(`Topology check failures:\n${msg}`);
    }
    expect(failures.length).toBe(0);
  });

  it("includes the path-vs-node collision check", () => {
    const checks = runTopologyChecks();
    const names = checks.map((c) => c.name);
    expect(names).toContain("no edge path passes through unrelated nodes");
  });
});
