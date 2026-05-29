"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Bug, RefreshCw, Zap, DollarSign, GitBranch } from "lucide-react";
import { api, type DebugResponse, type DebugAgentRun, type DebugCosts } from "../lib/api";
import { Badge, Card, SectionTitle, cx } from "../components/ui";

const TIER_TONE: Record<string, "blue" | "amber" | "green" | "red" | "default"> = {
  reasoning: "red", workhorse: "amber", fast: "blue", code: "green",
};

function fmtMs(ms: number | null) {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}
function ago(iso: string | null) {
  if (!iso) return "";
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export default function DebugPage() {
  const [data, setData] = useState<DebugResponse | null>(null);
  const [status, setStatus] = useState<string>("");
  const [itype, setItype] = useState<string>("");
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [auto, setAuto] = useState(true);
  const [costs, setCosts] = useState<DebugCosts | null>(null);

  const load = useCallback(() => {
    api.debugAgentRuns({ limit: 150, status: status || undefined, invocation_type: itype || undefined })
      .then(setData).catch(() => {});
  }, [status, itype]);

  useEffect(() => {
    load();
    if (!auto) return;
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load, auto]);

  useEffect(() => {
    const loadCosts = () => api.debugCosts().then(setCosts).catch(() => {});
    loadCosts();
    const id = setInterval(loadCosts, 10000);
    return () => clearInterval(id);
  }, []);

  if (!data) return <div className="text-sm text-slate-500">Loading…</div>;

  function toggle(id: number) {
    setOpen((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  }

  return (
    <div className="space-y-6">
      <Card>
        <SectionTitle
          icon={Bug}
          title="Debug — live agent runs"
          subtitle="Every model call the running company makes, newest first: what each agent produced, what it cost, and why it failed. Read-only."
        />
        <div className="flex flex-wrap items-center gap-2">
          <FilterChip label="all" active={status === ""} onClick={() => setStatus("")} />
          {Object.entries(data.facets.statuses).map(([s, n]) => (
            <FilterChip key={s} label={`${s} (${n})`} active={status === s} onClick={() => setStatus(s)}
              tone={s === "failed" ? "red" : s === "completed" ? "green" : "default"} />
          ))}
          <select
            value={itype}
            onChange={(e) => setItype(e.target.value)}
            className="ml-2 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs shadow-sm focus:outline-none"
          >
            <option value="">all tasks</option>
            {data.facets.invocation_types.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <button
            onClick={() => setAuto((a) => !a)}
            className={cx("ml-auto inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium",
              auto ? "border-emerald-300 bg-emerald-50 text-emerald-700" : "border-slate-200 bg-white text-slate-500")}
          >
            <RefreshCw className={cx("h-3.5 w-3.5", auto && "animate-spin")} /> {auto ? "live" : "paused"}
          </button>
        </div>
      </Card>

      <CostPanel costs={costs} />

      <div className="space-y-2">
        {data.runs.map((r) => (
          <RunRow key={r.id} r={r} open={open.has(r.id)} onToggle={() => toggle(r.id)} />
        ))}
        {data.runs.length === 0 && (
          <Card><div className="text-sm text-slate-500">No runs match this filter.</div></Card>
        )}
      </div>
    </div>
  );
}

function FilterChip({ label, active, onClick, tone = "default" }: {
  label: string; active: boolean; onClick: () => void; tone?: "default" | "red" | "green";
}) {
  return (
    <button
      onClick={onClick}
      className={cx("rounded-full border px-3 py-1 text-xs font-medium transition",
        active ? "border-slate-900 bg-slate-900 text-white"
          : tone === "red" ? "border-red-200 bg-white text-red-600 hover:border-red-300"
          : tone === "green" ? "border-emerald-200 bg-white text-emerald-700 hover:border-emerald-300"
          : "border-slate-200 bg-white text-slate-600 hover:border-slate-400")}
    >
      {label}
    </button>
  );
}

function RunRow({ r, open, onToggle }: { r: DebugAgentRun; open: boolean; onToggle: () => void }) {
  const failed = r.status === "failed";
  // Only researcher.* runs get a per-task tree, and only if we know the task_id.
  const showTreeLink = r.task_id != null && r.invocation_type.startsWith("researcher.");
  return (
    <Card className={cx("p-3", failed && "border-red-200")}>
      <div className="flex items-center gap-3">
        <button onClick={onToggle} className="flex flex-1 min-w-0 items-center gap-3 text-left">
          <span className="font-mono text-xs text-slate-400">#{r.id}</span>
          <Badge tone={TIER_TONE[r.tier] ?? "default"}>{r.tier}</Badge>
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-800">{r.invocation_type}</span>
          <span className="hidden truncate text-xs text-slate-400 sm:block" style={{ maxWidth: 180 }}>{r.model_name}</span>
          <Badge tone={failed ? "red" : r.status === "completed" ? "green" : "default"}>{r.status}</Badge>
          <span className="w-14 text-right text-xs text-slate-500">{fmtMs(r.latency_ms)}</span>
          <span className="hidden w-16 text-right text-xs text-slate-400 md:block">{ago(r.started_at)}</span>
        </button>
        {showTreeLink && (
          <Link
            href={`/debug/task/${r.task_id}`}
            className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-medium text-slate-600 hover:border-slate-400"
          >
            <GitBranch className="h-3 w-3" /> tree
          </Link>
        )}
      </div>
      {open && (
        <div className="mt-3 border-t border-slate-100 pt-3">
          {failed ? (
            <pre className="overflow-auto rounded-2xl bg-red-50 p-3 text-xs text-red-700">{r.error || "(no error text)"}</pre>
          ) : (
            <pre className="max-h-96 overflow-auto rounded-2xl bg-slate-50 p-3 text-xs leading-relaxed text-slate-800">
              {r.output_summary || "(no output recorded)"}
            </pre>
          )}
          <div className="mt-2 text-xs text-slate-400">
            in {r.input_tokens ?? "—"} tok · out {r.output_tokens ?? "—"} tok · {r.model_name}
          </div>
        </div>
      )}
    </Card>
  );
}

function CostPanel({ costs }: { costs: DebugCosts | null }) {
  if (!costs) return null;
  const ds = costs.deepseek;
  const pw = costs.power;
  // Prefer source-of-truth (balance delta); fall back to token estimate.
  const spentToday = ds.spent.spent_today_usd ?? ds.today_cost_usd;
  const fromSource = ds.spent.spent_today_usd !== null;
  const ratio = spentToday > 0 ? pw.projected_usd_per_day / spentToday : null;
  return (
    <Card>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="rounded-2xl border border-slate-100 p-4">
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            <DollarSign className="h-3.5 w-3.5" /> DeepSeek API (paid)
          </div>
          {ds.balance ? (
            <>
              <div className="text-3xl font-semibold tracking-tight">${ds.balance.total.toFixed(2)}</div>
              <div className="text-sm text-slate-500">balance remaining · {ds.balance.currency}</div>
              <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-500">
                <div>
                  spent today: <span className="font-medium text-slate-700">${spentToday.toFixed(4)}</span>
                  <span className="ml-1 text-slate-400">{fromSource ? "(source)" : "(est.)"}</span>
                </div>
                {ds.spent.spent_tracked_usd !== null && (
                  <div>tracked: <span className="font-medium text-slate-700">${ds.spent.spent_tracked_usd.toFixed(4)}</span></div>
                )}
                {ds.days[0] && <div>{ds.days[0].calls} reasoning calls today</div>}
              </div>
            </>
          ) : (
            <div className="text-sm text-slate-400">balance unavailable</div>
          )}
        </div>
        <div className="rounded-2xl border border-slate-100 p-4">
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            <Zap className="h-3.5 w-3.5" /> GPU electricity
          </div>
          <div className="text-3xl font-semibold tracking-tight">
            ~${pw.projected_usd_per_day.toFixed(2)}<span className="text-base font-normal text-slate-400">/day</span>
          </div>
          <div className="text-sm text-slate-500">{pw.total_watts} W now · ${pw.rate_usd_per_kwh}/kWh</div>
          <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-400">
            {pw.gpus.map((g) => (
              <span key={g.index}>GPU{g.index}: {g.watts.toFixed(0)}W ({g.util.toFixed(0)}%)</span>
            ))}
          </div>
        </div>
      </div>
      <div className="mt-3 text-xs text-slate-500">
        {ratio !== null ? (
          <>Local GPU power is running <span className="font-semibold text-slate-700">~{Math.round(ratio)}×</span> the DeepSeek API spend today — the &ldquo;free&rdquo; local models are the expensive part.</>
        ) : (
          <>Balance is live from DeepSeek; spend is derived from its drop (DeepSeek has no usage-history API). Accrues as reasoning verdicts fire.</>
        )}
      </div>
    </Card>
  );
}
