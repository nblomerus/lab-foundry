"use client";

// Research — the traditional research arc, per direction, with the REAL documents:
//   topic (the direction itself) → literature review → research proposal →
//   experiments → finding → article.
// Left: dossiers with stage chips. Right: the selected document's full markdown.
// (Replaced the legacy market-era CommandCenter that previously lived at this route.)

import { useCallback, useEffect, useState } from "react";

interface DocRef { id: number; title: string; at: string }
interface Dossier {
  claim_id: number;
  statement: string;
  status: string;
  gate: string | null;
  confidence: number | null;
  experiments_done: number;
  finding_supported: string | null;
  finding_confidence: number | null;
  documents: Partial<Record<"lit_review" | "proposal" | "article", DocRef>>;
}
interface DocFull {
  id: number; claim_id: number; direction: string; kind: string; title: string;
  body_md: string; citations: string[]; created_at: string;
}

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`/api${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} on ${path}`);
  return (await r.json()) as T;
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

export default function ResearchPage() {
  const [dossiers, setDossiers] = useState<Dossier[]>([]);
  const [doc, setDoc] = useState<DocFull | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await jget<{ dossiers: Dossier[] }>("/research/dossiers");
      setDossiers(d.dossiers);
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
        <span className="text-[12px] text-slate-400">
          the traditional arc per direction — topic → literature review → proposal → experiments → finding → article
        </span>
      </div>
      {error && <div className="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] text-rose-600">API unreachable: {error}</div>}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[640px_1fr]">
        <div className="max-h-[80vh] space-y-2 overflow-y-auto pr-1">
          {dossiers.length === 0 && (
            <div className="rounded-xl border border-slate-200 bg-white/70 p-6 text-center text-[12px] text-slate-400">
              no dossiers yet — the arc starts when a direction passes the gate
            </div>
          )}
          {dossiers.map((d) => (
            <div key={d.claim_id} className="rounded-xl border border-slate-200 bg-white/85 p-3 shadow-sm">
              <div className="flex items-start gap-2">
                <span className="shrink-0 text-[12px] font-semibold tabular-nums text-slate-500">#{d.claim_id}</span>
                <p className="min-w-0 flex-1 text-[12.5px] font-medium leading-snug text-slate-800">{d.statement}</p>
                <span className="shrink-0 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-500">
                  {d.status}{d.gate === "approved" ? " · gated ✓" : ""}
                </span>
              </div>
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
                  return (
                    <button
                      key={s.key}
                      disabled={!ref}
                      onClick={() => ref && openDoc(ref.id)}
                      className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${CHIP[st]} ${ref ? "cursor-pointer hover:ring-1 hover:ring-violet-300" : "cursor-default"}`}
                      title={ref ? `open: ${ref.title}` : undefined}
                    >
                      {st === "done" ? "✓ " : ""}{s.label}{extra}
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
              click a ✓ stage chip to read its document — the lab’s literature reviews, proposals, and articles
            </div>
          ) : (
            <article>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-violet-500">{doc.kind.replace("_", " ")} · direction #{doc.claim_id}</div>
              <h2 className="text-[17px] font-semibold leading-snug text-slate-900">{doc.title}</h2>
              <p className="mb-3 mt-1 text-[11px] text-slate-400">{doc.direction}</p>
              <pre className="whitespace-pre-wrap rounded-lg border border-slate-100 bg-slate-50/60 p-4 font-sans text-[13px] leading-relaxed text-slate-800">
                {doc.body_md}
              </pre>
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
