"use client";

import { motion } from "framer-motion";
import { type LucideIcon } from "lucide-react";
import { type ReactNode } from "react";

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
