"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, ArrowRight, CheckCircle2 } from "lucide-react";
import { api, type TraceJourney } from "../../../lib/api";
import { Badge, Card, cx } from "../../../components/ui";

type Tone = "amber" | "green" | "red" | "blue" | "default";
const KIND_TONE: Record<string, Tone> = {
  scout: "blue",
  discovered: "default",
  parse: "default",
  certify: "green",
  ingest: "green",
  rejected: "red",
  blocked: "amber",
  event: "default",
};
const KIND_DOT: Record<string, string> = {
  scout: "bg-blue-500",
  discovered: "bg-slate-400",
  parse: "bg-slate-400",
  certify: "bg-green-500",
  ingest: "bg-green-500",
  rejected: "bg-red-500",
  blocked: "bg-amber-500",
  event: "bg-slate-300",
};
const OUTCOME_TONE: Record<string, Tone> = {
  ingested: "green",
  rejected: "red",
  blocked: "amber",
  in_library: "blue",
  pending: "default",
};

function fmtTime(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export default function JourneyPage({ params }: { params: Promise<{ ref: string[] }> }) {
  const segs = use(params).ref;
  const ref = (Array.isArray(segs) ? segs.map(decodeURIComponent).join("/") : segs) ?? "";
  const [data, setData] = useState<TraceJourney | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.traceJourney(ref).then(setData).catch(() => setData(null)).finally(() => setLoading(false));
  }, [ref]);

  if (loading) return <div className="text-sm text-slate-500">Loading journey…</div>;
  if (!data?.subject) {
    return (
      <div>
        <div className="text-sm text-slate-500">No interaction found for &ldquo;{ref}&rdquo;.</div>
        <Link href="/trace/journeys" className="mt-4 inline-flex items-center gap-2 text-sm text-blue-700 hover:underline">
          <ArrowLeft className="h-4 w-4" /> Browse journeys
        </Link>
      </div>
    );
  }

  const s = data.subject;
  return (
    <div className="space-y-6">
      <div>
        <Link href="/trace/journeys" className="mb-2 inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-950">
          <ArrowLeft className="h-3 w-3" /> All journeys
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight text-slate-950">{s.title}</h1>
        <p className="mt-1 text-sm text-slate-500">
          <span className="font-mono">{s.source_kind ?? "?"}</span> · key{" "}
          <span className="font-mono">{s.canonical_key}</span>
          {s.doc_id != null && <span> · doc #{s.doc_id}</span>}
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Badge tone={OUTCOME_TONE[s.outcome] ?? "default"}>{s.outcome}</Badge>
          <span className="text-sm text-slate-500">{s.outcome_reason}</span>
          {s.trust_tier && <Badge tone="blue">{s.trust_tier}</Badge>}
          {s.queryable && <Badge tone="green">queryable</Badge>}
        </div>
      </div>

      <Card>
        <div className="mb-4 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Full event chain — start to finish
        </div>
        <ol className="relative ml-2 border-l border-slate-200">
          {data.steps.map((step, i) => {
            const payloadStr = step.payload ? JSON.stringify(step.payload, null, 2) : null;
            return (
              <li key={i} className="mb-6 ml-6">
                <span
                  className={cx(
                    "absolute -left-[7px] mt-1.5 h-3.5 w-3.5 rounded-full ring-4 ring-white",
                    KIND_DOT[step.kind] ?? "bg-slate-300",
                  )}
                />
                <div className="flex flex-wrap items-center gap-2">
                  <time className="font-mono text-xs text-slate-400">{fmtTime(step.at)}</time>
                  <Badge tone={KIND_TONE[step.kind] ?? "default"}>{step.label}</Badge>
                  {step.status && step.status !== "consumed" && (
                    <span className="font-mono text-[11px] text-slate-400">{step.status}</span>
                  )}
                  {step.session_id ? (
                    <Link
                      href={`/trace/${step.session_id}`}
                      className="inline-flex items-center gap-1 text-xs font-medium text-blue-700 hover:underline"
                    >
                      step DAG <ArrowRight className="h-3 w-3" />
                    </Link>
                  ) : null}
                </div>
                {step.detail && <div className="mt-1 text-sm text-slate-600">{step.detail}</div>}
                {payloadStr && (
                  <details className="mt-1.5 group">
                    <summary className="cursor-pointer list-none text-xs text-slate-400 hover:text-slate-700">
                      <span className="group-open:hidden">▸ payload</span>
                      <span className="hidden group-open:inline">▾ payload</span>
                    </summary>
                    <pre className="mt-1 overflow-x-auto rounded-xl bg-slate-50 p-3 text-[11px] leading-relaxed text-slate-700">
                      {payloadStr}
                    </pre>
                  </details>
                )}
              </li>
            );
          })}
          {s.queryable && (
            <li className="ml-6">
              <span className="absolute -left-[7px] mt-1.5 h-3.5 w-3.5 rounded-full bg-green-600 ring-4 ring-white" />
              <div className="flex items-center gap-2 text-sm font-medium text-green-700">
                <CheckCircle2 className="h-4 w-4" /> In the Library — retrievable via corpus_search
              </div>
            </li>
          )}
        </ol>
      </Card>
    </div>
  );
}
