"use client";

// Live-data inspectors for the floorplan — Mimir/Warden, Library, per-scout, and
// the intake Gate. Lifted from the original SVG Floorplan (retired in this
// upgrade) so the React Flow rebuild reuses the exact, working /knowledge/* panels.

import { useEffect, useMemo, useState, type ReactNode } from "react";
import Link from "next/link";
import {
  Boxes, CheckCircle2, ChevronRight, Clock, Compass, Database, Download, FileCode, FileText,
  Github, Globe, Network, Search, ShieldAlert, ShieldCheck,
} from "lucide-react";
import {
  api, type AcquireRequestRow, type AriadneOverview, type CorpusHit, type DebugCosts,
  type GatePanel, type HostStats, type KnowledgeStats, type MimirPanel, type PlannerPanel,
  type QmExperiment, type QmExperimentDetail, type QmExperiments, type QueueHealth, type RecentIngest,
  type ResearcherOverview, type ScoutPanel,
} from "../../lib/api";
import type { LabFoundryEvent, Snapshot } from "../../lib/types";
import { ago } from "../../lib/format";
import { cx } from "../ui";

// Corpus document-kind a scout's sources land under (fallback tile when the
// per-scout panel is unavailable).
const SCOUT_KIND: Record<string, string> = {
  web: "web", arxiv: "paper", github: "code", dataset: "dataset", openml: "dataset",
};

// Event types the Warden cares about (Mimir inspector's live rows + heat).
export const MIMIR_EVENT_TYPES = [
  "source.discovered", "document.parsed", "document.ingested", "mimir.ingest_blocked",
  "library.sweep_requested", "library.trends", "acquire.requested", "acquire.fulfilled", "acquire.rejected",
];

export function fmtTime(iso: string): string {
  try { return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
  catch { return "—"; }
}

export function sourceKindOf(e: LabFoundryEvent): string | null {
  const src = (e.payload as { source?: { source_kind?: unknown } } | null | undefined)?.source;
  const k = src?.source_kind;
  return typeof k === "string" ? k : null;
}

export function reasonOf(e: LabFoundryEvent): string {
  const r = (e.payload as { reasons?: unknown } | null | undefined)?.reasons;
  return typeof r === "string" ? r : "blocked";
}

export const TIER_COLORS: Record<string, string> = {
  peer_reviewed: "bg-emerald-500", preprint: "bg-emerald-400", official_repo: "bg-teal-400",
  web_reputable: "bg-blue-400", web_unknown: "bg-slate-300", user_asserted: "bg-violet-300", quarantined: "bg-red-300",
};

// =========================================================================
// Inspector primitives
// =========================================================================

export function StatTile({ label, value, tone = "slate" }: { label: string; value: string | number; tone?: "slate" | "emerald" | "blue" | "violet" | "red" | "amber" }) {
  const tones: Record<string, string> = {
    slate: "bg-slate-50 text-slate-800", emerald: "bg-emerald-50 text-emerald-700", blue: "bg-blue-50 text-blue-700",
    violet: "bg-violet-50 text-violet-700", red: "bg-red-50 text-red-700", amber: "bg-amber-50 text-amber-700",
  };
  return (
    <div className={`rounded-2xl px-3 py-2 ${tones[tone]}`}>
      <div className="text-[10px] font-semibold uppercase tracking-wide opacity-70">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tabular-nums">{typeof value === "number" ? value.toLocaleString() : value}</div>
    </div>
  );
}

export function TierBars({ tiers }: { tiers: Record<string, number> }) {
  const order = ["peer_reviewed", "preprint", "official_repo", "web_reputable", "web_unknown", "user_asserted", "quarantined"];
  const total = Object.values(tiers).reduce((a, b) => a + b, 0) || 1;
  const present = order.filter((t) => tiers[t]);
  return (
    <div className="space-y-1.5">
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
        {present.map((t) => <div key={t} className={TIER_COLORS[t] ?? "bg-slate-300"} style={{ width: `${(tiers[t] / total) * 100}%` }} title={`${t}: ${tiers[t]}`} />)}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-500">
        {present.map((t) => (
          <span key={t} className="flex items-center gap-1">
            <span className={`inline-block h-2 w-2 rounded-full ${TIER_COLORS[t] ?? "bg-slate-300"}`} />
            {t.replace(/_/g, " ")} · {tiers[t].toLocaleString()}
          </span>
        ))}
      </div>
    </div>
  );
}

export function SubHead({ label }: { label: string }) {
  return <div className="mb-1.5 mt-4 text-[10px] font-semibold uppercase tracking-wider text-slate-400">{label}</div>;
}

export function EventRows({ events, empty }: { events: LabFoundryEvent[]; empty: string }) {
  if (events.length === 0) return <p className="text-sm text-slate-400">{empty}</p>;
  return (
    <ul className="space-y-1">
      {events.map((e) => (
        <li key={e.id} className="flex items-center gap-2 rounded-xl bg-slate-50 px-2.5 py-1.5 text-xs">
          <span className="w-14 shrink-0 font-mono text-slate-400">{fmtTime(e.emitted_at)}</span>
          <span className="min-w-0 flex-1 truncate font-mono font-semibold text-slate-700">{e.event_type}</span>
          {e.target_id != null && <span className="font-mono text-[10px] text-slate-400">{e.target_type}#{e.target_id}</span>}
        </li>
      ))}
    </ul>
  );
}

// --- Library corpus search (inline) -------------------------------------

export function CorpusSearch() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<CorpusHit[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = async () => {
    const query = q.trim();
    if (!query) return;
    setLoading(true); setErr(null);
    try {
      const res = await api.corpusSearch(query, 6);
      setHits(res.hits);
      if (res.status !== "ok") setErr(res.error ?? "search unavailable");
    } catch (e) { setErr(String(e)); setHits([]); }
    finally { setLoading(false); }
  };

  return (
    <div>
      <SubHead label="Search the corpus" />
      <div className="flex gap-2">
        <div className="relative min-w-0 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") run(); }}
            placeholder="e.g. mixture-of-experts routing"
            className="w-full rounded-xl border border-slate-200 bg-white py-2 pl-9 pr-3 text-sm focus:border-emerald-300 focus:outline-none"
          />
        </div>
        <button type="button" onClick={run} disabled={loading || !q.trim()}
          className="shrink-0 rounded-xl bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-50">
          {loading ? "…" : "Search"}
        </button>
      </div>
      <p className="mt-1.5 text-[11px] text-slate-400">Search across titles, abstracts, code, entities, and full text.</p>
      {err && <p className="mt-2 text-xs text-red-500">{err}</p>}
      {hits && hits.length === 0 && !err && <p className="mt-2 text-sm text-slate-400">No matches.</p>}
      {hits && hits.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {hits.map((h) => (
            <li key={`${h.document_id}-${h.score}`} className="rounded-2xl border border-slate-200 bg-slate-50 p-2.5 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-1.5">
                  <span className={`inline-block h-2 w-2 rounded-full ${TIER_COLORS[h.trust_tier] ?? "bg-slate-300"}`} />
                  <span className="text-slate-400">{h.trust_tier.replace(/_/g, " ")}</span>
                </span>
                <span className="font-mono text-slate-400">score {h.score.toFixed(2)}</span>
              </div>
              {h.source_url ? (
                <a href={h.source_url} target="_blank" rel="noreferrer" className="mt-1 block font-medium text-emerald-700 hover:underline line-clamp-2">
                  {h.title || h.source_url}
                </a>
              ) : (
                <div className="mt-1 font-medium text-slate-700 line-clamp-2">{h.title || `doc #${h.document_id}`}</div>
              )}
              <p className="mt-1 line-clamp-2 text-slate-500">{h.snippet}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// =========================================================================
// Rich Library inspector pieces
// =========================================================================

export const KIND_HEX: Record<string, string> = {
  paper: "#3b82f6", web: "#10b981", code: "#f59e0b", dataset: "#8b5cf6",
  media: "#ec4899", note: "#64748b", log: "#94a3b8",
};
export const TIER_HEX: Record<string, string> = {
  peer_reviewed: "#059669", preprint: "#10b981", official_repo: "#14b8a6",
  web_reputable: "#3b82f6", web_unknown: "#94a3b8", user_asserted: "#a78bfa", quarantined: "#f87171",
};
export const TILE_ACCENT: Record<string, string> = {
  slate: "bg-slate-100 text-slate-500", emerald: "bg-emerald-50 text-emerald-600", blue: "bg-blue-50 text-blue-600",
  violet: "bg-violet-50 text-violet-600", amber: "bg-amber-50 text-amber-600",
};

export function StatCard({ icon: Icon, label, value, sub, subGreen = false, accent = "slate" }: {
  icon: typeof FileText; label: string; value: string | number; sub?: string; subGreen?: boolean; accent?: string;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-3">
      <span className={`inline-flex rounded-lg p-1.5 ${TILE_ACCENT[accent] ?? TILE_ACCENT.slate}`}><Icon className="h-4 w-4" /></span>
      <div className="mt-2 text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-0.5 text-xl font-semibold tabular-nums text-slate-900">{typeof value === "number" ? value.toLocaleString() : value}</div>
      {sub && <div className={`mt-0.5 text-[11px] ${subGreen ? "text-emerald-600" : "text-slate-400"}`}>{sub}</div>}
    </div>
  );
}

export function Composition({ kinds }: { kinds: Record<string, number> }) {
  const entries = Object.entries(kinds).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((s, [, v]) => s + v, 0) || 1;
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap gap-x-5 gap-y-3">
        {entries.map(([k, v]) => {
          const pct = (v / total) * 100;
          const hex = KIND_HEX[k] ?? "#94a3b8";
          return (
            <div key={k} className="min-w-[84px] flex-1">
              <div className="text-[10px] font-semibold uppercase tracking-wide" style={{ color: hex }}>{k}</div>
              <div className="mt-0.5 text-sm font-semibold tabular-nums text-slate-800">
                {v.toLocaleString()} <span className="text-[11px] font-normal text-slate-400">({pct.toFixed(0)}%)</span>
              </div>
              <div className="mt-1 h-1.5 w-full rounded-full bg-slate-100">
                <div className="h-full rounded-full" style={{ width: `${Math.max(pct, 3)}%`, background: hex }} />
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-3 text-[11px] text-slate-400">Total {total.toLocaleString()} items</div>
    </div>
  );
}

export function TrustBreakdown({ tiers }: { tiers: Record<string, number> }) {
  const order = ["peer_reviewed", "preprint", "official_repo", "web_reputable", "web_unknown", "user_asserted", "quarantined"];
  const total = Object.values(tiers).reduce((a, b) => a + b, 0) || 1;
  const present = order.filter((t) => tiers[t]);
  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-slate-100">
        {present.map((t) => <div key={t} style={{ width: `${(tiers[t] / total) * 100}%`, background: TIER_HEX[t] ?? "#94a3b8" }} title={`${t}: ${tiers[t]}`} />)}
      </div>
      <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
        {present.map((t) => (
          <span key={t} className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: TIER_HEX[t] ?? "#94a3b8" }} />
            <span className="text-slate-600">{t.replace(/_/g, " ")}</span>
            <span className="text-slate-400">{tiers[t].toLocaleString()} ({((tiers[t] / total) * 100).toFixed(1)}%)</span>
          </span>
        ))}
      </div>
      <div className="mt-1.5 text-right text-[11px] text-slate-400">Total {total.toLocaleString()} items</div>
    </div>
  );
}

export function KgTile({ label, value, caption, color }: { label: string; value: string | number; caption: string; color: string }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-3">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-0.5 text-xl font-semibold tabular-nums" style={{ color }}>{typeof value === "number" ? value.toLocaleString() : value}</div>
      <div className="mt-0.5 text-[11px] text-slate-400">{caption}</div>
    </div>
  );
}

export const SOURCE_META: Record<string, { icon: typeof FileText; bg: string; fg: string }> = {
  arxiv:  { icon: FileText, bg: "#fee2e2", fg: "#dc2626" },
  github: { icon: Github,   bg: "#f1f5f9", fg: "#0f172a" },
  web:    { icon: Globe,    bg: "#ecfdf5", fg: "#059669" },
  doi:    { icon: FileText, bg: "#eef2ff", fg: "#4f46e5" },
  dataset:{ icon: Database, bg: "#f5f3ff", fg: "#7c3aed" },
  openml: { icon: Database, bg: "#f5f3ff", fg: "#7c3aed" },
  code:   { icon: FileCode, bg: "#fffbeb", fg: "#d97706" },
};

export function IngestRow({ it }: { it: RecentIngest }) {
  const src = SOURCE_META[it.source_kind] ?? SOURCE_META.web;
  const Icon = src.icon;
  const isNew = it.at ? Date.now() - new Date(it.at).getTime() < 3_600_000 : false;
  const meta = it.arxiv_id ? `arXiv:${it.arxiv_id}` : it.source_kind;
  const blocked = it.status === "blocked" || it.status === "quarantined";
  return (
    <li className="flex items-start gap-2.5 rounded-2xl border border-slate-200 bg-white p-2.5">
      <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg" style={{ background: src.bg, color: src.fg }}>
        <Icon className="h-4 w-4" />
      </span>
      <div className="min-w-0 flex-1">
        {it.source_url ? (
          <a href={it.source_url} target="_blank" rel="noreferrer" className="block truncate text-sm font-medium text-slate-800 hover:text-emerald-700">{it.title || `doc #${it.id}`}</a>
        ) : (
          <div className="truncate text-sm font-medium text-slate-800">{it.title || `doc #${it.id}`}</div>
        )}
        <div className="mt-0.5 truncate text-[11px] text-slate-400">{meta}{it.at ? ` · Ingested ${ago(it.at)}` : ""}</div>
      </div>
      {blocked
        ? <span className="shrink-0 rounded-full border border-red-200 bg-red-50 px-2 py-0.5 text-[10px] font-semibold text-red-600">Blocked</span>
        : isNew && <span className="shrink-0 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">New</span>}
    </li>
  );
}

export function LatestIngests() {
  const [data, setData] = useState<{ today: number; items: RecentIngest[] } | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () => api.recentIngests(8).then((d) => { if (!cancelled) setData({ today: d.today, items: d.items }); }).catch(() => {});
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);
  const items = (data?.items ?? []).slice(0, 5);
  return (
    <div>
      <div className="mb-1.5 mt-4 flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Latest ingests</span>
        <a href="/events" className="text-[11px] font-medium text-emerald-700 hover:underline">View all</a>
      </div>
      {items.length === 0 ? (
        <p className="text-sm text-slate-400">No ingests yet.</p>
      ) : (
        <ul className="space-y-1.5">{items.map((it) => <IngestRow key={it.id} it={it} />)}</ul>
      )}
      {data && <p className="mt-2 text-[11px] text-slate-400">Showing {items.length} of {data.today} ingests today</p>}
    </div>
  );
}

export function LibraryInspector({ knowledge, snapshot }: { knowledge: KnowledgeStats | null; snapshot: Snapshot | null }) {
  const corpus = knowledge?.corpus;
  const graph = knowledge?.graph;
  const memory = knowledge?.memory;
  const totalDocs = corpus ? Object.values(corpus.documents_by_kind).reduce((a, b) => a + b, 0) : 0;
  const embedPct = corpus && corpus.chunks > 0 ? (corpus.chunks_embedded / corpus.chunks) * 100 : 0;
  const directions = snapshot?.state?.active_claims_count ?? memory?.claims ?? 0;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2">
        <StatCard icon={FileText} label="Documents" value={totalDocs} accent="emerald"
          sub={corpus && corpus.docs_today > 0 ? `+${corpus.docs_today.toLocaleString()} today` : "in the corpus"} subGreen={!!corpus && corpus.docs_today > 0} />
        <StatCard icon={Boxes} label="Embedded chunks" value={corpus?.chunks_embedded ?? 0} accent="blue"
          sub={corpus ? `${embedPct.toFixed(embedPct >= 99.95 ? 0 : 1)}% embedded` : undefined} />
        <StatCard icon={Network} label="Graph entities" value={graph?.status === "ok" ? (graph.nodes ?? 0) : "—"} accent="violet"
          sub={graph?.status === "ok" ? `${(graph.papers ?? 0).toLocaleString()} papers · Neo4j` : "graph offline"} />
        <StatCard icon={Compass} label="Active directions" value={directions} accent="amber"
          sub={snapshot?.state?.current_phase ? `phase · ${snapshot.state.current_phase}` : "research agenda"} />
      </div>

      {corpus && Object.keys(corpus.documents_by_kind).length > 0 && (
        <div><SubHead label="Corpus composition" /><Composition kinds={corpus.documents_by_kind} /></div>
      )}

      {corpus && Object.keys(corpus.docs_by_trust_tier).length > 0 && (
        <div><SubHead label="Trust tier breakdown" /><TrustBreakdown tiers={corpus.docs_by_trust_tier} /></div>
      )}

      <div>
        <SubHead label="Knowledge graph (structured memory)" />
        <div className="grid grid-cols-2 gap-2">
          <KgTile label="KG papers" value={graph?.status === "ok" ? (graph.papers ?? 0) : "—"} caption="in graph" color="#7c3aed" />
          <KgTile label="Datasets" value={(graph?.datasets ?? corpus?.datasets) ?? 0} caption="in graph" color="#0f766e" />
          <KgTile label="Experiments" value={memory?.experiments ?? 0} caption="runs" color="#d97706" />
          <KgTile label="Claims" value={memory?.claims ?? 0} caption="research directions" color="#059669" />
        </div>
      </div>

      <CorpusSearch />
      <LatestIngests />
    </div>
  );
}

// =========================================================================
// Rich Mimir (Warden) inspector — backed by GET /knowledge/mimir
// =========================================================================

export const LADDER_LABEL: Record<string, string> = {
  official_repo: "official repo", web_reputable: "web reputable", web_unknown: "web unknown",
};
export const MIX_LABEL: Record<string, string> = { arxiv: "arXiv", web: "Web", github: "GitHub", dataset: "Dataset" };
export const MIX_HEX: Record<string, string> = { arxiv: "#3b82f6", web: "#10b981", github: "#f59e0b", dataset: "#8b5cf6" };
export const REQ_BADGE: Record<string, string> = {
  requested: "border-violet-200 bg-violet-50 text-violet-700",
  fulfilled: "border-emerald-200 bg-emerald-50 text-emerald-700",
  rejected: "border-red-200 bg-red-50 text-red-600",
};

export function FunnelStep({ label, value, last = false }: { label: string; value: number; last?: boolean }) {
  return (
    <div>
      <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-3 py-1.5">
        <span className="text-xs text-slate-600">{label}</span>
        <span className="text-sm font-semibold tabular-nums text-slate-900">{value.toLocaleString()}</span>
      </div>
      {!last && <div className="mx-auto my-0.5 h-2 w-px bg-slate-200" />}
    </div>
  );
}

export function MimirWardenPanel({ panel }: { panel: MimirPanel }) {
  const g = panel.at_a_glance;
  const ingestDelta = g.ingested_today - g.ingested_yesterday;
  const ladder = Object.entries(panel.trust_ladder).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  const mix = panel.source_mix.filter((m) => m.count > 0);

  return (
    <div className="space-y-4">
      {/* Warden banner */}
      <div className="flex items-start gap-2.5 rounded-2xl border border-emerald-200 bg-emerald-50/70 p-3">
        <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-emerald-100 text-emerald-600">
          <ShieldCheck className="h-4 w-4" />
        </span>
        <p className="text-xs leading-snug text-emerald-900">
          <span className="font-semibold">Mimir is the Warden of Knowledge.</span>{" "}
          It ingests research from across the lab, evaluates trust, and certifies reliable content into the Library — or quarantines it.
        </p>
      </div>

      {/* At a glance */}
      <div>
        <SubHead label="At a glance" />
        <div className="grid grid-cols-2 gap-2">
          <StatCard icon={ShieldCheck} label="Certified" value={g.certified} accent="emerald"
            sub={`+${g.certified_today.toLocaleString()} today`} subGreen={g.certified_today > 0} />
          <StatCard icon={ShieldAlert} label="Quarantined" value={g.quarantined} accent="amber"
            sub={`+${g.quarantined_today.toLocaleString()} today`} />
          <StatCard icon={Clock} label="Pending review" value={g.pending} accent="amber" />
          <StatCard icon={Download} label="Ingested today" value={g.ingested_today} accent="blue"
            sub={`${ingestDelta >= 0 ? "+" : ""}${ingestDelta.toLocaleString()} vs yesterday`} />
        </div>
      </div>

      {/* Trust ladder · Intake pipeline · Source mix */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <div className="lg:col-span-3">
          <SubHead label="Trust ladder" />
          {ladder.length === 0 ? (
            <p className="text-sm text-slate-400">No certified sources yet.</p>
          ) : (
            <ul className="space-y-1">
              {ladder.map(([tier, count]) => (
                <li key={tier} className="flex items-center gap-2 text-[11px]">
                  <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: TIER_HEX[tier] ?? "#94a3b8" }} />
                  <span className="flex-1 text-slate-600">{LADDER_LABEL[tier] ?? tier.replace(/_/g, " ")}</span>
                  <span className="font-semibold tabular-nums text-slate-700">{count.toLocaleString()}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="lg:col-span-3">
          <SubHead label="Intake pipeline (today)" />
          <FunnelStep label="Discovered" value={panel.pipeline_today.discovered} />
          <FunnelStep label="Parsed" value={panel.pipeline_today.parsed} />
          <FunnelStep label="Ingested" value={panel.pipeline_today.ingested} />
          <FunnelStep label="Quarantined" value={panel.pipeline_today.quarantined} last />
        </div>

        <div className="lg:col-span-3">
          <SubHead label="Source mix" />
          {mix.length === 0 ? (
            <p className="text-sm text-slate-400">No sources yet.</p>
          ) : (
            <div className="space-y-1.5">
              {mix.map((m) => {
                const hex = MIX_HEX[m.kind] ?? "#94a3b8";
                return (
                  <div key={m.kind}>
                    <div className="flex items-center justify-between text-[11px]">
                      <span className="text-slate-600">{MIX_LABEL[m.kind] ?? m.kind}</span>
                      <span className="text-slate-400">{m.count.toLocaleString()} · {m.pct}%</span>
                    </div>
                    <div className="mt-0.5 h-1.5 w-full rounded-full bg-slate-100">
                      <div className="h-full rounded-full" style={{ width: `${Math.max(m.pct, 2)}%`, background: hex }} />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Recent certifications */}
      <div>
        <SubHead label="Recent certifications" />
        {panel.recent_certifications.length === 0 ? (
          <p className="text-sm text-slate-400">No certifications in the live window.</p>
        ) : (
          <ul className="space-y-1.5">
            {panel.recent_certifications.map((c, i) => {
              const badge = c.arxiv_id ? `arXiv:${c.arxiv_id}` : c.canonical_key ?? c.source_kind;
              return (
                <li key={`${c.canonical_key ?? c.arxiv_id ?? c.title ?? "cert"}-${i}`} className="flex items-start gap-2 rounded-2xl border border-slate-200 bg-white p-2.5">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                  <div className="min-w-0 flex-1">
                    <div className="line-clamp-1 text-sm font-medium text-slate-800">{c.title ?? badge}</div>
                    <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-slate-400">
                      <span className="truncate rounded-full bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">{badge}</span>
                      <span>{ago(c.at)}</span>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* Requests */}
      <div>
        <SubHead label="Requests" />
        {panel.requests.length === 0 ? (
          <p className="text-sm text-slate-400">No acquire requests yet — agents request sources here once the research workflow is live.</p>
        ) : (
          <ul className="space-y-1.5">
            {panel.requests.map((r, i) => (
              <li key={`${r.requester}-${i}`} className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white p-2.5 text-xs">
                <span className="shrink-0 font-medium text-slate-700">{r.requester}</span>
                <span className="min-w-0 flex-1 truncate text-slate-500">{r.ask ?? "—"}</span>
                <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${REQ_BADGE[r.status] ?? "border-slate-200 bg-slate-50 text-slate-500"}`}>{r.status}</span>
                <span className="shrink-0 text-[10px] text-slate-400">{ago(r.at)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export function MimirInspector({ knowledge, events }: {
  knowledge: KnowledgeStats | null; events: LabFoundryEvent[];
}) {
  const [panel, setPanel] = useState<MimirPanel | null>(null);
  const [loading, setLoading] = useState(true);
  const corpus = knowledge?.corpus;
  const roomEvents = useMemo(() => events.filter((e) => MIMIR_EVENT_TYPES.includes(e.event_type)).slice(0, 8), [events]);
  const blocked = useMemo(() => events.filter((e) => e.event_type === "mimir.ingest_blocked").slice(0, 6), [events]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.mimirPanel()
      .then((p) => { if (!cancelled) setPanel(p); })
      .catch(() => { if (!cancelled) setPanel(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  if (panel && panel.status === "ok") return <MimirWardenPanel panel={panel} />;

  // Fallback: corpus-based tiles (also the loading view) ------------------
  return (
    <div className="space-y-3">
      {loading
        ? <p className="text-sm text-slate-400">Loading Warden stats…</p>
        : <p className="text-sm text-slate-400">Live stats unavailable.</p>}
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="Today" value={corpus?.docs_today ?? 0} tone="blue" />
        <StatTile label="Certified" value={corpus?.by_status?.certified ?? 0} tone="emerald" />
        <StatTile label="Quarantined" value={corpus?.by_status?.quarantined ?? 0} tone="red" />
      </div>
      {corpus && Object.keys(corpus.docs_by_trust_tier).length > 0 && (
        <div><SubHead label="Trust ladder" /><TierBars tiers={corpus.docs_by_trust_tier} /></div>
      )}
      <div>
        <SubHead label={`Recently blocked${blocked.length ? ` (${blocked.length})` : ""}`} />
        {blocked.length === 0 ? (
          <p className="text-sm text-slate-400">Nothing blocked in the live window.</p>
        ) : (
          <ul className="space-y-1.5">
            {blocked.map((e) => (
              <li key={e.id} className="rounded-2xl border border-red-100 bg-red-50/60 p-2 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-red-600">doc #{e.target_id}</span>
                  <span className="text-slate-400">{fmtTime(e.emitted_at)}</span>
                </div>
                <p className="mt-0.5 line-clamp-2 text-slate-600">{reasonOf(e)}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <SubHead label="Recent ingest activity" />
        <EventRows events={roomEvents} empty="Quiet right now — Mimir sweeps on a schedule." />
      </div>
    </div>
  );
}

// =========================================================================
// Scout inspector — durable recent findings, backed by GET /knowledge/scout
// =========================================================================

export type ScoutItem = ScoutPanel["recent"][number];

export function arxivAbsUrl(id: string): string {
  return `https://arxiv.org/abs/${id}`;
}

export function ScoutRow({ kind, it }: { kind: string; it: ScoutItem }) {
  const at = <span className="ml-2 shrink-0 text-[10px] text-slate-400">{ago(it.at)}</span>;

  if (kind === "arxiv") {
    return (
      <li className="rounded-2xl border border-slate-200 bg-white p-2 text-xs">
        <div className="flex items-start justify-between gap-2">
          <div className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{it.title ?? "Untitled paper"}</div>
          {at}
        </div>
        {it.arxiv_id && (
          <a href={arxivAbsUrl(it.arxiv_id)} target="_blank" rel="noreferrer"
            className="mt-1 inline-block rounded-full bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-emerald-700 hover:underline">
            arXiv:{it.arxiv_id}
          </a>
        )}
        {it.snippet && <p className="mt-1 line-clamp-2 text-slate-500">{it.snippet}</p>}
      </li>
    );
  }

  if (kind === "web") {
    return (
      <li className="rounded-2xl border border-slate-200 bg-white p-2 text-xs">
        <div className="flex items-start justify-between gap-2">
          {it.source_url ? (
            <a href={it.source_url} target="_blank" rel="noreferrer" className="line-clamp-2 min-w-0 flex-1 font-medium text-emerald-700 hover:underline">
              {it.title || it.source_url}
            </a>
          ) : (
            <div className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{it.title || "Untitled"}</div>
          )}
          {at}
        </div>
        {it.source_url && <div className="mt-0.5 truncate text-[10px] text-slate-400">{it.source_url}</div>}
        {it.snippet && <p className="mt-1 line-clamp-2 text-slate-500">{it.snippet}</p>}
      </li>
    );
  }

  if (kind === "github") {
    const repo = it.canonical_key || it.title || "repository";
    const href = it.source_url || (it.canonical_key ? `https://github.com/${it.canonical_key}` : null);
    return (
      <li className="rounded-2xl border border-slate-200 bg-white p-2 text-xs">
        <div className="flex items-start justify-between gap-2">
          {href ? (
            <a href={href} target="_blank" rel="noreferrer" className="line-clamp-2 min-w-0 flex-1 font-medium text-emerald-700 hover:underline">{repo}</a>
          ) : (
            <div className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{repo}</div>
          )}
          {at}
        </div>
        {it.snippet && <p className="mt-1 line-clamp-2 text-slate-500">{it.snippet}</p>}
      </li>
    );
  }

  if (kind === "openml") {
    // canonical_key looks like "openml:123" → link to https://www.openml.org/d/123
    const id = it.canonical_key || it.title || "dataset";
    const numId = it.canonical_key?.startsWith("openml:") ? it.canonical_key.slice("openml:".length) : null;
    const href = it.source_url || (numId ? `https://www.openml.org/d/${numId}` : null);
    return (
      <li className="rounded-2xl border border-slate-200 bg-white p-2 text-xs">
        <div className="flex items-start justify-between gap-2">
          {href ? (
            <a href={href} target="_blank" rel="noreferrer" className="line-clamp-2 min-w-0 flex-1 font-medium text-emerald-700 hover:underline">{it.title || id}</a>
          ) : (
            <div className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{it.title || id}</div>
          )}
          {at}
        </div>
        <div className="mt-1 inline-block rounded-full bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">{id}</div>
        {it.snippet && <p className="mt-1 line-clamp-2 text-slate-500">{it.snippet}</p>}
      </li>
    );
  }

  // dataset
  const id = it.canonical_key || it.title || "dataset";
  return (
    <li className="rounded-2xl border border-slate-200 bg-white p-2 text-xs">
      <div className="flex items-start justify-between gap-2">
        {it.source_url ? (
          <a href={it.source_url} target="_blank" rel="noreferrer" className="line-clamp-2 min-w-0 flex-1 font-medium text-emerald-700 hover:underline">{id}</a>
        ) : (
          <div className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{id}</div>
        )}
        {at}
      </div>
      {it.snippet && <p className="mt-1 line-clamp-2 text-slate-500">{it.snippet}</p>}
    </li>
  );
}

export function ScoutPanelView({ kind, panel }: { kind: string; panel: ScoutPanel }) {
  const topics = panel.last_searched?.topics ?? [];
  const recentTitle = (kind === "dataset" || kind === "openml") ? "Top datasets · recently surfaced" : "Recently found";
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="In corpus" value={panel.in_corpus} tone="violet" />
        <StatTile label="Added today" value={panel.added_today} tone="emerald" />
        <StatTile label="Last searched" value={panel.last_searched?.at ? ago(panel.last_searched.at) : "—"} tone="slate" />
      </div>

      {topics.length > 0 && (
        <div>
          <SubHead label="Last searched" />
          <div className="flex flex-wrap gap-1.5">
            {topics.map((t, i) => (
              <span key={`${t}-${i}`} className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">{t}</span>
            ))}
          </div>
        </div>
      )}

      <div>
        <SubHead label={recentTitle} />
        {panel.recent.length === 0 ? (
          <p className="text-sm text-slate-400">No items yet — this scout hasn&apos;t surfaced anything into the corpus.</p>
        ) : (
          <ul className="space-y-1.5">
            {panel.recent.map((it, i) => (
              <ScoutRow key={`${it.canonical_key ?? it.arxiv_id ?? it.source_url ?? it.title ?? "item"}-${i}`} kind={kind} it={it} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export function ScoutInspector({ kind, corpus }: { kind: string; corpus: KnowledgeStats["corpus"] | undefined }) {
  const [panel, setPanel] = useState<ScoutPanel | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setPanel(null);
    api.scoutPanel(kind)
      .then((p) => { if (!cancelled) setPanel(p); })
      .catch(() => { if (!cancelled) setPanel(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [kind]);

  if (panel && panel.status === "ok") return <ScoutPanelView kind={kind} panel={panel} />;

  // Fallback: corpus-based count tiles (also the loading view) -------------
  return (
    <div className="space-y-3">
      {loading
        ? <p className="text-sm text-slate-400">Loading…</p>
        : <p className="text-sm text-slate-400">Live stats unavailable.</p>}
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="In corpus" value={corpus?.documents_by_kind?.[SCOUT_KIND[kind as keyof typeof SCOUT_KIND]] ?? 0} tone="violet" />
        <StatTile label="Added today" value="—" tone="emerald" />
        <StatTile label="Last searched" value="—" tone="slate" />
      </div>
    </div>
  );
}

// =========================================================================
// The Gate inspector — the main entrance; what's admitted vs turned away,
// with reasons. Backed by GET /knowledge/gate.
// =========================================================================

export const GATE_BADGE: Record<string, string> = {
  trust: "border-amber-200 bg-amber-50 text-amber-700",
  quality: "border-violet-200 bg-violet-50 text-violet-700",
};

export function GateInspector({ scope, title, onClose }: { scope: string; title: string; onClose: () => void }) {
  const [panel, setPanel] = useState<GatePanel | null>(null);
  const [loading, setLoading] = useState(true);
  const scoped = scope !== "all";

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setPanel(null);
    api.gatePanel(scope === "all" ? undefined : scope)
      .then((p) => { if (!cancelled) setPanel(p); })
      .catch(() => { if (!cancelled) setPanel(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [scope]);

  const header = (
    <div className="mb-3 flex items-start justify-between gap-2">
      <div>
        <div className="flex items-center gap-2">
          <h3 className="text-lg font-semibold tracking-tight text-slate-950">{title}</h3>
          <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold text-emerald-700">Live</span>
        </div>
        <div className="mt-0.5 text-xs text-slate-500">
          {scoped ? "This scout's sources clear trust + quality here" : "Every source clears trust + quality here"}
        </div>
      </div>
      <button type="button" onClick={onClose} className="rounded-xl border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50">Close</button>
    </div>
  );

  if (loading) {
    return <div>{header}<p className="text-sm text-slate-400">Loading gate stats…</p></div>;
  }
  if (!panel || panel.status !== "ok") {
    return <div>{header}<p className="text-sm text-slate-400">Gate stats unavailable.</p></div>;
  }

  const t = panel.today;
  return (
    <div>
      {header}

      <p className="mb-3 text-sm leading-snug text-slate-700">
        Two gates guard the Library — <span className="font-semibold text-amber-700">TRUST</span> (source credibility) and{" "}
        <span className="font-semibold text-violet-700">QUALITY</span> (real, substantial content). Both must pass.
      </p>

      <SubHead label="At a glance" />
      <div className={`grid gap-2 ${scoped ? "grid-cols-3" : "grid-cols-2"}`}>
        <StatTile label="Admitted" value={t.admitted} tone="emerald" />
        <StatTile label="Blocked · trust" value={t.blocked_trust} tone="amber" />
        <StatTile label="Rejected · quality" value={t.rejected_quality} tone="slate" />
        <StatTile label="Quarantined" value={panel.quarantined} tone="red" />
        {scoped && <StatTile label="In corpus" value={panel.in_corpus} tone="violet" />}
      </div>
      <div className="mt-2 text-[11px] text-slate-400">Discovered today · {t.discovered.toLocaleString()}</div>

      <SubHead label="Turned away (why)" />
      {panel.turned_away.length === 0 ? (
        <p className="text-sm text-slate-400">Nothing turned away in the recent window.</p>
      ) : (
        <ul className="space-y-1.5">
          {panel.turned_away.map((r, i) => {
            const label = r.title || r.url || r.source_kind || "source";
            const linkable = !!r.url && (r.source_kind === "web" || r.source_kind === "github" || r.source_kind === "dataset");
            return (
              <li key={`${r.url ?? r.title ?? r.source_kind ?? "ta"}-${i}`} className="rounded-2xl border border-slate-200 bg-white p-2.5 text-xs">
                <div className="flex items-start justify-between gap-2">
                  <span className="flex min-w-0 flex-1 items-start gap-2">
                    <span className={`mt-0.5 shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold ${GATE_BADGE[r.gate] ?? GATE_BADGE.quality}`}>{r.gate}</span>
                    {linkable && r.url ? (
                      <a href={r.url} target="_blank" rel="noreferrer" className="line-clamp-2 min-w-0 flex-1 font-medium text-emerald-700 hover:underline">{label}</a>
                    ) : (
                      <span className="line-clamp-2 min-w-0 flex-1 font-medium text-slate-800">{label}</span>
                    )}
                  </span>
                  <span className="shrink-0 text-[10px] text-slate-400">{ago(r.at)}</span>
                </div>
                {r.source_kind && <div className="mt-0.5 text-[10px] uppercase tracking-wide text-slate-400">{r.source_kind}</div>}
                <p className="mt-1 line-clamp-2 text-slate-500">{r.reason}</p>
              </li>
            );
          })}
        </ul>
      )}

      <SubHead label="Recently admitted" />
      {panel.admitted.length === 0 ? (
        <p className="text-sm text-slate-400">Nothing admitted in the recent window.</p>
      ) : (
        <ul className="space-y-1.5">
          {panel.admitted.map((a, i) => {
            const badge = a.arxiv_id ? `arXiv:${a.arxiv_id}` : a.canonical_key ?? a.source_kind;
            return (
              <li key={`${a.canonical_key ?? a.arxiv_id ?? a.title ?? "adm"}-${i}`} className="flex items-start gap-2 rounded-2xl border border-slate-200 bg-white p-2.5 text-xs">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                <div className="min-w-0 flex-1">
                  <div className="line-clamp-1 text-sm font-medium text-slate-800">{a.title ?? badge}</div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-slate-400">
                    {a.trust_tier && (
                      <span className="flex items-center gap-1">
                        <span className={`inline-block h-2 w-2 rounded-full ${TIER_COLORS[a.trust_tier] ?? "bg-slate-300"}`} />
                        {a.trust_tier.replace(/_/g, " ")}
                      </span>
                    )}
                    <span className="truncate rounded-full bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">{badge}</span>
                    <span>{ago(a.at)}</span>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// --- Ariadne (PI) inspector — her live agenda --------------------------
const ARIA_MODE_TONE: Record<string, string> = {
  active: "bg-emerald-100 text-emerald-700", advisory: "bg-blue-100 text-blue-700",
  shadow: "bg-amber-100 text-amber-700", off: "bg-slate-100 text-slate-500",
};
const ARIA_GATE_TONE: Record<string, string> = {
  approved: "text-emerald-700", held: "text-amber-700", rejected: "text-red-600", pending: "text-slate-400",
};

export function AriadneInspector({ ariadne }: { ariadne: AriadneOverview | null }) {
  if (!ariadne) return <p className="text-sm text-slate-500">Loading Ariadne…</p>;
  const g = ariadne.at_a_glance;
  return (
    <div className="space-y-4 text-sm">
      <div className="flex items-center gap-2">
        <span className={cx("rounded-full px-2 py-0.5 text-[11px] font-semibold", ARIA_MODE_TONE[ariadne.mode] ?? "bg-slate-100 text-slate-500")}>{ariadne.mode}</span>
        <span className="text-slate-500">{g.status}</span>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="Directions" value={g.active_directions} />
        <StatTile label="Approved" value={`${g.approved}/${g.gate_budget}`} tone="emerald" />
        <StatTile label="Lessons" value={g.lessons} />
      </div>
      {ariadne.mission && (
        <div>
          <SubHead label="Mission" />
          <p className="mt-1 leading-snug text-slate-700">{ariadne.mission.statement}</p>
        </div>
      )}
      <div>
        <SubHead label="Directions · gate · composite" />
        <div className="mt-1.5 space-y-1.5">
          {ariadne.directions.map((d) => (
            <div key={d.id} className="flex items-center gap-2">
              <span className={cx("w-16 shrink-0 text-[11px] font-semibold", ARIA_GATE_TONE[d.gate] ?? "text-slate-400")}>{d.gate}</span>
              <span className="w-8 shrink-0 font-mono text-[11px] text-slate-400">{d.composite ?? "—"}</span>
              <span className="truncate text-slate-700">{d.title}</span>
            </div>
          ))}
          {ariadne.directions.length === 0 && <p className="text-slate-400">No directions framed yet.</p>}
        </div>
      </div>
      {g.focus.length > 0 && (
        <div>
          <SubHead label="Focus · hot & emerging" />
          <div className="mt-1 flex flex-wrap gap-1">
            {g.focus.map((f) => (<span key={f} className="rounded-full bg-blue-50 px-2 py-0.5 text-[11px] text-blue-700">{f}</span>))}
          </div>
        </div>
      )}
      <Link href="/ariadne" className="inline-block text-[12px] font-medium text-violet-700 hover:underline">Open Ariadne dashboard →</Link>
    </div>
  );
}

// --- Request Queue inspector — recent acquire asks + resolution --------
const ACQ_OUTCOME_TONE: Record<string, string> = {
  fulfilled: "text-emerald-700", already_have: "text-blue-700", rejected: "text-red-600",
  rate_limited: "text-amber-700", pending: "text-slate-400",
};

// How long the oldest unanswered ask may wait before the queue counts as "backing up". Most
// acquires resolve in seconds (already_have) to ~1-2 min (fetch + ingest); past this, Mimir is
// slow or the harness is down and the queue is draining slower than it fills.
const QUEUE_LAG_THRESHOLD_S = 180;

function fmtDur(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function QueueHealthBanner({ h }: { h: QueueHealth }) {
  const age = h.oldest_pending_age_seconds;
  const drained = h.pending === 0;
  // "Backing up" means the queue is GROWING — asks arrive faster than Mimir resolves
  // them — not merely "deep". Acquire backpressure intentionally holds the queue near
  // its cap, so a healthy draining queue still sits at ~the cap with a minutes-old tail;
  // that's "flowing", not stuck. Gate on inflow > outflow (1h), with the oldest-wait
  // threshold as a secondary guard so a brief blip doesn't trip it.
  const backingUp =
    !drained && h.requested_1h > h.resolved_1h && age != null && age >= QUEUE_LAG_THRESHOLD_S;
  const dot = drained ? "bg-emerald-500" : backingUp ? "bg-amber-500" : "bg-sky-500";
  const label = drained
    ? "Drained — no asks waiting"
    : backingUp
      ? "Backing up — asks arriving faster than Mimir resolves"
      : "Flowing — asks in flight";
  return (
    <div className={cx("rounded-xl border p-2.5", backingUp ? "border-amber-200 bg-amber-50/40" : "border-slate-100")}>
      <div className="flex items-center gap-1.5 text-[11px] font-semibold text-slate-600">
        <span className={cx("inline-block h-2 w-2 rounded-full", dot, !drained && "pulse-dot")} />
        {label}
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2">
        <StatTile label="In queue" value={h.pending} tone={backingUp ? "amber" : drained ? "emerald" : "blue"} />
        <StatTile label="Oldest wait" value={age != null ? fmtDur(age) : "—"} tone={backingUp ? "amber" : "slate"} />
        <StatTile label="Resolved / h" value={h.resolved_1h} tone={h.resolved_1h > 0 ? "emerald" : "slate"} />
      </div>
      <p className="mt-1.5 text-[10px] text-slate-400">Last hour: {h.requested_1h} asked · {h.resolved_1h} resolved</p>
    </div>
  );
}

export function QueueInspector() {
  const [data, setData] = useState<{ requests: AcquireRequestRow[]; counts: Record<string, number>; health?: QueueHealth } | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () => api.ariadneRequests(15).then((d) => { if (!cancelled) setData(d); }).catch(() => {});
    load();
    const id = setInterval(load, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);
  if (!data) return <p className="text-sm text-slate-500">Loading requests…</p>;
  return (
    <div className="space-y-3 text-sm">
      <p className="leading-snug text-slate-500">Agents (PI / Researcher / Novelty) ask Mimir for specific evidence. Mimir resolves → dedupes → trust-gates the ingest.</p>
      {data.health && <QueueHealthBanner h={data.health} />}
      {Object.keys(data.counts).length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(data.counts).map(([k, n]) => (
            <span key={k} className={cx("rounded-full bg-slate-50 px-2 py-0.5 text-[11px] font-medium", ACQ_OUTCOME_TONE[k] ?? "text-slate-500")}>{k} · {n}</span>
          ))}
        </div>
      )}
      {data.requests.length === 0 ? (
        <p className="text-slate-400">No acquire requests yet — agents pull evidence here once they need a source the Library lacks.</p>
      ) : (
        <div className="space-y-1.5">
          {data.requests.map((r, i) => (
            <div key={i} className="rounded-xl border border-slate-100 p-2">
              <div className="flex items-center gap-2">
                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold text-slate-500">{r.requester}</span>
                <span className={cx("text-[11px] font-semibold", ACQ_OUTCOME_TONE[r.outcome] ?? "text-slate-500")}>{r.outcome}</span>
                <span className="ml-auto text-[10px] text-slate-400">{r.at ? ago(r.at) : ""}</span>
              </div>
              <div className="mt-0.5 truncate text-slate-700">{r.subject}</div>
              {r.reason && <div className="truncate text-[11px] text-slate-400">{r.reason}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --- Planner inspector — research tasks it produced + its backlog ---------
const TASK_STATUS_TONE: Record<string, string> = {
  pending: "text-amber-700", running: "text-blue-700", completed: "text-emerald-700",
  failed: "text-red-600", halted: "text-red-600", cancelled: "text-slate-400",
};

export function PlannerInspector() {
  const [data, setData] = useState<PlannerPanel | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () => api.ariadnePlanner().then((d) => { if (!cancelled) setData(d); }).catch(() => {});
    load();
    const id = setInterval(load, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);
  if (!data) return <p className="text-sm text-slate-500">Loading planner…</p>;
  const pending = data.by_status.pending ?? 0;
  return (
    <div className="space-y-3 text-sm">
      <p className="leading-snug text-slate-500">
        Turns Ariadne&apos;s <span className="font-medium">approved</span> directions into concrete,
        falsifiable research tasks (<span className="font-mono text-[11px]">task.created</span> → the Researchers).
      </p>
      <div className="flex items-center gap-2">
        <span className={cx("rounded-full px-2 py-0.5 text-[11px] font-semibold", ARIA_MODE_TONE[data.mode] ?? "bg-slate-100 text-slate-500")}>{data.mode}</span>
        {data.last_plan_at && <span className="text-[11px] text-slate-400">last planned {ago(data.last_plan_at)}</span>}
      </div>
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="Tasks" value={data.tasks_total} />
        <StatTile label="Pending" value={pending} tone={pending > 0 ? "amber" : "slate"} />
        <StatTile label="Awaiting plan" value={data.awaiting_plan} tone={data.awaiting_plan > 0 ? "blue" : "emerald"} />
      </div>
      {data.awaiting_plan > 0 && (
        <p className="-mt-1 text-[11px] text-slate-400">
          {data.awaiting_plan} approved direction{data.awaiting_plan === 1 ? "" : "s"} still awaiting a plan — the planner runs them on the next tick.
        </p>
      )}
      <div>
        <SubHead label="Research tasks" />
        {data.tasks.length === 0 ? (
          <p className="text-slate-400">No tasks yet — they appear once Ariadne approves a direction and the planner runs.</p>
        ) : (
          <div className="space-y-1.5">
            {data.tasks.map((t) => (
              <div key={t.id} className="rounded-xl border border-slate-100 p-2">
                <div className="flex items-center gap-2">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold text-slate-500">{t.task_type}</span>
                  <span className={cx("text-[11px] font-semibold", TASK_STATUS_TONE[t.status] ?? "text-slate-500")}>{t.status}</span>
                  <span className="ml-auto text-[10px] text-slate-400">{t.at ? ago(t.at) : ""}</span>
                </div>
                <div className="mt-0.5 line-clamp-2 text-slate-700">{t.description}</div>
                {t.direction && <div className="mt-0.5 truncate text-[11px] text-slate-400">← {t.direction}</div>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// --- Researcher inspector — grounded findings + dispositions --------------
export const DISPOSITION_TONE: Record<string, string> = {
  supported: "text-emerald-700", contradicted: "text-red-600",
  corpus_exhausted: "text-amber-700", thin_corpus: "text-blue-700",
  needs_experiment: "text-violet-700", inconclusive: "text-slate-500",
};

export function ResearcherInspector() {
  const [data, setData] = useState<ResearcherOverview | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () => api.researcherOverview(20).then((d) => { if (!cancelled) setData(d); }).catch(() => {});
    load();
    const id = setInterval(load, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);
  if (!data) return <p className="text-sm text-slate-500">Loading researcher…</p>;
  const running = data.by_status.running ?? 0;
  const findings = Object.values(data.by_disposition).reduce((a, b) => a + b, 0);
  return (
    <div className="space-y-3 text-sm">
      <p className="leading-snug text-slate-500">
        Executes the planner&apos;s tasks against the certified Library — grounded findings that steer each
        direction (supports / contradicts / blocked), and self-healing acquires when the corpus is thin.
      </p>
      <div className="flex items-center gap-2">
        <span className={cx("rounded-full px-2 py-0.5 text-[11px] font-semibold", ARIA_MODE_TONE[data.mode] ?? "bg-slate-100 text-slate-500")}>{data.mode}</span>
        {running > 0 && <span className="text-[11px] text-amber-600">{running} investigating…</span>}
      </div>
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="Tasks" value={data.tasks_total} />
        <StatTile label="Findings" value={findings} tone={findings > 0 ? "emerald" : "slate"} />
        <StatTile label="Acquires 24h" value={data.acquire.fired_24h} tone={data.acquire.fired_24h > 0 ? "blue" : "slate"} />
      </div>
      {Object.keys(data.by_disposition).length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(data.by_disposition).map(([k, n]) => (
            <span key={k} className={cx("rounded-full bg-slate-50 px-2 py-0.5 text-[11px] font-medium", DISPOSITION_TONE[k] ?? "text-slate-500")}>{k} · {n}</span>
          ))}
        </div>
      )}
      <div>
        <SubHead label="Recent findings" />
        {data.tasks.length === 0 ? (
          <p className="text-slate-400">No findings yet — they appear as the researcher works the planner&apos;s tasks.</p>
        ) : (
          <div className="space-y-1.5">
            {data.tasks.slice(0, 8).map((t) => (
              <div key={t.id} className="rounded-xl border border-slate-100 p-2">
                <div className="flex items-center gap-2">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold text-slate-500">{t.task_type}</span>
                  <span className={cx("text-[11px] font-semibold", DISPOSITION_TONE[t.finding?.disposition ?? ""] ?? "text-slate-400")}>{t.finding?.disposition ?? t.status}</span>
                  <span className="ml-auto text-[10px] text-slate-400">{t.at ? ago(t.at) : ""}</span>
                </div>
                {t.finding?.summary && <div className="mt-0.5 line-clamp-2 text-slate-700">{t.finding.summary}</div>}
                {t.direction && <div className="mt-0.5 truncate text-[11px] text-slate-400">← {t.direction}</div>}
              </div>
            ))}
          </div>
        )}
      </div>
      <Link href="/researchers" className="inline-block text-[12px] font-medium text-violet-700 hover:underline">Open Researchers page →</Link>
    </div>
  );
}

// --- Ops / Quartermaster inspector — host, GPUs, cost, recent decisions ----
export function OpsInspector({ host, costs, mimir }: { host: HostStats | null; costs: DebugCosts | null; mimir: MimirPanel | null }) {
  const gpus = costs?.power?.gpus ?? [];
  const spend = costs?.deepseek?.today_cost_usd ?? null;
  const projected = costs?.power?.projected_usd_per_day ?? null;
  const certs = (mimir?.recent_certifications ?? []).slice(0, 8);
  const pct = (v?: number) => (v != null ? `${Math.round(v)}%` : "—");
  return (
    <div className="space-y-4 text-sm">
      <div className="grid grid-cols-3 gap-2">
        <StatTile label="CPU" value={pct(host?.cpu_percent)} />
        <StatTile label="Memory" value={pct(host?.memory_percent)} />
        <StatTile label="Disk" value={pct(host?.disk_percent)} />
      </div>
      {host?.memory_used_gb != null && host?.memory_total_gb != null && (
        <p className="-mt-2 text-[11px] text-slate-400">RAM {host.memory_used_gb.toFixed(0)} / {host.memory_total_gb.toFixed(0)} GB</p>
      )}
      <div>
        <SubHead label="GPUs" />
        {gpus.length === 0 ? (
          <p className="text-slate-400">No GPU telemetry.</p>
        ) : (
          <div className="space-y-1.5">
            {gpus.map((g) => (
              <div key={g.index}>
                <div className="flex items-center justify-between text-[11px]">
                  <span className="truncate text-slate-600">{g.name}</span>
                  <span className="shrink-0 font-mono text-slate-700">{Math.round(g.util)}% · {Math.round(g.watts)}W</span>
                </div>
                <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
                  <div className="h-full rounded-full bg-emerald-500" style={{ width: `${Math.min(100, g.util)}%` }} />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      <div>
        <SubHead label="Cost · 24h" />
        <div className="grid grid-cols-2 gap-2">
          <StatTile label="API spend" value={spend != null ? `$${spend.toFixed(2)}` : "—"} tone="emerald" />
          <StatTile label="Power (proj)" value={projected != null ? `$${projected.toFixed(2)}/d` : "—"} />
        </div>
      </div>
      <QmExperimentsPanel />
      <div>
        <SubHead label="Recent certifications" />
        {certs.length === 0 ? (
          <p className="text-slate-400">None in the window.</p>
        ) : (
          <ul className="space-y-1">
            {certs.map((c, i) => (
              <li key={c.canonical_key ?? c.arxiv_id ?? `${i}`} className="flex items-start gap-1.5 text-xs text-slate-600">
                <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-500" />
                <span className="line-clamp-1">{c.title ?? c.arxiv_id ?? c.canonical_key ?? "a source"}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

const EXP_TONE: Record<string, string> = {
  running: "text-emerald-600",
  queued: "text-amber-600",
  completed: "text-slate-500",
  failed: "text-rose-600",
  killed: "text-rose-600",
};

function DetailBlock({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-[9px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      {children}
    </div>
  );
}

function ExperimentDetailView({ d }: { d: QmExperimentDetail }) {
  const prov = d.provenance ?? {};
  const pre = "mt-0.5 max-h-44 overflow-auto rounded bg-slate-900/90 p-2 font-mono text-[10px] leading-snug text-slate-100 whitespace-pre-wrap break-words";
  return (
    <div className="mt-1.5 space-y-2 border-t border-slate-200 pt-1.5">
      {d.dataset_plan && (
        <DetailBlock label="Dataset — how it was assembled">
          <p className="mt-0.5 text-[11px] text-slate-600">{d.dataset_plan}</p>
        </DetailBlock>
      )}
      {d.researcher_notes && (
        <DetailBlock label="Researcher note">
          <p className="mt-0.5 whitespace-pre-wrap text-[11px] text-slate-700">{d.researcher_notes}</p>
        </DetailBlock>
      )}
      {d.interpretation && (
        <DetailBlock label="Interpretation">
          <p className="mt-0.5 whitespace-pre-wrap text-[11px] text-slate-700">{d.interpretation}</p>
        </DetailBlock>
      )}
      {d.result != null && (
        <DetailBlock label="Result">
          <pre className={pre}>{JSON.stringify(d.result, null, 2)}</pre>
        </DetailBlock>
      )}
      {d.error && (
        <DetailBlock label="Error">
          <pre className={cx(pre, "text-rose-200")}>{d.error}</pre>
        </DetailBlock>
      )}
      {d.code && (
        <DetailBlock label="Code that ran">
          <pre className={pre}>{d.code}</pre>
        </DetailBlock>
      )}
      <DetailBlock label="Provenance (reproducibility)">
        <div className="mt-0.5 space-y-0.5 font-mono text-[10px] text-slate-500">
          <div>image: {String(prov.image ?? "—")}</div>
          {prov.image_digest != null && <div className="break-all">digest: {String(prov.image_digest)}</div>}
          <div>seed: {String(prov.seed ?? "—")} · code_hash: {String(prov.code_hash ?? "—")}</div>
          {d.duration_s != null && <div>duration: {d.duration_s}s {d.worker ? `· ${d.worker}` : ""}</div>}
        </div>
      </DetailBlock>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px]">
        {d.ingested_doc_id != null && <span className="text-emerald-600">→ Library note #{d.ingested_doc_id}</span>}
        {Array.isArray(d.dataset_refs) && d.dataset_refs.length > 0 && (
          <span className="text-emerald-600">→ dataset card captured</span>
        )}
      </div>
    </div>
  );
}

function ExperimentRow({ e, onKill }: { e: QmExperiment; onKill: (id: number) => void }) {
  const live = e.status === "running" || e.status === "queued";
  const budget = e.wall_clock_budget_s != null ? `${Math.round(e.wall_clock_budget_s / 60)}m` : null;
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<QmExperimentDetail | null>(null);
  const [loading, setLoading] = useState(false);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && detail == null && !loading) {
      setLoading(true);
      api.qmExperimentDetail(e.id)
        .then(setDetail)
        .catch(() => setDetail(null))
        .finally(() => setLoading(false));
    }
  };

  return (
    <li className="rounded-md border border-slate-100 bg-slate-50/60 px-2 py-1.5">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <button onClick={toggle} className="flex items-center gap-1.5 font-mono hover:opacity-70" title="Show what ran">
          <ChevronRight className={cx("h-3 w-3 text-slate-400 transition-transform", open && "rotate-90")} />
          <span className={cx("font-semibold uppercase", EXP_TONE[e.status] ?? "text-slate-500")}>{e.status}</span>
          <span className="text-slate-400">#{e.id}</span>
          {e.requires_gpu && <span className="rounded bg-violet-100 px-1 text-[10px] font-semibold text-violet-700">GPU</span>}
        </button>
        <span className="flex items-center gap-2 text-slate-400">
          {budget && <span>{budget}</span>}
          {e.iterations != null && <span>{e.iterations} it</span>}
          {live && (
            <button onClick={() => onKill(e.id)} className="text-rose-500 hover:text-rose-700" title="Kill experiment">
              <ShieldAlert className="h-3.5 w-3.5" />
            </button>
          )}
        </span>
      </div>
      {e.hypothesis && <p className="mt-0.5 line-clamp-2 cursor-pointer text-[11px] text-slate-600" onClick={toggle}>{e.hypothesis}</p>}
      {e.kill_reason && <p className="mt-0.5 text-[10px] text-rose-500">killed: {e.kill_reason}</p>}
      {!e.kill_reason && e.error && !open && <p className="mt-0.5 line-clamp-1 text-[10px] text-rose-500">{e.error}</p>}
      {open && (loading ? <p className="mt-1 text-[10px] text-slate-400">Loading…</p> : detail ? <ExperimentDetailView d={detail} /> : <p className="mt-1 text-[10px] text-slate-400">Unavailable.</p>)}
    </li>
  );
}

function QmExperimentsPanel() {
  const [data, setData] = useState<QmExperiments | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = () => api.qmExperiments(20)
      .then((d) => { if (!cancelled) setData(d); })
      .catch(() => { if (!cancelled) setData(null); })
      .finally(() => { if (!cancelled) setLoaded(true); });
    load();
    const t = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  const onKill = (id: number) => {
    api.qmKillExperiment(id).then(() => api.qmExperiments(20).then(setData).catch(() => {})).catch(() => {});
  };

  const rows = (data?.experiments ?? []).slice(0, 10);
  const mode = data?.mode ?? "off";
  return (
    <div>
      <div className="flex items-center justify-between">
        <SubHead label="Experiments" />
        <span className={cx("text-[10px] font-semibold uppercase", mode === "active" ? "text-emerald-600" : "text-slate-400")}>{mode}</span>
      </div>
      <div className="mb-1.5 grid grid-cols-2 gap-2">
        <StatTile label="Running" value={data?.running ?? 0} tone={data?.running ? "emerald" : undefined} />
        <StatTile label="Queued" value={data?.queued ?? 0} tone={data?.queued ? "blue" : undefined} />
      </div>
      {!loaded ? (
        <p className="text-slate-400">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-slate-400">No experiments yet.</p>
      ) : (
        <ul className="space-y-1">
          {rows.map((e) => <ExperimentRow key={e.id} e={e} onKill={onKill} />)}
        </ul>
      )}
    </div>
  );
}
