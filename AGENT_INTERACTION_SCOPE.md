# Agent-to-Agent Interaction — Design

## Problem

Today the lab is a **fixed assembly line**. The dispatcher routes each event
type to exactly **one** registered handler (`boardroom/harness/main.py`), so:

- An agent can't say *"Critic, look at this"* or *"Researcher, dig deeper on X."*
- Interactions are only the canonical edges (task → researcher → findings →
  evaluation/critic → claims → PI → phase).
- There's no way for an agent to **request help** from another agent on demand.

We want agents to **interact with each other when needed** — a network, not a
line — while keeping the system legible, bounded, and safe.

> **Scope correction (see `AGENT_OPERATING_MODEL.md`).** This document governs
> **Plane 2 — delegation only** (agent → agent requests). Reading/writing the
> knowledge substrate (RAG corpus, Neo4j graph, memory) is **Plane 1** — a
> universal tool every agent may use with no allow-list, depth limit, or
> cooldown. The allow-list below is for delegation, not for knowledge access.

## Model: directed requests over the existing event bus

Keep the event-bus architecture; add **two generic events** that carry a target
and a typed payload. No new transport, no agent-to-agent sockets.

```
agent.request   { from, to, intent, payload, parent_run_id, depth, budget }
agent.reply     { from, to, request_id, result, status }
```

- **`from` / `to`** — role names (`researcher`, `critic`, `pi`, `evaluation`,
  `planner`, `adjudicator`). `to` is the target division.
- **`intent`** — a small verb vocab: `investigate`, `challenge`, `verify`,
  `summarise`, `re-audit`, `prioritise`.
- **`payload`** — intent-specific (e.g. `{claim_id}`, `{question}`, `{finding_id}`).
- **`depth`** — how many hops deep this request chain is (loop guard).
- **`budget`** — token/cost ceiling carried down the chain.

### Routing

One new handler, `handle_agent_request`, registered for `agent.request`. It:
1. Validates `to` is an allowed target for `from` (see allow-list).
2. Checks guardrails (depth, budget, cooldown, dedupe).
3. Builds the target agent's recipe with the payload and invokes it via the
   existing curator + router (same path every agent already uses).
4. Optionally emits `agent.reply` (for request/response) or just lets the
   target's normal outputs flow (fire-and-forget).

This reuses the dispatcher's single-handler-per-event rule — the *fan-out by
target* happens inside the one handler, not by registering many handlers.

### Allow-list (who may call whom) — peer-to-peer, agreed

Delegation is **peer-to-peer** (not PI-only), but still an explicit matrix so it
stays legible and can't turn into runaway chatter. This is the agreed set
(mirrors the per-agent spec in `AGENT_OPERATING_MODEL.md`):

| From → To | intent | why |
|---|---|---|
| Critic → Researcher | `investigate` | "I need evidence on X to settle a challenge" |
| Evaluation → Researcher | `re-source` | "this finding is thin — re-source it" |
| Researcher → Evaluation | `verify` | "is this finding grounded enough?" |
| PI → Researcher | `investigate` | "explore this gap" |
| PI → Critic | `challenge` | "stress-test C14 before I commit" (incl. novelty consult: `challenge {mode:'novelty'}`) |
| Planner → PI | `prioritise` | "portfolio is stale, which directions?" |
| Adjudicator → Critic | `verify` | "confirm before phase advance" |
| Novelty → Critic | `challenge` | gate escape: "does this paper actually subsume the claim?" |
| Reviewer → Researcher | `investigate` | gate escape: "degraded quorum — re-source this claim" |

Anything not in the matrix is rejected and logged. New pairs are added here
explicitly as real needs appear.

**Notes (per `REAL_LAB_OPERATING_MODEL.md`):**
- The **PI→Critic novelty consult reuses `challenge`** with `{mode:'novelty'}` — no
  separate pair (allow-list is a union-of-intents per pair).
- The last two rows are the **gate's internal escapes** (Plane-2 inside a
  synchronous review stage); they exist only under `GATE_LOOP=v2`.
- The **novelty/quality gate is per-claim at promotion** (`claim.promotion_candidate`),
  not per-finding.

## Guardrails (non-negotiable)

- **Depth cap** — `depth ≤ 3`; a request that would exceed it is dropped + logged.
- **Cycle/dedupe** — per `(from,to,intent,payload-hash)` cooldown so A↔B can't
  ping-pong; identical in-flight requests are coalesced.
- **Budget propagation** — `budget` decremented down the chain; at zero, the
  request is refused (Quartermaster owns the ceiling).
- **No self-calls**; `from ≠ to`.
- **Timeouts** — replies expected within N seconds; otherwise the chain ends.
- **Full provenance** — every request/reply is an event row + `agent_runs`
  session, so it shows up in Trace and can be replayed.

## Backend changes

1. **Migration** `010_agent_requests.sql` — `agent_requests` table (id, from,
   to, intent, payload, status, parent_id, depth, run_id, created_at).
2. **Events** — emit `agent.request` / `agent.reply` via the state client
   (mirrors existing event emission).
3. **Handler** `handlers/agent_request.py` — `handle_agent_request` with the
   allow-list + guardrails above; registered in `harness/main.py`.
4. **Agent outputs** — give recipes an optional structured `requests: []` field
   so an agent can *ask* during its normal run (the handler emits them). Start
   with the Critic (`investigate`) and PI (`challenge`) as the first two.
5. **Snapshot/API** — include recent agent requests so the UI can draw them.

## Visualisation (dynamic edges)

Once the events exist, the graph stops being purely static:

- **Static edges** = the canonical pipeline (as now).
- **Dynamic edges** = drawn live from `agent.request` events: a temporary,
  distinctly-styled arc `from → to` that **lights up when a request fires and
  fades when it completes** (the existing pulse machinery already does this for
  event types — extend it to render an arc between two agent nodes on demand).
- In the circular loop: a request shows as a **chord across the ring** (e.g.
  Critic → Researcher cuts straight across), making "agents reaching out"
  visually obvious vs. the clockwise flow.
- Allow-list pairs can be shown as faint "capability" edges; actual requests
  render solid + animated.

## Phasing

1. **Events + handler + allow-list** (Critic → Researcher first) + provenance.
2. **Dynamic edges** in the node graph (live arcs from `agent.request`).
3. **Chords** in the circular loop.
4. Expand the allow-list + intents as real needs appear.

## Accuracy fixes shipped alongside this doc
- Planned knowledge-layer edges (ingest→RAG→KG, RAG/KG→Researcher) and the
  Quartermaster gate now render **dashed/dim** — clearly not built yet.
- Added the real **Evaluation → Claims** slop-breaker edge (`audit.slop_detected`).
