"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BrainCircuit, Eye, Layers3, Target, TerminalSquare, Play } from "lucide-react";
import { cx } from "./ui";

const NAV = [
  { href: "/",       icon: Target,         label: "Command" },
  { href: "/theses", icon: Layers3,        label: "Theses" },
  { href: "/events", icon: TerminalSquare, label: "Events" },
  { href: "/org",    icon: Eye,            label: "Org" },
];

export function SiteNav() {
  const path = usePathname();
  return (
    <aside className="fixed inset-y-0 left-0 hidden w-20 border-r border-slate-200 bg-white/70 p-3 backdrop-blur xl:block">
      <div className="flex h-full flex-col items-center justify-between">
        <div className="space-y-4">
          <Link
            href="/"
            className="flex h-12 w-12 items-center justify-center rounded-3xl bg-slate-950 text-white shadow-sm"
            aria-label="Boardroom"
          >
            <BrainCircuit className="h-6 w-6" />
          </Link>
          <div className="space-y-2">
            {NAV.map((item) => {
              const active = path === item.href || (item.href !== "/" && path.startsWith(item.href));
              const Icon = item.icon;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  title={item.label}
                  className={cx(
                    "flex h-12 w-12 items-center justify-center rounded-2xl transition",
                    active
                      ? "bg-slate-950 text-white"
                      : "text-slate-500 hover:bg-slate-100 hover:text-slate-950",
                  )}
                >
                  <Icon className="h-5 w-5" />
                </Link>
              );
            })}
          </div>
        </div>
        <div
          className="flex h-12 w-12 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-700"
          title="Autonomous loop running"
        >
          <Play className="h-5 w-5" />
        </div>
      </div>
    </aside>
  );
}
