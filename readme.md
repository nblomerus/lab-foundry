# Boardroom — Autonomous AI-Native Company

You give it a problem. It figures out what business to build, validates it
against real-world evidence, and ships to a paying customer — within 30 days.
You do not touch it after bootstrap. You watch.

This is the v1 implementation. Everything runs locally on Ollama. The harness,
the model router, and the protocols are designed so the same code swaps
cleanly to cloud models when the local loop is proven.

## What it actually does

The harness drives a phase machine: **exploration → convergence → commitment →
execution**. Inside each phase, a team of stateless agents reacts to events
from a Postgres event bus. The autonomous re-think you asked for — "if a
thesis gets invalidated 2 hours later, get back to thinking" — happens in
seconds, not days.

### Loop (high level)

```
bootstrap
  └─ CEO generates 4-6 candidate business categories
     └─ each category → thesis (probationary)
        └─ each thesis → 3 disambiguating research tasks queued

then forever (event-driven):

  task.created   →  Researcher swarm claims & runs (SKIP LOCKED in parallel)
  task.completed →  Auditor scores each finding for slop
  finding.high_signal →  Adversary hunts contradictions on that thesis
  thesis.invalidated →  CEO spawns replacement categories or pivots
  thesis.confidence_changed →  Phase adjudicator checks transition criteria
  phase.transition_proposed →  CEO ratifies, writes charter on commitment
  audit.slop_detected →  Circuit breaker halts research on that thesis
  queue.empty →  Planner refills the task queue
  reflection.requested →  Lessons mined from dissent runs (probationary)
  phase.budget_exceeded →  Watchdog forces transition past 1.5× budget
```

Every dissent run (audit slop, adversary kill, critic non-pass) auto-fires a
reflection that may yield a probationary lesson. Lessons are injected into
future invocations via the curator. They're promoted to "active" after 5
supportive applications, retired after 3 contradicting ones.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                 HARNESS — the company's nervous system                    │
│                                                                          │
│  Event bus (Postgres LISTEN/NOTIFY)                                      │
│  Dispatcher  →  cooldowns + cost caps + slop circuit-breaker             │
│  Router      →  4 tiers (Reasoning / Workhorse / Fast / Code)            │
│  Curator     →  layered prompts (system / constitution / phase /         │
│                  lessons / recall / task / schema), Zep recall, budgets  │
│  Watchdog    →  stale tasks, missed events, phase budget enforcement     │
│                                                                          │
│  Tool layer (MCP servers)                                                │
│  ├── boardroom-state    →  typed read/write to Postgres                  │
│  └── boardroom-research →  HN / Reddit / DuckDuckGo / fetch              │
└──────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                       AGENTS — stateless LLM calls                        │
│                                                                          │
│  Strategic   Tactical    Execution    Critics              Governance    │
│  CEO         Planner     Researcher   Auditor (slop)       Adjudicator   │
│              Curator     (swarm)      Adversary (kill)     Reflection    │
└──────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│   Postgres (state)   +   Zep (episodic memory)   +   Langfuse (traces)   │
└──────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  FastAPI command center   +   Next.js dashboard (watch surface)          │
└──────────────────────────────────────────────────────────────────────────┘
```

## Stack

| Layer | Choice | Why |
|---|---|---|
| Database | Postgres 16 | Event bus (`LISTEN/NOTIFY`), single source of truth, `FOR UPDATE SKIP LOCKED` |
| Memory | Zep cloud / self-host | Episodic narrative recall; knowledge graph via Graphiti |
| Tracing | Langfuse | Per-invocation latency, tokens, cost |
| Local inference | Ollama | OpenAI-compatible, single-GPU serial scheduler |
| Model tiers | DeepSeek-R1 / GLM-4.7 / Qwen3-14B / qwen3-coder | One backend per tier; routing in code |
| Tool protocol | MCP (Model Context Protocol) | Typed tool surfaces; portable |
| Backend | FastAPI + asyncpg + WebSocket | REST snapshots + live event push |
| Frontend | Next.js 16 + React 19 + Tailwind v4 + recharts + framer-motion + lucide | Light slate theme |
| Packaging | pyenv + uv + Make | Matches `argos` conventions |

## Repository layout

```
boardroom/
├── docker-compose.yml             Postgres + Langfuse (+ optional Zep / SearXNG)
├── .env.example                   Config template (copy to .env)
├── Makefile                       pyenv / install / migrate / api / web / dev
├── requirements.in                Human-edited deps
├── requirements.txt               uv pip compile -U output
├── migrations/
│   ├── 001_initial.sql            Core schema (state, theses, tasks, events, …)
│   ├── 002_skills.sql             Lessons + reflection trigger
│   └── 003_triggers.sql           task.created + queue.empty triggers
├── boardroom/
│   ├── bootstrap.py               Seed company + first CEO kickoff
│   ├── harness/
│   │   ├── main.py                Entry point — wires everything, runs forever
│   │   ├── dispatch.py            Event-driven dispatcher with friction gates
│   │   ├── router.py              4-tier model router + GPU lock
│   │   └── curator.py             Layered context builder; recipes
│   ├── handlers/                  One file per event type
│   ├── state/client.py            Async typed Postgres surface
│   ├── memory/client.py           Zep client wrapper
│   ├── skills/client.py           Lessons fetch + insert
│   ├── api/                       FastAPI command center
│   └── mcp_servers/
│       ├── boardroom_state/       State MCP server
│       └── boardroom_research/    HN / Reddit / web search MCP server
└── web/                           Next.js dashboard
    ├── package.json
    └── app/
        ├── layout.tsx             Side rail + page nav
        ├── page.tsx               Command center (overview)
        ├── theses/page.tsx        Theses board
        ├── events/page.tsx        Event audit log
        ├── org/page.tsx           Org chart + runs
        ├── components/            UI primitives + panels
        └── lib/                   API client, WebSocket hook, TS types
```

## The seed

You give the company three things, set once:

- **Problem statement** — the open-ended job to solve
- **Stance** — what you'd be embarrassed to be associated with (filters trash)
- **Success criterion** — what counts as winning

The current default seed is "Discover and execute a business that produces
real revenue within 30 days, starting from zero" — the system has to figure
out the niche, audience, product, and GTM on its own.

## Phases

| Phase | Days | What the system does |
|---|---|---|
| Exploration | ~1-10 | Map plausible categories. Spawn research broadly. Most theses die. |
| Convergence | ~11-17 | Narrow to top 3 theses. Adversary intensifies. Disambiguate between them. |
| Commitment | day ~18 | CEO picks one thesis, writes the full charter, transitions to execution. |
| Execution | ~19-30 | Ship a deliverable to a paying stranger. |

Each phase has a budget; the watchdog forces a transition at 1.5× the budget
if the adjudicator hasn't proposed one.

## Setup

```bash
# 0. Make sure pyenv + Node 20 (via nvm) are installed.

# 1. Python env + deps
make pyenv          # creates the boardroom virtualenv via pyenv
make install        # uv pip sync requirements.txt

# 2. Infrastructure (Postgres, Langfuse)
cp .env.example .env
# Edit .env — set ZEP_API_KEY (free tier at app.getzep.com).
make infra          # docker compose up -d

# 3. Schema
make migrate        # applies all SQL migrations into the boardroom DB

# 4. Frontend deps
make web-install    # npm install in web/

# 5. Pull models (Ollama on the host)
ollama pull qwen3:14b
ollama pull qwen2.5:14b-instruct-q4_K_M
ollama pull mistral:7b-instruct-q4_K_M
ollama pull gemma2:27b                          # R-tier stand-in
# Optional but recommended for reasoning:
ollama pull deepseek-r1:32b-qwen-distill-q4_K_M
```

## Run

```bash
make bootstrap      # one-shot — seeds company, generates 4-6 categories
make dev            # api on :8503 + web on :3000 (Ctrl-C stops both)
make harness        # autonomous loop (separate terminal; runs forever)
```

Then open **http://localhost:3000** and watch.

## Common operations

| Command | What it does |
|---|---|
| `make psql` | Open a psql shell inside the Postgres container |
| `make db-reset` | Drop and recreate the boardroom schema; re-apply migrations |
| `make infra-logs` | Tail the docker-compose stack |
| `make upgrade` | Recompile `requirements.txt` from `requirements.in` |
| `make ruff` | Format + lint |

## Cost & friction primitives

Five mechanisms keep autonomous operation honest:

- **Cooldowns** — per-(invocation, target) windows. Re-thinks can't thrash.
- **Critic gates** — Auditor and Adversary verdicts gate downstream events.
- **Slop circuit-breaker** — Auditor's 24-hour slop rate > 40% on a thesis
  halts pending research and lowers confidence by 0.20.
- **Cost caps** — Reasoning tier capped at 4 calls/day; others have generous
  but real ceilings.
- **Idempotency** — every event carries a dedup key; handlers can't re-fire.

## Customizing the seed

`boardroom/bootstrap.py` has three constants at the top: `SEED_PROBLEM`,
`SEED_STANCE`, `SEED_SUCCESS`. Edit them, drop the company (`make db-reset`),
re-bootstrap. The CEO will regenerate categories from your new seed.

## Models

The current router maps tiers to whatever you have pulled. Pragmatic defaults:

| Tier | Used for | Local default |
|---|---|---|
| Reasoning (R) | Thesis kill, charter write, phase transition, adversary kill verdict | `deepseek-r1:32b-qwen-distill-q4_K_M` (best) / `gemma2:27b` (fallback) |
| Workhorse (W) | CEO weekly synthesis, planner, adversary contradiction hunt | `qwen3:14b` (or `glm-4.7-flash`) |
| Fast (F) | Auditor, phase adjudicator, reflection | `mistral:7b-instruct-q4_K_M` |
| Code (C) | Researcher tool-use loops | `qwen2.5:14b-instruct-q4_K_M` (or `qwen3-coder:30b`) |

Change them in [boardroom/harness/router.py](boardroom/harness/router.py).
The `ROUTE` table maps invocation types to tiers — that mapping is the right
place to tune model usage.

## What's still TODO

- **Production-grade execution-phase handlers.** The exploration → commitment
  loop is complete; execution-phase actions (publishing, customer outreach,
  payment flow) are not.
- **A2A agent-to-agent protocol.** Currently agents communicate via shared
  Postgres. Worth adopting when departments multiply.
- **DSPy-style prompt tuning.** Lessons are the lightweight version; full
  prompt evolution is out of v1 scope.
- **Hybrid local/cloud routing.** All tiers point at Ollama today. The router
  is designed to swap backends per tier without touching agent code.

## License

Personal project. No license yet.
