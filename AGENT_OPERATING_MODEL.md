# Agent Operating Model — agents as real researchers

## The misalignment we're resolving

- The **circular loop** says knowledge is the **hub**: every agent reads/writes
  it freely. ✅ correct.
- The **interaction doc** put knowledge in the same **gated allow-list** as
  agent-to-agent delegation. ❌ wrong — that makes querying the library a
  "request" with depth limits and cooldowns.
- The **PI** is modelled as a single-shot rubber stamp (`pi.claim_verdict`,
  `pi.phase_transition_ratify`). A real principal investigator sets goals,
  forms expectations, and works in steps. ❌ wrong.

Fix: **two interaction planes** + a **common multi-step agent contract** that
every agent (PI included) obeys.

---

## Two planes of interaction

### Plane 1 — Knowledge (universal, ungated)
The knowledge substrate (RAG corpus + Neo4j graph + episodic memory) is
**infrastructure, not a peer agent**. Every agent can:

- **Read** anything, anytime, as a *tool call* — `corpus_search`,
  `kg_evidence(claim)`, `kg_finding_influence`, `memory_recall`. No allow-list,
  no depth limit, no cooldown. This is the hub→spoke in the loop.
- **Write** the part it owns (role-typed, but always allowed for the owner):

  | Role | Writes to knowledge |
  |---|---|
  | Researcher | Findings, evidence, (cited) Papers |
  | Critic | CriticVerdicts |
  | PI | Claims / hypotheses + their goals |
  | Librarian (planned) | Corpus docs, Paper/Dataset nodes |
  | Evaluation | audit verdicts on findings |

In the graph: **spokes to the hub = this plane.** Always present, pulse on use.

### Plane 2 — Delegation (directed, gated)
Agent-to-agent requests — "Critic, challenge C14", "Researcher, dig into X".
This is the only thing the **allow-list + guardrails** (from
`AGENT_INTERACTION_SCOPE.md`) govern: depth cap, dedupe, budget, provenance.

In the graph: **chords across the ring = this plane.** Appear live, fade when done.

> The allow-list in the interaction doc now governs **Plane 2 only**. Knowledge
> access is never a request.

---

## Common agent contract (every agent operates like a researcher)

Each agent is a **goal-driven, multi-step loop**, not a function call:

```
  GOAL (standing, tied to the mission)
    │
    ▼
  PERCEIVE → read state + query knowledge (Plane 1)        ← always allowed
    │
    ▼
  PLAN → decide steps; form EXPECTATIONS (what success looks like)
    │
    ▼
  ACT → produce outputs · write knowledge · DELEGATE (Plane 2 if needed)
    │
    ▼
  REFLECT → expectation vs outcome; record lessons; loop or stop
```

Where each agent stands today:

| Agent | Multi-step loop today? | Goal/expectations today? |
|---|---|---|
| Researcher | ✅ `research/loop.py` (plan→extract→synthesise→experiment) | partial |
| Evaluation | ✅ `audit/loop.py` (cross-check→batch-score) | rubric only |
| Critic | ✅ `adversarial/loop.py` (plan-attack→counter→stress→judge) | partial |
| Planner | ✅ `planner/loop.py` (assess→propose→critique) | partial |
| **PI** | ❌ single-shot recipes | ❌ none |
| Adjudicator | ❌ single-shot | ❌ none |

So the loops mostly exist; what's missing is **explicit goals + expectations +
reflection**, and a **PI harness** entirely.

---

## PI harness (the headline change)

A new `pi/loop.py`, peer to the research/audit/adversarial loops. The PI is the
lab's principal investigator: it owns the portfolio and drives it toward the
mission (a publishable, novel result). It runs a deliberation loop, not a stamp.

### Steps
1. **`pi.assess_portfolio`** — read claims, findings, verdicts, phase; query the
   KG for evidence depth + contradictions; summarise *state vs mission*: what's
   strong, what's stalled, what's novel, where the gaps are.
2. **`pi.set_expectations`** — for each live/candidate hypothesis, record:
   - **expectation** — what evidence would confirm it
   - **kill condition** — what would refute it
   - **novelty bar** — why it'd be publishable / not already done
   - **next milestone** — the concrete next proof
3. **`pi.decide_actions`** — choose and emit:
   - spawn / merge / kill hypotheses (Claim mutations)
   - **delegate** research (→ Researcher) and challenges (→ Critic) via Plane-2
     `agent.request`
   - propose phase transition when expectations across the portfolio are met
4. **`pi.reflect`** — compare this round's outcomes to the expectations set last
   round; update goals; record lessons (reflection division).

### State the PI needs (persisted)
Per hypothesis (extend `claims` or a new `claim_goals` table):
`expectation`, `kill_condition`, `novelty_target`, `next_milestone`,
`expectation_set_at`, `last_reviewed_at`.

This makes the PI's behaviour legible and reviewable: you can see *what it
expected*, *whether that happened*, and *what it did about it* — in Trace.

### Wiring
- Triggered by the events it already owns (`phase.transition_proposed`,
  claim-confidence shifts) **plus** a periodic/`queue.empty`-style cadence so it
  reviews the portfolio even when nothing forces it.
- Legacy single-shot recipes kept behind `PI_LOOP=v2` until validated (same
  pattern as `ADVERSARY_LOOP` / `AUDITOR_LOOP`).

---

## How the viz now maps cleanly

| Plane | Loop view | Graph view |
|---|---|---|
| Knowledge (universal) | spokes hub ↔ every stage | every agent has a knowledge edge |
| Delegation (gated) | chords across the ring | dynamic arcs from `agent.request` |
| Pipeline (canonical) | clockwise ring | the static edges |

The PI stage stops being a single "spawn" output and shows its **loop**
(assess → expect → act → reflect) when drilled into.

---

## Decisions (locked in)

1. **Delegation = peer-to-peer.** Any allow-listed pair may delegate (not just
   the PI). The lab behaves like a real lab; the Plane-2 guardrails make it safe.
2. **All agents get the upgrade** — every agent gains an explicit standing goal,
   per-run expectations, and a reflect step (not just the PI).
3. **PI goals live in a separate `claim_goals` table** (history preserved).
4. **PI cadence = event + periodic** — reacts to its events AND sweeps on a
   periodic/`queue.empty` cadence so the portfolio never goes stale.

## Per-agent spec (peer-to-peer, all upgraded)

Every agent: reads knowledge freely (Plane 1), runs a multi-step loop, sets
expectations, may delegate to its allow-listed peers (Plane 2), reflects.

| Agent | Standing goal | Delegates to (Plane 2) | Writes |
|---|---|---|---|
| Researcher | Produce grounded, novel evidence for open sub-questions; prefer primary sources | Evaluation (`verify`) | Findings, evidence, cited Papers |
| Evaluation | Let only substantive, grounded findings pass; kill slop | Researcher (`re-source`) | audit verdicts |
| Critic | Refute weak hypotheses; surface contradictions | Researcher (`investigate`) | CriticVerdicts |
| Planner | Keep the queue full of high-value, non-redundant work aligned to PI goals | PI (`prioritise`) | Tasks |
| **PI** | Drive the portfolio to a publishable, novel result; set + track expectations | Researcher (`investigate`), Critic (`challenge`) | Claims + `claim_goals` |
| Adjudicator | Advance the phase only when evidence warrants; guard premature convergence | Critic (`verify`) | phase proposals |

(Allow-list = the union of the "Delegates to" column; anything else is rejected.
This supersedes the matrix in `AGENT_INTERACTION_SCOPE.md`.)

## `claim_goals` schema (migration 011)

```sql
CREATE TABLE claim_goals (
  id              BIGSERIAL PRIMARY KEY,
  claim_id        BIGINT NOT NULL REFERENCES claims(id),
  expectation     TEXT NOT NULL,   -- what evidence would confirm it
  kill_condition  TEXT NOT NULL,   -- what would refute it
  novelty_target  TEXT,            -- why it'd be publishable / not already done
  next_milestone  TEXT,            -- the concrete next proof
  status          TEXT NOT NULL DEFAULT 'open',  -- open|met|missed|revised
  set_by_run_id   BIGINT,
  set_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reviewed_at     TIMESTAMPTZ,
  outcome         TEXT             -- filled at reflect: what actually happened
);
CREATE INDEX claim_goals_claim ON claim_goals(claim_id);
```

Non-PI agents' per-run expectations are lighter — logged on their
`agent_runs` (expectation + outcome columns) rather than a durable table.

## Build phasing

1. **Shared scaffolding** — `claim_goals` table; `agent_runs` gains
   `expectation`/`outcome`; a reusable "goal + expectations + reflect" prompt
   layer in the curator that every recipe composes.
2. **PI harness** — `pi/loop.py` (assess → set_expectations → decide_actions →
   reflect) writing `claim_goals`; event + periodic cadence; `PI_LOOP=v2`.
3. **Adjudicator harness** — multi-step, goal-aware.
4. **Augment existing loops** — add goals/expectations/reflect to Researcher,
   Evaluation, Critic, Planner (extend, don't rewrite their loops).
5. **Delegation plane** — `agent.request`/`agent.reply` + peer-to-peer
   allow-list + guardrails (per `AGENT_INTERACTION_SCOPE.md`).
6. **Viz** — knowledge spokes always-on (Plane 1); live delegation chords
   (Plane 2); PI stage shows its loop when drilled in.
