# Lab agents — actions & interactions

The lab is an **event-driven** system. Every interaction is one of ~41 event types flowing through the
dispatcher (`harness/dispatch.py`). To understand any agent, read three registries:

- **`harness/main.py`** — `dispatcher.register("<event>", <handler>)`: what each agent **consumes**.
- **`harness/loop_engine.py`** — the `Transition` registry: the declarative *"when condition X holds,
  emit event Y (owner Z)"* backbone that drives + re-arms the research loop.
- **`grep emit_corpus_event` in `agents/<name>/`** (+ a few direct `INSERT INTO events` inside
  `state/client.py`): what each agent **produces**.

Mode-dial: the dispatcher derives an agent's dial from its module path (`agents.<name>.* → <name>`,
`harness/agent_modes.py:agent_of`) and runs it only when the dial is `advisory|active`. Loop-engine
transitions are gated by `Transition.owner`.

> This file is hand-maintained from those registries. The diagrams below render on GitHub / most IDEs.

---

## 1. The whole system

```mermaid
graph TD
    subgraph KNOWLEDGE["Knowledge loop (continuous)"]
        SCOUTS["scouts / collectors<br/>(web · arXiv · GitHub · OpenML · HF)"]
        MIMIR["Mimir<br/>AI curator of knowledge"]
        LIB[("Library<br/>corpus + vector + context graph")]
        SCOUTS -->|source.discovered| MIMIR
        MIMIR -->|document.parsed → document.ingested| LIB
        MIMIR -->|library.sweep_settled / library.trends| LIB
    end

    subgraph STRATEGY["Strategy & gate"]
        PACE["ariadne_pace<br/>(pacemaker)"]
        ARIADNE["Ariadne<br/>Principal Investigator"]
        NOVELTY["Novelty<br/>independent adjudicator"]
        PACE -->|ariadne.deliberate / ariadne.reflect| ARIADNE
        ARIADNE -->|writes claims + direction_scores| GATE{{"direction gate<br/>(auto-approve + assign researcher)"}}
        PACE -->|direction.adjudicate| NOVELTY
        NOVELTY -->|verdict pass/hold → direction_adjudications| GATE
    end

    subgraph SCHOLARSHIP["Scholarship arc"]
        ARIADNE -->|ariadne.review → lit_review| ARIADNE
        ARIADNE -->|ariadne.propose → proposal + hypotheses| ARIADNE
    end

    subgraph EXECUTION["Execution (researcher owns it)"]
        PLANNER["Planner<br/>direction → tasks"]
        RESEARCHER["Researcher<br/>full-stack: investigate + AUTHOR experiment + interpret"]
        QM["Quartermaster<br/>off-slot run + debug loop"]
        SANDBOX[["sandbox<br/>--network none, /data, /models, llm broker"]]
        GATE -->|planner.plan| PLANNER
        PLANNER -->|task.created| RESEARCHER
        RESEARCHER -->|experiment.requested → design + preflight + queue| QM
        QM --> SANDBOX
        SANDBOX -->|result| QM
        QM -->|experiment.completed / experiment.failed| RESEARCHER
    end

    subgraph OUTPUT["Synthesis & verification"]
        SYNTH["Synthesis<br/>paper-shaped finding + IMRaD article"]
        EVAL["Evaluation<br/>slop/groundedness audit"]
        CRITIC["Critic<br/>adversarial refutation"]
        RESEARCHER -->|finding.synthesize| SYNTH
        SYNTH -->|graduates direction + source.discovered| LIB
        RESEARCHER -->|task.completed| EVAL
        EVAL -->|finding.high_signal| CRITIC
        EVAL -->|audit.slop_detected| EVAL
        CRITIC -->|claim.invalidated / confidence| GATE
    end

    LIB -.->|new knowledge triggers re-deliberation| PACE
    RESEARCHER -.->|loop.unclosed: needs_capability / needs_real_dataset| ARIADNE
```

**The spine in one line:** new knowledge → Ariadne frames a *direction* → Novelty adjudicates → the
gate approves + **assigns a researcher** → Ariadne writes the review + proposal → Planner makes tasks →
the **Researcher authors** experiments → the **Quartermaster** runs them → the Researcher interprets →
**Synthesis** composes the finding → Evaluation + Critic stress it → the finding re-enters the Library
and feeds Ariadne's next deliberation.

---

## 2. Event taxonomy

| Class | Events | Notes |
|---|---|---|
| **Loop-advancing** | `ariadne.deliberate/reflect/review/propose`, `direction.adjudicate`, `planner.plan`, `task.created/completed`, `experiment.requested/completed/failed`, `finding.synthesize`, `synthesis.article`, `finding.high_signal`, `audit.slop_detected` | each has exactly one consuming handler |
| **Knowledge** | `source.discovered`, `document.parsed`, `document.ingested`, `acquire.requested/fulfilled/rejected`, `library.sweep_requested/settled`, `library.trends`, `mimir.ask/answered/ingest_blocked` | Mimir's intake + Q&A |
| **Lifecycle** | `claim.created` (→ Neo4j sink), `claim.invalidated`, `claim.confidence_changed`, `direction.reopened` | claim/direction state changes |
| **Indicators / telemetry** | `loop.unclosed`, `lab.pulse`, `quartermaster.snapshot`, `dispatch.saturated`, `session.*`, `step.*`, `queue.empty`, `lessons.reconciled` | poll-consumed / closure guard (CLOSURE_EXEMPT in `harness/dispatch.py`) |

---

## 3. Individual agents

Each diagram reads left→right: **events consumed** → **the agent + what it does** → **events/writes produced**.

Every live singleton agent is a **named, persistent identity** (migration 024 `agent_identities`); the
curator resolves each one's system persona from that registry (falling back to the `SYSTEM_PROMPTS`
code anchor if a row is missing). `agent_modes` is the control dial; `agent_identities` is the
persona/name layer — same `agent_name` key, two concerns. The researcher roster (`researchers`,
migration 022) is the one multi-member identity.

| `agent_name` | identity | role |
|---|---|---|
| `ariadne` | **Ariadne** | Principal Investigator (frames mission + directions) |
| `mimir` | **Mimir** | Warden of the Library (curation, trust, GraphRAG) |
| `novelty` | **Themis** | independent prior-art adjudicator |
| `planner` | **Metis** | direction → research tasks |
| `synthesis` | **Calliope** | finding + article author |
| `evaluation` | **Aletheia** | groundedness / slop audit |
| `critic` | **Momus** | adversarial refutation |
| `reflection` | **Mnemosyne** | lessons / memory reconciliation |
| `researcher` | **Daedalus · Hypatia · Heron** | full-stack researchers (roster) |
| `quartermaster` | *(ops loop, unnamed)* | resource + experiment execution |

Edit them with `python -m ops.identities` (singletons) / `python -m ops.researchers` (the roster).

### Ariadne — Principal Investigator (`agents/ariadne/`, paced by `harness/ariadne_pace.py`)
Frames the mission + falsifiable directions, scores them against the field model, writes the literature
review + proposal, and steers the standing agenda. Strategy only — never executes.

```mermaid
graph LR
    A1["ariadne.deliberate"] --> AR(("Ariadne"))
    A2["ariadne.reflect"] --> AR
    A3["ariadne.review"] --> AR
    A4["ariadne.propose"] --> AR
    AR --> O1["claims + direction_scores + claim_goals"]
    AR --> O2["claim.created → Neo4j sink"]
    AR --> O3["research_documents: lit_review, proposal+hypotheses"]
    AR --> O5["acquire.requested → Mimir (demand side)"]
    AR --> O6["mimir.ask / mimir.answered (GraphRAG)"]
    AR --> O7["source.discovered → Library (review/proposal as lab_scholarship)"]
```

> The gate approval + `researcher_id` assignment is done by `ariadne_pace._auto_approve` (reading
> Novelty's `verdict='pass'` + the self-score floors), not by Ariadne's handler itself. `ariadne.deliberate`/
> `reflect` are **pacemaker-only** (the loop engine does not own them); the rest of the arc
> (`adjudicate`/`plan`/`review`/`propose`/`article`/`experiment.requested`) is driven by either the
> loop-engine transitions or the legacy `_maybe_*` pacemaker steps.

### Themis — independent adjudicator (`agents/novelty/handler.py`, `agent_name=novelty`)
The external prior-art check. Reads the nearest corpus prior art (excluding the lab's own) + prior
directions, scores novelty/impact, and returns a deterministic pass/hold (OR-verdict: high-impact
passes at modest novelty).

```mermaid
graph LR
    N0["direction.adjudicate"] --> NV(("Themis"))
    NV --> NO1["novelty.adjudicate (LLM)"]
    NV --> NO2["direction_adjudications: verdict pass/hold"]
    NO2 --> NO3["gate reads verdict → approve/hold"]
```

### Metis — direction → tasks (`agents/planner/`, `agent_name=planner`)
Decomposes an approved direction into 1–2 lean research tasks (department='research'), inheriting the
direction's owning researcher.

```mermaid
graph LR
    P0["planner.plan"] --> PL(("Metis"))
    P1["queue.empty"] --> PL
    PL --> PO1["tasks rows (researcher_id inherited)"]
    PO1 --> PO2["task.created (trigger)"]
```

### Researcher — full-stack author (`agents/researcher/`)
ML engineer + SW engineer + scientist in one. Investigates a task against the Library; when a number
needs an experiment, it **authors** the experiment (design + preflight), and later **interprets** its own
result. Every experiment is linked to a specific researcher.

```mermaid
graph LR
    R0["task.created"] --> RS(("Researcher"))
    R1["experiment.requested"] --> RS
    R2["experiment.completed / failed"] --> RS
    RS --> RO1["investigate_task → finding + confidence move"]
    RS --> RO2["experiment.requested (needs_experiment)"]
    RS --> RO3["design + preflight → queue_experiment(researcher_id)"]
    RS --> RO4["interpret → confidence + lab note (source.discovered)"]
    RS --> RO5["finding.synthesize (≥ N completed)"]
    RS --> RO6["loop.unclosed: needs_capability / needs_real_dataset"]
    RS --> RO7["task.completed (trigger)"]
```

### Quartermaster — experiment execution (`harness/quartermaster.py`)
A background watchdog (not a dispatcher handler). Allocates CPU/GPU, runs each queued experiment in the
hardened sandbox, drives the design→run→debug retry loop off-slot, and records the outcome.

```mermaid
graph LR
    Q0["queued experiment_runs (poll)"] --> QM(("Quartermaster"))
    QM --> QO1["sandbox run (--network none, /data, /models, llm broker)"]
    QM --> QO2["debug retry loop (experiments.debug)"]
    QM --> QO3["record result + failure_class"]
    QM --> QO4["experiment.completed / experiment.failed"]
    QM --> QO5["kill on stall / VRAM pressure"]
```

### Calliope — finding & article (`agents/synthesis/`, `agent_name=synthesis`)
Reads across a direction's completed experiments to compose the single paper-shaped `ResearchFinding`,
graduates the direction's lifecycle status (a decisive + confident finding reaches **concluded**), and
(written arc) composes the IMRaD article (citation-graded). Hands each finding to the verification spine.

```mermaid
graph LR
    S0["finding.synthesize"] --> SY(("Calliope"))
    S1["synthesis.article"] --> SY
    SY --> SO1["synthesis.compose (LLM) → research_findings"]
    SY --> SO2["graduate direction: tested / weakly_supported / concluded"]
    SY --> SO3["research_documents: article (citation ≥ 0.8)"]
    SY --> SO4["source.discovered → re-ingest finding/article"]
    SY --> SO5["finding.composed → Aletheia (verification spine)"]
```

### Aletheia — slop/groundedness audit (`agents/evaluation/`, `agent_name=evaluation`)
Audits a synthesized `research_finding` for substance + groundedness against its experiments; a
confident pass promotes it to high-signal (arming Momus). Also keeps the legacy per-task finding audit
+ the per-claim slop circuit-breaker.

```mermaid
graph LR
    E0["finding.composed"] --> EV(("Aletheia"))
    E1["audit.slop_detected"] --> EVS(("slop handler"))
    EV --> EO1["audit (LLM) → research_findings.audit_score/verdict"]
    EV --> EO2["finding.high_signal (pass + confidence ≥ 0.7) → Momus"]
    EV --> EO3["audit.slop_detected (slop rate ≥ 40%)"]
    EVS --> EO4["halt research tasks + lower confidence"]
```

### Momus — adversary (`agents/critic/`, `agent_name=critic`)
For each high-signal finding, runs a targeted refutation pass (plans an attack, fetches counter-evidence,
stress-tests, judges) and emits watch / weaken / kill. Directions are weaken-only.

```mermaid
graph LR
    C0["finding.high_signal"] --> CR(("Momus"))
    CR --> CO1["adversary loop (LLM, own web search)"]
    CR --> CO2["critic_verdicts row"]
    CR --> CO3["weaken: confidence delta on the direction"]
    CR --> CO4["kill: claim.invalidated (non-direction)"]
```

### Mimir — curator of knowledge (`agents/mimir/`)
The knowledge engine: ingests discovered sources (parse → embed → trust/certify → graph), runs discovery
sweeps + self-healing acquires, and answers agents' multi-hop questions over the corpus.

```mermaid
graph LR
    M0["source.discovered"] --> MI(("Mimir"))
    M1["library.sweep_requested"] --> MI
    M2["acquire.requested"] --> MI
    M3["mimir.ask"] --> MI
    MI --> MO1["document.parsed → document.ingested → corpus/graph"]
    MI --> MO2["library.sweep_settled / library.trends"]
    MI --> MO3["acquire.fulfilled / rejected"]
    MI --> MO4["mimir.answered (answer + citations + gaps)"]
    MI --> MO5["source.discovered (new scout hits) / mimir.ingest_blocked"]
```

### Mnemosyne — lessons / memory (`agents/reflection/`, `agent_name=reflection`)
Consumes `reflection.requested` to reconcile lessons / steer; lightweight.

```mermaid
graph LR
    RF0["reflection.requested"] --> RFL(("Mnemosyne")) --> RFO["lessons.reconciled / memory"]
```

### Removed / dormant
- **pi** (`agents/pi/`) — market-era PI; **neutralized** (trigger + handlers cut, Stage 0) and dropped from
  the dial. Directory removal is a deferred follow-up (still bench-coupled); inert at runtime.
- **librarian** — **deleted** (was collapsed into Mimir long ago).
- **reviewer** — **deleted** (empty stub; its output-gate role is covered by Aletheia + Momus + Calliope's
  citation grading). The `reviewer`/`auditor` mode dials were removed too.

---

## 4. Closed loops & anti-stall guards

The `loop_engine` re-arm transitions re-issue an in-band event if it was missed, so the loop self-heals:

| Transition (owner) | Re-emits | When |
|---|---|---|
| `adjudicate` (novelty) | `direction.adjudicate` | scored, un-adjudicated directions exist |
| `plan` (planner) | `planner.plan` | approved direction with no tasks |
| `arc_review` / `arc_propose` / `arc_article` (ariadne/synthesis) | the arc event | a scholarship doc is missing |
| `experiment_coverage` / `confirm_real_data` (experiments) | `experiment.requested` | direction under the coverage target / needs real-data confirmation |
| `rearm_interpret` (experiments) | `experiment.completed/failed` | a settled run was never interpreted |
| `rearm_conclude` (synthesis) | `finding.synthesize` | enough evidence, no finding |
| `rearm_audit` (evaluation) | `finding.composed` | a `research_finding` is unaudited (`audit_verdict IS NULL`) |
| `rearm_attack` / `rearm_attack_research` (critic) | `finding.high_signal` | high-signal finding never challenged |

`loop.unclosed` is the closure-guard indicator: "work was produced but the consumer that should advance
it never ran" (also the researcher's `needs_capability` / `needs_real_dataset` signals to Ariadne).
`CLOSURE_EXEMPT_EVENTS` (in `harness/dispatch.py`, shared with `ops.closure_audit`) excludes telemetry/
poll-consumed events so they're never false-flagged.

**Two structural loop-closers from the agent consolidation:**
- **Conclude wall:** `ariadne.persist` only supersedes *un-worked* directions on a re-frame (no completed
  experiment_run and no research_finding), so a worked direction survives to reach `concluded`.
- **Tasking guard (migration 026):** a BEFORE-INSERT trigger on `tasks` SKIPS a `department='research'`
  task on a MISSION/FINDING claim; `ops.reap_orphan_tasks --halt` clears historical zombies.

---

## 5. Best ways to keep understanding this

1. **The registries (authoritative):** `harness/main.py` (consumes), `harness/loop_engine.py` (the
   transition backbone), `grep -rn emit_corpus_event agents/ harness/` + the `INSERT INTO events` in
   `state/client.py` (produces). Cross-reference = this graph.
2. **Live flow:** `SELECT event_type, count(*), max(emitted_at) FROM events GROUP BY 1` shows what's
   actually firing; `ops.experiment_audit`, `ops.closure_audit`, and `ops.lab_doctor` summarize health.
3. **The floorplan UI** (`/`) — the same graph, live, as rooms + flow edges.
4. **This doc** — regenerate the diagrams whenever a `register()` line or a `Transition` is added.
