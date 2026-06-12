// Narration — the floorplan's speech bubbles: what is RUNNING WHERE and WHO is
// WAITING FOR WHAT, in plain sentences anyone can read. Composed client-side from
// the same polls the cards use (Ariadne overview + Quartermaster ledger), so a
// bubble can never disagree with the card under it. State-driven, not event-driven:
// a bubble persists exactly as long as the state it describes.

import type { AriadneOverview, QmExperiment, QmExperiments } from "../../lib/api";

export interface Bubble {
  kind: "running" | "waiting" | "reading";
  text: string;
}

function elapsed(iso?: string | null): string {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return s < 120 ? `${Math.round(s)}s` : `${Math.round(s / 60)}m`;
}

function clip(s: string | null | undefined, n: number): string {
  if (!s) return "";
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export function composeNarration(ariadne: AriadneOverview | null, qm: QmExperiments | null): Record<string, Bubble> {
  const out: Record<string, Bubble> = {};
  const ag = ariadne?.at_a_glance;
  const running: QmExperiment[] = (qm?.experiments ?? []).filter((e) => e.status === "running");
  const queued: QmExperiment[] = (qm?.experiments ?? []).filter((e) => e.status === "queued");
  const on = (m?: string | null) => m === "advisory" || m === "active";

  // Experiments — what is running WHERE (lane + container), on which direction, for how long.
  if (running.length > 0) {
    const e = running[0];
    const lane = e.requires_gpu ? "GPU" : "CPU";
    const where = e.worker ? ` · ${e.worker}` : "";
    const more = running.length > 1 ? ` (+${running.length - 1} more)` : "";
    out["experiments"] = {
      kind: "running",
      text: `#${e.id} running on ${lane}${where} (${elapsed(e.started_at)}) — “${clip(e.hypothesis, 90)}” · direction #${e.claim_id}${more}`,
    };
  } else if (queued.length > 0) {
    const e = queued[0];
    out["experiments"] = {
      kind: "waiting",
      text: `#${e.id} queued for a ${e.requires_gpu ? "GPU" : "CPU"} slot — “${clip(e.hypothesis, 80)}” · direction #${e.claim_id}`,
    };
  }

  if (ag) {
    const tasksOpen = (ag.research_tasks_pending ?? 0) + (ag.research_tasks_running ?? 0);
    const expBusy = running.length > 0 || queued.length > 0;
    const firstBusy = running[0] ?? queued[0];

    // Researchers — investigating, or naming exactly what they wait on.
    if ((ag.research_tasks_running ?? 0) > 0) {
      out["researchers"] = { kind: "running", text: `investigating ${ag.research_tasks_running} task(s) against the Library` };
    } else if ((ag.research_tasks_pending ?? 0) > 0) {
      out["researchers"] = { kind: "waiting", text: `${ag.research_tasks_pending} task(s) queued — picking up next` };
    } else if (expBusy && firstBusy) {
      out["researchers"] = { kind: "waiting", text: `waiting on experiment #${firstBusy.id} (direction #${firstBusy.claim_id}) to interpret` };
    } else if (on(ag.researcher_mode)) {
      out["researchers"] = { kind: "waiting", text: "waiting for the planner's next tasks" };
    }

    // Planner — supply state.
    if (on(ag.planner_mode) && tasksOpen === 0) {
      out["planner"] = expBusy
        ? { kind: "waiting", text: "all directions planned — waiting on experiment results" }
        : (ag.approved ?? 0) > 0
          ? { kind: "waiting", text: "approved directions all worked — waiting for the next gap or approval" }
          : { kind: "waiting", text: "waiting for gate approvals to plan against" };
    }

    // Ariadne — steering vs waiting vs exhausted.
    if ((ag.active_directions ?? 0) === 0) {
      out["ariadne"] = { kind: "waiting", text: "agenda exhausted — a fresh deliberation will fire on the cooldown" };
    } else if (expBusy && firstBusy) {
      out["ariadne"] = { kind: "waiting", text: `waiting on direction #${firstBusy.claim_id}'s evidence before steering` };
    } else if (ag.status) {
      out["ariadne"] = { kind: "reading", text: `${ag.status} — ${ag.active_directions} direction(s), ${ag.findings ?? 0} finding(s) so far` };
    }

    // Gate — slots + what it waits for.
    const approved = ag.approved ?? 0;
    const budget = ag.gate_budget ?? 0;
    if (budget > 0) {
      out["gate-promotion"] =
        approved < budget
          ? { kind: "waiting", text: `${approved}/${budget} slots used — waiting for adjudicated 'pass' candidates` }
          : { kind: "reading", text: `gate budget full (${approved}/${budget}) — directions in flight` };
    }

    // Critic — has it ever had anything to challenge?
    if (on(ag.critic_mode)) {
      out["critic"] =
        (ag.critic_verdicts ?? 0) === 0
          ? { kind: "waiting", text: "waiting for a high-signal finding to challenge" }
          : { kind: "reading", text: `${ag.critic_verdicts} verdict(s) issued — watching for the next high-signal finding` };
    }

    // Request queue — acquisitions in flight with Mimir.
    if ((ag.acquire_pending ?? 0) > 0) {
      out["request-queue"] = { kind: "running", text: `${ag.acquire_pending} acquisition(s) being adjudicated by Mimir` };
    }
  }

  return out;
}
