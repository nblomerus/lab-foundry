# LabFoundry Real-Lab Operating Model

> **Status:** EXTENDS `AGENT_OPERATING_MODEL.md` and `AGENT_INTERACTION_SCOPE.md`.
> Obeys all locked decisions (two planes; common multi-step contract;
> `claim_goals`; PI cadence = event + periodic; augment-not-rewrite behind
> `*_LOOP` env gates). Produced by a 5-dimension design fan-out, then an
> adversarial review against the live codebase. **The review's binding fixes
> are folded in below and listed in §9.** Verdict was *ship-with-fixes*.

## 0. The "real lab" framing

| Lab role | LabFoundry agent | Owns |
|---|---|---|
| Lab Director | human / `company_state` mandate | The mission; can pause (`company_state.paused`) |
| Principal Investigator | **PI** (`pi/loop.py`, new) | The agenda: mission→directions→hypotheses→sub-questions tree; per-hypothesis goals (`claim_goals`) |
| Senior postdoc / lab manager | **Planner** (`planner/loop.py`, augmented) | The **scheduling meeting**: deliberative ranked schedule |
| Methods reviewer | **Evaluation** (`audit/loop.py`) | Substance/groundedness (the **entry gate**, not a panel vote) |
| Adversarial reviewer | **Critic** (`adversarial/loop.py`) | Challenge — can I break it (panel **vote 1**) |
| Related-work reviewer | **Novelty** (`novelty/loop.py`, new) | Has someone shown this (panel **vote 2**) |
| Area chair | **Reviewer** (new thin role) | Chairs the panel, applies consensus, records dissent, owns the single promotion write |
| Phase chair | **Adjudicator** (new harness) | Advances phase only when warranted |
| Lab notebook | **Lessons store** (`skills/`, `lessons`) | Know-how; updated by expectation-vs-outcome |

The lab loop: PI breaks novel research down → Planner schedules it in a
deliberative meeting → researchers work → every result clears a **novelty/quality
gate + peer-review panel** → the PI reconciles expected-vs-actual and the lab
writes **lessons** → lessons sharpen the next round. Every loop has **multiple,
explicit termination conditions** adjudicated by one deterministic referee.

---

## 1. PI as Research Director — decomposition + harness

### 1.1 The decomposition tree (one table + a discriminator)
Four-level tree on the **existing `claims` table** via `claims.parent_id` + a new
`claim_kind` discriminator. No new tree table.

```
mission       claim_kind='mission'      parent_id=NULL      (projection of company_state.problem_statement)
 └ direction    claim_kind='direction'    parent_id=mission   (the novelty unit: "attack X via approach Y")
     └ hypothesis  claim_kind='hypothesis'  parent_id=direction (unit of belief; confidence + claim_goals; = today's claim)
         └ subquestion claim_kind='subquestion' parent_id=hypothesis (promoted only when load-bearing — §8)
```

**⚠️ Review fix — the leak (was a hard bug).** `get_active_claims()`
(`state/client.py:128`) filters only on `status`, never `claim_kind`. PI-created
`mission`/`direction` rows use the same statuses, so eight readers
(`research/loop.py`, `planner/loop.py`, `phase_adjudicator.py`,
`phase_transition.py`, `queue_empty.py`, `claim_invalidated.py`) would research
directions as hypotheses. **Binding:** migration 009 adds `claim_kind` `NOT NULL
DEFAULT 'hypothesis'` + backfills existing rows; the **same phase** adds an
explicit kind filter (`claim_kind IN ('hypothesis','subquestion')`) to
`get_active_claims` and a separate `get_claim_tree`/`get_directions` for the PI;
a regression test asserts a `direction` row never appears in `get_active_claims`.
`get_claim_tree` must **not** be cloned from `get_active_claims` (latent
`OR/AND` precedence bug in its `exclude_ids` branch).

- Mission is a one-way projection of `company_state.problem_statement` (source of
  truth), refreshed at bootstrap.
- Ephemeral sub-questions stay in `research_inquiries`; the PI promotes one to a
  `subquestion` claim only when it earns a durable confidence track.
- **Direction confidence is a computed rollup** (`recompute_direction_confidence`),
  never LLM-set — a direction stalls objectively.

### 1.2 The PI harness (`boardroom/pi/loop.py`, `PI_LOOP=v2`)
Same skeleton as the other loops (prompt-builders → idempotent `RECIPES` →
orchestrator over `curator.build()` + `router.invoke(session=…, step_name=…)` so
`/trace` shows the deliberation as a DAG). Five steps:

1. **`pi.assess_portfolio`** — read the tree (`get_claim_tree` by `claim_kind`),
   rollups, recent findings, open verdicts, `slop_rate_by_claim`; query knowledge
   (Plane 1: `corpus_search`, `kg_evidence`, `kg_prior_claims`). → `PortfolioAssessment`
   (`strong|stalled|novel|saturated|contradicted`, the one load-bearing gap,
   `directions_under_target`).
2. **`pi.set_expectations`** — per hypothesis/direction, write/refresh a
   `claim_goals` row (locked fields: `expectation`, `kill_condition`,
   `novelty_target`, `next_milestone`). Append-only; prior open row → `revised`;
   per-claim advisory lock against concurrent-run races.
3. **`pi.decompose`** — "conducts novel research and breaks it down." Emits a
   `DecompositionPlan` of `TreeMutation`s; a `spawn_direction` MUST carry a
   `novelty_rationale` grounded in the Plane-1 prior-art query (contrastive: state
   prior art, then the gap; weak rationale rejected by schema `min_length`).
   Proposes only — persistence is step 4.
4. **`pi.decide_actions`** — (a) persist the plan in one transaction; (b) delegate
   (Plane 2): PI→Researcher `investigate {claim_id, question}`, PI→Critic
   `challenge {claim_id, kill_condition}`, novelty consult = `challenge
   {mode:'novelty'}` (no new pair); (c) when expectations are met, emit
   `phase.transition_proposed` for the Adjudicator.
5. **`pi.reflect`** — fill `claim_goals.outcome`, set `reviewed_at`, status →
   `met|missed|revised`; feed Lessons (§5).

**⚠️ Review fix — `phase.transition_proposed` is single-handler.** Today that
event's one handler (`handle_phase_transition_proposed`) **is** the PI ratifying +
writing the charter. `PI_LOOP=v2` **replaces** that handler and **inherits the
charter-writing responsibility** — it does not "wake alongside" it.

**⚠️ Review fix — cost.** Routing all five steps to REASONING would exhaust the
50/day REASONING cap and starve kill/charter calls. **Binding:** `assess_portfolio`,
`decide_actions` → REASONING; `set_expectations`, `decompose` → WORKHORSE;
`reflect` → WORKHORSE. Add a **separate daily PI-run budget** so sweeps can't
starve the shared REASONING tier.

### 1.3 Cadence (event + periodic)
- **Wakes:** `phase.transition_proposed` (replaced handler), `claim.confidence_changed`
  (re-assess subtree, `pi.assess_portfolio` cooldown ≈1800s), `audit.slop_detected` /
  `claim.invalidated` (URGENT, bypass cooldown), `agent.reply` to `pi`.
- **Periodic sweep** in `dispatch.py:_watchdog_loop` (`PI_SWEEP_HOURS`, default 12):
  if a `claim_goals` row is `open` past due OR a direction is `under_target`, emit
  `pi.sweep_requested` (date-bucket deduped, like `_check_phase_budget`) →
  `handle_pi_sweep_requested` → `run_pi_loop(reason='periodic')`. This is the
  "group meeting" — `reflect` runs even when quiet.

---

## 2. Planner as Deliberative Scheduler (`PLANNER_LOOP=v3`)
Augments the existing v2 (`assess→propose→critique`); v2 stays the validated
default, v3 ships after a shadow A/B (highest blast radius in the swarm).

```
perceive → propose_schedule → [Plane-2 Planner→PI prioritise, round 1] → critique_schedule → revise? ─stop→ commit → reflect
                                       └────────────── round loop (≤ PLANNER_MAX_ROUNDS=3) ──────────────┘
```

- **`planner.perceive`** — claims + `claim_goals` (alignment target) + budget
  (`cost_tracking`) + dependencies (`parent_id`, pending/running per claim) +
  staleness. Per-claim `ClaimGapWithGoal`. **Degrades gracefully** when
  `claim_goals` is empty (PI not yet live): falls back to confidence-trend.
- **`planner.propose_schedule`** — LLM estimates components; the orchestrator
  recomputes `composite` **deterministically in Python**:
  `composite = (0.35·align + 0.25·info_gain + 0.15·staleness − 0.15·cost − 0.10·diminishing)·dep_ready`
  (`dep_ready` multiplicative — a blocked item can't lead).
- **Plane-2 Planner→PI `prioritise`** — round 1 only, fired on ambiguity; feature-
  gated (`hasattr(dispatcher,'request_agent')`) so v3 ships before Plane 2.
- **`planner.critique_schedule`** — test against six failure modes (off-goal,
  over-budget, blocked, imbalance, diminishing, redundant). → `CritiquedSchedule`.
- **commit** — top-K (cost fits budget) → existing `tasks` queue with
  `priority=clamp(round(1+composite·9),1,10)`; fires `task.created`. Score
  decomposition persisted in `tasks.payload.score` (JSONB, no schema change).
- Termination (§4): top-K Jaccard ≥0.9 across rounds, deliberation budget, max
  rounds, or no new candidates.

---

## 3. Novelty/Quality Gate + Peer Review (`GATE_LOOP=v2`, default off)
Drives the `claim_status` machine (migration 008:
`proposed→tested→weakly_supported→replicated→invalidated→merged`), which nothing
systematically drove before.

**Per-claim promotion gate (Director decision §8).** The gate runs **per claim, at
promotion candidacy** — not per high-signal finding. A new event
`claim.promotion_candidate` fires when a `tested` claim accumulates ≥N passed
supporting findings or crosses a confidence threshold (emitted from
`update_finding_audit`/confidence updates, deduped per claim per day). The gate
evaluates the claim *as a whole* (its evidence chain + prior art), which is the
meaningful unit for novelty. The legacy per-finding `finding.high_signal` →
single-Critic path remains when `GATE_LOOP` is off.

```
claim.promotion_candidate ─▶ GATE (boardroom/gate/loop.py) — SYNCHRONOUS, one Session, one DAG
   ENTRY (precondition) │ Claim must have ≥1 Evaluation-passed finding (audit=pass) — NOT a vote
   VOTE 1  CHALLENGE     ├─ Critic over the claim's evidence chain — emits a vote, does NOT mutate (review fix)
   VOTE 2  NOVELTY       ├─ Novelty (boardroom/novelty/loop.py) vs prior art, over the whole claim
                         ▼
   PANEL CHAIR           └─ Reviewer (new thin role) applies quorum + consensus → promote | hold | reject | merge
```

**⚠️ Review fix — honest voter set.** The gate fires on `finding.high_signal`,
emitted only when `audit_verdict='pass' AND relevance_score>=8`. Evaluation has
**already** passed the finding, so it is the **entry precondition, not a third
vote**. The panel has **two voters: Critic + Novelty.** "All advance → PROMOTE"
means **Critic=advance AND Novelty∈{novel,incremental}**.

**⚠️ Review fix — Critic side-effect collision.** `handle_finding_high_signal`
today *also* calls `invalidate_claim`/`update_claim_confidence` on its own verdict.
Under `GATE_LOOP=v2` the Critic stage **produces a verdict but applies no
mutation**; it persists the verdict and emits the vote, and the **Reviewer/chair
owns the single mutation**. Legacy unilateral behaviour remains only when
`GATE_LOOP` is off.

**Stage Novelty (`boardroom/novelty/loop.py`, `NOVELTY_LOOP=v2`, `agent="evaluation"`)**
— 3 steps (mirrors adversarial loop): `recall_prior_art` (Neo4j `kg_prior_claims`,
`search_web`/`search_hacker_news`, **new real `search_arxiv` tool**),
`extract_overlap` (per-candidate fan-out: `identical|subsumes|adjacent|orthogonal`),
`score` → `NoveltyVerdict {novelty_score, band, prior_art}` (band derived
deterministically from score, à la `_verdict_from_score`).
**Honest gap:** no vector index yet (KG has no `Paper` node, no embeddings); v2 is
lexical + LLM overlap — catches identical/subsumes, weak on paraphrase.

**⚠️ Review fix — Novelty cannot solo-REJECT.** Lexical-only novelty can torpedo a
genuinely novel claim on a superficially-similar title. Until a `Paper`/`PriorArt`
node + pgvector exists, **Novelty may only HOLD/MERGE/demote; an `invalidate_claim`
requires a second confident reject (Critic).** The vector index is a gating
dependency before `GATE_LOOP=v2` goes default-on.

**Quorum + consensus (chair applies):**
- Both voters must vote (quorum). If Novelty hard-fails (arXiv+search down), 2→1
  with `degraded_quorum=true` recorded — never a silent pass.
- Any `reject` at conf ≥0.7 → REJECT (subject to the Novelty-solo-reject limit above).
- Both advance → PROMOTE. Mixed (no confident reject) → chair decides,
  **default-bias HOLD**. `redundant`+strong → MERGE (route to PI).
- **Dissent first-class:** every vote (incl. overruled) → new `gate_reviews` table +
  Zep `dissent` session; `PanelDecision.dissent_summary` names overruled votes.

**⚠️ Review fix — HOLD starvation.** HOLD leaves status unchanged and
`finding.high_signal` is one-shot per finding (`dedup_key highsig-{id}`), so a
HELD-but-good claim dies silently. **Binding:** `gate_runs.held_until`; a watchdog
tick (peer to `_check_phase_budget`) re-emits `gate.requested` for HELD claims
after N hours, capped at `MAX_GATE_HOLDS` per claim, then auto-routes to PI as
"undecided — needs direction."

**⚠️ Review fix — runaway gate↔researcher cycle.** Gate→Critic→`agent.request`
Researcher→finding→`high_signal`→gate is a loop `depth≤3` doesn't guard (it's an
event cycle, not one chain). **Binding:** per-claim `gate.run` cooldown (2–4h,
mirrors the `critic.kill_verdict` 4h cooldown) + a hard cap on gate runs per claim
per day.

**⚠️ Review fix — gate can't hang the swarm.** A synchronous Plane-2 await inside a
stage that never replies would hold one of only 4 handler semaphores. The §4
`TIMEOUT`/`ERROR_FLOOR` terminators MUST wrap the gate's synchronous waits.

**Claim-lifecycle integration:** PROMOTE → `weakly_supported` (or `replicated` if
novelty=novel + replication present); HOLD → unchanged + `gate.held`; REJECT →
`invalidate_claim` (→ PI); MERGE → `merged`. On PROMOTE the gate closes the PI
loop: sets the satisfied `claim_goals.status='met'` + fills `outcome`.

**⚠️ Review fix — slow-burn claims.** Only rel≥8 findings reach the gate, so most
findings never advance the status machine. Add a **periodic promotion sweep**:
`tested` claims with N passed supporting findings → `weakly_supported` without a
`high_signal`-triggered gate. Pair with **slop-rate feedback** (`slop_rate_by_claim`
exists) so claims whose high-signal findings repeatedly REJECT get downward
pressure — counters relevance-inflation gaming.

**Conflict resolution (binding):** the **PI→Critic novelty consult reuses
`challenge` with `{mode:'novelty'}`** (no new pair). The gate's **internal**
escapes — `Novelty(evaluation)→Critic:challenge` and `Reviewer→Researcher:investigate`
— ARE new allow-list pairs added to `AGENT_INTERACTION_SCOPE.md`.

---

## 4. Shared termination model (`boardroom/harness/termination.py`, `TERMINATION_V2=on`)
One deterministic referee, no LLM calls, evaluated between rounds, outside the
budget it polices. `TerminationController` holds ordered `StopCondition`s (order =
precedence: safety/cost before quality); first to trip wins; each round the loop
reports `RoundSignals` (all fields optional). Adoption: `async for round_index in
controller.rounds(max_rounds): …; controller.report(RoundSignals(...))` (~5–8 lines
around an existing `while`).

| StopReason | Trips when | Source |
|---|---|---|
| `HUMAN_STOP` | `company_state.paused` (company-wide) or a claim/session `human.stop` | **review fix:** reuse existing `company_state.paused`; reserve `human.stop` for scoped stops |
| `BUDGET_CAP` | tier capped today OR **session** token spend ≥ ceiling | `router._calls_today`/`DAILY_CAPS`/`cost_tracking.cap_reached` **+ new** `SUM(token counts) WHERE session_id=?` (review fix: per-session sum is new code, not ready-made) |
| `KILL_CONDITION` | round reports `kill_condition_hit` | critic `judge_verdict='kill'`; PI `claim_goals.kill_condition` |
| `PEER_REVIEW_PASSED/FAILED` | a Plane-2 reply verdict returned | `agent_requests` status |
| `GOAL_MET` | `expectation_satisfied` | researcher `Synthesis`, planner confidence, PI `claim_goals.status='met'` |
| `DIMINISHING_RETURNS` | rolling gain < `min_info_gain` for `patience` rounds | `gain = 1.0·new_evidence + 1.5·novel_sources + 1.0·max(0,Δconf)·10` |
| `NO_NEW_INFORMATION` | round's dedup-hash set ⊆ accumulated | output content hashes |
| `TIMEOUT` | `now − session.started_at > wall_s` | `session.started_at` |
| `ERROR_FLOOR` | N consecutive zero-output/error rounds | round `error` flag |
| `MAX_ITERATIONS` | `round_index ≥ max_rounds` | controller (backstop, last) |

`HUMAN_STOP`/`BUDGET_CAP`/`MAX_ITERATIONS` always on. For the researcher,
`DIMINISHING_RETURNS` **replaces** the LLM `gap_check` as the hard gate (gap_check
only *proposes* follow-ups). Defaults: researcher 2, planner 3, PI 4 (`patience=2`),
evaluation/critic 1 (uniform reporting + budget/human gates; critic `kill`→
`KILL_CONDITION`), adjudicator 2 (gated by a `PeerReviewGate`: advance only after a
Critic `verify` reply passes — "peer review before work advances" as a real
terminator).

Every stop writes `loop_terminations` + emits **`step.terminated`** (via
`Session.emit_event`) so `/trace` shows a terminal node and distinguishes
**terminal-with-meaning** (`GOAL_MET`, `KILL_CONDITION`, `PEER_REVIEW_*`,
`HUMAN_STOP`, `BUDGET_CAP`) from **terminal-as-safety** — a safety stop is itself a
strong lesson candidate. **Log per-round gain even when not tripping**, for a week
of shadow calibration of the (currently guessed) weights.

**⚠️ Review fix — URGENT budget carve-out.** Adding `human.stop` to `URGENT_EVENTS`
means URGENT-woken loops bypass the cost gate; give `BUDGET_CAP` an explicit
carve-out via the new per-session ceiling so an URGENT-woken PI can't burn past cap.

---

## 5. Expectation → outcome → Lessons feedback
The lessons machinery is **broken at two joints** (verified — zero callers):
1. `lesson_applications.outcome` is never written (router inserts NULL at
   `router.py:654`; the `pending` partial index exists for a judge that doesn't exist).
2. `reconcile_lessons()` is never called — so its promote (`supportive≥5 AND
   contradicting≤1`) / retire (`contradicting≥3 …`) rules can never fire; every
   lesson is frozen `probationary`. **Closing these two hinges is the spine — build first.**

```
PLAN  → write EXPECTATION (claim_goals.expectation / agent_runs.expectation)
 … act / writes / delegate …
REFLECT → write OUTCOME (claim_goals.outcome+status / agent_runs.outcome+expectation_met)
   ├─ (A) reflect.judge_applications → writes lesson_applications.outcome   ← missing hinge
   └─ (B) reflect.batch_propose_lessons (exists) → candidates, dedupe-guarded
periodic (watchdog, hourly) → reflect.reconcile → reconcile_lessons() + decay     ← missing hinge
next PLAN → curator._lessons_layer injects matching lessons → improvement lands
```

- **Hinge A — `reflect.judge_applications`** (new, `agent="evaluation"`, FAST,
  gated on `len(prompt.lesson_ids)>0`): judge each in-context lesson
  `supportive|contradicting|inconclusive` (**default inconclusive**), then
  `UPDATE lesson_applications SET outcome=… WHERE agent_run_id=ANY(pending) AND
  outcome IS NULL`. **Review fix:** server-side guard — only write `supportive`
  when the `lesson_id` was actually in `prompt.lesson_ids` AND the run succeeded.
- **Hinge B — `reflect.reconcile`** (watchdog hourly, first caller of
  `reconcile_lessons()`): reconcile + a **decay pass** (retire probationary lessons
  with 0 supportive in 14d; decay confidence of active lessons unused 30d), then
  emit `lessons.reconciled`. **Review fix:** re-read and **re-tune** the
  `reconcile_lessons()` thresholds before building — with supportive now written at
  the rate of every reflect step, the dissent-era `≥5` rule fires far faster.
- **Expectation storage:** PI → `claim_goals`; non-PI → light `agent_runs`
  `expectation`/`outcome`/`expectation_met` columns (reconciled at reflect via
  `session_id`).
- **Make retrieval land:** (1) lesson-propose prompts use the *current*
  invocation vocab (`adversary.judge_verdict`, `pi.set_expectations`), not legacy
  (`critic.kill_verdict`) — validate `applies_to_invocation` before insert; (2)
  stable `applies_when` context vocab `{phase, agent, task_type, claim_status}`;
  (3) dedupe via `pg_trgm` >0.6 — a near-duplicate earns a synthetic supportive
  application (promotion pressure, not row spam).
- **⚠️ Review fix — lesson spam is real, caps are mandatory:** per-category caps in
  `curator._lessons_layer` (top-5 is flat today) so PI-strategic lessons don't
  crowd out tactical ones; enforce `default inconclusive` in-prompt **and** server-side.

**Conflict resolution (binding):** `pi.reflect` writes the mechanical
`status`/`outcome` diff itself, but the **lesson judgment on PI expectation
misses runs through `reflect.judge_applications` (`agent="evaluation"`)** — the PI
doesn't grade its own mispredictions into lessons.

**⚠️ Review fix — Zep session bug.** `adversarial/loop.py:367` recalls
`theses-lifecycle` but `main.py` only creates `claims-lifecycle` → silent empty
recall. All new loops (PI reflect, gate dissent, reviewer) must use canonical
session names (`claims-lifecycle`, `pi-deliberations`, `dissent`) and `main.py
ZEP_SESSIONS` must add any new session before first use.

---

## 6. Consolidated NEW artifacts

> **Migration numbering (amendment to raise, not silently bind).**
> `AGENT_OPERATING_MODEL.md` pins `claim_goals` to "migration 011". Existing
> migrations end at 008. This doc proposes `claim_goals` move to **009** (it's the
> shared dependency). **This is a deviation from a locked doc — flag to the
> Director before binding.**

**Migrations:** `009_pi_directions` (`claim_kind` enum+col+backfill+index;
`claim_goals`) · `010_agent_requests` · `011_agent_run_expectations` ·
`012_gate_reviews` (`gate_runs`, `gate_reviews`, `held_until`,
`degraded_quorum`) · `013_loop_terminations` · `014_lessons_dedupe_decay`
(`pg_trgm` + trgm index + decay).

**Files:** `boardroom/pi/{loop,schemas}.py` · `boardroom/gate/{loop,schemas}.py` ·
`boardroom/novelty/{loop,schemas}.py` · `handlers/gate.py` ·
`handlers/agent_request.py` · `harness/termination.py`.

**Roles:** `reviewer` (thin chair) · `novelty` prompt anchor (or reuse `evaluation`).

**Recipes:** `pi.{assess_portfolio,set_expectations,decompose,decide_actions,reflect}` ·
`planner.{perceive,propose_schedule,critique_schedule}` ·
`novelty.{recall_prior_art,extract_overlap,score}` · `reviewer.adjudicate_panel` ·
`reflect.judge_applications`.

**ROUTE:** PI assess/decide → REASONING, set_expectations/decompose/reflect →
WORKHORSE (cost fix); planner.* → WORKHORSE; novelty.* → WORKHORSE;
reviewer.adjudicate_panel → REASONING; reflect.judge_applications → FAST.

**Events:** `pi.sweep_requested` · `agent.request`/`agent.reply` ·
`gate.requested`/`gate.completed`/`gate.held` · `review.requested`/`review.completed` ·
`claim.promoted` · `step.terminated` · `human.stop` (URGENT) · `lessons.reconciled`.
**Each must be wired into `COOLDOWNS` + `URGENT_EVENTS` explicitly** (review fix —
unthrottled otherwise), incl. a per-claim `gate.run` cooldown and the PI 1800s cooldown.

**MCP tools:** `search_arxiv` (real API) · `kg_prior_claims` (KG prior-art read).

**state/skills methods:** `create_claim(...,claim_kind=)`, `set_claim_goal`,
`review_claim_goal`, `get_open_claim_goals`, `get_claim_tree`, `get_children`,
`recompute_direction_confidence`, `get_claim_goals(claim_id)`,
`judge_pending_applications`, `set_application_outcome`, `find_near_duplicate`,
`credit_recurrence`.

**Watchdog/curator:** `_pi_periodic_sweep` (`PI_SWEEP_HOURS`),
`_reconcile_lessons_if_due`+`_decay_pass` (hourly), HELD-claim re-review tick,
slow-burn promotion sweep; shared goal/expectations/reflect prompt layer +
per-category lesson caps. **Adjudicator phase vocab fix:** replace
`AdjudicatorVerdict.target_phase` Literal with the migration-008 enum
(`frame/hypothesize/experiment/validate/write/submit`) — current stale values
would emit invalid phases.

**ENV:** `PI_LOOP=v2`, `PI_SWEEP_HOURS=12`, `PLANNER_LOOP=v3`,
`PLANNER_MAX_ROUNDS=3`, `PLANNER_DELIBERATION_BUDGET`, `GATE_LOOP=v2`,
`NOVELTY_LOOP=v2`, `TERMINATION_V2=on`, `MAX_GATE_HOLDS`, per-agent
`*_MAX_ROUNDS`/`*_WALL_S`.

---

## 7. Build phasing (ordered by "smallest change that makes the lab learn")
0. ✅ **DONE — Close the learning circuit** (highest ROI, no new harness):
   migrations 011+014 (applied); `reflect.judge_applications` (hinge A, routed
   FAST, shipped behind `LESSON_JUDGE=on`) + watchdog reconcile/decay (hinge B,
   live); scope-hygiene (drop lessons scoped to unregistered invocations),
   predicate-vocab hygiene (debug-log unknown `applies_when` keys), dedupe
   (`find_near_duplicate` → `credit_recurrence` instead of row spam), and the
   `theses-lifecycle`→`claims-lifecycle` Zep fix. End-to-end verified against the
   live DB: 5 supportive applications promote a probationary lesson to `active`.
   *Remaining to flip on:* enable `LESSON_JUDGE=on` after a shadow window so the
   LLM judge's verdicts are calibrated before they drive promotion/retirement.
1. **Shared scaffolding:** migration 009 (`claim_kind`+backfill+reader filters+
   `claim_goals`); reusable goal/expectations/reflect prompt layer.
2. **Shared termination:** `harness/termination.py` + migration 013 +
   `step.terminated`/`human.stop`. Adopt in researcher first, then degenerate loops.
3. **PI harness:** `pi/loop.py` + tree + cadence; `PI_LOOP=v2` **replaces**
   `handle_phase_transition_proposed` (inherits charter). Literature-only first.
4. **Augment existing loops:** goals/expectations/reflect + controller into
   Researcher/Evaluation/Critic/Planner; land Planner **v3** (reads `claim_goals`,
   degrades gracefully).
5. **Delegation plane:** migration 010 + `handle_agent_request` + allow-list/
   guardrails; PI→Researcher/Critic, Planner→PI, `PeerReviewGate` terminators.
6. **Novelty/quality gate + panel:** migration 012; **build the `Paper`/`PriorArt`
   node + pgvector index FIRST (Director decision — prerequisite, not optional)**;
   then `novelty/loop.py` (+`search_arxiv`,`kg_prior_claims`), `gate/loop.py`,
   `reviewer` role; per-**claim** promotion gate (`claim.promotion_candidate`
   trigger); split Critic side effects; HOLD lifecycle; per-claim gate cooldown;
   slow-burn sweep. `GATE_LOOP=v2`/`NOVELTY_LOOP=v2` default-on only after the
   vector index + a shadow A/B.
7. **Adjudicator harness:** multi-step, goal-aware, `PeerReviewGate`-terminated,
   correct phase vocab.
8. **Viz:** knowledge spokes always-on; live delegation chords; PI loop drill-in;
   `step.terminated` terminal nodes; `lessons.reconciled` in Bench/Debug; the
   planner "scheduling meeting" rounds.

---

## 8. Decisions

**Resolved by the Director:**
- ✅ **Migration renumber** `claim_goals` 011→009 — **approved.** 009 is the
  canonical home; the operating-model "011" reference is superseded.
- ✅ **Novelty granularity = per-claim, at promotion** (not per-finding). The gate
  is a **per-claim promotion gate**: the Novelty + peer-review panel runs when a
  claim is a promotion candidate, not on every high-signal finding. See §3 note.
- ✅ **Vector index before the gate goes live** — the `Paper`/`PriorArt` node +
  pgvector index is built **before** `GATE_LOOP=v2` is enabled by default
  (phase 6 prerequisite). Lexical-only novelty is not shipped as the gate's basis.

**Still open (lower stakes, resolve during build):**
- **Mission projection** — confirm no edit path desyncs `company_state.problem_statement`
  from the `mission` root.
- **Direction confidence rollup** — mean / max / evidence-weighted; zero-hypothesis
  direction = `stalled` or `unstarted`?
- **Sub-question promotion threshold** (e.g. load-bearing for ≥2 hypotheses).
- **`replicated` rule** — novelty mandatory for any publishable `replicated`?
- **Planner convergence metric** — top-K Jaccard vs Kendall-tau; budget envelope
  source (ENV+`cost_tracking` vs a real Quartermaster service).
- **Termination weight calibration** — shadow-log gain for a week first.
- **`reconcile_lessons()` re-tuning** — scale thresholds by application volume.
- **Lesson layer caps** — per-category split sizes.

---

## 9. Corrections applied from adversarial review (binding)
1. `claim_kind` leak → reader filters + backfill + separate `get_claim_tree` +
   regression test (§1.1).
2. Gate has **2 voters** (Critic+Novelty); Evaluation is the entry precondition (§3).
3. Critic stage **emits a vote, applies no mutation** under `GATE_LOOP=v2`; chair
   owns the single mutation (§3).
4. `PI_LOOP=v2` **replaces** `handle_phase_transition_proposed` + inherits charter (§1.2).
5. `BUDGET_CAP` per-session token sum is **new code**, not a ready-made signal (§4).
6. **Novelty cannot solo-REJECT** until a vector index exists (§3).
7. **HOLD lifecycle** (held_until + re-review + cap → route to PI) (§3).
8. **Per-claim gate cooldown** + daily cap to break the gate↔researcher cycle (§3).
9. Gate synchronous waits wrapped by `TIMEOUT`/`ERROR_FLOOR` so they can't hang a
   semaphore (§3/§4).
10. **Slow-burn promotion sweep** + slop-rate feedback so non-high-signal claims
    advance and relevance-gaming is penalised (§3).
11. `company_state.paused` = company-wide HUMAN_STOP; URGENT budget carve-out (§4).
12. Adjudicator **phase vocabulary** updated to the migration-008 enum (§6).
13. Lesson feedback: server-side `supportive` guard, **re-tune** reconcile
    thresholds, **mandatory** per-category caps, fix `theses-lifecycle` recall (§5).
14. New events explicitly added to `COOLDOWNS`/`URGENT_EVENTS` (§6).
15. PI step routing rebalanced (not all REASONING) + separate PI daily budget (§1.2).
