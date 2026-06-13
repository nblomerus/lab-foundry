"use client";

// Research — the traditional research arc, per direction, with the REAL documents:
//   topic (the direction itself) → literature review → research proposal →
//   experiments → finding → article.
// Left: dossiers with stage chips. Right: the selected document's full markdown.
// (Replaced the legacy market-era CommandCenter that previously lived at this route.)

import { type ReactNode, useCallback, useEffect, useState } from "react";

interface DocRef { id: number; title: string; at: string; in_mimir: boolean }
interface Dossier {
  claim_id: number;
  statement: string;
  status: string;
  gate: string | null;
  stage: string | null;     // the single derived stage (direction_stage_v) — server-authoritative
  blocker: string | null;   // why it's parked, if anything (e.g. "held by adjudicator")
  confidence: number | null;
  experiments_done: number;
  finding_supported: string | null;
  finding_confidence: number | null;
  finding_realism: "real" | "builtin" | "synthetic" | null;
  documents: Partial<Record<"lit_review" | "proposal" | "article", DocRef>>;
}
interface Hypothesis { hid: string; statement: string; metric?: string; threshold?: string; dataset_plan?: string }
interface DocMeta {
  citations_resolved?: number;
  research_questions?: string[];
  hypotheses?: Hypothesis[];
  success_criteria?: string;
  abstract?: string;
}
interface DocFull {
  id: number; claim_id: number; direction: string; kind: string; title: string;
  body_md: string; citations: string[]; created_at: string;
  meta: DocMeta; in_mimir: boolean; versions: number;
}
interface DocListItem {
  id: number; claim_id: number; direction: string; kind: string; title: string;
  status: string; in_mimir: boolean; at: string;
}

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`/api${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} on ${path}`);
  return (await r.json()) as T;
}

// Inline emphasis: **bold**, `code`. Splits on the markers and alternates styling. Safe (no HTML).
function inline(text: string, keyBase: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return parts.map((p, i) => {
    if (p.startsWith("**") && p.endsWith("**")) return <strong key={`${keyBase}-${i}`} className="font-semibold text-slate-900">{p.slice(2, -2)}</strong>;
    if (p.startsWith("`") && p.endsWith("`")) return <code key={`${keyBase}-${i}`} className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[12px] text-violet-700">{p.slice(1, -1)}</code>;
    return <span key={`${keyBase}-${i}`}>{p}</span>;
  });
}

// A small, dependency-free Markdown renderer for the constrained subset the lab's documents use:
// ##/### headings, - and 1. lists, **bold**/`code`, and paragraphs. Avoids a heavyweight dep and
// dangerouslySetInnerHTML; anything unrecognized falls through as a paragraph.
function Markdown({ md }: { md: string }) {
  const lines = (md || "").replace(/\r/g, "").split("\n");
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) {
      blocks.push(
        <ul key={`ul-${blocks.length}`} className="my-2 list-disc space-y-1 pl-5 text-[13px] leading-relaxed text-slate-700">
          {list.map((it, i) => <li key={i}>{inline(it, `li-${blocks.length}-${i}`)}</li>)}
        </ul>,
      );
      list = [];
    }
  };
  lines.forEach((raw, idx) => {
    const ln = raw.trimEnd();
    if (/^#{2,6}\s/.test(ln)) {
      flush();
      const level = ln.match(/^#+/)![0].length;
      const txt = ln.replace(/^#+\s/, "");
      blocks.push(
        level <= 2
          ? <h3 key={idx} className="mb-1 mt-4 border-b border-slate-100 pb-1 text-[13px] font-semibold uppercase tracking-wide text-violet-600">{txt}</h3>
          : <h4 key={idx} className="mb-1 mt-3 text-[13px] font-semibold text-slate-800">{txt}</h4>,
      );
    } else if (/^[-*]\s/.test(ln)) {
      list.push(ln.replace(/^[-*]\s/, ""));
    } else if (/^\d+\.\s/.test(ln)) {
      list.push(ln.replace(/^\d+\.\s/, ""));
    } else if (ln.trim() === "") {
      flush();
    } else {
      flush();
      blocks.push(<p key={idx} className="my-2 text-[13px] leading-relaxed text-slate-700">{inline(ln, `p-${idx}`)}</p>);
    }
  });
  flush();
  return <div>{blocks}</div>;
}

function timeago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

const STAGES: { key: string; label: string }[] = [
  { key: "topic", label: "Topic" },
  { key: "lit_review", label: "Lit review" },
  { key: "proposal", label: "Proposal" },
  { key: "experiments", label: "Experiments" },
  { key: "finding", label: "Finding" },
  { key: "article", label: "Article" },
];

function stageState(d: Dossier, key: string): "done" | "active" | "todo" {
  switch (key) {
    case "topic":
      return "done";
    case "lit_review":
      return d.documents.lit_review ? "done" : "active";
    case "proposal":
      return d.documents.proposal ? "done" : d.documents.lit_review ? "active" : "todo";
    case "experiments":
      return d.experiments_done > 0 ? "done" : d.documents.proposal ? "active" : "todo";
    case "finding":
      return d.finding_supported ? "done" : d.experiments_done > 0 ? "active" : "todo";
    case "article":
      return d.documents.article ? "done" : d.finding_supported ? "active" : "todo";
    default:
      return "todo";
  }
}

const CHIP: Record<string, string> = {
  done: "border-emerald-200 bg-emerald-50 text-emerald-700",
  active: "border-amber-200 bg-amber-50 text-amber-700",
  todo: "border-slate-200 bg-slate-50 text-slate-400",
};

const KIND_TONE: Record<string, string> = {
  lit_review: "border-sky-200 bg-sky-50 text-sky-700",
  proposal: "border-violet-200 bg-violet-50 text-violet-700",
  article: "border-emerald-200 bg-emerald-50 text-emerald-700",
};

export default function ResearchPage() {
  const [dossiers, setDossiers] = useState<Dossier[]>([]);
  const [allDocs, setAllDocs] = useState<DocListItem[]>([]);
  const [view, setView] = useState<"dossiers" | "all">("dossiers");
  const [doc, setDoc] = useState<DocFull | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [d, a] = await Promise.all([
        jget<{ dossiers: Dossier[] }>("/research/dossiers"),
        jget<{ documents: DocListItem[] }>("/research/documents"),
      ]);
      setDossiers(d.dossiers);
      setAllDocs(a.documents);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10_000);
    return () => clearInterval(t);
  }, [refresh]);

  const openDoc = useCallback(async (id: number) => {
    try {
      setDoc(await jget<DocFull>(`/research/documents/${id}`));
    } catch {
      /* transient */
    }
  }, []);

  return (
    <main className="mx-auto max-w-[1680px] px-4 pb-10 pt-4">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold text-slate-800">Research</h1>
        <div className="inline-flex overflow-hidden rounded-lg border border-slate-200 text-[11px] font-semibold">
          <button
            onClick={() => setView("dossiers")}
            className={`px-3 py-1 ${view === "dossiers" ? "bg-violet-600 text-white" : "bg-white text-slate-500 hover:bg-slate-50"}`}
          >
            Dossiers
          </button>
          <button
            onClick={() => setView("all")}
            className={`px-3 py-1 ${view === "all" ? "bg-violet-600 text-white" : "bg-white text-slate-500 hover:bg-slate-50"}`}
          >
            All documents ({allDocs.length})
          </button>
        </div>
        <span className="text-[12px] text-slate-400">
          {view === "dossiers"
            ? "the traditional arc per direction — topic → literature review → proposal → experiments → finding → article"
            : "every literature review, proposal, and article the lab has written — newest first"}
        </span>
      </div>
      {error && <div className="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] text-rose-600">API unreachable: {error}</div>}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[640px_1fr]">
        <div className="max-h-[80vh] space-y-2 overflow-y-auto pr-1">
          {view === "all" && (
            allDocs.length === 0 ? (
              <div className="rounded-xl border border-slate-200 bg-white/70 p-6 text-center text-[12px] text-slate-400">
                no documents yet
              </div>
            ) : (
              allDocs.map((m) => (
                <button
                  key={m.id}
                  onClick={() => openDoc(m.id)}
                  className={`flex w-full items-center gap-2 rounded-lg border bg-white/85 px-3 py-2 text-left shadow-sm transition hover:ring-1 hover:ring-violet-300 ${doc?.id === m.id ? "ring-1 ring-violet-400" : "border-slate-200"}`}
                >
                  <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${KIND_TONE[m.kind] ?? "border-slate-200 bg-slate-50 text-slate-500"}`}>
                    {m.kind.replace("_", " ")}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-slate-800">{m.title}</span>
                  <span className="shrink-0 text-[10px] text-slate-400">#{m.claim_id} · {timeago(m.at)}</span>
                  <span className="shrink-0" title={m.in_mimir ? "in Mimir (queryable)" : "not yet in Mimir"}>{m.in_mimir ? "🟢" : "🟡"}</span>
                </button>
              ))
            )
          )}
          {view === "dossiers" && dossiers.length === 0 && (
            <div className="rounded-xl border border-slate-200 bg-white/70 p-6 text-center text-[12px] text-slate-400">
              no dossiers yet — the arc starts when a direction passes the gate
            </div>
          )}
          {view === "dossiers" && dossiers.map((d) => (
            <div key={d.claim_id} className="rounded-xl border border-slate-200 bg-white/85 p-3 shadow-sm">
              <div className="flex items-start gap-2">
                <span className="shrink-0 text-[12px] font-semibold tabular-nums text-slate-500">#{d.claim_id}</span>
                <p className="min-w-0 flex-1 text-[12.5px] font-medium leading-snug text-slate-800">{d.statement}</p>
                <span className="shrink-0 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-500">
                  {d.status}{d.gate === "approved" ? " · gated ✓" : ""}
                </span>
              </div>
              {d.blocker && (
                <div className="mt-1.5 inline-flex items-center gap-1 rounded-md border border-rose-200 bg-rose-50 px-2 py-0.5 text-[10px] font-semibold text-rose-600">
                  ⏸ blocked: {d.blocker}
                </div>
              )}
              {d.finding_realism && d.finding_realism !== "real" && (
                <div className="ml-1 mt-1.5 inline-flex items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700"
                  title="finding rests on synthetic/builtin data — a real-data confirmation is being driven before it can conclude">
                  ⚗ finding: {d.finding_realism} data
                </div>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                {STAGES.map((s) => {
                  const st = stageState(d, s.key);
                  const ref = (d.documents as Record<string, DocRef | undefined>)[s.key];
                  const extra =
                    s.key === "experiments" && d.experiments_done > 0
                      ? ` ${d.experiments_done}`
                      : s.key === "finding" && d.finding_supported
                        ? ` ${d.finding_supported}`
                        : "";
                  // A green dot on a document chip = the artifact is queryable in Mimir; amber = written but not yet ingested.
                  const mimir = ref ? (ref.in_mimir ? "🟢" : "🟡") : "";
                  return (
                    <button
                      key={s.key}
                      disabled={!ref}
                      onClick={() => ref && openDoc(ref.id)}
                      className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${CHIP[st]} ${ref ? "cursor-pointer hover:ring-1 hover:ring-violet-300" : "cursor-default"}`}
                      title={ref ? `${ref.in_mimir ? "in Mimir ✓ · " : "not yet in Mimir · "}open: ${ref.title}` : undefined}
                    >
                      {st === "done" ? "✓ " : ""}{s.label}{extra}{mimir && <span className="ml-1">{mimir}</span>}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>

        <div className="max-h-[80vh] overflow-y-auto rounded-xl border border-slate-200 bg-white/85 p-5 shadow-sm">
          {!doc ? (
            <div className="p-10 text-center text-[12px] text-slate-400">
              click a stage chip to read its document — the lab’s literature reviews, proposals, and articles
            </div>
          ) : (
            <article>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-violet-500">{doc.kind.replace("_", " ")} · direction #{doc.claim_id}</div>
              <h2 className="text-[17px] font-semibold leading-snug text-slate-900">{doc.title}</h2>
              <p className="mb-2 mt-1 text-[11px] text-slate-400">{doc.direction}</p>
              {/* Tracking row: freshness, rewrite count, citation grounding, and whether Mimir carries it. */}
              <div className="mb-3 flex flex-wrap items-center gap-1.5 text-[10px] font-semibold">
                <span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 text-slate-500">{timeago(doc.created_at)}</span>
                {doc.versions > 1 && <span className="rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 text-slate-500">rev {doc.versions}</span>}
                {typeof doc.meta?.citations_resolved === "number" && (
                  <span className="rounded-md border border-sky-200 bg-sky-50 px-2 py-0.5 text-sky-700">{Math.round(doc.meta.citations_resolved * 100)}% citations resolved</span>
                )}
                {doc.in_mimir ? (
                  <span className="rounded-md border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-emerald-700">🟢 in Mimir (queryable)</span>
                ) : (
                  <span className="rounded-md border border-amber-200 bg-amber-50 px-2 py-0.5 text-amber-700">🟡 ingesting…</span>
                )}
              </div>

              {/* Proposals carry structured hypotheses — surface them as a tracked checklist (the lab's plan of record). */}
              {doc.kind === "proposal" && doc.meta?.hypotheses && doc.meta.hypotheses.length > 0 && (
                <div className="mb-4 rounded-lg border border-violet-100 bg-violet-50/40 p-3">
                  <div className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-violet-600">Hypotheses ({doc.meta.hypotheses.length}) — the falsifiable plan</div>
                  <div className="space-y-2">
                    {doc.meta.hypotheses.map((h) => (
                      <div key={h.hid} className="rounded-md border border-slate-200 bg-white p-2">
                        <div className="text-[12px] leading-snug text-slate-800"><span className="font-semibold text-violet-700">{h.hid}</span> {h.statement}</div>
                        <div className="mt-1 flex flex-wrap gap-1.5 text-[10px]">
                          {h.metric && <span className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-slate-600">metric: {h.metric}</span>}
                          {h.threshold && <span className="rounded border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 text-emerald-700">decision: {h.threshold}</span>}
                          {h.dataset_plan && <span className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-slate-500">data: {h.dataset_plan}</span>}
                        </div>
                      </div>
                    ))}
                  </div>
                  {doc.meta.success_criteria && (
                    <div className="mt-2 text-[11px] text-slate-600"><span className="font-semibold text-slate-700">Success criteria:</span> {doc.meta.success_criteria}</div>
                  )}
                </div>
              )}

              {/* The document body, rendered (was raw markdown in a <pre>). */}
              <div className="rounded-lg border border-slate-100 bg-white p-4">
                <Markdown md={doc.body_md} />
              </div>

              {doc.citations?.length > 0 && (
                <div className="mt-3">
                  <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">Citations ({doc.citations.length})</div>
                  <ul className="list-disc pl-5 text-[11.5px] text-slate-600">
                    {doc.citations.map((c, i) => (
                      <li key={i}>{c}</li>
                    ))}
                  </ul>
                </div>
              )}
            </article>
          )}
        </div>
      </div>
    </main>
  );
}
