# AI-Native Company

A self-running company where three agents — **CEO**, **Planner**, and **Researcher** — operate as a closed loop. The CEO sets weekly strategic objectives, the Planner breaks them into concrete tasks, the Researcher executes them and produces findings, and next week's CEO reads those findings to set new objectives. You observe and grade.

This is the v1 proof-of-concept. Everything runs locally on Ollama with no API costs. Once the architecture is validated, the same code swaps cleanly to cloud models (DeepSeek V4, Claude, etc.) by changing config.

## Vision

A genuinely autonomous company, observable from a dashboard, that:

- Reads the world in a specific niche, every day.
- Decides what's worth pursuing, every week.
- Produces useful intelligence, continuously.
- Improves itself over time (later phases).

You are the board, not the operator. Agents propose; you ratify (at first); autonomy widens as evals justify it.

## Architecture

```
                  ┌──────────────────────────┐
                  │   CEO  (weekly review)   │
                  │                          │
                  │  Reads: findings, last   │
                  │   week's objectives      │
                  │  Writes: new objectives  │
                  └────────────┬─────────────┘
                               │
                               ▼
                  ┌──────────────────────────┐
                  │  Planner (daily cycle)   │
                  │                          │
                  │  Reads: active objectives│
                  │  Writes: task queue      │
                  └────────────┬─────────────┘
                               │
                               ▼
                  ┌──────────────────────────┐
                  │ Researcher (continuous)  │
                  │                          │
                  │  Reads: tasks, sources   │
                  │  Writes: findings        │
                  └────────────┬─────────────┘
                               │
                               └─────► back to CEO next week
```

**Key design choices:**

- **Stateless agents, stateful database.** Agents never assume in-memory state across runs. Every invocation loads what it needs from Postgres. This makes restarts, debugging, and parallel execution trivial.
- **One model serves all three agents** in v1, with different temperatures and prompts. Differentiate later only when evals show it helps.
- **Structured outputs end-to-end.** Every LLM call returns a typed Pydantic object. No regex parsing, no hallucinated schemas.
- **Tasks are work units.** Anything an agent does is a row in the `tasks` table. This is the unit of observability and the substrate for future scheduling.
- **No `langgraph` yet.** For three sequential agents with database-backed state, plain Python is clearer. We'll add LangGraph when we need branching, retries, or human-in-loop gates.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Local inference | Ollama (5070 Ti) | Easiest GPU model server; OpenAI-compatible |
| Main model | `qwen3-coder:30b` (Q4) | Strong tool calling, fits 16 GB, good at structured outputs |
| Embeddings (later) | `nomic-embed-text` on 2070 Super | Lightweight; for semantic search over findings |
| LLM client | `langchain-ollama` | Built-in Pydantic structured outputs; easy to swap providers |
| State | Postgres 16 | Single source of truth, ACID, easy to inspect with `psql` |
| Observability | Langfuse (self-hosted) | Traces every LLM call, costs, latencies |
| Scheduler | APScheduler | Simple in-process scheduling for v1 |
| CLI | Click | Quick manual runs while developing |

## Hardware

| GPU | Role |
|---|---|
| RTX 5070 Ti (16 GB) | Main reasoning model (Qwen3-Coder-30B Q4) |
| RTX 2070 Super (8 GB) | Embeddings + small classifier (later phases) |

If `qwen3-coder:30b` is too tight at Q4, fall back to `qwen3:14b` or `glm-4.7-flash`. Both fit comfortably in 16 GB with room for 32K context.

## Project structure

```
ai-native-company/
├── docker-compose.yml          # Postgres + Langfuse
├── .env.example                # Config template (copy to .env)
├── requirements.txt
├── README.md
├── migrations/
│   └── 001_initial.sql         # Schema
└── src/
    ├── config.py               # Settings from .env
    ├── models.py               # Pydantic types (CEOOutput, Finding, etc.)
    ├── db.py                   # Postgres pool + helpers
    ├── main.py                 # CLI entry point
    ├── shared/
    │   └── llm.py              # langchain-ollama wrapper
    └── departments/
        ├── ceo.py              # Weekly strategic review
        ├── planner.py          # Objective → task breakdown
        ├── researcher.py       # Task execution
        └── research_tools.py   # HN, arXiv, web fetch
```

## Database schema

Four tables, all in one schema for v1.

**`weekly_objectives`** — what the CEO has set
- `id`, `week_start`, `objective`, `success_criteria`, `rationale`, `status` (active/archived), `created_at`, `created_by_run_id`

**`tasks`** — concrete work items, owned by a department
- `id`, `objective_id`, `department`, `task_type`, `description`, `payload` (JSONB), `priority`, `status` (pending/running/completed/failed), `created_at`, `started_at`, `completed_at`, `result` (JSONB)

**`findings`** — intelligence the Researcher produced
- `id`, `task_id`, `source`, `url`, `title`, `summary`, `relevance_score` (1-10), `why_it_matters`, `created_at`

**`agent_runs`** — every agent invocation, for observability
- `id`, `department`, `agent_name`, `started_at`, `completed_at`, `status`, `input_summary`, `output_summary`, `error`, `langfuse_trace_id`, `tokens_used`

Task claiming uses `FOR UPDATE SKIP LOCKED` so multiple Researcher workers can run safely in parallel later.

## The three agents

### CEO

**Cadence:** weekly (Monday mornings, or on demand)
**Inputs:** last week's objectives with status, top 20 findings by relevance
**Outputs:** 2-5 `WeeklyObjective` records, optional stop-doing list
**Temperature:** 0.4 (some creativity, but mostly grounded)

System prompt frames it as a demanding founder. Hard rules:

- Objectives must be specific enough that the Planner can break them into concrete tasks in one pass.
- Every objective has a measurable success criterion checkable by next Monday.
- Week-1 mode is *foundational intelligence* — figure out what the niche looks like before trying to produce output.
- Not allowed to write content. Not allowed to plan tasks. Direction only.

### Planner

**Cadence:** daily (or whenever the task queue is shallow)
**Inputs:** active objectives, current pending-task count
**Outputs:** 3-10 `PlannedTask` records with department, type, priority, and JSON payload
**Temperature:** 0.3 (mostly mechanical)

Skips the run if there are already >20 pending tasks (prevents pile-up). Each task references a real `objective_id`. For research tasks, the payload includes `query` (string) and `sources` (e.g., `["hacker_news", "arxiv"]`).

### Researcher

**Cadence:** continuous (claims one task at a time)
**Inputs:** one claimed task from the queue + raw material from sources
**Outputs:** 0-N `Finding` records with relevance scores and reasoning
**Temperature:** 0.2 (precision over creativity)

The Researcher's system prompt emphasizes ruthless selectivity: most things score 3-5; 8+ is reserved for genuinely important items; 10 means the finding should change company strategy. Empty findings lists are explicitly acceptable when nothing in the raw material is genuinely relevant.

Tools available:
- `fetch_hacker_news_top(n)` — Algolia API, returns front page
- `fetch_arxiv(query, n)` — arXiv API, sorted by submission date
- `fetch_url_text(url)` — full-page fetch with BeautifulSoup cleanup

## Setup

### 1. Install Ollama and pull models

```bash
# Install: https://ollama.com/download
ollama pull qwen3-coder:30b      # ~18 GB
ollama pull nomic-embed-text     # ~270 MB
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — at minimum set COMPANY_NICHE and COMPANY_AUDIENCE to your domain
```

### 3. Start infrastructure

```bash
docker compose up -d
# Wait ~30s, then visit http://localhost:3000 to set up Langfuse
# Create a project, generate API keys, paste them into .env
```

### 4. Install Python deps

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 5. Smoke test

```bash
python -m src.main ceo          # Sets weekly objectives
python -m src.main planner      # Breaks objectives into tasks
python -m src.main researcher   # Executes one task
python -m src.main status       # Shows counts
```

Or run a full cycle in one command:

```bash
python -m src.main cycle        # CEO → Planner → drain Researcher
```

## What to look for in the first run

The whole point of v1 is to **grade the agents manually** while watching Langfuse traces. After one cycle, inspect Postgres directly:

```bash
docker compose exec postgres psql -U company -d company

\dt                                                        # list tables
SELECT objective, success_criteria FROM weekly_objectives; # CEO output
SELECT department, task_type, description, priority FROM tasks ORDER BY id;
SELECT title, relevance_score, why_it_matters FROM findings ORDER BY id DESC;
```

Grade these against your own judgment:

| Agent | Question to ask | Failure mode |
|---|---|---|
| CEO | Are these objectives *sharp*? Would I personally pursue them? | Vague, motherhood-and-apple-pie objectives |
| Planner | Are these tasks concrete enough that I could do them? | Too abstract; restates objective |
| Researcher | Are the high-scored findings *actually* interesting to me? | Inflates scores; surfaces obvious or generic stuff |

The faster you iterate on prompts based on what you see in Langfuse, the faster the system gets good. Don't move to v2 until v1 produces output you'd be willing to share.

## Cost-control philosophy

Even with local models, costs aren't zero — they're paid in time and electricity. A few rules baked in from v1:

- **Per-cycle caps**: Planner skips if >20 pending tasks.
- **Bounded context**: Raw material capped at ~20K chars per Researcher call.
- **Single model**: One Ollama instance handling all three agents; no GPU contention.
- **Structured outputs**: No retry loops chasing badly-formatted JSON. If the schema fails, we fail loud.

When we move to cloud APIs, the same patterns transfer directly to token-cap middleware.

## Roadmap

### v1 — Prove the loop (current)
- [x] CEO, Planner, Researcher in sequence
- [x] Postgres-backed state
- [x] Manual CLI execution
- [x] Langfuse tracing
- [ ] APScheduler running cycles automatically
- [ ] First eval harness (golden examples for each agent)

### v2 — Tighten the loop
- [ ] Embeddings + semantic search over findings (uses 2070 Super)
- [ ] LangGraph orchestration with checkpointing
- [ ] Per-agent prompt versioning in git
- [ ] Cost telemetry per run
- [ ] Streamlit dashboard

### v3 — Expand the company
- [ ] Editorial department (outliner → drafter → critic → editor → publisher)
- [ ] Local Markdown publishing pipeline
- [ ] Newsletter integration (Beehiiv API)
- [ ] Customer Support (when there's an audience)

### v4 — Self-improvement
- [ ] Agents propose improvements to their own prompts
- [ ] Eval-gated prompt deployment
- [ ] CEO can spawn new departments

### v5 — Hybrid model routing
- [ ] Route easy tasks → local, hard reasoning → DeepSeek V4-Flash, strategic → V4-Pro/Sonnet
- [ ] Prompt-cache-aware request shaping
- [ ] Off-peak batching for evals

## Open questions to answer before v2

1. **Niche.** What domain do you know well enough to grade the agents' output? Hobbies count.
2. **Definition of "running itself."** Unattended for a day? a week? Affects how aggressive we get with autonomous publishing.
3. **Public vs pseudonymous.** Real name or brand? Changes the human-in-loop thresholds.
4. **Budget tolerance.** When we eventually go hybrid, what's the monthly cap?

## License

Personal project. No license set yet — keep the repo private until decided.
