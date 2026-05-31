# LabFoundry — Technical Architecture (Revision 1)

_A repurposing of the Boardroom harness into an AI-native ML/AI research lab that discovers, validates, and publishes. Written 2026-05-28. Assumes you've read `ARCHITECTURE.md` (Boardroom) and `labfoundry_org_chart.md`, and that you own `rag-bench`. This document is the bridge between the three._

---

## 0. The thesis in one paragraph

You do not need to build a new system. **Boardroom is already 70% of LabFoundry.** The phase machine, the event-sourced Postgres bus, the four-tier router, the Curator, the friction primitives, the session/trace framework, and the MCP pattern are domain-agnostic agent infrastructure. The thing that makes Boardroom work — *a unit of belief (a thesis) that carries a confidence, gets attacked by an adversary, gated by an auditor, and can only advance through quality checks* — is the exact shape of the **Claim Ledger** that `labfoundry_org_chart.md` puts at its center. Swap the domain ontology (thesis→claim, CEO→PI, business-evidence→scientific-evidence), bolt on **three genuinely new subsystems** (an experiment-execution sandbox, a Data Foundry with a blocking Data Steward, and a publication pipeline), and fold `rag-bench` in as both the literature-retrieval organ and the seed of the queryable research graph. Then harden the harness with the 2026 techniques you asked about — context engineering, durable execution, sub-agent tree search, layered memory with consolidation, and adaptive GraphRAG. The result is a lab that can take a research mandate and produce a validated, reproducible, paper-ready claim without you touching it.

---

## 1. What this revision changes, at a glance

| Concern | Boardroom (today) | LabFoundry (target) | Effort |
|---|---|---|---|
| Unit of belief | `theses` + confidence | **`claims`** with a status ledger (`proposed → tested → weakly_supported → replicated → paper_ready`) | Re-map (schema + handlers) |
| Top decision-maker | CEO handlers | **Principal Investigator (PI)** | Re-prompt |
| Evidence gathering | web `researcher.py` | **Knowledge Acquisition** (arXiv / GitHub / OpenML scouts) + **experiment execution** | Split + 1 new runtime |
| Quality gate | Auditor (slop scoring) | **Evaluation Division** (metrics, statistics, leakage, reproducibility) | Re-map + extend |
| Refutation | Adversary | **Critic** (novelty, baselines, leakage, cherry-picking, over-claiming) | Re-prompt + extend |
| Planning | Planner (v2: assess→propose→critique) | **Experiment Designer** (same 3-step shape) | Re-prompt |
| Learning | Reflection → `lessons` | **Methodology lessons** (procedural memory) | Keep |
| Memory | Zep episodic + Postgres | **4-layer memory**: working + episodic + temporal research graph + procedural | Extend (fold in rag-bench Neo4j) |
| Retrieval | SearXNG/DDG/HN/Reddit | **Adaptive GraphRAG** over 21k-paper corpus + the lab's own knowledge | Fold in rag-bench |
| Success criterion | "ship to a paying stranger in 30 days" | "a claim that **survives adversarial + statistical + reproducibility review** and is reproducible from pinned artifacts; paper passes an automated reviewer threshold; human approves submission" | Redefine |
| New: run real experiments | — (HTTP-only "experiments") | **Experiment sandbox** (code exec, training/eval, artifacts, seeds, compute budget) | **Build** |
| New: dataset governance | — | **Data Foundry** + blocking **Data Steward** | **Build** |
| New: publish | — | **Publication pipeline** (LaTeX, figures w/ vision review, internal reviewer, rebuttal) | **Build** |

**Repurposing ratio:** of the LabFoundry MVP's 9 agents (§9), ~6 are re-prompts/re-schemas of existing Boardroom handlers. Only **3 are genuinely new runtimes** — the ML Engineer (execution sandbox), the Data Steward gate, and the Paper Writer. Everything below the agent layer (events, router, curator, trace, friction) is reused as-is.

---

## 2. The core reframe: Boardroom → LabFoundry mapping

```
BOARDROOM                          LABFOUNDRY
─────────                          ──────────
seed (problem, stance, KPI)   →    research mandate (topic, success bar, compute budget)
thesis (confidence 0..1)      →    claim (status machine + confidence + evidence set)
CEO (synthesize/rescore/spawn)→    Principal Investigator (agenda, hypothesis selection, paper-worthiness)
researcher (web tool-loop)    →    Knowledge Acquisition scouts + Experiment execution
auditor (slop score)          →    Evaluation Division (metrics / stats / leakage / reproducibility)
adversary (kill/weaken/watch) →    Critic (same verdict shape, science-specific attacks)
phase adjudicator             →    phase adjudicator (research lifecycle)
planner (assess→propose→crit) →    Experiment Designer (same 3-step pipeline)
reflection (lessons)          →    methodology lessons (procedural memory)
finding (web evidence)        →    evidence (literature claim OR experiment result)
phases: explore→commit→exec   →    frame → hypothesize → experiment → validate → write → submit
```

Notice how clean the **critic gate** mapping is. In Boardroom, `finding.high_signal` is gated by the Auditor and `thesis.invalidated` is gated by the Adversary — "the chain only progresses past quality checks." That *is* the scientific method the LabFoundry doc is asking for: "separate discovery from validation, separate results from claims." You already built the discipline; LabFoundry just renames the roles and adds science-specific checks.

---

## 3. Revised domain model

### 3.1 The Claim Ledger (the heart of the lab)

This replaces `theses`. A claim is the lab's unit of belief and the thing the publication pipeline is allowed to write about. Its status machine is the central invariant:

```
proposed ──(experiment run + metrics)──▶ tested
tested ──(passes eval, survives 1 critic pass)──▶ weakly_supported
weakly_supported ──(reproducibility agent re-runs, result holds)──▶ replicated
replicated ──(internal reviewer ≥ threshold, no open critic objections)──▶ paper_ready
            ◀──(critic kills / leakage found / fails to reproduce)── any state can demote
```

This is the Boardroom thesis-confidence loop with named gates. Each transition emits an event, exactly as thesis confidence changes do today. The **Paper Writer cannot cite a claim below `paper_ready`** — enforced the same way the Curator gates lessons on `status IN ('probationary','active')`.

Key tables (Postgres, transactional truth):
- `claims` (id, statement, status, confidence, hypothesis_id, created_at, superseded_at)
- `evidence` (id, claim_id, kind: `literature|experiment`, source_ref, stance: `supports|refutes|neutral`, strength, quote/metric, provenance)
- `experiments` (id, hypothesis_id, plan JSONB, dataset_id, status, compute_budget_gpu_h, env_hash, seed, metrics JSONB, artifact_path)
- `datasets` (id, source, license, dataset_card JSONB, split_strategy, leakage_report JSONB, steward_status: `blocked|approved`)
- `decisions` (id, who, what, rationale, claim_ids[]) — the Decision Ledger
- plus the existing `events`, `agent_runs`, `agent_sessions`, `cooldowns`, `cost_tracking`, `lessons`.

### 3.2 The research phase machine

Redefine `company_state.current_phase` from Boardroom's four phases to six. The machine, budgets, and the watchdog's `phase.budget_exceeded` logic carry over unchanged — only the phase set and the constitution text change.

| Phase | Goal | Dominant agents | Exit condition |
|---|---|---|---|
| **frame** | Turn mandate into researchable questions; survey lit/code/data | PI, Knowledge scouts, Dataset scout | A ranked question set + a feasible dataset shortlist |
| **hypothesize** | Generate testable hypotheses with predicted outcomes + failure conditions | PI, Hypothesis agent | ≥1 hypothesis with an experiment plan the Steward can approve |
| **experiment** | Run experiments via agentic tree search | Experiment Designer, ML Engineer | Metrics collected for the promising branch(es) |
| **validate** | Eval, critic attack, reproducibility re-run | Evaluation Division, Critic | Claims reach `weakly_supported`/`replicated` or die |
| **write** | Draft paper, figures, internal review, rebuttal | Paper Writer, Figure agent, Internal Reviewer | Reviewer score ≥ threshold, no open objections |
| **submit** | Human approval + (optional) external submission | Approval Gate, Human Sponsor | Human approves or rejects |

### 3.3 The success contract (preserve the forcing function)

Boardroom's "ship to a paying stranger" is a *grounding* forcing function — it stops the swarm from believing its own narrative. The research analog must preserve that. The strongest analog is **not** "write a paper"; it is:

> A claim that (a) survives an adversarial Critic pass, (b) passes statistical and leakage review, (c) **reproduces from pinned environment + seed**, and (d) is written into a paper that scores above an automated-reviewer threshold — before a human is asked to approve.

The reproducibility re-run is the "paying stranger": an *independent* re-execution that doesn't get to believe the first run's story. Sakana's AI Scientist used an automated reviewer threshold (their accepted workshop paper averaged 6.33, above the human acceptance bar) as the external signal — adopt the same idea as the `write→submit` gate.

---

## 4. Runtime topology (revised)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  YOU (research sponsor)  →  dashboard :8088  →  approval gate                  │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                     ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  labfoundry-api.service  FastAPI :8503                                         │
│  routers: snapshot, stream(WS), claims, experiments, datasets, trace, bench    │
└──────┬─────────────────────────────────────────────────────────┬──────────────┘
       │ asyncpg                                                  │ pg_notify
       ▼                                                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  Postgres 16 — operational truth + event bus + LEDGERS                         │
│  claims, evidence, experiments, datasets, decisions, hypotheses,               │
│  events, agent_runs, agent_sessions, cooldowns, cost_tracking,                 │
│  compute_budget, lessons, checkpoints                                          │
└──────┬───────────────────────────────────────────────┬───────────────────────┘
       ▲                                                 ▲
       │                                                 │
┌──────┴───────────────────────────────────────────────┴────────────────────────┐
│  labfoundry-harness.service  (the Boardroom dispatcher, unchanged spine)        │
│   ├─ Dispatcher (LISTEN; friction: cooldowns + cost/compute caps + integrity)   │
│   ├─ Watchdog (stale tasks, orphans, phase budget, checkpoints)                 │
│   ├─ Router (4 tiers; premium + free + local)  ── GPULock (arbitrates! §6.7)    │
│   ├─ Curator (layered prompts + compaction + offloading + probe-eval §6.1)      │
│   └─ Sub-agent registry (PI, scouts, designer, engineer, eval, critic, writer)  │
└──┬─────────────┬───────────────┬──────────────┬───────────────┬────────────────┘
   ▼             ▼               ▼              ▼               ▼
┌──────────┐ ┌───────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────────┐
│ Ollama   │ │ Premium   │ │ Experiment   │ │ rag-bench    │ │ MCP servers    │
│ :11434   │ │ chain     │ │ sandbox NEW  │ │ retrieval    │ │ state /        │
│ 2 GPUs   │ │ DeepSeek→ │ │ (code exec,  │ │ (hybrid +    │ │ knowledge /    │
│ (shared  │ │ OpenAI→   │ │ train/eval,  │ │ GraphRAG +   │ │ data-foundry / │
│ w/ exp!) │ │ GitHub…)  │ │ artifacts)   │ │ CRAG + eval) │ │ publication    │
└──────────┘ └───────────┘ └──────────────┘ └──────┬───────┘ └────────────────┘
                                                    ▼
                                          ┌──────────────────────┐
                                          │ Neo4j — TEMPORAL      │
                                          │ RESEARCH GRAPH        │
                                          │ papers│datasets│claims│
                                          │ experiments│decisions │
                                          │ (validity-windowed)   │
                                          └──────────────────────┘
┌──────────────────────────────────────────────────────────────────────────────┐
│  Zep (episodic) · Langfuse (traces) · SearXNG · Prometheus/Grafana (rag-bench) │
│  labfoundry-liveness.timer (external stall detector, unchanged from Boardroom) │
└──────────────────────────────────────────────────────────────────────────────┘
```

Two new pieces are first-class: the **experiment sandbox** (a compute consumer that now contends with Ollama for GPUs — see §6.7) and **Neo4j as the semantic research graph** (alongside Postgres as operational truth — see §7.2).

---

## 5. How the three codebases combine

| Codebase | Role in LabFoundry | What changes |
|---|---|---|
| **Boardroom** | The spine: events, dispatcher, router, curator, session/trace, friction, dashboard, MCP pattern | Re-ontology; new ledgers; new friction primitive (compute budget); harness upgrades §6 |
| **rag-bench** | Knowledge Acquisition retrieval organ + seed of the semantic graph + grounding/eval discipline | Add a **write path** (§6.4); generalize CRAG into adaptive routing (§6.5); expose as an MCP server `boardroom-knowledge` |
| **NEW code** | Experiment sandbox, Data Foundry, Publication pipeline, experiment-manager tree search | Built from scratch (templates: AI-Scientist-v2 for the loop, your existing v2-loop pattern for structure) |

The happy accident: **all three already target the same dual-GPU + Ollama + qwen2.5:14b box.** Boardroom and rag-bench share the inference stack; integration is mostly wiring, not porting. The unhappy consequence: experiments now compete with inference for the same GPUs — addressed in §6.7.

---

## 6. Harness upgrades (the "latest techniques" you asked for)

Each upgrade is framed as: *what you have now → the current technique → the concrete change → why it matters here.*

### 6.1 Curator → context engineering

**Now:** layered prompts with priority (system/constitution → phase/task → lessons → recall), `tiktoken` budgeting, and — as you flagged — recall compaction is "naive truncation."

**Current practice (2026):** context engineering is the dominant reliability lever; the field's framing (Phil Schmid / Manus / Martin Fowler's "harness engineering") is four operations — **offload, reduce/compact, retrieve selectively, isolate** — with the context window treated as a constrained budget across three zones (stable system, dynamic, query). The recurring failure mode is agents going "sloppy around step 15–20" from context rot, which is precisely the regime a multi-hour research run lives in.

**Concrete changes:**
1. **Replace truncation with real compaction.** Your design note already calls for "an F-tier summary preserving decisions/dissent/dates" — build it. Compaction is the single highest-value change because the lab's contexts (papers + code + experiment logs) are an order of magnitude larger than Boardroom's web snippets.
2. **Context offloading.** Full papers, large evidence sets, and experiment logs live in Postgres/files and enter the window *by pointer + just-in-time summary*, never wholesale. The `fetch_cache` pattern generalizes to an "artifact store" the Curator references.
3. **Explicit per-zone token budgets** (system / dynamic-retrieved / query), logged per `agent_run` so `/trace` shows budget pressure and what got dropped.
4. **Probe-based context eval (new surface).** Periodically test whether the assembled context actually *contains* the facts a step needs (inject a probe question, check recall). This is a context-quality metric the way rag-bench measures retrieval quality — wire it into the bench/debug tabs.

### 6.2 Dispatcher / Session → durable execution

**Now:** event-sourced Postgres bus, `agent_runs` journal with `fallback_attempts`, a `replay` mode that bypasses cost/side-effects, an internal watchdog, and an external liveness timer.

**Current practice (2026):** durable execution went mainstream for exactly your reason ("no silent flatlines"). The two dominant mechanisms are **journal-based replay** (record each step, replay on crash) and **database checkpointing** (persist state at each node), with the **Saga pattern** for compensating rollbacks on partial failure. The "harness/compute split" (orchestration logic separate from LLM+tool execution) is the canonical shape. Frameworks (LangGraph, Pydantic AI, OpenAI Agents SDK, Temporal, Microsoft Agent Framework) all adopted it. Reported impact: checkpointing cuts wasted compute 60%+ on multi-step workflows; >4-hour runs without persistence carry ~90% higher total-failure risk.

**You already have journal-based replay** (event sourcing + `agent_runs` + replay mode) and the harness/compute split (dispatcher vs router). Don't adopt a framework — harden what you have:
1. **Explicit checkpoints** at phase boundaries and immediately before/after expensive experiment runs (a `checkpoints` table snapshotting claim-ledger + experiment state). A crashed 6-hour training run should resume from the last checkpoint, not phase-restart.
2. **Saga-style compensation** for partial experiment failure: a training job that dies mid-run fires a compensating action (clean partial artifacts, mark the experiment `failed_but_informative`, write a negative-result evidence row) instead of leaving the claim ledger in a corrupt state. Negative results are *kept* — the LabFoundry doc explicitly wants "experiments that failed but were still informative."
3. **HITL suspend/resume** mapped to the event bus for the `submit` gate: the workflow durably suspends awaiting human approval (hours/days) without holding state in memory — a direct use of the suspend/resume primitive.

### 6.3 Handlers → sub-agent registry + agentic tree search

**Now:** 10 stateless handler files, one per event type; the v2 loops decompose single-shot calls into 3–5 bounded invocations; researcher v2 already parallelizes sub-questions (≤4) under the GPULock.

**Current practice (2026):** the orchestrator-worker / "deep agents" pattern treats sub-agents as *first-class autonomous agents* with their own planning loop, memory context, and tool access, registered with capability descriptions so the orchestrator can delegate. For research specifically, the proven design is **progressive agentic tree search guided by an experiment-manager agent** (Sakana AI Scientist-v2): the system branches on hypotheses/configs, prunes weak branches, and pursues promising ones — *not* a flat plan→run→audit line. This, plus a vision-capable critique of figures and an automated reviewer with an archive feedback loop, is what produced the first AI paper to pass peer review.

**Concrete changes:**
1. **Formalize the sub-agent registry.** The divisions (PI, scouts, designer, engineer, eval, critic, writer) become registered sub-agents with capability descriptions; the PI delegates by capability. Your dispatcher + session contextvar already give each invocation an isolated context — this is mostly making the registry explicit.
2. **Experiment Division adopts tree search.** Replace the linear planner→researcher line in the `experiment` phase with an **experiment-manager** that maintains a search tree of (hypothesis → config → result) nodes, scored by the Evaluation Division, expanded/pruned under the compute budget (§6.7). This is the single biggest *capability* upgrade — it's the difference between "runs one experiment" and "does research." The AI-Scientist-v2 repo is the concrete template; your existing v2-loop structure (`plan → fan-out → synthesize → gap_check → iterate`) is the right skeleton — tree search is `gap_check` generalized into branch/prune.
3. **Coding agent for the ML Engineer.** The execution step needs an Aider/Claude-Code-style coding sub-agent that edits an experiment template, runs it, reads errors, and iterates — gated by the sandbox (§8.1).

### 6.4 Memory → four-layer model + consolidation + write path

**Now:** Zep episodic (theses-lifecycle, dissent, ceo-deliberations, charter, phase-transitions) + Postgres tables + the `lessons` table.

**Current practice (2026):** the field has converged on the **CoALA four-layer model** — working (context window), episodic (experiences), semantic/relational (a knowledge graph, ideally **temporal** with validity windows à la Zep/Graphiti/Mem0), and procedural (self-updating rules). Two further shifts matter: **consolidation** — the Feb-2026 position paper "Episodic Memory is the Missing Piece for Long-Term LLM Agents" argues agents get smarter "not by storing more but by consolidating what they store," and Anthropic shipped a **Dreaming** primitive (May 6 2026) that runs async between sessions to merge duplicates and surface patterns, modeled on hippocampal consolidation — and the **write path**: 2026 agents "need a write path, not just a retriever."

**Map your stack onto the four layers — you already have three of them:**
- **Working** = Curator-assembled context (§6.1).
- **Episodic** = Zep (keep as-is).
- **Semantic/relational** = the **temporal research graph** in Neo4j — fold in rag-bench's existing graph and add lab-generated nodes. This *is* the "Queryable Research Graph" the org chart centers on. Facts carry validity windows: `claim_012 was weakly_supported on 2026-06-01, replicated on 2026-06-09`. This answers the org-chart's target queries ("which datasets support claim_012", "which papers influenced our method") as multi-hop graph traversals.
- **Procedural** = the `lessons` table — already a procedural-memory / self-updating-rules system (promote after 5 supportive, retire after 3 contradicting). Keep it; it's ahead of most frameworks.

**Two additions:**
1. **The write path (critical).** rag-bench is read-only over published literature. LabFoundry must *write* its own generated knowledge into the graph. Every division emits typed nodes + provenance + temporal edges: papers→methods→datasets→experiments→claims→figures→drafts→critiques. Reuse rag-bench's `entity_extractor` + `graph_store` (MERGE dedup, batch writes) but point them at lab-internal events, not just arXiv ingestion.
2. **Consolidation ("Dreaming"-style).** Generalize your dissent-triggered reflection into a scheduled between-phase consolidation pass: review transcripts + evidence + graph, merge duplicate claims/entities, surface cross-experiment patterns, promote/retire lessons. This is `reconcile_lessons()` widened from "per-run" to "periodic global consolidation."

### 6.5 Retrieval → adaptive GraphRAG (fold in rag-bench)

**Now (rag-bench):** hybrid (BM25 + BGE + RRF) → citation boost → 1-hop graph augmentation → cross-encoder rerank → **CRAG confidence routing** (CORRECT/AMBIGUOUS/INCORRECT + HyDE) → relevance gate → grounded generation with `[Source N]` citations, plus a faithfulness/citation/relevance eval suite.

**Current practice (2026):** the emerging best practice is **Adaptive RAG** — a complexity/intent classifier routes each query to the cheapest sufficient pipeline: simple factual → hybrid+rerank; relationship/multi-hop → graph traversal; deep/open-ended → an agentic deep-search loop with **dual-channel retrieval** (semantic over chunks + relational over the graph, e.g. GraphSearch). **Failure-aware routing** (route by empirical failure patterns, not just query surface) and **LazyGraphRAG** (≈0.1% of full GraphRAG indexing cost) are the scaling moves. HippoRAG-style personalized-PageRank retrieval is the continual-memory direction.

**Concrete changes:**
1. **Generalize CRAG into the adaptive router.** Your CRAG confidence routing is already a precursor — add an intent/complexity classifier in front so a novelty check ("has anyone shown X?") routes to graph traversal, a method lookup routes to hybrid, and an open-ended literature synthesis routes to the agentic deep-search loop. This is the Knowledge Acquisition division's core skill.
2. **Dual-channel deep search** for the `frame` phase and for the Critic's novelty/baseline attacks: issue semantic queries over the 1.6M chunks *and* relational queries over the research graph in the same retrieval round.
3. **LazyGraphRAG for incremental indexing** — the corpus grows continuously (new arXiv ingestion + the lab's own write path), so cheap incremental graph updates matter.
4. **Reuse the eval suite as a grounding gate.** rag-bench's faithfulness/citation metrics become the signal the Auditor/Evaluation Division uses to gate literature evidence — a claim citing papers it didn't ground gets the same treatment Boardroom's auditor gives ungrounded findings.

### 6.6 Validation → keep Boardroom's rigor, add the science-specific critics

**Now:** Auditor derives a verdict from calibrated `audit_score` bands; Adversary issues kill/weaken/watch with a guaranteed confidence delta; slop circuit-breaker halts research on a thesis >40% slop.

**Current practice (2026):** automated reviewers (Sakana's Automated Reviewer, Google's ScholarPeer) now score papers near human-level against conference standards and feed an iterative improvement loop. Your validation layer is *already more rigorous* than most generative research systems — the gap in AI-Scientist-class systems is exactly the adversarial/ledger discipline you've built. Lean into it.

**Concrete changes:**
1. **Critic = Adversary + science attacks.** Keep the verdict shape; add the LabFoundry Critic's attack surface: novelty (against the research graph), baseline fairness, leakage, cherry-picking, claim-stronger-than-evidence, ignored negative results, "would a reviewer reject this?"
2. **Evaluation Division = Auditor, split.** Metrics agent (compute), Statistical Reviewer (significance, multiple-comparison correction, effect sizes, error bars), Leakage Detection (works with the Data Steward), Reproducibility (re-runs from pinned env+seed). Each gates a specific claim-ledger transition (§3.1).
3. **Internal Reviewer = automated reviewer** as the `write→submit` gate, scoring drafts to conference standards; its reviews feed the archive and the next generation's lessons (the AI-Scientist feedback loop, which your reflection system already implements in miniature).

### 6.7 New friction primitive: compute budget + GPU arbitration

This is the non-obvious one and it's important. Boardroom's five friction primitives assume cheap, fast LLM calls (local Ollama doesn't even count against cost caps). **ML experiments break that assumption** — they burn real GPU-hours, and on your hardware they contend with Ollama for the *same two GPUs*.

**Concrete changes:**
1. **Compute budget as a sixth friction primitive.** Add `compute_budget` (GPU-hours per experiment / per phase / per mandate), tracked like `cost_tracking`. The Governance office's "Compute Cost Agent" enforces it — built out as the **Resource Manager (§6.8)**; the experiment-manager's tree search prunes against it. On exhaustion, the lab must *converge on what it has* rather than launch new branches — the research analog of Boardroom degrading to local-only.
2. **GPULock must arbitrate inference vs. experiments.** Today the GPULock serializes per-model inference. It now also has to decide between (a) the lab's own LLM calls and (b) experiment training/eval jobs. Pinning inference to one GPU and experiments to the other (matching rag-bench's existing 5070Ti / 2070S split) is the floor; the committed design — a lease-based scheduler with preemption — is in **§6.8**. Flag this early — it's the most likely source of deadlock/starvation in the integrated system.
3. **Honest scope:** a consumer dual-GPU box bounds you to experiments that *fit* — small models, tabular/benchmark tasks, fine-tuning, ablations on existing checkpoints, and **reproductions** of published results. That's not a limitation to hide; reproduction studies and ablations are genuinely publishable and are the safest place for an autonomous lab to start (see §10).

### 6.8 Resource Manager (Quartermaster)

§6.7 names the sixth friction primitive; this is the component that implements it. It is **one agent plus one scheduler**, and the organizing principle is that they are different things: a deterministic *mechanism* you must never hand to an LLM, and an LLM *policy* that makes the judgment calls.

| Decision | Owned by | Why |
|---|---|---|
| Does this VRAM footprint fit on these GPUs? | **Mechanism** (scheduler) | Must be exact; a model can't "probably" fit in 16GB |
| Does the budget cover this run to completion? | **Mechanism** | Arithmetic on the ledger, not judgment |
| Grant / queue / reap a GPU lease | **Mechanism** | Fast, correct, runs on every request |
| Is this experiment worth its GPU-hours *now*? | **Policy** (agent) | Weighs claim value vs. remaining budget |
| Spill reasoning to DeepSeek, or wait for a GPU? | **Policy** | Trades dollars against GPU-seconds |
| Which branch to prune under budget pressure? | **Policy** | Ranks expected information gain per cost |
| Which run to preempt when inference is starved? | **Policy** | Picks lowest-value, most-resumable |

The scheduler enforces; the agent sets policy. Keep them separate and the agent's probabilistic latency never sits on the critical path of a VRAM check.

**Three allocation modes, not two GPUs.** Your hardware has a structure the scheduler must model explicitly:

| Mode | VRAM | Hosts | Conflict profile |
|---|---|---|---|
| 2070S alone | 8 GB | retrieval (BGE embed + reranker), light inference | low — retrieval lives here near-permanently (rag-bench's existing split) |
| 5070Ti alone | 16 GB | WORKHORSE/CODE inference (~9 GB) **or** a small experiment (≤~6 GB co-located, else evict the model) | the contended card |
| **both (spanned)** | 24 GB | local REASONING (`deepseek-r1:32b`, ~19 GB) **or** a large experiment | **mutually exclusive whole-box ops** |

The crux is the spanned mode: your REASONING tier's local model already needs both cards, so **local REASONING and any large experiment cannot run at the same time.** That single fact drives the policy — during the `experiment` phase, the Resource Manager routes REASONING-tier thinking to DeepSeek so experiments can own the box. Your premium chain already makes DeepSeek the REASONING lead; the manager gains the authority to *force* that routing under GPU pressure, not just on tier policy.

**Multi-currency budget — and the exchange rate that makes it an agent.** The manager balances three currencies: **GPU-seconds** (the hard physical ceiling), **DeepSeek dollars** (~$0.0006/call; derived from `deepseek_balance_log` deltas, since DeepSeek exposes no usage API), and **wall-clock** against the deadline. The core economic fact is that the second buys the first: every reasoning call spilled to DeepSeek frees GPU-seconds for experiments. Fittingly, the manager itself runs *on* DeepSeek, off-box — so allocating never consumes the silicon it allocates. A floor of reserved DeepSeek budget is non-negotiable: it guarantees the lab can always think even when both GPUs are committed.

```
compute_budget                      -- mirrors cost_tracking; one row per scope
  id
  scope_type        mandate | phase | experiment
  scope_id
  gpu_seconds_allocated             -- hard ceiling for this scope
  gpu_seconds_reserved              -- committed to admitted-but-unfinished runs
  gpu_seconds_consumed              -- reconciled from nvidia-smi sampling
  deepseek_usd_allocated
  deepseek_usd_consumed             -- from deepseek_balance_log deltas
  wall_clock_deadline               -- timestamptz
  updated_at
  -- invariant (admission enforces): reserved + consumed <= allocated
```

**The scheduler: leases + preemption.** `GPULock` (router.py) widens from "per-model lock + global 4-in-flight semaphore" into a lease-granting scheduler. Inference takes a *shared* lease; an experiment takes an *exclusive* lease on the GPU set it needs. The live state:

```
gpu_leases
  id
  experiment_id            -- null for inference leases
  lease_class              exclusive | shared
  gpus                     int[]            -- {0} | {1} | {0,1}  (the three modes)
  vram_mb_reserved
  est_duration_s           -- drives admission + budget reservation
  state                    requested | granted | active | preempted | released | leaked
  granted_at
  heartbeat_at             -- liveness; watchdog reaps stale leases
  released_at
```

Lease lifecycle — admission is **reserve-to-finish**, not reserve-to-start, so a 90%-done run is never killed by the meter ticking over:

```
requested
   │  scheduler: VRAM fits on gpus[] (no exclusive-lease conflict)
   │             AND reserved + consumed + est_duration_s <= allocated (+ margin)?
   ├── no ──────────────────▶ deferred ──▶ re-queued (RM may resize or reject)
   └── yes ─▶ granted ──(process starts; heartbeat begins)──▶ active
                                                              │
                          ┌──────────(completes)─────────────┤
                          ▼                                   │
                      released                                │
                  (consumed reconciled, reserved freed)       │
                                                              │
              (OOM / crash; heartbeat stops) ─────────────────┤
                          │                                   │
                          ▼                                   │
                       leaked ──(watchdog reap)──▶ released   │
                          + Saga compensation (§6.2):         │
                          mark failed_but_informative,        │
                          write negative-result evidence      │
                                                              │
              (RM preempt: $ floor hit & inference starved) ──┘
                          │
                          ▼
                     preempted ──(checkpoint written, §6.2)──▶ requested
                                  (re-enters queue; resumes later)
```

**The agent.** A new event-driven handler, like every other role:

| | |
|---|---|
| **Triggers** | `experiment.proposed`, `budget.pressure`, `thermal.pressure`, `experiment.oom`, `gpu.contention` |
| **Emits** | admit / defer / resize decisions; `route.force_premium` (spill directives); `experiment.preempt`; branch-prune recommendations to the experiment-manager (§6.3) |
| **Reads** | `compute_budget` + `gpu_leases`, the claim ledger (portfolio value at stake), the experiment search tree (candidate value), `nvidia-smi` telemetry |
| **Tier** | REASONING via DeepSeek (off-box, so it never consumes the GPUs it manages) |

**Integration points — extend, don't add surface:**
1. **`GPULock` → scheduler** (router.py): per-GPU VRAM ledger, exclusive vs. shared leases, preemption. It already gates all GPU access — the natural home.
2. **Admission = the sixth dispatcher friction gate** (dispatch.py), alongside cooldowns / cost caps / slop breaker: an `experiment` task cannot be claimed unless the scheduler grants a fitting lease *and* the budget covers it to completion.
3. **Watchdog reconciliation** (dispatch.py, 5-min loop): sample `nvidia-smi` (utilization + per-process VRAM + temps — you already read watts in `/debug/costs`), reconcile `gpu_seconds_consumed`, reap leaked leases (exactly like the existing orphan-`agent_runs` reap), and emit `budget.pressure` / `thermal.pressure` the way it already emits `phase.budget_exceeded`.

**Three consumer-hardware failure modes it owns** — the `experiment`-phase analog of Boardroom's "silent flatline" class. (No MIG on these cards; partitioning is process-level via `CUDA_VISIBLE_DEVICES`, so a hot or VRAM-hungry experiment genuinely degrades everything sharing the box — isolation is cooperative, not enforced.)

1. **Thermal throttling.** Both cards at load generate heat; a throttle slows inference *and* experiments and reads like a hang. On `thermal.pressure` the manager serializes work / backs off before the throttle point rather than letting the box quietly degrade.
2. **OOM → learned footprints.** A run that under-estimates VRAM dies and burns GPU-seconds. Catch it, fire Saga compensation (above), and write the *observed* peak VRAM + duration back to the `lessons` table. The manager's estimates improve through procedural memory (§6.4) — a model the lab has run once is costed accurately the next time. (rag-bench's existing OOM protection is the precedent to lift.)
3. **Inference starvation.** The dangerous deadlock: the lab can't *think* because experiments hold the GPUs. The DeepSeek valve prevents true starvation while dollars remain; if the dollar floor is hit, the manager **preempts** the lowest-value, most-resumable run (checkpoint via §6.2), lets critical inference through, and resumes. The reserved-dollar floor is the guarantee that this path always exists.

**The policy that unifies admission and pruning: marginal value per GPU-hour.** Each candidate experiment branch carries an expected information gain (how far it would move a claim's confidence or close a gap) against an estimated cost in GPU-seconds + dollars. Under pressure the manager admits and keeps the high-ratio branches and prunes the tail — the research generalization of Boardroom's rule that the REASONING tier is reserved for decisions where quality dominates cost. The Resource Manager is, in one line, the lab's economic conscience across tier routing, experiment admission, branch pruning, and the spill valve.

---

## 7. Storage: don't collapse Postgres and Neo4j

### 7.1 Two stores, two query patterns

You'll be tempted to unify Boardroom's Postgres and rag-bench's Neo4j. Don't. The 2026 production hybrid is exactly this split (vector/relational store + graph store, e.g. Qdrant+Neo4j): each serves a query pattern the other is bad at.

- **Postgres = operational truth + event bus + ledgers.** Transactional integrity, the `FOR UPDATE SKIP LOCKED` task claim, the NOTIFY/LISTEN bus, the claim/experiment/dataset ledgers, cost/compute accounting, the trace DAG. OLTP.
- **Neo4j = semantic research graph.** Multi-hop relationship reasoning the org chart asks for ("which datasets support claim_012", "which papers influenced this method", "which experiments failed but were informative"). Plus the literature graph rag-bench already built. Relationship-OLAP.

### 7.2 The Claim Ledger lives in Postgres but is *projected* into Neo4j

The ledger needs transactional integrity and the event triggers — so it's authoritative in Postgres. But relationship queries over it are graph-shaped — so each ledger transition also writes a node/edge into Neo4j via the write path (§6.4). Postgres is the system of record; Neo4j is the queryable projection. This keeps Boardroom's `emit_*` trigger discipline intact while giving you the graph the org chart centers on.

---

## 8. The three new subsystems

### 8.1 Experiment execution sandbox (the biggest build)

The one genuinely new runtime. Boardroom's "experiments" are HTTP calls (fetch pricing, count demand). LabFoundry needs to run actual ML code.

- **Isolation:** containerized execution (Docker, à la your existing compose stack), no network egress except an allowlist, resource limits, wall-clock + GPU-hour caps enforced by the dispatcher.
- **Lifecycle:** the ML Engineer sub-agent (coding agent) edits an experiment template → sandbox installs pinned deps → runs train/eval → captures metrics + artifacts + logs + **`env_hash` + `seed`** → writes an `experiments` row + evidence. Mirrors AI-Scientist-v2's "plan-directed code-level changes + execute" loop.
- **Durability:** checkpoint before/after (§6.2); Saga compensation on mid-run death.
- **Reproducibility:** the Reproducibility agent re-runs from `env_hash` + `seed` and asserts metrics reproduce within tolerance — the `weakly_supported→replicated` gate and the "paying stranger" forcing function.

### 8.2 Data Foundry + the Data Steward gate

The org chart's "most important division," and its rule is a hard gate you can implement with the auditor pattern:

> **No dataset enters the experiment pipeline without a dataset card, license status, task definition, split strategy, and leakage report.**

- **Dataset Scout** (MCP tools: OpenML, HuggingFace, GitHub) discovers candidates; **Profiling agent** computes rows/columns/target/imbalance/leakage-risk; **Data Steward** is a *blocking gate* — `datasets.steward_status` must be `approved` before any experiment can reference the dataset, enforced as a friction check in the dispatcher (same shape as the slop breaker). A blocked dataset emits an event the PI sees.
- **Synthetic/Creation agents** let the lab *generate* assets (controlled-leakage synthetic sets, benchmark tasks from OpenML, paper-claim meta-datasets) — the part of the org chart that makes the lab a producer, not just a consumer, of data.

### 8.3 Publication pipeline

- **Paper Architect** (structure from `paper_ready` claims only) → **Figure agent** (plots, with a **vision-model critique** pass à la AI-Scientist-v2 / PaperVizAgent) → **LaTeX agent** → **Internal Reviewer** (automated reviewer, §6.6) → **Rebuttal agent**.
- Tooling exists to lean on: Google's PaperOrchestra (logs+notes→manuscript) and ScholarPeer (reviewer) are the reference points; Semantic Scholar for citation discovery (as AI Scientist used) wired as an MCP tool.
- Output is a versioned draft in the `paper_draft_registry` (a Postgres table + Neo4j node), gated to `submit` only after the human approves.

---

## 9. Build plan (MVP → full), tied to your existing arc

Start from Boardroom's 10-handler spine and the LabFoundry **9-agent MVP** (plus the **Resource Manager**, §6.8, once experiments come online) — not the 25-agent full org. Here is the MVP mapped onto what you already have:

| MVP agent | Source | New work |
|---|---|---|
| Principal Investigator | CEO handlers (`thesis_invalidated`, `phase_transition`) | Re-prompt to research agenda + claim selection |
| Literature Scout / Code Scout / Dataset Scout | `researcher.py` + rag-bench retrieval | Split into scout tools; wire arXiv/GitHub/OpenML MCP |
| Data Steward | Auditor gate pattern | New blocking gate + dataset card schema |
| Hypothesis agent | Planner / CEO | Re-prompt |
| Experiment Designer | `planner/` v2 (`assess→propose→critique`) | Re-prompt; add tree-search hooks |
| **ML Engineer** | — | **New: sandbox + coding agent (§8.1)** |
| Critic / Reproducibility | `adversarial/` v2 + `task_completed.py` | Add science attacks + reproduce-run |
| **Paper Writer** | — | **New: publication pipeline (§8.3)** |
| **Resource Manager (Quartermaster)** | `GPULock` + dispatcher friction | **New: lease scheduler + `compute_budget`/`gpu_leases` + spill valve (§6.8)** |

**Phasing (each phase = a shippable, gated loop, mirroring how you shipped Boardroom):**

1. **Re-ontology (1–2 weeks).** Migrate `theses→claims` with the status machine; re-prompt CEO→PI, planner→designer, adversary→critic, auditor→eval. Reuse all friction/trace/router. *Exit: the loop runs frame→hypothesize→validate on literature-only evidence (no experiments yet) — i.e. the lab can already form and adversarially test claims grounded in rag-bench.*
2. **Fold in rag-bench (1 week).** Expose it as the `knowledge` MCP server; add the write path; stand up Neo4j as the semantic graph; generalize CRAG→adaptive routing. *Exit: claims are grounded by GraphRAG and projected into the research graph; org-chart queries work.*
3. **Experiment sandbox + Resource Manager (2–4 weeks, the hard part).** Build §8.1, and stand up the **Resource Manager (§6.8)** *with* it — the lease-based GPU scheduler, the `compute_budget`/`gpu_leases` tables, admission as the sixth friction gate, and the DeepSeek spill valve — then wire the ML Engineer coding agent. The scheduler has to land alongside the sandbox: experiments are meaningless to schedule before they exist, and unsafe to run without it (this is where inference starvation and OOM waste appear). Default behind an env flag (`EXPERIMENTS=off`) exactly like your v2-loop toggles. *Exit: the lab runs a real reproduction/ablation end-to-end under budget — reasoning spilling to DeepSeek while the box is busy — and a claim reaches `replicated`.*
4. **Tree search + Data Foundry (2–3 weeks).** Upgrade the experiment phase to agentic tree search (§6.3); add the Data Steward gate (§8.2). *Exit: the lab autonomously explores a small hypothesis tree under budget with governed datasets.*
5. **Publication (2–3 weeks).** Build §8.3; add the automated Internal Reviewer + the human approval gate (HITL suspend/resume, §6.2). *Exit: end-to-end mandate→paper draft, human-approved.*
6. **Context + memory hardening (ongoing).** Compaction, offloading, probe-eval (§6.1); consolidation/Dreaming (§6.4); checkpoints + Saga (§6.2). Fold in as the runs get longer and the failure modes show up — same way Boardroom's defensive patches accreted.

---

## 10. Constraints, risks, and honest limits

- **Compute is the binding constraint, not intelligence.** A consumer dual-GPU box can't train frontier models. The lab's viable research surface is reproductions, ablations, small-model and tabular experiments, synthetic-benchmark studies, and method comparisons. This is a *feature* for a first autonomous lab — reproduction and ablation studies are publishable, lower-risk, and exactly where rigor (your strength) matters most. Set the mandate scope accordingly.
- **Novelty assessment is the hard problem.** AI-Scientist-class systems sometimes "discover" known results. The research graph (rag-bench over 21k papers) is your mitigation — a novelty check against the indexed literature in the Critic's attack — but it's imperfect; novelty should be a Critic verdict the PI weighs, not a hard gate.
- **Reproducibility requires discipline you must wire, not assume.** `env_hash` + `seed` pinning, deterministic data splits, and the re-run gate are the difference between a real lab and a plausible-text generator. This is non-negotiable for the success contract (§3.3).
- **GPU contention can deadlock the integrated system.** Inference and experiments share the GPUs; resolve the arbitration (§6.7) before turning experiments on, or you'll get starvation that looks like a "silent flatline."
- **Ethics and disclosure.** The 2026 coverage of autonomous research systems flags real concerns: flooding peer review, fabricated results, credential inflation. The org chart's Governance & Safety Office should enforce: clear AI-authorship disclosure on any output, a hard human-approval gate before *any* external submission, and an Audit agent over the claim ledger. Build the human gate as a true durable suspend (§6.2), not an honor system.
- **Don't adopt a heavyweight agent framework.** You already have journal-replay durability, the harness/compute split, layered context, procedural memory, and a trace DAG — most of what LangGraph/Temporal/Deep Agents sell. Borrow their *patterns* (checkpoints, Saga, sub-agent registry, tree search); keep your harness.

---

## 11. References (current work this revision draws on)

- **Context engineering:** Phil Schmid's four operations (offload / reduce / retrieve / isolate); Manus team's production techniques; Martin Fowler's "harness engineering" (context + architectural constraints + entropy management); practitioner guides on probe-based context eval (2026).
- **Durable execution:** journal-replay + DB-checkpoint + Saga patterns; the harness/compute split; Temporal/LangGraph/Pydantic-AI/OpenAI-Agents-SDK adoption; LangChain Deep Agents (Mar 2026) for long-running agents.
- **GraphRAG:** Adaptive RAG (complexity routing); dual-channel agentic deep search (GraphSearch); failure-aware routing (Neo4j NODES AI 2026); LazyGraphRAG (cheap indexing); HippoRAG (PageRank memory); Microsoft GraphRAG (community detection) — much of which you've already implemented in rag-bench.
- **Autonomous research:** Sakana **AI Scientist-v2** (progressive agentic tree search + experiment manager + automated reviewer + archive loop; first AI paper through peer review; Nature, Mar 2026; open source); Analemma **FARS** (166 papers / ~417h); Google **PaperOrchestra** (logs→manuscript) and **ScholarPeer** / **PaperVizAgent** (reviewer + figures).
- **Memory:** CoALA four-layer model (working/episodic/semantic/procedural); temporal knowledge graphs with validity windows (Zep/Graphiti, Mem0); procedural memory as self-updating rules (LangMem); consolidation — "Episodic Memory is the Missing Piece for Long-Term LLM Agents" (Feb 2026) and Anthropic's **Dreaming** primitive (May 6 2026); the "write path, not just a retriever" framing.

_Point-in-time. The autonomous-research and context-engineering spaces are moving fast; re-verify the named systems before committing to any one as a template._