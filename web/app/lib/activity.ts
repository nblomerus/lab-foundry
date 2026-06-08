// Humanize raw bus events into friendly Live-Activity lines. Returns null for
// noise (document.parsed, session.*, heartbeats) so the feed stays meaningful.

import { CheckCircle2, Compass, Database, FileText, Github, Globe, MessagesSquare, Microscope, Search, ShieldAlert, Sparkles, TrendingUp, type LucideIcon } from "lucide-react";
import type { Accent } from "../components/ui";
import type { LabFoundryEvent } from "./types";

export interface ActivityLine {
  id: number;
  icon: LucideIcon;
  tone: Accent;
  text: string;
  at: string;
}

const KIND_LABEL: Record<string, string> = {
  arxiv: "arXiv", web: "Web", github: "GitHub", dataset: "Dataset", openml: "OpenML", paper: "arXiv", code: "GitHub",
};
const KIND_ICON: Record<string, LucideIcon> = {
  arxiv: FileText, paper: FileText, web: Globe, github: Github, code: Github, dataset: Database, openml: Database,
};

function asObj(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
}
function srcKind(e: LabFoundryEvent): string | null {
  const src = asObj(e.payload).source;
  const k = asObj(src).source_kind ?? asObj(e.payload).source_kind;
  return typeof k === "string" ? k : null;
}
function title(e: LabFoundryEvent): string | null {
  const p = asObj(e.payload);
  const t = p.title ?? asObj(p.source).title;
  return typeof t === "string" && t.trim() ? t.trim() : null;
}
function clip(s: string, n = 60): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
function cap(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

export function humanizeEvent(e: LabFoundryEvent): ActivityLine | null {
  const base = { id: e.id, at: e.emitted_at };
  const kind = srcKind(e);
  const kindLabel = kind ? KIND_LABEL[kind] ?? kind : null;
  const t = title(e);
  switch (e.event_type) {
    case "source.discovered": {
      const scout = kindLabel ? `${kindLabel} scout` : "A scout";
      return {
        ...base,
        icon: (kind && KIND_ICON[kind]) || Compass,
        tone: "info",
        text: t ? `${scout} surfaced “${clip(t)}”` : `${scout} surfaced a new source`,
      };
    }
    case "document.ingested":
      return { ...base, icon: CheckCircle2, tone: "live", text: t ? `Mimir certified “${clip(t)}”` : "Mimir certified a document" };
    case "mimir.ingest_blocked": {
      const reason = asObj(e.payload).reasons;
      return {
        ...base,
        icon: ShieldAlert,
        tone: "warn",
        text: typeof reason === "string" ? `Mimir quarantined a source — ${clip(reason, 46)}` : "Mimir quarantined a source",
      };
    }
    case "library.ingest_rejected":
      return { ...base, icon: ShieldAlert, tone: "warn", text: "Gate turned away a low-quality source" };
    case "library.trends": {
      const topics = asObj(e.payload).topics;
      const list = Array.isArray(topics) ? topics.slice(0, 3).join(", ") : null;
      return { ...base, icon: Sparkles, tone: "info", text: list ? `Library trends: ${clip(list)}` : "Library trends refreshed" };
    }
    case "acquire.requested":
      return { ...base, icon: Search, tone: "info", text: "A source acquisition was requested" };
    case "acquire.fulfilled":
      return { ...base, icon: CheckCircle2, tone: "live", text: "A source acquisition was fulfilled" };
    case "mimir.ask": {
      const p = asObj(e.payload);
      const who = typeof p.asker === "string" ? cap(p.asker) : "An agent";
      const q = typeof p.question === "string" ? p.question : null;
      return { ...base, icon: MessagesSquare, tone: "info", text: q ? `${who} asked Mimir: “${clip(q, 64)}”` : `${who} asked Mimir a question` };
    }
    case "mimir.answered": {
      const p = asObj(e.payload);
      const who = typeof p.asker === "string" ? p.asker : "the agent";
      const gaps = Array.isArray(p.gaps) ? p.gaps.length : 0;
      const cites = typeof p.citations === "number" ? p.citations : 0;
      const bits = [cites ? `${cites} cited` : null, gaps ? `${gaps} gap${gaps === 1 ? "" : "s"} flagged` : null].filter(Boolean).join(", ");
      return { ...base, icon: Sparkles, tone: "live", text: bits ? `Mimir answered ${who} — ${bits}` : `Mimir answered ${who}` };
    }
    case "task.completed":
      return { ...base, icon: Microscope, tone: "live", text: "A researcher finished investigating a task" };
    case "claim.confidence_changed": {
      const p = asObj(e.payload);
      const to = typeof p.to === "number" ? p.to : null;
      const from = typeof p.from === "number" ? p.from : null;
      const move = from != null && to != null ? ` (${from.toFixed(2)} → ${to.toFixed(2)})` : "";
      return { ...base, icon: TrendingUp, tone: "info", text: `Research steered a direction${move}` };
    }
    default:
      return null;
  }
}
