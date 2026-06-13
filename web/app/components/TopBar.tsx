"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { BrainCircuit } from "lucide-react";
import { StatusPill, cx, type LiveStatus } from "./ui";
import { fmtClock, fmtDate } from "../lib/format";
import { api } from "../lib/api";

// All real, reachable routes (replaces the SiteNav rail + PageNav pill bar so
// nothing is unreachable below xl). Floorplan is home.
const TABS = [
  { href: "/", label: "Floorplan" },
  { href: "/agents", label: "Agents" },
  { href: "/claims", label: "Claims" },
  { href: "/ariadne", label: "Ariadne" },
  { href: "/researchers", label: "Researchers" },
  { href: "/experiments", label: "Experiments" },
  { href: "/research", label: "Research" },
  { href: "/events", label: "Events" },
  { href: "/org", label: "Org" },
  { href: "/trace", label: "Trace" },
  { href: "/bench", label: "Bench" },
  { href: "/debug", label: "Debug" },
];

const OPERATOR = process.env.NEXT_PUBLIC_OPERATOR_NAME || "Director of Research";

function isActive(path: string, href: string): boolean {
  return href === "/" ? path === "/" : path.startsWith(href);
}

// Honest health from the live intake: certifying now → healthy; reachable but
// quiet → idle; intake reporting not-ok → degraded; API unreachable → offline.
function healthFrom(ingested: number | null, ok: boolean): { status: LiveStatus; label: string } {
  if (ingested == null) return { status: "offline", label: "API offline" };
  if (!ok) return { status: "degraded", label: "Intake degraded" };
  if (ingested > 0) return { status: "healthy", label: "System Healthy" };
  return { status: "idle", label: "Idle" };
}

export function TopBar() {
  const path = usePathname();
  const [now, setNow] = useState<Date | null>(null);
  const [health, setHealth] = useState<{ status: LiveStatus; label: string }>({ status: "idle", label: "…" });

  useEffect(() => {
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api.mimirPanel()
        .then((m) => { if (!cancelled) setHealth(healthFrom(m.at_a_glance?.ingested_today ?? 0, m.status === "ok")); })
        .catch(() => { if (!cancelled) setHealth(healthFrom(null, false)); });
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  return (
    <header className="glass-panel sticky top-0 z-30 flex flex-wrap items-center gap-x-4 gap-y-3 rounded-card px-4 py-3">
      {/* Brand */}
      <Link href="/" className="flex items-center gap-2.5" aria-label="LabFoundry">
        <span className="flex h-9 w-9 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-sm">
          <BrainCircuit className="h-5 w-5" />
        </span>
        <span className="leading-tight">
          <span className="block text-sm font-semibold tracking-tight text-slate-950">LabFoundry</span>
          <span className="block text-[11px] text-slate-400">AI Research Organization</span>
        </span>
      </Link>

      {/* Tabs */}
      <nav className="order-3 flex w-full items-center gap-0.5 overflow-x-auto rounded-2xl border border-slate-200/80 bg-white/60 p-1 lg:order-2 lg:w-auto">
        {TABS.map((t) => (
          <Link
            key={t.href}
            href={t.href}
            className={cx(
              "shrink-0 rounded-xl px-3 py-1.5 text-xs font-medium transition",
              isActive(path, t.href)
                ? "bg-slate-950 text-white"
                : "text-slate-600 hover:bg-slate-100 hover:text-slate-950",
            )}
          >
            {t.label}
          </Link>
        ))}
      </nav>

      {/* Status · clock · identity */}
      <div className="order-2 ml-auto flex items-center gap-3 lg:order-3">
        {health && <StatusPill status={health.status} label={health.label} />}
        <div className="hidden text-right leading-tight sm:block">
          <div className="text-sm font-semibold tabular-nums text-slate-800">{now ? fmtClock(now) : "--:--"}</div>
          <div className="text-[11px] text-slate-400">{now ? fmtDate(now) : ""}</div>
        </div>
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-200 text-[11px] font-semibold text-slate-600">
            {OPERATOR.split(" ").map((w) => w[0]).join("").slice(0, 2).toUpperCase()}
          </span>
          <span className="hidden text-xs font-medium text-slate-600 xl:block">{OPERATOR}</span>
        </div>
      </div>
    </header>
  );
}
