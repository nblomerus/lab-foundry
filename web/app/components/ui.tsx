"use client";

import { motion } from "framer-motion";
import { ArrowDownRight, ArrowUpRight, Minus, type LucideIcon } from "lucide-react";
import { type ReactNode } from "react";
import { Area, AreaChart, ResponsiveContainer } from "recharts";

export function cx(...classes: (string | false | null | undefined)[]) {
  return classes.filter(Boolean).join(" ");
}

type Tone = "default" | "green" | "amber" | "blue" | "red" | "dark";

const TONES: Record<Tone, string> = {
  default: "bg-slate-100 text-slate-700 border-slate-200",
  green:   "bg-emerald-50 text-emerald-700 border-emerald-200",
  amber:   "bg-amber-50 text-amber-700 border-amber-200",
  blue:    "bg-blue-50 text-blue-700 border-blue-200",
  red:     "bg-red-50 text-red-700 border-red-200",
  dark:    "bg-slate-900 text-white border-slate-800",
};

export function Badge({ children, tone = "default" }: { children: ReactNode; tone?: Tone }) {
  return (
    <span
      className={cx(
        "inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-medium",
        TONES[tone],
      )}
    >
      {children}
    </span>
  );
}

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cx(
        "rounded-3xl border border-slate-200 bg-white/85 p-5 shadow-sm backdrop-blur",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function SectionTitle({
  icon: Icon,
  title,
  subtitle,
  action,
}: {
  icon: LucideIcon;
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-4 flex items-start justify-between gap-4">
      <div className="flex items-start gap-3">
        <div className="rounded-2xl border border-slate-200 bg-slate-50 p-2">
          <Icon className="h-4 w-4 text-slate-700" />
        </div>
        <div>
          <h2 className="text-base font-semibold tracking-tight text-slate-950">{title}</h2>
          {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
        </div>
      </div>
      {action}
    </div>
  );
}

export function Progress({ value, tone = "default" }: { value: number; tone?: "default" | "pass" | "warn" | "slop" | "info" }) {
  const fillColor =
    tone === "pass" ? "bg-emerald-600"
    : tone === "warn" ? "bg-amber-500"
    : tone === "slop" ? "bg-red-500"
    : tone === "info" ? "bg-blue-600"
    : "bg-slate-950";
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
      <motion.div
        initial={{ width: 0 }}
        animate={{ width: `${Math.max(0, Math.min(100, value))}%` }}
        transition={{ duration: 0.8, ease: "easeOut" }}
        className={cx("h-full rounded-full", fillColor)}
      />
    </div>
  );
}

export function StatTile({
  icon: Icon,
  value,
  label,
  helper,
  helperTone = "default",
}: {
  icon: LucideIcon;
  value: ReactNode;
  label: string;
  helper?: ReactNode;
  helperTone?: Tone;
}) {
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between">
        <div className="rounded-2xl bg-slate-50 p-2">
          <Icon className="h-4 w-4 text-slate-600" />
        </div>
        {helper && <Badge tone={helperTone}>{helper}</Badge>}
      </div>
      <div className="mt-4 text-3xl font-semibold tracking-tight">{value}</div>
      <div className="text-sm text-slate-500">{label}</div>
    </Card>
  );
}

// =========================================================================
// Floorplan-dashboard card kit — KPI cards, sparklines, status pills, gauges.
// New surfaces consume the design tokens in globals.css (rounded-card,
// shadow-card, the --color-* accents, .glass-panel).
// =========================================================================

export type Accent = "live" | "info" | "warn" | "danger" | "idle" | "slate";

const ACCENT_HEX: Record<Accent, string> = {
  live: "#10b981", info: "#2563eb", warn: "#d97706", danger: "#dc2626", idle: "#94a3b8", slate: "#64748b",
};

/** A thin, axis-less trend line for at-a-glance cards. Flat hairline if <2 points. */
export function Sparkline({
  data,
  tone = "live",
  height = 34,
  strokeWidth = 1.6,
}: {
  data: number[];
  tone?: Accent;
  height?: number;
  strokeWidth?: number;
}) {
  const hex = ACCENT_HEX[tone];
  if (data.length < 2) {
    return (
      <div style={{ height }} className="flex w-full items-center">
        <div className="h-px w-full bg-slate-100" />
      </div>
    );
  }
  const series = data.map((v, i) => ({ i, v }));
  const gid = `spark-${tone}`;
  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={series} margin={{ top: 3, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={hex} stopOpacity={0.26} />
              <stop offset="100%" stopColor={hex} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area type="monotone" dataKey="v" stroke={hex} strokeWidth={strokeWidth} fill={`url(#${gid})`} dot={false} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Directional delta indicator, e.g. "↑ 18%" / "↓ 4". */
export function DeltaBadge({ delta, suffix = "", invert = false }: { delta: number; suffix?: string; invert?: boolean }) {
  const positive = invert ? delta < 0 : delta > 0;
  const negative = invert ? delta > 0 : delta < 0;
  const cls = positive ? "text-emerald-600" : negative ? "text-red-500" : "text-slate-400";
  const Arrow = delta > 0 ? ArrowUpRight : delta < 0 ? ArrowDownRight : Minus;
  return (
    <span className={cx("inline-flex items-center gap-0.5 text-xs font-semibold tabular-nums", cls)}>
      <Arrow className="h-3.5 w-3.5" />
      {Math.abs(delta).toLocaleString()}{suffix}
    </span>
  );
}

export type LiveStatus = "live" | "healthy" | "busy" | "degraded" | "idle" | "offline";

const STATUS_META: Record<LiveStatus, { dot: string; ring: string; text: string }> = {
  live:     { dot: "bg-emerald-500", ring: "border-emerald-200 bg-emerald-50", text: "text-emerald-700" },
  healthy:  { dot: "bg-emerald-500", ring: "border-emerald-200 bg-emerald-50", text: "text-emerald-700" },
  busy:     { dot: "bg-amber-500",   ring: "border-amber-200 bg-amber-50",     text: "text-amber-700" },
  degraded: { dot: "bg-amber-500",   ring: "border-amber-200 bg-amber-50",     text: "text-amber-700" },
  idle:     { dot: "bg-slate-400",   ring: "border-slate-200 bg-slate-50",     text: "text-slate-500" },
  offline:  { dot: "bg-slate-300",   ring: "border-slate-200 bg-slate-50",     text: "text-slate-400" },
};

/** Pill with a status dot (pulses while live/healthy). */
export function StatusPill({ status, label, pulse = true }: { status: LiveStatus; label?: string; pulse?: boolean }) {
  const m = STATUS_META[status];
  const text = label ?? status[0].toUpperCase() + status.slice(1);
  const live = status === "live" || status === "healthy";
  return (
    <span className={cx("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium", m.ring, m.text)}>
      <span className={cx("inline-block h-1.5 w-1.5 rounded-full", m.dot, pulse && live && "pulse-dot")} />
      {text}
    </span>
  );
}

/** Labelled horizontal gauge (ops utilization etc.). Auto warns/danger at 75/90%. */
export function MiniBar({ label, value, max = 100, tone = "live", valueLabel }: {
  label: string; value: number; max?: number; tone?: Accent; valueLabel?: string;
}) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  const auto = tone === "live" && pct >= 90 ? "danger" : tone === "live" && pct >= 75 ? "warn" : tone;
  return (
    <div>
      <div className="flex items-center justify-between text-[11px]">
        <span className="text-slate-500">{label}</span>
        <span className="font-semibold tabular-nums text-slate-700">{valueLabel ?? `${Math.round(pct)}%`}</span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div className="h-full rounded-full" style={{ width: `${Math.max(pct, 2)}%`, background: ACCENT_HEX[auto] }} />
      </div>
    </div>
  );
}

/** KPI header card. `planned` renders a muted dashed placeholder (dormant agents). */
export function KpiCard({
  label, value, delta, sparkline, sparkTone = "live", planned = false, accent = "slate", footer, icon: Icon,
}: {
  label: string;
  value?: ReactNode;
  delta?: ReactNode;
  sparkline?: number[];
  sparkTone?: Accent;
  planned?: boolean;
  accent?: Accent;
  footer?: ReactNode;
  icon?: LucideIcon;
}) {
  if (planned) {
    return (
      <div className="rounded-card border border-dashed border-slate-200 bg-white/50 p-4">
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</span>
          <Badge tone="default">Planned</Badge>
        </div>
        <div className="mt-3 text-2xl font-semibold tracking-tight text-slate-300">—</div>
        {footer && <div className="mt-1 text-[11px] text-slate-400">{footer}</div>}
      </div>
    );
  }
  return (
    <div className="rounded-card border border-slate-200 bg-white/85 p-4 shadow-card backdrop-blur">
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
          {Icon && <Icon className="h-3.5 w-3.5" style={{ color: ACCENT_HEX[accent] }} />}
          {label}
        </span>
        {delta}
      </div>
      <div className="mt-2 text-2xl font-semibold tracking-tight text-slate-900 tabular-nums">{value}</div>
      {sparkline && sparkline.length > 1 && <div className="mt-1"><Sparkline data={sparkline} tone={sparkTone} height={28} /></div>}
      {footer && <div className="mt-1 text-[11px] text-slate-400">{footer}</div>}
    </div>
  );
}

/** Frosted floating panel for overlays on the canvas (legends, feed, controls). */
export function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={cx("glass-panel rounded-card", className)}>{children}</div>;
}
