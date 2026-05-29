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

## Open decisions (need your call)
1. **PI goal storage** — extend `claims` with goal fields, or a separate
   `claim_goals` table? (Separate is cleaner for history.)
2. **PI as orchestrator** — should the PI be the main delegator (it requests
   research + challenges), or do Critic/Evaluation also delegate peer-to-peer?
3. **Which agents get the full goal/expectation/reflect upgrade first** — PI
   only, or PI + Adjudicator, or all of them?
4. **Knowledge writes** — confirm the role→write ownership table above.
5. **Cadence** — how often should the PI wake to review the portfolio?
