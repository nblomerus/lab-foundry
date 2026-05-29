"use client";

import { useState } from "react";
import { Sparkles, Send, Loader } from "lucide-react";
import type { Snapshot, QueryResponse } from "../lib/types";
import { cx } from "./ui";

// Context-aware starter questions. Adapts to what the org is actually doing so
// the prompts feel alive rather than canned.
function suggestionsFor(snap: Snapshot): string[] {
  const out: string[] = [];
  if (snap.active_claims.length > 0) {
    const top = snap.active_claims[0];
    out.push(`Why is C${top.id} ranked where it is, and what would disprove it?`);
  }
  if (snap.dissent.length > 0) {
    out.push("Which hypotheses are weakest right now?");
  }
  if (snap.stats.failed_runs_today > 0) {
    out.push("What's failing today and why?");
  }
  out.push("What changed since yesterday?");
  out.push("Who is working and on what?");
  out.push("Are we ready to leave exploration?");
  return out.slice(0, 4);
}

function renderAnswer(text: string): string {
  return text
    .replace(/\n/g, "<br />")
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/_(.*?)_/g, "<em>$1</em>");
}

export function AskPanel({ snap }: { snap: Snapshot }) {
  const [query, setQuery] = useState("");
  const [response, setResponse] = useState<QueryResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const suggestions = suggestionsFor(snap);

  const run = async (q: string) => {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q.trim() }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Query failed (${res.status})`);
      }
      setResponse(await res.json());
      setQuery("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
      setResponse(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="rounded-3xl bg-slate-950 p-4 text-white">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <Sparkles className="h-4 w-4 text-emerald-300" />
        Ask the Command Center
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          run(query);
        }}
        className="flex gap-2"
      >
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Ask what we're doing, who's working, what's blocked…"
          disabled={loading}
          className="flex-1 rounded-2xl border border-white/10 bg-white/10 px-3 py-2 text-sm text-white placeholder-slate-400 focus:border-emerald-300/50 focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={loading || !query.trim()}
          className={cx(
            "rounded-2xl px-3 py-2 transition",
            loading || !query.trim()
              ? "bg-white/10 text-slate-500"
              : "bg-emerald-400 text-slate-950 hover:bg-emerald-300",
          )}
        >
          {loading ? <Loader className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
        </button>
      </form>

      {/* Answer / error / suggestions */}
      {error ? (
        <div className="mt-3 rounded-2xl border border-red-400/30 bg-red-500/10 p-3 text-sm text-red-200">
          {error}
          <div className="mt-1 text-xs text-red-300/70">
            The query endpoint may not be wired up yet (POST /api/query).
          </div>
        </div>
      ) : response ? (
        <div className="mt-3 space-y-3">
          <div
            className="rounded-2xl border border-white/10 bg-white/10 p-3 text-sm leading-6 text-slate-100"
            dangerouslySetInnerHTML={{ __html: renderAnswer(response.answer) }}
          />
          {response.sources?.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {response.sources.slice(0, 6).map((s, i) => (
                <span
                  key={i}
                  className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-xs text-slate-300"
                >
                  {s.type} #{s.id}
                </span>
              ))}
            </div>
          )}
          {response.follow_up_queries?.length > 0 && (
            <div className="grid gap-1.5">
              {response.follow_up_queries.slice(0, 3).map((fq, i) => (
                <button
                  key={i}
                  onClick={() => run(fq)}
                  className="rounded-xl bg-white/10 px-3 py-2 text-left text-xs text-slate-200 hover:bg-white/15"
                >
                  {fq}
                </button>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="mt-3 grid gap-2 text-xs text-slate-300">
          {suggestions.map((s) => (
            <button
              key={s}
              onClick={() => run(s)}
              disabled={loading}
              className="rounded-xl bg-white/10 px-3 py-2 text-left hover:bg-white/15 disabled:opacity-50"
            >
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
