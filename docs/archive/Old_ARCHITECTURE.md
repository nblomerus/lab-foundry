# Boardroom — Technical Architecture

_Snapshot of the current system as of 2026-05-28. Covers the shipped harness (14 commits) plus the in-flight loop-decomposition + premium-routing arc on the working tree. Reader is expected to know async Python, Postgres, FastAPI, MCP, and Next.js App Router; this doc focuses on **what is wired to what** and **why the design exists**._

---

## 1. Vision in one paragraph

Boardroom is an autonomous AI-native company. You hand it a seed (problem statement, stance, success criterion) and it must self-discover its niche, validate the thesis against real-world evidence, and ship to a paying stranger within 30 days — without you touching it. The system is built as a **phase machine** (exploration → convergence → commitment → execution) driving a swarm of **stateless agents** (CEO, Planner, Researcher, Auditor, Adversary, Adjudicator, Reflection) that react to events on a Postgres bus. A thesis killed two hours after creation gets rethought in seconds, not days, because every state mutation emits an event that wakes the right handler. The local-first stack (Ollama on a dual-GPU host) keeps the loop cheap; a thin premium chain (DeepSeek → OpenAI → GitHub → free providers) is reserved for high-leverage decisions (thesis kill, phase transition, charter write, planning, slop scoring) where quality dominates cost.

See [`memory/project_boardroom_vision.md`](../.claude/projects/-home-nicholas-workspace-boardroom/memory/project_boardroom_vision.md) for the canonical framing.

---

## 2. Runtime topology

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                                YOU (board)                                      │
│                       browser → http://localhost:8088                           │
└─────────────────────────────────────┬──────────────────────────────────────────┘
                                      │ VS Code port-forward
                                      ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│  web/  Next.js 16 dashboard           dev on :8088 (Node 20, pinned in .nvmrc)  │
│  └─ /api/* and /ws/* rewritten ────────────────────────────────────────────────┐│
└────────────────────────────────────────────────────────────────────────────────┤│
                                      │                                          ││
                                      ▼                                          ││
┌────────────────────────────────────────────────────────────────────────────────┐│
│  boardroom-api.service  FastAPI on :8503                                       ││
│  routers: snapshot, stream(WS), bench, debug, trace                            ││
│  StreamHub: single Postgres LISTEN → fan-out to all WS clients                 ││
└─────┬──────────────────────────────────────────────────────────┬───────────────┘│
      │ asyncpg pool                                              │ pg_notify     │
      │                                                           │               │
      ▼                                                           ▼               │
┌──────────────────────────────────────────────────────────────────────────────┐ │
│  Postgres 16 (Docker, :5432) — single source of truth + event bus             │ │
│  tables: company_state, theses, tasks, findings, events, agent_runs,          │ │
│          agent_sessions, adversary_verdicts, phase_transitions, cooldowns,    │ │
│          cost_tracking, memory_pointers, lessons, lesson_applications,        │ │
│          bench_runs, deepseek_balance_log, research_inquiries, evidence,      │ │
│          experiment_runs, fetch_cache                                         │ │
│  triggers: emit_task_created, emit_queue_empty_if_drained,                    │ │
│            trigger_reflection_on_dissent, notify_event                        │ │
│  MV: slop_rate_by_thesis (refreshed by watchdog every 5 min)                  │ │
└──────────────────────────────────────────────────────────────────────────────┘ │
      ▲                                       ▲                                   │
      │                                       │                                   │
┌─────┴───────────────────────────────────────┴─────────────────────────────────┐ │
│  boardroom-harness.service  python -m boardroom.harness.main                   │ │
│   ├─ preflight (Postgres + Ollama)                                             │ │
│   ├─ Dispatcher (LISTEN events; semaphore-bounded handler concurrency)         │ │
│   │   ├─ friction: cooldowns + cost caps + slop circuit-breaker                │ │
│   │   ├─ watchdog: stale tasks, orphan reap, liveness pump, phase budget       │ │
│   │   └─ Session contextvar (one per handler invocation → /trace)              │ │
│   ├─ Router (4 tiers; premium chain + free chain + local)                      │ │
│   │   └─ GPULock (per-model lock + global 4-in-flight semaphore)               │ │
│   └─ Curator (layered prompts: system/constitution/phase/lessons/recall/task)  │ │
└────────────────────────────────────────────────────────────────────────────────┘ │
      │             │                  │                    │                    │
      ▼             ▼                  ▼                    ▼                    │
┌──────────┐ ┌─────────────┐ ┌─────────────────┐ ┌─────────────────────────┐    │
│ Ollama   │ │ Premium     │ │ Free cloud      │ │ MCP servers (stdio)     │    │
│ :11434   │ │ chain       │ │ chain           │ │  ├─ boardroom-state     │    │
│ 2 GPUs   │ │ DeepSeek →  │ │ Gemini → Groq → │ │  └─ boardroom-research  │    │
│ R5070Ti  │ │ OpenAI →    │ │ GitHub mini →   │ │     (HN/Reddit/SearXNG  │    │
│ R2070S   │ │ GitHub gpt-4o│ │ NVIDIA          │ │      /DDG/Trafilatura) │    │
└──────────┘ └─────────────┘ └─────────────────┘ └─────────────────────────┘    │
                                                                                  │
┌──────────────────────────────────────────────────────────────────────────────┐ │
│  Zep Cloud (memory)         Langfuse Cloud (traces)        SearXNG :8080      │ │
│  episodic narrative          per-call latency, tokens,     meta-search        │ │
│  + Graphiti recall           cost. v4 API + JSONB payload  (Docker)           │ │
└──────────────────────────────────────────────────────────────────────────────┘ │
                                                                                  │
┌──────────────────────────────────────────────────────────────────────────────┐ │
│  boardroom-liveness.timer  every 3 min → liveness_check                       │ │
│  if no activity in 20 min while unpaused → restart harness + fire webhook     │ │
└──────────────────────────────────────────────────────────────────────────────┘─┘
```

### Process inventory

| Unit | Where | Role |
|---|---|---|
| `boardroom-api.service` | `~/.config/systemd/user/` | FastAPI on :8503; restart=always |
| `boardroom-harness.service` | `~/.config/systemd/user/` | Autonomous loop; preflight + crash-loop retry every 10s |
| `boardroom-liveness.service` + `.timer` | `~/.config/systemd/user/` | External stall detector — restarts harness on soft hang |
| `ollama.service` (system) | `/etc/systemd/system/` | Dual-GPU override `CUDA_VISIBLE_DEVICES=0,1` |
| `boardroom-postgres-1` | docker compose | :5432, healthchecked, migrations on startup |
| `boardroom-searxng-1` | docker compose | :8080, config at `infra/searxng/settings.yml` |
| `web` dev server | not a service | `make dev` or `cd web && npm run dev` (port pinned 8088) |

Demo stack (`docker-compose.demo.yml`) duplicates Postgres on :5433 and SearXNG on :8081 for isolated researcher-loop validation via `scripts/demo_research_loop.py`.

---

## 3. The harness loop in detail

### 3.1 Entry point — `boardroom/harness/main.py`

`main()` (lines 102–207) is the single entry. Boot sequence:

1. **`.env` loaded before imports** so `DEEPSEEK_API_KEY` reaches the router at build time (recent fix; demo side-instances were silently dropping premium routing).
2. **`_preflight()`** (lines 61–99) round-trips Postgres and Ollama. Non-zero exit on failure → systemd restarts in 10s. No silent flatlines.
3. **Zep init with rate-shaping** (line 155): `ZEP_INIT_GAP_S` default 0.25s spaces session-ensure calls so the 5 req/min thread cap doesn't trip on startup.
4. Build clients: `PostgresClient`, `ZepClient`, `LessonsClient`, `Curator`, `GPULock`, `Router`, `Dispatcher`. Clients are **attached to the dispatcher** (lines 169–173) so every handler reaches them via `dispatcher.state/.memory/.lessons/.curator/.router` — no globals, no DI framework.
5. Register handlers (lines 176–185) one per event type.
6. `await dispatcher.run()` (line 194) blocks until SIGINT/SIGTERM. Clean shutdown closes the pool and Zep client.

There is no separate watchdog process — the dispatcher's internal 5-minute watchdog loop handles everything.

### 3.2 Dispatcher — `boardroom/harness/dispatch.py`

The dispatcher is the **nervous system**. It owns:

- A dedicated asyncpg connection that runs `LISTEN events` (line 131).
- A `_handler_sem` semaphore (default 4) bounding concurrent handler invocations (line 103).
- A registry mapping `event_type → handler callable` (line 47).
- A `_revive_lock` (line 73) that prevents the startup drain and the first watchdog tick from double-issuing liveness pump triggers.
- A `Session` contextvar `_current_session` (line 38) so every handler invocation has its own session record without explicit threading.

**Event lifecycle** (lines 159–261): when a NOTIFY arrives the dispatcher fetches the event row, looks up the handler, and gates it through three friction primitives in order:

1. **Cooldowns** — `cooldowns` table keyed on `(invocation_type, target_type, target_id)`. Per-event windows: 4 h for adversary kill verdicts, 30 m for adjudicator checks, 10 m for planner refill. Skipping is recorded as `events.status='suppressed'`.
2. **Cost caps** — daily counters in `cost_tracking`. REASONING capped at 50/day (raised from 4 after DeepSeek made the tier cheap and reliable), WORKHORSE 4000, FAST 2000, CODE 500. **Local Ollama calls do not count** (`Router._record_cost` short-circuits on `Provider.OLLAMA` — line 956 in router). On cap, the dispatcher degrades to local-only rather than raising `CostCapExceeded`.
3. **Slop pause** — checks `slop_rate_by_thesis` materialized view; if any active thesis is >40 % slop over the last 24 h, downstream adversary/auditor invocations on that thesis are paused.

After gating, the dispatcher creates a `Session(handler_name, triggered_by_event_id)`, calls `session.start(pool)`, awaits the handler, and calls `session.finish(status)`. The handler can fan out N `router.invoke()` calls; each one writes an `agent_runs` row with `session_id, step_name, parent_step_id, step_order` and emits `step.started/completed/failed` events — that's how `/trace` reconstructs the DAG.

**Watchdog loop** (lines 425–504) runs every 5 minutes:

- Reset stale `running` tasks back to `pending` after 30 min.
- Reap orphan `agent_runs` (started but never finished).
- **Liveness pump** (lines 374–421) — if pending tasks > pending `task.created` events, emit fresh `task.created` triggers with unique dedup keys. This catches the case where the harness restarted with tasks in the queue but no events left to wake the researcher.
- Re-emit any unhandled events older than 2 minutes.
- Check phase budget — if `(now - phase_started_at) > 1.5 × budget`, emit `phase.budget_exceeded`.
- Refresh the slop materialized view.

The watchdog is the answer to the "silent flatline" class of failure: the loop self-heals from a wide variety of stalls without operator intervention.

### 3.3 Router — `boardroom/harness/router.py`

Four tiers, mapped per invocation_type in the `ROUTE` table (lines 258–326):

| Tier | Daily cap | Local default | Used for |
|---|---|---|---|
| **REASONING (R)** | 50 | `deepseek-r1:32b-qwen-distill-q4_K_M` | thesis kill, phase transition, charter write, adversary kill verdict |
| **WORKHORSE (W)** | 4000 | `qwen3:14b` | CEO synthesis/rescore/spawn, planner, adversary contradiction-hunt, auditor slop scoring, all v2 loop orchestrators |
| **FAST (F)** | 2000 | `mistral:7b-instruct-q4_K_M` | phase adjudicator, batch reflection |
| **CODE (C)** | 500 | `qwen2.5:14b-instruct-q4_K_M` (calibrated; gemma2:27b and qwen3-coder garbled extraction even across both GPUs) | researcher tool-use loop, per-page extraction |

**Premium chain** (lines 209–251) — recent arc, currently live for R and W:

```
DeepSeek (deepseek-v4-flash, json_object, ~$0.0006/call)
  → OpenAI (gpt-5.5, json_schema)              [out of quota at the moment]
  → GitHub Models (gpt-4o full)                [generous free tier]
  → free chain (Gemini 2.5 Flash → Groq → GitHub mini → NVIDIA)
  → local Ollama
```

**Free chain** is the default for FAST. It chains four free model providers before falling back to local; 429s are common on Gemini/Groq, so the chain is essential. CODE intentionally stays **local-first** (lines 725–727) so qwen2.5-coder's per-page extraction calibration is preserved; cloud is only a fallback.

**Fallback** (`_invoke_with_fallback`, lines 744–796): on any error (rate-limit, 5xx, timeout, schema mismatch), advance to the next provider in the chain. Every attempt is appended to `agent_runs.fallback_attempts` (JSONB), so `/trace` shows the full chain including which providers errored and why.

**Cap behaviour** (lines 522–528) — if REASONING is capped, the router downgrades to WORKHORSE and prepends a "think step by step" scaffold to the prompt. Other tiers don't downgrade; they degrade to local-only. Replay sessions skip cost accounting entirely (line 649).

**Call timeout** is a wall-clock `asyncio.wait_for(call_timeout_s)` (default 300 s, line 461) so a hung Ollama process kills the call instead of stalling the dispatcher.

**GPULock** (lines 369–408) — per-model `asyncio.Lock` plus a global semaphore (default 4 in-flight). Same model serializes (VRAM thrash prevention); different models run concurrently. This is what lets multiple researchers run while a thesis-kill verdict is being computed.

### 3.4 Curator — `boardroom/harness/curator.py`

Every model invocation gets its prompt assembled here. The Curator is recipe-driven: each invocation_type maps to a `Recipe` (lines 56–306) that declares:

- The tier
- The token budget (4 k for kickoff up to 22 k for researcher v1)
- Recall sessions to pull from Zep (e.g. `theses-lifecycle`, `dissent`, `ceo-deliberations`)
- A `task_data_builder` callable that fetches context from Postgres (active theses, finding counts, etc.)
- An output schema (Pydantic) the router validates against

The prompt is assembled as **layers with priority** (lines 330–338):

| Priority | Layer | Behaviour under budget pressure |
|---|---|---|
| 0 | system role, constitution, schema hint | never dropped |
| 1 | phase context, task_data | compacted before dropping |
| 2 | lessons (top 5 by confidence) | dropped first |
| 3 | recall (Zep episodic) | dropped first |

Token counting via `tiktoken cl100k_base`. If total > budget: first compact recall (currently a naive truncation; the design intent is an F-tier summary preserving decisions/dissent/dates), then drop priority-2-and-up layers.

**Constitution flips** between "Seed problem" (exploration) and "Charter" (execution) based on `company_state.current_phase` (lines 408–420). The phase layer also adds `days_in_phase` and `days_remaining`, so the model has a clock.

**Lessons** (priority 2) are fetched from the `lessons` table filtered by `applies_to_invocation` and gated on `status IN ('probationary','active')`. Lesson outcomes are reconciled after each run (002_skills.sql:175–224) — a lesson gets promoted to `active` after 5 supportive applications and retired after 3 contradicting.

### 3.5 Session — `boardroom/harness/session.py` (new)

The session is the new framework-level container for multi-step handler invocations. One `Session` per claimed handler call; one `agent_run` per LLM call within it. Mode is `live` or `replay` — replay (used by bench and trace replay) bypasses cost tracking and side effects (cooldowns, mutations). The session writes an `agent_sessions` row on `start()` and updates it on `finish(status)`. `next_step_order()` increments a counter so step_order is monotonic per session, which is what the trace DAG renders.

This is the spine of the observability story — every step's input/output/error/fallback chain is recoverable from the DB without external traces.

---

## 4. Handlers and agent roles

The autonomous loop is ten handler files under `boardroom/handlers/`. Each file is a stateless function that takes an event payload and emits state changes + downstream events.

### 4.1 Event → handler routing

| Event | Handler file | Tier | Role | Emits |
|---|---|---|---|---|
| `task.created` (research) | `researcher.py` | C / W (v2) | Researcher | `task.completed` |
| `task.completed` (research) | `task_completed.py` | W | Auditor | `finding.high_signal`, `audit.slop_detected` |
| `finding.high_signal` | `adversary.py` | R (legacy) / W (v2) | Adversary | `thesis.invalidated` |
| `audit.slop_detected` | `audit_slop_detected.py` | DB-only | System (breaker) | — |
| `thesis.invalidated` | `thesis_invalidated.py` | R | CEO | `task.created`, new theses |
| `thesis.confidence_changed` | `phase_adjudicator.py` | F | Adjudicator | `phase.transition_proposed` |
| `phase.transition_proposed` | `phase_transition.py` | R | CEO | — (writes charter) |
| `phase.budget_exceeded` | `phase_budget_exceeded.py` | DB-only | Watchdog | `phase.transition_proposed` (forced) |
| `queue.empty` (research) | `queue_empty.py` | W | Planner | — (writes tasks) |
| `reflection.requested` | `reflection.py` | F | Reflector | — (writes lessons) |

### 4.2 Defensive code worth knowing

These are the patches that fixed past flatlines. Don't remove without understanding why they exist.

- **`adversary.py:27–60`** — `DEFAULT_WEAKEN_DELTA = -0.1`. A "weaken" verdict with no `proposed_confidence_delta` used to be a silent no-op; now a Pydantic validator + handler default guarantee confidence actually moves. This was a major flatline cause.
- **`task_completed.py:46–56`** — `_verdict_from_score`. The auditor's model-emitted verdict field was ~18 % mislabelled, inflating slop rates and tripping the 40 % circuit-breaker on healthy theses. Verdict is now **derived from the calibrated `audit_score` bands**: 0–0.3 = slop, 0.3–0.7 = unclear, 0.7–1.0 = pass.
- **`task_completed.py:294–312`** — thesis-reinforcement upward force. Before this, confidence could only go DOWN (adversary weakens + slop penalty). Now `pass + relevance ≥ 8 + supports_thesis=True` findings reinforce confidence by `+0.08` each, capped at `+0.20` per batch. `ceo.thesis_rescore` exists in `ROUTE` but isn't yet implemented — this is the missing upward signal that broke the equilibrium.

### 4.3 Cooldowns and dedup

| Invocation | Cooldown | Why |
|---|---|---|
| `adversary.kill_verdict` | 4 h per thesis | Prevents verdict thrash when high-signal findings arrive in a burst |
| `phase_adjudicator.check` | per-thesis (configured in dispatch) | Avoids re-checking on every confidence delta |
| `planner.generate_tasks` | 10 min | Dedups rapid `queue.empty` fires when a small batch of researchers all finish at once |
| `reflect.batch_propose_lessons` (v2) | 6 h, max 20 runs | Batches dissent runs into one lesson-extraction pass |
| `phase.transition_proposed` | dedup on `(from_phase, to_phase, day)` | ON CONFLICT DO NOTHING on the event insert |

---

## 5. The v2 loops (in-flight)

The dominant in-flight arc is **loop decomposition for observability and iterative refinement** — opaque single-shot LLM calls are being replaced with structured multi-step pipelines where each step is its own bounded, debuggable invocation. Every v2 loop is gated behind an env var, default `legacy` for everything except researcher (default `v2`).

### 5.1 Researcher v2 — `boardroom/research/`

**Toggle:** `RESEARCHER_LOOP=v2` (default). Legacy preserved as `_legacy_handle_task_created` in `handlers/researcher.py`.

```
plan_inquiry (W, 12k)
  → InquiryPlan { sub_questions, proposed_experiments }
    │
    ├─ per sub-question (parallel up to 4):
    │     search (HN / Reddit / SearXNG / DDG)
    │     web_fetch_many   (Trafilatura → BeautifulSoup4 fallback, cached in fetch_cache)
    │     extract_evidence (C, 8k)  → EvidenceBatch (quote + claim + stance + confidence per page)
    │
    ├─ per experiment (dispatched by kind):
    │     fetch_pricing | count_demand_signal | compare_repo_growth | gh_search_trend
    │     interpret_experiment (W, ~6k) → ExperimentInterpretation
    │
    ▼
synthesize (W, 12k)
  → FindingOut[] (same shape as legacy — downstream handlers see no schema change)
    │
    ▼
gap_check (W, 5k)
  → iterate? (up to 2 iterations)
```

**Why this exists:** legacy researcher concatenated search snippets and asked one LLM call to produce findings. Findings cited URLs they hadn't read; the auditor couldn't ground them; the adversary couldn't refute them. v2 fetches the actual pages, extracts verbatim quotes, and treats experiments (real HTTP calls into GitHub / pricing pages / Reddit JSON) as first-class evidence. The output schema is preserved so the rest of the loop sees no change.

**The retrieval moat:** `fetch_cache` (URL → content, extractor, status_code, expires_at) — every fetch is cached with a TTL so re-runs don't hammer external sites and so the trace UI can show the exact bytes the model saw.

### 5.2 Auditor v2 — `boardroom/audit/`

**Toggle:** `AUDITOR_LOOP=v2`. Default legacy until shadow-validated.

```
per-finding (parallel up to 4):
  cross_check_finding (W, 10k)
    → EvidenceCrossCheck { claims[3..6], substance: low|medium|high, duplicate, notes }
       each claim matched against full evidence: yes | partial | no

batch_score (W, 8k)
  → AuditBatch (legacy schema — downstream state writes unchanged)
```

The split lets per-finding grounding be judged against the full evidence trail (the v1 prompt truncated to 3 items per page); the second stage is a pure aggregation step.

### 5.3 Adversary v2 — `boardroom/adversarial/`

**Toggle:** `ADVERSARY_LOOP=v2`. Default legacy.

```
plan_attack (W, 8k)         → AttackPlan { weak_points[2..3], proposed_experiment? }
  ├─ per weak_point (parallel up to 4 extracts, ≤3 pages each):
  │     search → web_fetch_many → extract_counter (W, 14k)
  │       → CounterEvidenceBatch (refutes | supports | neutral per page)
  ├─ optional stress_test_interp (W, 6k) — interpret adversarial experiment
  └─ judge_verdict (R, 10k, cold-path recall on dissent + theses-lifecycle)
       → AdversaryVerdictOut (legacy schema — kill | weaken | watch)
         calibrated: kill if ≥2 high-conf refutes from independent sources,
         weaken if ≥1 high-conf or multiple medium-conf, else watch
```

Same pattern as researcher — fetch real evidence, judge from gathered material, output the legacy schema so the kill path is unchanged.

### 5.4 Planner v2 — `boardroom/planner/`

**Toggle:** `PLANNER_LOOP=v2`. Default legacy.

```
assess_state (W, 8k)
  → StateAssessment (per-thesis gaps + portfolio notes + target_task_count)

propose_tasks (W, 10k)
  → PlannedTasks (legacy shape)

critique (W, 10k, self-review)
  → CritiquedTasks (final_tasks, changes_summary, confidence)
     catches: duplicates, wrong task_type, off-thesis, vague queries, mis-distributed, over-count
```

The third step is the value — a self-review pass that catches duplicates and off-thesis tasks before they poison the swarm.

---

## 6. Data model

Migrations are applied in numerical order by `make migrate`.

| Migration | Adds | Why |
|---|---|---|
| `001_initial.sql` | `company_state`, `theses`, `tasks`, `findings`, `events`, `agent_runs`, `phase_transitions`, `cooldowns`, `cost_tracking`, `memory_pointers`, `adversary_verdicts`, `slop_rate_by_thesis` MV, `notify_event()` trigger | Core state machine + event bus |
| `002_skills.sql` | `lessons`, `lesson_applications`, `tool_description_versions`, `trigger_reflection_on_dissent()`, `reconcile_lessons()` | Outcome-driven lesson lifecycle; fires `reflection.requested` on dissent |
| `003_triggers.sql` | `emit_task_created()`, `emit_queue_empty_if_drained()` | Auto-emit events from task row changes; decouples insertion from event awareness |
| `004_bench_runs.sql` | `bench_runs` (job_id, invocation_type, tier, results JSONB) | Persisted model-comparison history for the `/bench` tab |
| `005_deepseek_balance_log.sql` | `deepseek_balance_log` (total_balance, topped_up, granted) | DeepSeek has no usage API — spend is derived from balance deltas |
| `006_research_loop.sql` | `research_inquiries`, `evidence`, `experiment_runs`, `fetch_cache` | The v2 researcher's audit trail + retrieval moat |
| `007_agent_sessions.sql` | `agent_sessions`; `agent_runs` += (`session_id, step_name, parent_step_id, step_order, fallback_attempts`); `events += session_id`; updated `notify_event()` to include `session_id` in NOTIFY payload | The trace framework |

### Key invariants

- **`events.UNIQUE(event_type, target_type, target_id, dedup_key)`** — idempotency. Handlers can't re-fire on a replayed event.
- **`tasks` pending claim** — `FOR UPDATE SKIP LOCKED` in `state.claim_task` so multiple researchers parallel-claim without thrashing.
- **`findings.audit_score` is the truth** — `audit_verdict` is derived from it (see §4.2).
- **`agent_runs` cost** — `cost_usd` is populated only for non-local providers; local Ollama rows have `cost_usd = 0`. This is what `_record_cost` enforces.
- **Replay mode rows** — `agent_runs.session_id` linked to an `agent_sessions` with `mode='replay'` are excluded from daily cost caps.

### Memory (Zep v3)

`ZepClient` (`boardroom/memory/client.py`) wraps zep-cloud's async API. The v2 → v3 migration renamed the namespace from `memory` to `thread`; the upgrade also required coercing `created_at` from ISO string to `datetime` (commit 89341cb).

| Session (thread) | Written by |
|---|---|
| `theses-lifecycle` | every thesis event (created, confidence_changed, killed, reinforced) |
| `phase-transitions` | phase advances |
| `ceo-deliberations` | CEO working-out on non-trivial decisions |
| `dissent` | adversary verdicts + `audit.slop_detected` events |
| `charter` | written once at commitment, immutable |

**Reliability hardening:** per-session `asyncio.Lock` collapses concurrent `ensure_session()` calls during startup (otherwise N handlers all narrating to `dissent` would N-fan the thread.create() and hit Zep's 5 req/min cap). `write_message()` is best-effort — narrative writes that 429 log and return empty string rather than raising.

---

## 7. API surface

The FastAPI app composes five routers (`boardroom/api/main.py`).

### REST

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/snapshot` | full dashboard state (company, theses, findings, telemetry, org, costs) |
| GET | `/events?limit=N` | recent raw events |
| GET | `/theses/{id}/findings` | per-thesis findings |
| GET | `/bench/options` | invocation_types, models, theses for the bench picker |
| POST | `/bench/run` | start a model-comparison job (async, results stream into job) |
| GET | `/bench/jobs/{id}` | poll job progress |
| GET | `/bench/runs` | persisted history |
| POST | `/bench/replay-step` | re-run a frozen `agent_run` with a different model |
| GET | `/debug/agent-runs?status=&invocation_type=` | filtered agent_runs feed |
| GET | `/debug/research-tree/{task_id}` | full task audit trail: inquiries → evidence → experiments → findings → agent_runs |
| GET | `/debug/costs` | DeepSeek spend + GPU power (live nvidia-smi watts) |
| GET | `/trace/sessions?handler=&status=&mode=` | session list |
| GET | `/trace/sessions/{id}` | session + step DAG |

### WebSocket — `/ws/events`

Single `StreamHub` keeps one Postgres LISTEN connection alive and fans events out to all connected clients (`boardroom/api/stream.py:22–183`). Events are **enriched** with related state (theses, tasks, findings, company_state) before broadcast — except `step.*` and `session.*` events, which carry their own UI-relevant payload and skip the two-pool-query enrichment to avoid amplifying high-volume trace traffic.

---

## 8. Web dashboard

Next.js 16 + React 19 + Tailwind v4 + recharts + framer-motion + lucide + `@xyflow/react` + dagre. Pinned: Node 20 (`.nvmrc`), port 8088 (`web/package.json` dev script).

### Routes

| Route | What you see | Data |
|---|---|---|
| `/` | Command center: phase, stats, theses, task queue, findings, telemetry, skills, live event ticker | `/snapshot` poll 8 s + WS |
| `/flow` | LiveFlow DAG of the workflow loop | `/snapshot` poll 6 s |
| `/theses` | Active + killed theses with confidence, finding counts | `/snapshot` poll 8 s |
| `/events` | Event audit log, live | `/events?limit=200` + WS (capped 200) |
| `/org` | Org chart + recent agent runs (model, tier, latency, Langfuse link) | `/snapshot` poll 5 s |
| `/bench` **(new)** | Model comparison playground; persisted runs | `/bench/*` |
| `/debug` **(new)** | Live agent_runs feed + cost panel (DeepSeek balance, GPU watts) | `/debug/*` poll 5 s |
| `/debug/task/[id]` **(new)** | Per-task research tree: inquiries → evidence → experiments → findings → LLM payloads | `/debug/research-tree/{id}` |
| `/trace` **(new)** | Session list, filterable | `/trace/sessions` + WS session.* |
| `/trace/[id]` **(new)** | Session step DAG (ReactFlow + Dagre), node detail panel, replay with model/prompt swap | `/trace/sessions/{id}` + WS |

`/bench`, `/debug`, `/trace` are the in-flight tabs not yet committed. They are the observability surface for the v2 loops — without them, the multi-step pipelines would be opaque.

### Data layer

- `web/app/lib/api.ts` — typed REST client (`jget<T>` / `jpost<T>` with `cache: "no-store"`).
- `web/app/lib/ws.ts` — `useEventStream` hook with exponential-backoff reconnect (max 8 s) and 25 s keepalive ping.
- `web/app/lib/types.ts` — every server payload shape in TS.

---

## 9. MCP tool surface

Two MCP servers run as stdio child processes.

### `boardroom-state`

Typed read/write surface over Postgres. Read tools: `get_company_state`, `get_active_theses`, `get_thesis`, `count_active_theses`. Write tools: `create_thesis`, `update_thesis_confidence`, `kill_thesis`, `create_adversary_verdict`, `record_finding`, `claim_task`, `complete_task`. All writes emit the appropriate event so handlers wake up.

### `boardroom-research`

Three search sources with explicit fallback:

1. **`search_hacker_news`** — Algolia public API, no auth, score-ordered.
2. **`search_web`** — SearXNG (self-hosted at `SEARXNG_URL`, default `:8080`) with DDG HTML fallback. SearXNG 5 s timeout; on failure, DDG with granular timeouts (connect 3 s, read 10 s).
3. **`search_reddit`** — unauthenticated public JSON API (~60/min cap). **Recent fix:** requests `limit × 3` and post-filters with `_meaningful_terms` (length ≥ 3, not stopwords) — without this filter, "MCP adoption" was matching `/r/cats`. Reddit OAuth is still an open lever.

Plus `web_fetch_many` (used by the v2 researcher) — Trafilatura primary, BeautifulSoup4 fallback, caches every URL into `fetch_cache` with TTL.

---

## 10. Friction primitives — the autonomy contract

Five mechanisms keep autonomous operation honest. Removing any of them risks runaway behaviour.

1. **Cooldowns** — per-(invocation, target) windows. Re-thinks can't thrash.
2. **Critic gates** — Auditor verdicts gate `finding.high_signal`; Adversary verdicts gate `thesis.invalidated`. The chain only progresses past quality checks.
3. **Slop circuit-breaker** — if `slop_rate_by_thesis` > 40 % over 24 h on any active thesis, research halts on it and confidence drops 0.20.
4. **Cost caps** — REASONING 50/day, WORKHORSE 4000, FAST 2000, CODE 500. Local Ollama is excluded. On cap, REASONING downgrades to WORKHORSE-with-scaffold; others degrade to local-only.
5. **Idempotency** — every event has `dedup_key` in a UNIQUE constraint. Handlers can't re-fire.

Plus three operational watchdogs:

- **Internal dispatcher watchdog** — every 5 min, reaps stale tasks, orphan runs, missed events, phase-budget overruns.
- **Liveness pump** — re-emits `task.created` if pending tasks > pending events (the "harness restarted with work queued" case).
- **External liveness timer** — `boardroom-liveness.timer` every 3 min; if no activity in 20 min while unpaused and before the 30-day deadline, restart the harness and fire `ALERT_WEBHOOK_URL`. The 3-minute external timer catches soft hangs that an in-process watchdog can't.

---

## 11. Current state (shipped vs in-flight)

### Shipped (HEAD)

- Phase machine and event-driven loop, end to end exploration → execution stub.
- Legacy single-shot handlers for researcher, adversary, auditor, planner.
- 4-tier router, free-cloud chain, GPU lock, cost caps.
- Curator with layered prompts and Zep recall.
- Dispatcher with cooldowns, slop breaker, internal watchdog.
- External liveness timer + crash-loop retry on harness.
- FastAPI snapshot + WS event stream; Next.js dashboard with `/`, `/flow`, `/theses`, `/events`, `/org`.
- Langfuse v4 traces, Zep v3 memory.
- SearXNG + DDG fallback + HN + Reddit research surface.

### In-flight (working tree)

- **v2 loops** — researcher (default on), auditor, adversary, planner (default legacy until shadow-validated). Each is decomposed into 3–5 invocations with explicit schemas.
- **Premium routing** — DeepSeek as the lead for REASONING + WORKHORSE; chain falls through OpenAI → GitHub gpt-4o → free chain → local. WORKHORSE was added to `PREMIUM_TIERS` on 2026-05-27.
- **Trace framework** — `agent_sessions` table, session contextvar, `session_id` on `agent_runs` and `events`. Backend serves it via `/trace/sessions`; UI is `/trace` and `/trace/[id]` (ReactFlow + Dagre).
- **Bench tab** — `/bench` model comparison playground; `bench_runs` table.
- **Debug tab** — `/debug` agent_runs feed + cost panel; DeepSeek balance log via scheduled snapshots.
- **Research retrieval moat** — `fetch_cache` with TTL, Trafilatura extraction, evidence + experiment tables for full audit trail.
- **Reddit filtering** — `_meaningful_terms` post-filter to kill /r/cats-style off-topic results.

### Known open levers

- `ceo.thesis_rescore` is in `ROUTE` but unimplemented; the upward-confidence force currently lives in `task_completed.py` instead.
- Curator's recall compaction is naive truncation; the design calls for an F-tier summary preserving decisions / dissent / dates.
- Reddit OAuth would unblock the throttling; currently unauth + post-filter.
- Web intake is still SEO-heavy despite SearXNG; the v2 researcher mitigates by fetching pages and extracting, but the source list still leans SEO.
- Execution-phase handlers (publishing, customer outreach, payment flow) are not yet built — the loop is complete through commitment.

---

## 12. Repository layout (current)

```
boardroom/
├── docker-compose.yml          Postgres + SearXNG (Langfuse / Zep cloud-hosted)
├── docker-compose.demo.yml     Isolated side stack on :5433 / :8081 for researcher demo
├── Makefile                    pyenv, install, migrate, api, harness, web, dev, bench, infra-*
├── requirements.in / .txt      uv-managed; trafilatura + typer added in-flight
├── readme.md                   Project pitch + setup
├── ARCHITECTURE.md             (this file)
├── migrations/                 001..007 SQL
├── infra/searxng/settings.yml  SearXNG instance config
├── scripts/                    demo_research_loop.py + others (new)
├── deploy/                     ops artifacts
├── boardroom/
│   ├── bootstrap.py            seeds company_state + first CEO kickoff
│   ├── harness/
│   │   ├── main.py             entry; preflight, wire clients, dispatcher.run
│   │   ├── dispatch.py         LISTEN→fan-out, friction gates, watchdog
│   │   ├── router.py           4 tiers, premium + free chains, GPULock, fallback
│   │   ├── curator.py          layered prompts, Zep recall, budgets, recipes
│   │   └── session.py          (new) Session container for /trace
│   ├── handlers/               one file per event type (10 total)
│   ├── research/    (new)      v2 researcher: plan_inquiry → fetch → extract → synthesize
│   ├── audit/       (new)      v2 auditor: per-finding cross_check + batch_score
│   ├── adversarial/ (new)      v2 adversary: plan_attack → counter-evidence → verdict
│   ├── planner/     (new)      v2 planner: assess_state → propose → critique
│   ├── state/client.py         async typed Postgres surface
│   ├── memory/client.py        Zep v3 wrapper (per-session lock, best-effort writes)
│   ├── skills/                 (placeholder; lesson client lives in state)
│   ├── api/
│   │   ├── main.py             FastAPI app
│   │   ├── stream.py           WS hub (single LISTEN, fan-out, enrich)
│   │   ├── bench.py  (new)     model comparison
│   │   ├── debug.py  (new)     agent_runs + costs + research tree
│   │   └── trace.py  (new)     sessions + step DAG
│   └── mcp_servers/
│       ├── boardroom_state/    typed state MCP
│       └── boardroom_research/ HN / Reddit / SearXNG / DDG / fetch
├── tests/                      pytest; test_router_fallback, test_research_*
└── web/                        Next.js 16 dashboard
    ├── package.json            port 8088, dev = next dev
    ├── .nvmrc                  20
    ├── .npmrc                  engine-strict
    ├── next.config.ts          /api/* and /ws/* → :8503
    └── app/
        ├── layout.tsx
        ├── page.tsx            command center
        ├── components/         StatsGrid, WorkflowLoop, ThesesPanel, etc.
        ├── lib/                api.ts, ws.ts, types.ts
        ├── theses/page.tsx
        ├── events/page.tsx
        ├── org/page.tsx
        ├── flow/page.tsx
        ├── bench/page.tsx      (new)
        ├── debug/page.tsx      (new)
        ├── debug/task/[id]/    (new)
        ├── trace/page.tsx      (new)
        └── trace/[id]/page.tsx (new)
```

---

## 13. Where to read first if you're new

1. **Vision** — `readme.md` and `memory/project_boardroom_vision.md`.
2. **Loop** — `boardroom/harness/dispatch.py` (events) → `boardroom/harness/router.py` (tier policy) → `boardroom/handlers/*` (what each event does).
3. **Routing arc** — `commit ec65f2f` (REASONING premium chain) and `commit 504da2a` (silent-flatline supervision). Together they explain why the routing/fallback story looks the way it does.
4. **v2 loops** — start with `boardroom/research/loop.py` (the flagship). It's the template the other three follow.
5. **Observability** — `migrations/007_agent_sessions.sql`, `boardroom/harness/session.py`, `web/app/trace/[id]/page.tsx`.

---

_Document is point-in-time. Verify against current code before acting on any specific file:line citation._
