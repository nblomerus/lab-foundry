"use client";

import { motion } from "framer-motion";
import {
  AlertTriangle,
  ArrowRight,
  BrainCircuit,
  ChevronRight,
  GitBranch,
  Layers3,
  Search,
  ShieldCheck,
  Telescope,
  type LucideIcon,
} from "lucide-react";
import type { OrgRole } from "../lib/types";
import { Badge, Card, SectionTitle, Progress, cx } from "./ui";

interface AgentSpec {
  key: string;             // matches OrgRole.role
  name: string;
  cadence: string;
  reads: string;
  writes: string;
  icon: LucideIcon;
}

const PRIMARY_FLOW: AgentSpec[] = [
  {
    key: "pi",
    name: "PI",
    cadence: "Strategic · R-tier",
    reads: "Findings, invalidated claims",
    writes: "Claims, charter, kill verdicts",
    icon: BrainCircuit,
  },
  {
    key: "planner",
    name: "Planner",
    cadence: "Tactical · W-tier",
    reads: "Active claims, queue",
    writes: "Research tasks (4–16)",
    icon: GitBranch,
  },
  {
    key: "researcher",
    name: "Researcher",
    cadence: "Swarm · C-tier",
    reads: "Tasks + HN / Reddit / web",
    writes: "Findings with relevance",
    icon: Search,
  },
];

const CRITICS: AgentSpec[] = [
  {
    key: "evaluation",
    name: "Evaluation",
    cadence: "Every finding · W-tier",
    reads: "Findings",
    writes: "Pass / slop / unclear",
    icon: ShieldCheck,
  },
  {
    key: "critic",
    name: "Critic",
    cadence: "High-signal · W/R-tier",
    reads: "Claims + findings",
    writes: "Kill / weaken / watch",
    icon: Telescope,
  },
];

function statusFor(role: OrgRole | undefined): "live" | "recent" | "idle" {
  if (!role) return "idle";
  if (role.running_count > 0) return "live";
  if (role.runs_today > 0)    return "recent";
  return "idle";
}

function AgentNode({ spec, role, index }: { spec: AgentSpec; role?: OrgRole; index: number }) {
  const Icon = spec.icon;
  const st = statusFor(role);
  const stateTone =
    st === "live" ? "green" : st === "recent" ? "blue" : "default";
  const stateLabel =
    st === "live" ? "Running" : st === "recent" ? "Recent" : "Idle";
  const runsToday = role?.runs_today ?? 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.08 }}
      className="relative rounded-3xl border border-slate-200 bg-white p-4 shadow-sm"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="relative rounded-2xl bg-slate-950 p-2 text-white">
            <Icon className="h-4 w-4" />
            {st === "live" && (
              <span className="pulse-dot absolute -right-1 -top-1 h-2 w-2 rounded-full bg-emerald-400" />
            )}
          </div>
          <div>
            <div className="font-semibold text-slate-950">{spec.name}</div>
            <div className="text-xs text-slate-500">{spec.cadence}</div>
          </div>
        </div>
        <Badge tone={stateTone}>{stateLabel}</Badge>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 text-xs">
        <div className="rounded-2xl bg-slate-50 p-3">
          <div className="text-slate-400">Reads</div>
          <div className="mt-1 font-medium text-slate-700">{spec.reads}</div>
        </div>
        <div className="rounded-2xl bg-slate-50 p-3">
          <div className="text-slate-400">Writes</div>
          <div className="mt-1 font-medium text-slate-700">{spec.writes}</div>
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between text-xs text-slate-500">
        <span>Runs today</span>
        <span className="font-mono">{runsToday}</span>
      </div>
      <div className="mt-2">
        <Progress value={Math.min(100, runsToday * 5)} tone={st === "live" ? "pass" : "info"} />
      </div>
      {role?.avg_duration_s != null && (
        <div className="mt-2 text-right text-[10px] font-mono text-slate-400">
          avg {role.avg_duration_s.toFixed(1)}s
        </div>
      )}
    </motion.div>
  );
}

type Liveness = {
  state: "live" | "quiet" | "stalled" | "never";
  ago: string;
};

function humanizeAgo(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function computeLiveness(lastActivityAt: string | null | undefined): Liveness {
  if (!lastActivityAt) return { state: "never", ago: "never" };
  const elapsed = Date.now() - new Date(lastActivityAt).getTime();
  const ago = humanizeAgo(elapsed);
  // < 3 min: live. 3–20 min: quiet. > 20 min: stalled.
  if (elapsed < 3 * 60_000) return { state: "live", ago };
  if (elapsed < 20 * 60_000) return { state: "quiet", ago };
  return { state: "stalled", ago };
}

export function WorkflowLoop({
  roles,
  latestEventType,
  lastActivityAt,
}: {
  roles: OrgRole[];
  latestEventType?: string;
  lastActivityAt?: string | null;
}) {
  const byRole = new Map(roles.map((r) => [r.role, r]));
  const get = (k: string) => byRole.get(k);
  const live = computeLiveness(lastActivityAt);

  return (
    <Card className="lg:col-span-8">
      <SectionTitle
        icon={Layers3}
        title="Autonomous organisation loop"
        subtitle="Strategic → Tactical → Execution, with critics gating every output."
        action={
          <div className="flex items-center gap-2">
            <Badge tone={
              live.state === "live" ? "green"
              : live.state === "quiet" ? "amber"
              : live.state === "stalled" || live.state === "never" ? "red"
              : "default"
            }>
              {live.state === "live" ? "live"
              : live.state === "quiet" ? `quiet · ${live.ago}`
              : live.state === "never" ? "never run"
              : `stalled · ${live.ago}`}
            </Badge>
            <Badge tone="dark">No human in loop</Badge>
          </div>
        }
      />

      <div className="grid gap-4 md:grid-cols-3">
        {PRIMARY_FLOW.map((spec, idx) => (
          <div key={spec.key} className="relative">
            <AgentNode spec={spec} role={get(spec.key)} index={idx} />
            {idx < PRIMARY_FLOW.length - 1 && (
              <div className="pointer-events-none absolute -right-3 top-1/2 z-10 hidden -translate-y-1/2 rounded-full border border-slate-200 bg-white p-1 shadow-sm md:block">
                <ArrowRight className="h-4 w-4 text-slate-500" />
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        {CRITICS.map((spec, idx) => (
          <AgentNode key={spec.key} spec={spec} role={get(spec.key)} index={PRIMARY_FLOW.length + idx} />
        ))}
      </div>

      <div
        className={cx(
          "mt-5 rounded-3xl border p-4 text-white",
          live.state === "stalled" || live.state === "never"
            ? "border-amber-400/60 bg-slate-950 ring-1 ring-amber-400/40"
            : "border-slate-200 bg-slate-950",
        )}
      >
        <div className="flex flex-col justify-between gap-4 md:flex-row md:items-center">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold">
              {live.state === "stalled" || live.state === "never" ? (
                <>
                  <AlertTriangle className="h-4 w-4 text-amber-400" />
                  <span className="text-amber-300">
                    {live.state === "never"
                      ? "No activity yet — the loop has never run."
                      : `Stalled — last activity ${live.ago}.`}
                  </span>
                </>
              ) : (
                <>
                  <span className="relative flex h-2.5 w-2.5">
                    <span className={cx(
                      "absolute inline-flex h-full w-full animate-ping rounded-full opacity-75",
                      live.state === "live" ? "bg-emerald-400" : "bg-amber-400",
                    )} />
                    <span className={cx(
                      "relative inline-flex h-2.5 w-2.5 rounded-full",
                      live.state === "live" ? "bg-emerald-500" : "bg-amber-500",
                    )} />
                  </span>
                  {latestEventType
                    ? <>Latest event: <span className="font-mono">{latestEventType}</span></>
                    : live.state === "live"
                      ? <>Live — last activity {live.ago}.</>
                      : <>Quiet — last activity {live.ago}.</>}
                </>
              )}
            </div>
            <p className="mt-1 max-w-2xl text-sm text-slate-300">
              {live.state === "stalled"
                ? "The autonomous loop isn't producing events. The harness may be down or wedged — restart it (make harness) to re-ignite."
                : "Findings get audited, kills get reviewed, phases transition on data. The Adjudicator decides; the PI ratifies; you watch."}
            </p>
          </div>
          <a
            href="/events"
            className="inline-flex items-center justify-center gap-2 rounded-2xl bg-white px-4 py-2 text-sm font-semibold text-slate-950 shadow-sm transition hover:bg-slate-100"
          >
            Inspect events <ChevronRight className="h-4 w-4" />
          </a>
        </div>
      </div>
    </Card>
  );
}
