// Small shared formatters for the floorplan dashboard. (Consolidates the
// ago()/fmtTime() helpers that were duplicated across components.)

export function ago(iso?: string | null): string {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function fmtClock(d: Date): string {
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function fmtDate(d: Date): string {
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

/** Compact magnitudes: 32_430 -> "32.4k", 1_780_505 -> "1.78M". */
export function compact(n: number | null | undefined): string {
  if (n == null) return "—";
  const abs = Math.abs(n);
  if (abs < 1000) return String(n);
  if (abs < 1_000_000) return `${(n / 1000).toFixed(abs < 10_000 ? 1 : 0)}k`.replace(".0k", "k");
  if (abs < 1_000_000_000) return `${(n / 1_000_000).toFixed(2)}M`.replace(".00M", "M");
  return `${(n / 1_000_000_000).toFixed(2)}B`;
}
