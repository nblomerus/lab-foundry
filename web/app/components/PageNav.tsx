"use client";

import Link from "next/link";
import { useRouter, usePathname } from "next/navigation";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { cx } from "./ui";

export const PAGE_ORDER = [
  { href: "/",       label: "Research OS" },
  { href: "/flow",   label: "Floorplan" },
  { href: "/agents", label: "Agent Lab" },
  { href: "/claims", label: "Claims" },
  { href: "/events", label: "Events" },
  { href: "/org",    label: "Org" },
];

export function PageNav() {
  const path = usePathname();
  const router = useRouter();
  const idx = PAGE_ORDER.findIndex((p) =>
    p.href === "/" ? path === "/" : path.startsWith(p.href),
  );
  const prev = idx > 0 ? PAGE_ORDER[idx - 1] : null;
  const next = idx >= 0 && idx < PAGE_ORDER.length - 1 ? PAGE_ORDER[idx + 1] : null;

  return (
    <div className="mb-4 flex items-center gap-2">
      <button
        onClick={() => router.back()}
        className="inline-flex items-center gap-1 rounded-2xl border border-slate-200 bg-white/85 px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:bg-slate-50"
        title="Browser back"
      >
        <ChevronLeft className="h-3.5 w-3.5" /> Back
      </button>

      <div className="flex items-center gap-1 rounded-2xl border border-slate-200 bg-white/85 p-1 shadow-sm">
        {PAGE_ORDER.map((p) => {
          const active = p.href === "/" ? path === "/" : path.startsWith(p.href);
          return (
            <Link
              key={p.href}
              href={p.href}
              className={cx(
                "rounded-xl px-3 py-1 text-xs font-medium transition",
                active
                  ? "bg-slate-950 text-white"
                  : "text-slate-600 hover:bg-slate-100 hover:text-slate-950",
              )}
            >
              {p.label}
            </Link>
          );
        })}
      </div>

      <div className="ml-auto flex items-center gap-2">
        {prev && (
          <Link
            href={prev.href}
            className="inline-flex items-center gap-1 rounded-2xl border border-slate-200 bg-white/85 px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:bg-slate-50"
          >
            <ChevronLeft className="h-3.5 w-3.5" /> {prev.label}
          </Link>
        )}
        {next && (
          <Link
            href={next.href}
            className="inline-flex items-center gap-1 rounded-2xl border border-slate-200 bg-white/85 px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:bg-slate-50"
          >
            {next.label} <ChevronRight className="h-3.5 w-3.5" />
          </Link>
        )}
      </div>
    </div>
  );
}
