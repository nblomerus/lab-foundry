"use client";

import Link from "next/link";
import { type CompanyState } from "../lib/types";
import { PAGE_ORDER } from "./PageNav";
import { Badge, cx } from "./ui";

const PHASE_TAGLINE: Record<string, string> = {
  exploration: "Mapping the territory. Casting wide.",
  convergence: "Narrowing to top claims. Hunting contradictions.",
  commitment:  "Committing to a thesis. Charter being written.",
  execution:   "Charter set. Shipping to a paying customer.",
};

const PHASE_OBJECTIVE: Record<string, string> = {
  exploration: "Map gaps, evidence, and the strongest candidate directions",
  convergence: "Narrow to the few directions worth pursuing",
  commitment:  "Lock a thesis and write the research plan",
  execution:   "Run the work and draft the article",
};

const NEXT_MILESTONE: Record<string, string> = {
  exploration: "Thesis selection",
  convergence: "Direction commitment",
  commitment:  "Research plan sign-off",
  execution:   "Article draft",
};

export function Header({ state }: { state: CompanyState }) {
  const dayN = Math.max(0, state.days_since_start);
  const phase = state.current_phase;

  return (
    <header className="mb-6 rounded-[2rem] border border-slate-200 bg-white/85 p-5 shadow-sm backdrop-blur">
      <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
        <div className="max-w-4xl">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Badge tone="dark">Research OS</Badge>
            <Badge tone="blue">Phase · {phase}</Badge>
            <Badge tone="green">Autonomous with review gates</Badge>
            {state.paused && <Badge tone="amber">Paused</Badge>}
          </div>

          <h1 className="text-3xl font-semibold tracking-tight text-slate-950 sm:text-4xl">
            {state.charter && state.thesis
              ? state.thesis
              : "Explore, challenge, and converge on a publishable research direction."}
          </h1>

          <p className="mt-3 max-w-3xl text-sm leading-7 text-slate-600">
            <span className="font-semibold text-slate-900">Mission:</span>{" "}
            {state.problem_statement.split("\n").join(" ")}
          </p>

          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Objective
              label="Current objective"
              value="Identify the strongest article-worthy direction"
            />
            <Objective label="Phase goal" value={PHASE_OBJECTIVE[phase] ?? "—"} />
            <Objective label="Days since start" value={dayN === 1 ? "1 day" : `${dayN} days`} />
            <Objective label="Next milestone" value={NEXT_MILESTONE[phase] ?? "—"} />
          </div>
        </div>
      </div>

      <div className="mt-6 flex flex-wrap gap-2">
        {PAGE_ORDER.map((p) => (
          <Link
            key={p.href}
            href={p.href}
            className={cx(
              "rounded-2xl px-4 py-2 text-sm font-semibold transition",
              p.href === "/"
                ? "bg-slate-950 text-white shadow-sm"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200 hover:text-slate-950",
            )}
          >
            {p.label}
          </Link>
        ))}
      </div>
    </header>
  );
}

function Objective({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl bg-slate-50 p-3">
      <div className="text-xs font-semibold uppercase tracking-wide text-slate-400">
        {label}
      </div>
      <div className="mt-1 text-sm font-semibold text-slate-800">{value}</div>
    </div>
  );
}
