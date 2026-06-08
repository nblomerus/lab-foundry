# Ariadne Build-and-Activation Plan

> **Revision note.** This was previously framed as an *activation readiness* plan — as if Ariadne already existed and only needed to be switched on through staged modes. She does not exist as code. This revision re-frames it as a **build-then-activate** spec, front-loads the agent itself and the one genuinely-missing substrate item (a retrieval eval suite), and adds the safety prerequisite the original omitted: *enabling the research workflow today is not read-only*.

## Purpose

Ariadne is the lab's strategic steering agent — the research **Principal Investigator (PI)**. She owns the mission → direction → hypothesis → subquestion tree and the per-hypothesis claim goals. She must not steer the lab until the lab can prove its knowledge, agents, routing, and evidence are trustworthy enough for strategic reasoning **and** until the system can *structurally* guarantee she is read-only while she is unproven.

**Identity bridge (read this first).** "Ariadne" is the human-facing alias for the research PI specified in the design docs as `pi/loop.py` — a 5-step deliberation loop (`assess_portfolio → set_expectations → decompose → decide_actions → reflect`). **It is not built.** The existing `agents/pi/` is a *different* agent: a reactive company/market-lifecycle controller that ratifies phase transitions and writes a market charter. The two share storage (`company_state`, the `claims` tree) and **cannot coexist** (see Stage 0). Do not reuse `agents/pi/` as Ariadne's base.

Core activation principle:

> **Ariadne only comes online once (1) the lab can prove its knowledge/agents/routing/evidence are trustworthy enough for strategic reasoning, and (2) the runtime can guarantee she writes nothing until proven.**

And the most important sequencing rule, unchanged:

> **Ariadne should first observe and recommend. Only later should she steer priorities.**

---

## How to read this plan

The original 0→12 "ladder" implied linear progress. In reality the substrate subsystems (scouts, trust gate, retrieval, graph, acquire bus, harness) are **largely independent** and have little true ordering between them — serializing them manufactures a false sense of progress and gates Ariadne on building an entire platform. So this plan is organized as **three parts**, and each requirement is tagged with the lowest mode it actually gates:

```text
Part I   Control plane      — must exist before ANY agent runs (gates: shadow)
Part II  Trusted substrate  — the knowledge layer Ariadne reasons over
Part III Tests & modes      — proving the agent, then shadow → advisory → active
```

Only three things truly block **shadow mode**: the control plane (Part I), a trustworthy **retrieval path + eval suite** (Part II §Retrieval), and the **agent + a read-only runner** (Part III). Everything else is advisory/active-gating and can follow.

---

# PART I — Control Plane (gates: shadow)

This is the prerequisite the original plan never named. It must exist before any research agent — including a "read-only" Ariadne — is allowed to run.

## Stage 0 — Safety-neutralize the market-PI

**"Shadow mode is read-only" is false at the system level today.** The moment `KNOWLEDGE_CORE_ONLY` is cleared to let *any* research agent run, `harness/main.py:246` also registers `phase.budget_exceeded` and `phase.transition_proposed`. That arms an unattended chain with **no human in the loop**:

```text
phase_budget_exceeded.py   (watchdog: 1.5× phase overrun)
        ↓  emits a FORCED phase.transition_proposed
phase_transition.py        (autonomous ratify → commitment/execution)
        ↓  writes full CharterContent (niche/audience/product/gtm) into company_state,
           marks losing claims 'merged', jumps to execution
curator constitution       (swaps EVERY agent's system prompt to the committed charter)
```

Because the market-PI shares Ariadne's storage, activating the workflow lets it overwrite the research mandate and re-parent/merge claims out from under any future Ariadne. **Research-PI and market-PI cannot coexist on this schema.**

Required before anything runs:

```text
✓ Hard-disable or remove the market-lifecycle handlers:
    phase_transition, phase_adjudicator, phase_budget_exceeded, claim_invalidated
✓ Neuter the execution-charter prompt-swap in the curator constitution
✓ Reconcile / quarantine the market columns of company_state (charter/niche/audience)
✓ Decouple Ariadne registration from the KNOWLEDGE_CORE_ONLY flag entirely
```

Note: the phase machinery is also partly **dead/contradictory** — the schema enum is `{frame,hypothesize,experiment,validate,write,submit}` but the code reasons about `{exploration,convergence,commitment,execution}` (`execution: None` in the budget map; the constitution keys on `current_phase=='execution'`, not a valid enum value). Disabling it removes a live hazard *and* dead code.

## Stage 1 — Build the per-agent mode dial

Today the **only** activation lever is one boolean that registers ten research handlers atomically. Flipping it yields a fully-writing, self-driving loop — not a read-only observer. The staged ladder has nowhere to attach.

```text
✓ Agent registry carries mode ∈ {off | shadow | advisory | active}
✓ Dispatcher honors mode PER HANDLER, not per-flag
✓ A shadow runner is STRUCTURALLY incapable of emitting events / writing state
✓ Split the overloaded ariadne_active(): it is currently BOTH the workflow gate
  AND the discovery-pump mode switch, so toggling "shadow" silently re-targets
  the pump (plan_sweep) and perturbs the substrate. These must be separate flags.
```

## Stage 2 — Build Ariadne's output storage

Her outputs must persist somewhere testable before shadow runs.

```text
✓ claim_kind discriminator on claims (backfilled): mission|direction|hypothesis|subquestion
✓ claim_goals table with the LOCKED fields only:
    expectation, kill_condition, novelty_target, next_milestone,
    priority_hint, status, outcome
```

Drop the plan's earlier invented claim-goal fields (`baseline`, `success_criteria`, `reviewer_risk`) — reviewer-risk belongs to the OUTPUT-gate panel (Critic/Novelty/Reviewer), not the PI's row.

## Stage 3 — Build the agent (not a harness)

```text
✓ Implement the research-PI loop: assess_portfolio → set_expectations →
  decompose → decide_actions → reflect
✓ Structured output schema (see Part III)
✓ Register it in the Agent Lab catalog as a DRY agent
```

---

# PART II — Trusted Substrate

What Ariadne reasons over. Current reality is sharply **bimodal**: the "what is relevant" layer (corpus + trust gate) is production-grade and over-delivers; the "why it matters" layer (reasoning graph, entity extraction, retrieval evaluation) is stub-to-absent. Optimize the second half — that is where Ariadne is actually blocked.

## Library — already seeded; keep it broad

The corpus over-delivers the original Stage-0 ask by ~100×: **~35k documents (≈25k preprints + ≈9k code repos), ~1.8M chunks at 99.7% embedded, ~35k certifications, ~316 quarantined**. So:

- **Do not "seed one narrow domain."** The frontier pump already produces a broad ML/AI corpus across ~48 topics. Pre-seeding a single niche (context graphs / GraphRAG) *short-circuits the central vision* — Ariadne "discovering" the lab should work on context graphs when the Library *only contains* context-graph papers is not discovery, it's reading back the builder's constraint. **Make picking the niche Ariadne's first shadow deliverable** (3–5 grounded candidate directions over the broad corpus). If a narrow demo seed is ever needed, label it explicitly: *"bootstrap demo — bypasses self-discovery."*
- The Stage-0 minimum-count table from the original plan is retired. **Counts are capability theater** when the capability is absent (see below). The continuous backpressure pump fills the substrate; treat thresholds as *"watermarks the pump has crossed,"* not a build phase to finish-and-freeze. Intake does **not** stop at activation.

### Capability gates that replace the old count thresholds

| Old count gate | Problem | Replacement (capability gate) |
|---|---|---|
| Context graph 500+ nodes / 2,000+ rels | Already "passed" by a flat `Paper-[:FROM]->Source` projection (one edge type) | ≥6 populated **reasoning** edge types (excl. provenance `FROM/BY`); a `paper-[:USES_METHOD]->method-[:EVALUATED_ON]->dataset` traversal returns grounded paths |
| Extracted methods 50+ / datasets 20+ | Gates a pipeline step that **does not exist** — reads 0/50 forever | Build-gate first: *"entity extractor implemented and wired into certify→graph,"* then score counts |
| Mimir 90% trust accuracy / 95% schema-valid | No gold set, no schema, no harness; classifier emits a categorical tier with **no numeric `trust_score`** | Frozen ~120-source offline gold set (`tests/test_trust_goldset.py`); define-or-drop `trust_score`; publish the real `certifications` schema. Until then label Stage "unmeasurable." |

## Scouts — exist; the gap is the contract, not the collectors

All five scouts (arXiv, Web, GitHub, OpenML, HF-Dataset) are real and feed Mimir (arXiv is the only one on by default). What's missing relative to the original Stage-1 ask is the **per-scout test contract**, not the scouts:

```text
input query · expected source type · mocked + real API modes ·
normalization output · dedup check · failure handling · rate-limit behavior ·
schema validation
```

Scout output should normalize to one envelope (this remains a good target; note it does **not** match the current `SourceDescriptor` shape — reconcile them):

```json
{
  "source_id": "arxiv_2405_12345",
  "source_type": "paper",
  "collector": "arxiv_scout",
  "title": "...", "url": "...", "authors": [],
  "published_at": "...", "raw_text_ref": "...",
  "metadata": {}, "topic_tags": [],
  "collection_reason": "Matched request rq_007",
  "confidence": 0.84, "status": "collected"
}
```

Invariant (unchanged, correct): **Scouts discover. Mimir verifies. The Library stores trusted knowledge.** No scout is ever an addressable unit of work.

## Mimir — the input gate (mature; measurement is the gap)

Mimir is the most mature agent in the repo: trust-gated ingest (stage → classify → certify/quarantine → embed), provenance via `certifications`, retraction/license hard-gates, dedupe, an LLM tie-breaker for the `web_unknown` boundary only, and the acquire/focus demand path. Keep the test categories (trust classification, quarantine, dedup, relevance, provenance, decision trace, request fulfilment, entity extraction, graph write, Library handoff).

Two corrections:

- The structured certify envelope shown in the original plan (`{decision, trust_tier, trust_score, actions[], library_write_allowed}`) is **not** the real output shape — Mimir writes a `certifications` row with `decision/reasons/signals/used_llm`, and there is no numeric `trust_score`. Either build the envelope or test against the real shape.
- **Entity extraction and graph write are the missing pieces, and they are the "doer" half.** The design splits Mimir (governor: the single trust write) from a Librarian (doer: fetch/chunk/embed/extract/upsert/MERGE-KG). The code collapsed both into one Mimir that calls deterministic pipeline functions — that's fine and the "never self-certify" invariant is preserved by the function split — but **the extract→graph step was never built** (0 Method nodes, 0 dataset rows). This is the single biggest substrate construction task.

Pass criteria before Ariadne (keep, but now measurable only after the gold set exists):

```text
✓ schema-valid certify outputs (define the schema first)
✓ trust classification accuracy on the frozen gold set
✓ 100% decision-trace coverage   (already true: every certify writes a certification)
✓ 0 unverified sources written as certified
✓ duplicates detected reliably · quarantine path works · Library write path works
✓ request → source → Library lineage works
```

> **No unverified source may enter the trusted Library.** (Unchanged.)

## Retrieval — the one correctly-placed, genuinely-blocking, genuinely-missing item

If Ariadne steers on retrieval and you cannot *measure* retrieval, every novelty/grounding judgement is unanchored. **This is the real gate for shadow mode.** Two facts:

1. **No retrieval eval suite exists** (only a stray comment).
2. The live path is **dense-only**: `corpus_search` (`library/corpus/tools.py:385`) does embed → pgvector ANN → a linear `0.60·sim + 0.30·trust + 0.10·recency` rerank, with `kg_expand` (vector+graph fusion) **present but unwired**. Of the original Stage-3 test types, **semantic ✅ provenance ✅ freshness ⚠️ (a rerank term, not a query) graph ❌ hybrid ❌**.

This collides with the standing owner constraint: **use the full rag-bench retrieval stack** — hybrid BM25 + dense + cross-encoder rerank (`rag_bench/core/retriever.py`), graph-chunk injection (`graph_retriever.py`, GraphRAG), corrective RAG (`crag.py`), agentic retrieval (`agent.py`). rag-bench's ingest (chunker/parser) is *already vendored*, so adopting more of it is an established pattern, not a new dependency.

**Plan (front-loaded, before graph work):**

```text
1. Build a frozen retrieval eval suite FIRST:
     ~30–50 queries, labeled relevant doc/chunk IDs, recall@k + nDCG@k,
     failed-retrieval logging.
2. Baseline the current dense-only path against it.
3. Adopt the full rag-bench retrieval, PORTED onto pgvector/nomic (do NOT
   reverse the nomic-768d/pgvector decision for Chroma/BGE), phased:
     shadow:   BM25 (Postgres tsvector/ts_rank) + dense, fused (RRF),
               cross-encoder rerank, + graph-chunk injection from Neo4j
     advisory: + CRAG relevance gate
     active:   + agentic multi-step retrieval
   Make the port/no-port call EMPIRICALLY from the eval numbers, not by assertion.
4. Couple Ariadne's "novelty gap" trust to the per-topic recall number — a "gap"
   is indistinguishable from a recall miss below the bar.
```

## Context graph — demote to a post-shadow milestone, build to the *locked* vocabulary

The original Stage-4 specced 13 node types / 16 edge types. The live Neo4j is a **flat `Paper-[:FROM]->Source` projection with one relationship type**, so the headline query "show the strongest novelty gap for topic X" cannot run against it. But a *read-only shadow Ariadne does not need the rich graph* — she can run novelty over the production vector corpus plus the existing `kg_prior_claims` CONTAINS-match, which is the design's own `recall_prior_art = corpus_search ∪ kg_prior_claims`.

So:

- **Shadow** runs on `corpus_search` + `kg_prior_claims` over the broad corpus. Sufficient.
- The rich graph is an **advisory/active milestone**, and it must be built to the design's **locked** vocabulary derived from the read tools that already exist (`kg_prior_claims`, `kg_evidence`, `context_graph_explain`) — *not* a plan-invented taxonomy. Note `Direction` is `claim_kind='direction'` (a Postgres row), not a Neo4j node; don't invent `Direction`/`Method` nodes that clash with reality.
- The graph is a deterministic Neo4j projection (not Graphiti/Zep). The `§2` cognition layer (AgentRun/Interaction/Decision as graph citizens, `context_graph_explain`/`state_as_of`) is entirely unbuilt and is an active-mode concern.

## Acquisition — use the existing levers; do NOT build a request lifecycle

The original Stage-5 standalone 8-state queue (`created→routed_to_scouts→under_mimir_review→…`) and topic-level `rq_NNN` object with scout fan-out is a **parallel invention the design explicitly rejected**, and it gates *active*, not shadow (a read-only agent writes nothing, so it cannot exercise a request lifecycle by definition). Reality has two correct levers:

```text
single-source PULL   request_acquire → acquire.requested  (allow-list pi/researcher/novelty;
                     reply vocab approved/rejected/already_have/rate_limited; requester
                     does NOT wait for ingest)
broad-topic PUSH     request_focus() / plan_sweep          (steers the scout sweep toward the
                     PI's active claims)
```

**Delete the lifecycle and the `rq_NNN` fan-out.** Reframe this work as *"verify the existing acquire + focus levers and their lineage"* (the allow-list already includes `pi`). "Ariadne never directly controls scouts; she sets focus/agenda and pulls single sources via Mimir" — the *principle* in the original plan is right; the fan-out-to-named-scouts mechanism is wrong.

## Agent harness — Agent Lab DRY is enough for shadow

The rich harness in the original Stage-6 (mock/replay/shadow/active modes, tool-call trace, cost, replay, diff-vs-previous, input-state editor) is **advisory+ tooling, not a shadow prerequisite**. What exists — the **Agent Lab** (`api/agentlab.py` + `web/app/agents/`) — already runs an agent in isolation, DRY, schema-validated, with latency/tokens/prompt-preview and self-evaluating suites for Mimir + collectors. That is sufficient to bring Ariadne up in DRY/shadow. Defer replay/diff/cost/tool-trace until they're needed.

Every agent output stays **structured** (this is correct and important). Example Ariadne output:

```json
{
  "mission_frame": "...",
  "directions": [],
  "claim_goals": [],
  "novelty_risks": [],
  "requests": [],
  "kill_conditions": [],
  "priority_updates": [],
  "reflection": "..."
}
```

---

# PART III — Proving Ariadne, Then Activating Her

## Ariadne-specific tests (keep — these are good)

She needs deeper tests than other agents because she sets strategy.

1. **Mission framing** — from a seed problem, produce a clear research mission, domain, possible contribution, constraints.
2. **Direction tree** — 3–5 directions, each with a novelty rationale, claim goals, risks, and kill conditions. A direction MUST carry a `novelty_rationale` grounded in a **first-party prior-art query** — this is the binding spawn gate, not a checkbox.
3. **Novelty judgement** — given a saturated direction, downgrade priority and explain the closest prior work. (This is the hardest capability; it depends on retrieval quality — see Part II §Retrieval.)
4. **Claim-goal quality** — every claim goal carries the locked fields (`expectation, kill_condition, novelty_target, next_milestone, priority_hint`).
5. **No task writing** — tempted to write tasks, she creates claim goals / requests instead. (Planner is sole scheduler. ✅ matches design.)
6. **No raw investigation** — asked to "search GitHub," she creates a Mimir request, not a scout call. (✅ matches design.)
7. **Mimir override resistance** — an unverified-but-useful dataset is marked exploratory only, with a verification request.
8. **Milestone reflection** — a 5% improvement below kill threshold → she pauses/downgrades/requests more evidence only if justified.
9. **Reviewer-risk awareness** — surfaces weak evaluation, weak baselines, LLM-judge bias, novelty/reproducibility concerns.
10. **Priority steering** — reprioritizes on evidence, not vibes.

## End-to-end scenarios (keep)

1. **Field update** — Ariadne requests recent work → Mimir routes via acquire/focus → scouts collect → Mimir certifies → Library updates → Ariadne receives a grounded brief → updates the direction tree. *Pass: request trace complete; sources certified; Library updated; brief grounded; direction update references verified sources.*
2. **Bad source** — Web scout finds an SEO "SOTA" blog → Mimir quarantines → Ariadne cannot cite it. *Pass: quarantined; not retrievable as trusted; decision trace stored.*
3. **Saturated direction** — Library shows crowding → Ariadne downgrades, names closest prior work. *Pass: prior work identified; update justified; no blind new work.*
4. **Claim-goal creation** — Ariadne proposes a claim goal → Planner makes tasks. *Pass: Ariadne creates a claim goal not a task list; Planner creates tasks; goal linked to direction.*
5. **Evidence feedback** — weak result → Critic flags limitations → Ariadne reflects and updates. *Pass: reflection grounded in result; kill condition considered; direction updated; lessons recorded.*

## Observability & audit (mostly real; close the gaps)

Already strong: `agent_runs` (model/tier/tokens/cost_usd/timestamps), `agent_sessions` step DAG + `/trace` UI, `certifications` decision traces. Gaps to close for full replayability: full **input state** (only a summary today), retrieved-context snapshot, `prompt_version`, explicit tool-call list, and an explicit downstream-writes link (only event causality today).

Every strategic decision should answer: *who decided, what context they saw, what sources supported it, what was ignored, what changed, can we replay it.*

## Activation criteria

### Shadow mode — Ariadne may enter when:

```text
✓ Control plane done (Part I): market-PI neutralized; per-agent mode dial honored;
  shadow runner structurally cannot write; ariadne_active() split from the pump
✓ Ariadne agent + output schema + claim_goals storage exist
✓ Retrieval eval suite exists and the live path is baselined (Part II §Retrieval)
✓ Corpus readable via Plane-1 (corpus_search + kg_prior_claims) — already true
✓ Ariadne harness validates output schema (Agent Lab DRY)
✓ She cannot write tasks · cannot directly scout · cannot promote claims
```

Dropped from the original shadow criteria as not-actually-blocking: *"Request system works"* and *"Context graph has usable relationships."*

### Advisory mode — when:

```text
✓ Shadow trajectory (not just snapshots) reviewed by a human
✓ Direction trees useful — ≥2 of 3 named raters score ≥3/5 on a WRITTEN rubric
✓ Claim goals well-formed — 100% validate against the locked claim_goals schema
✓ Novelty rationales grounded — 100% of citations resolve to certified in-corpus
  document_ids from a first-party prior-art query
✓ Requests to Mimir are valid acquire/focus calls (no invented lifecycle)
✓ No hallucinated citations · no unverified sources used as evidence
✓ Trust-decay model exists (see Risks) · prompt-injection wrapping in place
```

Note: *useful / well-formed / grounded* are now hard machine predicates where possible; only "useful" stays subjective, and it requires named raters + a rubric.

### Active steering mode — when:

```text
✓ Human approves the first direction tree
✓ Planner can consume claim goals · Critic can challenge directions
✓ Mimir can fulfil Ariadne acquire/focus requests
✓ A promotion gate blocks unsupported promotion (NB: no such Gate exists today —
  it must be built; only the Mimir trust gate + dispatcher friction gates exist)
✓ Per-agent cost/loop ceiling for Ariadne (see Risks)
✓ Graduated de-activation control exists (active→advisory→shadow→off)
✓ Full audit trace exists
```

---

# Risks & Gaps to Engineer (fold in; don't expand into stages)

- **Trust decay / re-certification.** Seeded docs are `status='certified'` but `trust_state='provisional'` (bulk-approved; the certify lifecycle never ran), and `'certified'`/`'decayed'` are currently *unreachable* states. A paper retracted *after* ingest reads as verified forever. Make decay an advisory prerequisite; flag the corpus as "exploratory/provisional" to shadow Ariadne.
- **Prompt injection.** Scouted web text flows into Mimir's LLM and (later) into Ariadne's reasoning over retrieved chunks; `build_context` emits bare `[#i] {text}`. Wrap retrieved context in delimiters with a standing "data, not commands" instruction; add an injection case to the Mimir gold set. (The tier-cap and metadata-only tie-breaker are already enforced — document as invariants.)
- **Cost / loop ceilings.** The only brake today is a global daily kill-switch that, when tripped, *starves Mimir intake* (Mimir isn't in `URGENT_EVENTS`). Add an Ariadne-scoped suppression lever, a deliberation-loop iteration cap, and a max-requests-per-reflection bound.
- **Feedback loop for wrong output.** Shadow produces artifacts nobody is required to grade. Persist graded shadow runs with an error taxonomy so each run improves the next.
- **Shadow tests the wrong thing if frozen.** Steering a frozen lab ≠ steering a live one. Run shadow against the **live event stream, read-only, over N ticks**; demote snapshot replay to CI regression. The advisory review judges the *trajectory* (did kill_conditions fire, did she avoid agenda thrashing).
- **Readiness board.** Per-stage percentages are vibes (no formula), and averaging hides that the state is **bimodal** (corpus ~95%; graph/requests/Ariadne ~0%). Replace `%` with `{state, gates: shadow|advisory|active}` status, add an **Ariadne row** (the one hardest blocker), and never average a structurally-empty item into a benign middle number.

---

# What To Build First

The smallest honest path to a first shadow-mode Ariadne, given what exists today. Copy the one proven template: `ops/mimir_firstlight.py` (a real isolated end-to-end driver).

```text
1. SAFETY-NEUTRALIZE the market-PI (Stage 0). Before anything runs.
2. STORAGE: claim_kind on claims + claim_goals table (locked fields).
3. THE AGENT (not a harness): the 5-step research-PI loop + output schema.
   Bridge naming: Ariadne == spec'd pi/loop.py, NOT the existing agents/pi/.
4. MODE DIAL: per-agent off|shadow|advisory|active honored per-handler;
   split the overloaded ariadne_active() from the discovery pump.
5. RETRIEVAL EVAL SUITE (the one real substrate blocker): frozen ~30–50 query
   gold set, recall@k/nDCG@k; baseline the dense path; make the full-rag-bench
   port call empirically; couple novelty trust to per-topic recall.
6. ops/ariadne_firstlight.py (copy mimir_firstlight): read live corpus via
   Plane-1, run the loop once, ground each novelty_rationale in a first-party
   prior-art query, emit the schema, WRITE NOTHING, print and read back.
7. GRADE with predicates, not adjectives: 100% schema-valid claim_goals;
   100% novelty citations resolve to certified in-corpus docs; ≥2/3 raters
   score "useful" ≥3/5. Run against the LIVE stream; review the trajectory.
```

Everything else — rich context graph, request-lifecycle work, replay/diff harness, trust-decay, per-agent cost ceilings, the promotion Gate — moves **after** first shadow output, driven by what that output reveals as weak. Front-load the agent and the measurement instrument; defer the platform.

---

# Final Activation Rule

> **Ariadne cannot steer the lab until the runtime can guarantee she writes nothing while unproven, Mimir can prove what the lab knows, the Library can retrieve it with provenance *and we can measure that retrieval*, and the agent itself exists and can be run in isolation and replayed.**

Neutralize the hazard. Build the agent. Build the measuring instrument. Then activate the PI — slowly.
