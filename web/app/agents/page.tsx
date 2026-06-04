"use client";

import { useEffect, useMemo, useState } from "react";
import { Bot, Play } from "lucide-react";
import { api, type AgentCatalog, type AgentMode, type AgentRunResult } from "../lib/api";
import { Badge, Card, cx } from "../components/ui";

export default function AgentLabPage() {
  const [cat, setCat] = useState<AgentCatalog | null>(null);
  const [agentId, setAgentId] = useState<string | null>(null);
  const [modeKey, setModeKey] = useState<string | null>(null);
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [claimId, setClaimId] = useState<number | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AgentRunResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.agentCatalog()
      .then((c) => {
        setCat(c);
        const a0 = c.agents[0];
        if (a0) { setAgentId(a0.id); setModeKey(a0.modes[0]?.key ?? null); }
      })
      .catch((e) => setErr(String(e)));
  }, []);

  const agent = useMemo(() => cat?.agents.find((a) => a.id === agentId) ?? null, [cat, agentId]);
  const mode = useMemo<AgentMode | null>(() => agent?.modes.find((m) => m.key === modeKey) ?? null, [agent, modeKey]);

  function pickAgent(id: string) {
    const a = cat?.agents.find((x) => x.id === id);
    setAgentId(id);
    setModeKey(a?.modes[0]?.key ?? null);
    setInputs({}); setResult(null); setClaimId(null);
  }
  function pickMode(k: string) { setModeKey(k); setInputs({}); setResult(null); }

  async function run() {
    if (!agent || !mode) return;
    setRunning(true); setResult(null); setErr(null);
    try {
      setResult(await api.agentRun({ agent: agent.id, mode: mode.key, claim_id: claimId, inputs }));
    } catch (e) {
      setResult({ status: "error", error: String(e) });
    } finally {
      setRunning(false);
    }
  }

  if (err) return <div className="rounded-3xl border border-red-200 bg-red-50 p-5 text-sm text-red-700">{err}</div>;
  if (!cat) return <div className="text-sm text-slate-500">Loading agents…</div>;

  return (
    <div className="space-y-5">
      <div className="rounded-3xl border border-slate-200 bg-white/85 px-5 py-4 shadow-sm backdrop-blur">
        <div className="flex items-center gap-2">
          <Bot className="h-5 w-5 text-slate-700" />
          <h1 className="text-lg font-semibold tracking-tight text-slate-950">Agent Lab</h1>
        </div>
        <p className="mt-1 text-sm text-slate-500">
          Run any agent in isolation. LLM agents run <span className="font-medium text-slate-700">dry</span> — real
          context + model, validated output, but no writes or events. Mimir runs <span className="font-medium text-slate-700">safe-live</span> (its
          paths are idempotent).
        </p>
      </div>

      {/* agent picker */}
      <div className="flex flex-wrap gap-2">
        {cat.agents.map((a) => (
          <button
            key={a.id}
            type="button"
            onClick={() => pickAgent(a.id)}
            className={cx(
              "rounded-2xl border px-3.5 py-2 text-left transition",
              a.id === agentId ? "border-emerald-400 bg-emerald-50 ring-2 ring-emerald-500/20" : "border-slate-200 bg-white hover:bg-slate-50",
            )}
          >
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-slate-900">{a.label}</span>
              <span className={cx(
                "rounded-full border px-1.5 py-0.5 text-[10px] font-semibold",
                a.status === "live" ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-slate-200 bg-slate-50 text-slate-500",
              )}>{a.status === "live" ? "Live" : "Planned"}</span>
            </div>
            <div className="mt-0.5 text-[11px] text-slate-400">{a.role}</div>
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
        {/* control panel */}
        <section className="lg:col-span-5">
          <Card>
            {agent && (
              <>
                <h2 className="text-base font-semibold tracking-tight text-slate-950">{agent.label}</h2>
                <p className="mt-1 text-sm leading-snug text-slate-600">{agent.what}</p>

                {/* modes */}
                <div className="mt-4 flex flex-wrap gap-1.5">
                  {agent.modes.map((m) => (
                    <button
                      key={m.key}
                      type="button"
                      onClick={() => pickMode(m.key)}
                      className={cx(
                        "rounded-xl border px-2.5 py-1 text-xs font-medium transition",
                        m.key === modeKey ? "border-slate-800 bg-slate-900 text-white" : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50",
                      )}
                    >{m.label}</button>
                  ))}
                </div>

                {mode && (
                  <div className="mt-4 space-y-3">
                    <div className="flex flex-wrap items-center gap-2 text-[11px]">
                      <Badge tone={mode.kind === "mimir" ? "green" : "blue"}>{mode.kind === "mimir" ? "safe-live" : "dry-run"}</Badge>
                      {mode.model && <span className="rounded-lg bg-slate-100 px-2 py-0.5 font-mono text-slate-500">{mode.model}</span>}
                      {mode.tier && <span className="text-slate-400">tier · {mode.tier}</span>}
                    </div>
                    {mode.note && <p className="text-[12px] text-slate-500">{mode.note}</p>}

                    {/* inputs */}
                    {mode.inputs.map((inp) => (
                      <div key={inp.name}>
                        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-slate-400">{inp.label}</label>
                        <input
                          value={inputs[inp.name] ?? ""}
                          onChange={(e) => setInputs((s) => ({ ...s, [inp.name]: e.target.value }))}
                          placeholder={inp.placeholder}
                          className="w-full rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-sm focus:border-emerald-300 focus:outline-none"
                        />
                      </div>
                    ))}

                    {/* claim picker */}
                    {mode.needs_claim && (
                      <div>
                        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-slate-400">Claim (context)</label>
                        <select
                          value={claimId ?? ""}
                          onChange={(e) => setClaimId(e.target.value ? Number(e.target.value) : null)}
                          className="w-full rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-sm focus:border-emerald-300 focus:outline-none"
                        >
                          <option value="">(latest active claim)</option>
                          {cat.claims.map((c) => <option key={c.id} value={c.id}>C{c.id} · {c.claim.slice(0, 60)}</option>)}
                        </select>
                      </div>
                    )}

                    {mode.runnable === false && (
                      <p className="text-[12px] text-amber-600">This recipe&apos;s output schema isn&apos;t resolvable yet — running may error.</p>
                    )}

                    <button
                      type="button"
                      onClick={run}
                      disabled={running}
                      className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
                    >
                      <Play className="h-4 w-4" /> {running ? "Running…" : mode.kind === "mimir" ? "Run (live)" : "Run (dry)"}
                    </button>
                    {mode.emits && <p className="text-[11px] text-slate-400">Live, this would emit: {mode.emits}</p>}
                  </div>
                )}
              </>
            )}
          </Card>
        </section>

        {/* result panel */}
        <section className="lg:col-span-7">
          <Card>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Result</div>
            {!result && !running && <p className="mt-2 text-sm text-slate-400">Pick an agent + mode and run it.</p>}
            {running && <p className="mt-2 text-sm text-slate-500">Running… (an LLM dry-run calls the model, ~5–30s).</p>}
            {result && <ResultView r={result} />}
          </Card>
        </section>
      </div>
    </div>
  );
}

function ResultView({ r }: { r: AgentRunResult }) {
  if (r.status === "error") {
    return (
      <div className="mt-2 rounded-2xl border border-red-200 bg-red-50 p-3 text-sm">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-red-500">Error</div>
        <div className="mt-1 text-red-700">{r.error}</div>
      </div>
    );
  }

  if (r.kind === "mimir") {
    const res = r.result ?? {};
    return (
      <div className="mt-3 space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge tone="green">live · {r.action}</Badge>
          {"decision" in res && <Badge tone={res.decision === "approve" ? "green" : "red"}>{String(res.decision)}</Badge>}
          {"tier" in res && res.tier != null && <span className="rounded-lg bg-slate-100 px-2 py-0.5 text-slate-600">{String(res.tier)}</span>}
          {"deduped" in res && Boolean(res.deduped) && <span className="text-slate-400">already in corpus</span>}
          {"discovered" in res && <span className="text-slate-600">{String(res.discovered)} new / {String(res.scanned)} scanned</span>}
          {"status" in res && <span className="text-slate-600">{String(res.status)}</span>}
        </div>
        {r.note && <p className="text-[12px] text-slate-500">{r.note}</p>}
        <Json obj={res} />
      </div>
    );
  }

  // LLM dry-run
  return (
    <div className="mt-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge tone="blue">dry-run</Badge>
        <Badge tone={r.valid ? "green" : "red"}>{r.valid ? "schema valid" : "invalid"}</Badge>
        {r.model && <span className="rounded-lg bg-slate-100 px-2 py-0.5 font-mono text-slate-500">{r.model}</span>}
        {r.latency_ms != null && <span className="text-slate-400">{(r.latency_ms / 1000).toFixed(1)}s</span>}
        {r.output_tokens != null && <span className="text-slate-400">{r.output_tokens} tok</span>}
      </div>
      {r.context_note && <p className="text-[12px] text-slate-500">Context: {r.context_note}</p>}
      {r.validation_error && (
        <div className="rounded-2xl border border-red-200 bg-red-50 p-2 text-[12px] text-red-700">{r.validation_error}</div>
      )}
      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400">Output</div>
        <Json obj={(r.validated ?? r.parsed ?? r.raw) as unknown} />
      </div>
      {r.would_emit && <p className="text-[11px] text-slate-400">Live, this would emit: {r.would_emit}</p>}
      {r.prompt_preview && (
        <details className="rounded-2xl border border-slate-200 bg-slate-50 p-2">
          <summary className="cursor-pointer text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            Prompt ({r.prompt_tokens} tok)
          </summary>
          <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-600">{r.prompt_preview}</pre>
        </details>
      )}
    </div>
  );
}

function Json({ obj }: { obj: unknown }) {
  const text = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  return (
    <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-words rounded-2xl bg-slate-950/95 p-3 text-[11.5px] leading-relaxed text-slate-100">{text}</pre>
  );
}
