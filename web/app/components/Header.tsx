"use client";

import { useState } from "react";
import { type CompanyState } from "../lib/types";
import { Badge, cx } from "./ui";

const PHASE_TAGLINE: Record<string, string> = {
  exploration: "Mapping the territory. Casting wide.",
  convergence: "Narrowing to top theses. Hunting contradictions.",
  commitment:  "Committing to a thesis. Charter being written.",
  execution:   "Charter set. Shipping to a paying customer.",
};

export function Header({ state }: { state: CompanyState }) {
  const tabs = ["Command", "Theses", "Events", "Org"];
  const [tab, setTab] = useState("Command");
  return (
    <header className="mb-6 rounded-[2rem] border border-slate-200 bg-white/80 p-5 shadow-sm backdrop-blur">
      <div className="flex flex-col justify-between gap-5 lg:flex-row lg:items-center">
        <div>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Badge tone="dark">Boardroom</Badge>
            <Badge tone="blue">Phase · {state.current_phase}</Badge>
            <Badge tone="green">Autonomous</Badge>
            {state.paused && <Badge tone="amber">Paused</Badge>}
          </div>
          <h1 className="max-w-4xl text-3xl font-semibold tracking-tight text-slate-950 sm:text-5xl">
            {state.charter ? state.thesis : "Discover a business that makes real money in 30 days."}
          </h1>
          <p className="mt-3 max-w-3xl text-base leading-7 text-slate-600">
            {state.charter
              ? PHASE_TAGLINE.execution
              : PHASE_TAGLINE[state.current_phase] || "Autonomous AI-native company. Watch it run."}
          </p>
        </div>
        <div className="grid min-w-[280px] gap-3 rounded-3xl bg-slate-950 p-4 text-white sm:grid-cols-2 lg:grid-cols-1">
          <Row label="Active theses" value={String(state.active_thesis_count)} />
          <Row label="Killed" value={String(state.killed_thesis_count)} />
          <Row label="Day in phase" value={`${state.days_in_phase}`} />
          <Row label="Days to deadline" value={`${state.days_remaining}`} />
        </div>
      </div>

      <div className="mt-6 flex flex-wrap gap-2">
        {tabs.map((item) => (
          <button
            key={item}
            onClick={() => setTab(item)}
            className={cx(
              "rounded-2xl px-4 py-2 text-sm font-semibold transition",
              tab === item
                ? "bg-slate-950 text-white shadow-sm"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200 hover:text-slate-950",
            )}
          >
            {item}
          </button>
        ))}
      </div>
    </header>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-sm text-slate-300">{label}</span>
      <span className="text-sm font-semibold">{value}</span>
    </div>
  );
}
