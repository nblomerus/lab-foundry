"use client";

// Custom edge: a static path plus activity-gated particles. Particles travel
// only while data.hot is true (a matching live event arrived recently), so the
// floorplan is still unless the lab is actually moving data.

import { getBezierPath, type EdgeProps } from "@xyflow/react";

const COLORS: Record<string, string> = { intake: "#2c5fb8", knowledge: "#10b981", workflow: "#9aa3ad", converse: "#8b5cf6", feedback: "#f59e0b" };
// Particle colour matches the edge (kept green for the original kinds so they look unchanged; the
// conversation channel travels violet, and the research→PI FEEDBACK loop amber — so the closed
// loop reads distinctly from the forward flows).
const PARTICLE: Record<string, string> = { intake: "#10b981", knowledge: "#10b981", workflow: "#10b981", converse: "#8b5cf6", feedback: "#f59e0b" };

export interface FlowEdgeData {
  kind: "intake" | "knowledge" | "workflow" | "converse" | "feedback";
  live: boolean;
  hot: boolean;
  [key: string]: unknown;
}

export function FlowEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data }: EdgeProps) {
  const d = (data ?? {}) as FlowEdgeData;
  const kind = d.kind ?? "intake";
  const live = !!d.live;
  const hot = !!d.hot;
  const [path] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  const color = COLORS[kind] ?? COLORS.intake;
  const particle = PARTICLE[kind] ?? "#10b981";
  const active = live && hot;
  return (
    <>
      <path
        id={id}
        d={path}
        fill="none"
        stroke={color}
        strokeWidth={live ? 2.2 : 1.6}
        strokeDasharray={live ? undefined : "6 5"}
        strokeLinecap="round"
        style={{
          opacity: live ? (hot ? 1 : 0.7) : 0.4,
          filter: active ? `drop-shadow(0 0 1.5px ${particle})` : "none",
          transition: "opacity .4s ease, filter .4s ease",
        }}
      />
      {active &&
        [0, 1, 2].map((i) => (
          <circle key={`${id}-${i}-${hot}`} r={i === 0 ? 3.6 : 2.6} fill={particle} opacity={i === 0 ? 1 : 0.7} style={{ filter: `drop-shadow(0 0 2px ${particle})` }}>
            <animateMotion dur="1.5s" repeatCount="indefinite" begin={`${i * 0.5}s`} path={path} />
          </circle>
        ))}
    </>
  );
}

export const EDGE_TYPES = { flow: FlowEdge };
