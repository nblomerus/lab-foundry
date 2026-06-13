# The Research Loop — Formal Spec

> **Status:** the single source of truth for how a research *direction* moves from a fresh
> idea to a written article (or an honest dead end). It is kept honest by the code it
> describes: the stages come from `direction_stage_v` (migration 019), the transitions from
> `harness/loop_engine.py:REGISTRY`, and the legal status edges from
> `state/client.py:_LEGAL_DIRECTION_TRANSITIONS`. If you change one, change this doc.

Before this formalization the loop was *emergent* — its stage was re-derived independently in
five places, status writes were scattered across six unguarded writers, and the drivers were
hand-written `_maybe_*` functions each with its own dedup scheme (the source of the recurring
"one-shot dedup deadlock" stalls). This document describes the formalized loop.

## The unit: a direction

A **direction** is a `claims` row with `claim_kind='direction'`, child of a `mission`. It is the
novelty unit — "attack X via approach Y". Its life is one trip through the loop.

## The stage ladder (derived, read-only)

A direction's **stage** is a pure function of its DB state, computed in ONE place —
`direction_stage_v.stage` — and read by the pacemaker context, the API (`/research/dossiers`),
the web `/research` page, and `lab_doctor`'s *Research loop* section. Never re-derive it elsewhere.

```
proposed → scored → {held | passed} → approved → review → proposal → experiments → finding → article → concluded
                                                                                        └→ (terminal) invalidated | merged
```

| Stage | Means | Set by |
|---|---|---|
| `proposed` | a direction claim exists, not yet scored | deliberation (`agents/ariadne/persist.py`) |
| `scored` | `direction_scores` row written | deliberation |
| `held` | independent adjudicator returned `verdict='hold'`, not gate-approved | `agents/novelty` |
| `passed` | adjudicator returned `verdict='pass'`, not yet gated | `agents/novelty` |
| `approved` | `direction_gate.status='approved'` (auto or human) | `ariadne_pace._auto_approve` / human |
| `review` | a final `lit_review` document exists | `agents/ariadne/scholarship.py` |
| `proposal` | a final `proposal` document (falsifiable hypotheses) exists | `agents/ariadne/scholarship.py` |
| `experiments` | ≥1 completed `experiment_runs` | `agents/experiments` + quartermaster |
| `finding` | ≥1 `research_findings` row | `agents/synthesis` |
| `article` | a final `article` document exists | `agents/synthesis/article.py` |
| `concluded` | `claims.status='concluded'` (decisive finding ≥ bar) | `state.advance_direction` |
| `invalidated` | gapped / retired / superseded (terminal except `reopen`) | `state.advance_direction` |

Terminal statuses win the stage label (an `invalidated` direction that still carries an old
`hold` adjudication reads as `invalidated`, not `held`).

### Blockers

`direction_stage_v.blocker` names the ONE reason a live direction is parked, surfaced in the UI
and `lab_doctor`:
- **`held by adjudicator`** — active, adjudicated `hold`, not gate-approved.
- **`evidence cap reached`** — active, ≥ `ARIADNE_EVIDENCE_CAP` (9) completed experiments but not
  concluded; Ariadne's reflect owns retire/advance from here.

## The write path: `state.advance_direction()`

Every lifecycle status change goes through **one** guarded method (`state/client.py`).
`claims.status` has no DB CHECK, so legality is enforced in Python:

```
proposed         → tested | weakly_supported | replicated | concluded | invalidated
tested           → weakly_supported | replicated | concluded | invalidated
weakly_supported → replicated | concluded | invalidated
replicated       → concluded | invalidated
invalidated      → proposed            (reopen ONLY)
concluded        → ∅                   (terminal)
merged           → ∅                   (legacy/market-era, terminal)
```

Each call: validates the edge (and an optional monotone-rank guard for finding graduations),
applies it, writes a `direction_transitions` audit row stamped with the transition kind
(`graduate | conclude | gap | retire | supersede | reopen`), marks unaudited findings stale on
invalidation, and emits the lifecycle event (`direction.concluded` on conclude;
`claim.invalidated` on gap/retire/supersede). The five writers that route through it:

| Writer | Transition | `decided_by` |
|---|---|---|
| `state.persist_research_finding` | `graduate` / `conclude` (monotone) | `synthesis` |
| `dispatch._declare_gap` | `gap` | `closure` |
| `agents/ariadne/persist.persist_reflection` (retire verdict) | `retire` | `reflect` |
| `dispatch._reopen_gapped_directions` | `reopen` | `closure` |
| `agents/ariadne/persist.persist_directions` (supersede) | `supersede` (bulk + per-claim audit) | `deliberate` |

The free-text `invalidation_reason` is no longer the only record of *why* a direction died —
`direction_transitions.transition` makes gap-vs-retire-vs-supersede queryable.

## The engine: `harness/loop_engine.py`

One declarative `REGISTRY` of `Transition`s drives the loop forward, replacing the hand-written
`_maybe_adjudicate` / `_maybe_plan` / `_maybe_scholarship` / `_maybe_drive_experiments` and the
watchdog's `_rearm_research_spines`. Each transition declares a **from-guard** (an SQL predicate
re-derived from DB state every tick — state-derived and self-healing, never replaying event
history), an **owner** (whose mode dial pauses it — *defers*, never destroys), the **event** it
emits, a **dedup bucket**, and an optional **stall SLA**.

| Transition | Owner | Emits | Fires when | Dedup |
|---|---|---|---|---|
| `adjudicate` | ariadne | `direction.adjudicate` | scored, active, unadjudicated directions exist | per-tick singleton |
| `plan` | ariadne | `planner.plan` | approved direction with no tasks | per-tick singleton |
| `arc_review` | ariadne | `ariadne.review` | approved, no final lit_review (1/tick) | `arc-…-{cid}-{day}` |
| `arc_propose` | ariadne | `ariadne.propose` | has review, no proposal (1/tick) | `arc-…-{cid}-{day}` |
| `arc_article` | synthesis | `synthesis.article` | finding on file + settled (concluded/evidence-capped) | `arc-…-{cid}-{day}` |
| `experiment_coverage` | experiments | `experiment.requested` | approved + proposal, under coverage target, nothing in flight | `drive-exp-{cid}-{attempts}-{6h}` |
| `rearm_interpret` | experiments | `experiment.completed`/`failed` | terminal run nothing interpreted | `rearm-exp-{id}-{day}` |
| `rearm_conclude` | synthesis | `finding.synthesize` | threshold-crossed direction, stale finding | `rearm-synth-{cid}-{day}` |
| `rearm_audit` | evaluation | `task.completed` | completed task, unaudited findings | `rearm-audit-{tid}-{day}` |
| `rearm_attack` | critic | `finding.high_signal` | audited-pass rel≥8 finding, unattacked (1/claim) | `rearm-highsig-{fid}-{day}` |

**Re-arm is no longer special.** A transition whose precondition is still true simply fires
again next tick under its dedup bucket — so a crash / timeout / dial-off / cost-cap suppression
that orphaned a one-shot event self-heals on the next tick, instead of needing a bespoke re-armer.

**Stall SLA.** When a direction has been *eligible* for an SLA-bearing transition
(`adjudicate`/`plan`/`arc_review`/`arc_propose`) longer than its SLA, the engine emits
`loop.unclosed[stage_stalled:{stage}]` (active mode only) — a dead handler or a precondition that
never clears becomes visible instead of silent.

### Where it runs

The engine runs in the **pacemaker** (`harness/ariadne_pace.py`), where the `_maybe_*` it replaces
already lived, gated by `LOOP_ENGINE`. `_auto_approve` stays a discrete pacemaker step (it writes
the *gate*, not a stage transition). `_decide` (deliberate/reflect cadence, the exhaustion hatch,
the daily held re-adjudication) stays — it is Ariadne's cognitive cadence, not a stage transition.
The watchdog's `_rearm_research_spines` runs only when `LOOP_ENGINE` is off.

## The deliberate↔adjudicate feedback edge (anti-churn)

When the independent adjudicator holds an agenda wholesale, the re-frame must see *why* or it
re-proposes near-duplicates and the lab churns deliberate→hold→exhausted. Two halves close this:
- **Adjudication side:** the adjudicator reads prior directions *with their outcomes* (a question
  is only "answered" if a prior attempt concluded decisively) and re-looks at held directions daily.
- **Deliberation side:** `run_shadow` injects the recently-**held** directions + their hold
  rationales ("do NOT re-propose near-duplicates of these") into the deliberation context.
- **Visibility:** `loop.unclosed[deliberate_churn]` fires when every live direction is held and
  `agenda_exhausted` has recurred this hour — so the fix above can be judged.

## Observability

- **`ops.lab_doctor`** — *Research loop* section: the per-stage census, active blockers, and the
  24h `direction_transitions` tally.
- **`direction_stage_v`** — query `stage`, `blocker` per direction directly.
- **`direction_transitions`** — the audit trail: `SELECT transition, count(*) … GROUP BY 1`.
- **`loop.unclosed[*]`** indicators: `stage_stalled:{stage}`, `deliberate_churn`, plus the
  pre-existing `agenda_exhausted`, `sweep_unsettled`, `sweep_blind`, `unhandled_event`.

## Out of scope of the engine (intentionally hand-written)

`_decide` (cadence + exhaustion hatch) · the closure ladder `_advance_research_closure`
(a multi-step thin_corpus→acquire→scout→gap machine; its terminal `_declare_gap` write does route
through `advance_direction`) · `_reopen_gapped_directions` detection (its write routes through
`advance_direction`) · `_detect_eaten_events` / `_emit_lab_pulse` (telemetry) · `_auto_approve`
(a gate write).

## Env gates

| Var | Default | Effect |
|---|---|---|
| `LOOP_ENGINE` | off | engine drives the loop; `_maybe_*` + watchdog re-armer are skipped |
| `LOOP_ENGINE_SHADOW` | off | `_maybe_*` still drive; engine only *logs* what it would emit (cutover validation) |

Rollback is instant: unset `LOOP_ENGINE` and restart the harness. Migration 019 (the view + audit
table) is additive and harmless if unread; `advance_direction` routing is behavior-preserving and
not gated.
