# LabFoundry

[![CI](https://github.com/nblomerus/lab-foundry/actions/workflows/ci.yml/badge.svg)](https://github.com/nblomerus/lab-foundry/actions/workflows/ci.yml)
![Coverage](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/nblomerus/ab1796b050ea71cedf5f34a92544aa82/raw/coverage.json)
![Python](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/nblomerus/ab1796b050ea71cedf5f34a92544aa82/raw/python.json)
![Version](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/nblomerus/ab1796b050ea71cedf5f34a92544aa82/raw/version.json)

An **autonomous AI research lab**. Given a seed problem, it runs itself like a lab:
a Principal Investigator decomposes the mission into directions and hypotheses
(**claims**); researchers gather grounded evidence; a novelty/quality gate and a
peer-review panel decide what gets promoted; and the lab writes **lessons** that
sharpen the next round. You set the mission and watch.

Everything runs locally on Ollama by default; the model router swaps to cloud
tiers per-invocation without touching agent code.

> **Status (v0.1.15).** The *knowledge substrate* is live and continuous: a fleet
> of discovery **scouts** feeds **Mimir** (the Warden), which trust- and
> quality-gates every source into the **Library** (pgvector corpus + Neo4j graph).
> The research workflow (PI/Planner/Researcher/Critic/…) is **built but dormant**,
> gated off by `KNOWLEDGE_CORE_ONLY=1` so the lab fills a strong base first.
> Full current-state reference: **[docs/TECHNICAL_OVERVIEW.md](docs/TECHNICAL_OVERVIEW.md)**.

## How it works

The harness is an **event-driven loop over a Postgres event bus** (`LISTEN/NOTIFY`).
Async agents react to events; mutations and the events they emit share a
transaction so consumers wake reliably.

**Knowledge intake (live now).** Scouts discover sources; a backpressure **pump**
keeps the pipeline fed continuously; Mimir gates and certifies; the Library
stores and serves them.

| Stage | Component | Owns |
|---|---|---|
| Discovery | **Scouts** (arXiv · Web · GitHub · HF-datasets · OpenML) | find new sources; each pages its own cursor + a novelty ledger so it never re-fetches the same data |
| The Warden | **Mimir** | two gates — **trust** (peer-reviewed > preprint > official-repo > reputable > unknown; + license/retraction; one LLM tie-breaker) and **quality** (substance, no error-walls) — then certify → embed |
| Knowledge store | **Library** | pgvector corpus (chunks, 768-d) + Neo4j context graph; semantic `corpus_search` |

**Research workflow (built, dormant).** PI (**Ariadne**) frames directions and
hypotheses (**claims**); **Planner** schedules; **Researchers** gather evidence;
**Evaluation/Critic/Novelty** review; **Reviewer/Adjudicator** promote and advance
phases (**frame → hypothesize → experiment → validate → write → submit**, each
budgeted). Mimir can direct the scouts' focus toward Ariadne's agenda
(`agents/mimir/focus.py`).

See [docs/TECHNICAL_OVERVIEW.md](docs/TECHNICAL_OVERVIEW.md),
[docs/MIMIR_WARDEN_SCOPE.md](docs/MIMIR_WARDEN_SCOPE.md), and
[docs/REAL_LAB_OPERATING_MODEL.md](docs/REAL_LAB_OPERATING_MODEL.md).

## Stack

| Layer | Choice |
|---|---|
| Database | Postgres 16 + **pgvector** (event bus, source of truth, vector corpus) |
| Memory | Zep (episodic narrative + Graphiti knowledge graph) |
| Graph | Neo4j 5 (claims/findings + corpus context graph) |
| Tracing | Langfuse |
| Discovery | SearXNG (web search) + arXiv/GitHub/HuggingFace/OpenML APIs |
| Web fetch | httpx + trafilatura, Playwright rung-2 for JS-only pages |
| Local inference | Ollama (per-tier model routing in code) |
| Embeddings | `nomic-embed-text` (768-d), local |
| Backend | FastAPI + asyncpg + WebSocket |
| Frontend | Next.js + React + Tailwind |
| Packaging | pyenv + Make; top-level packages (`pythonpath=["."]`) |

## Repository layout

```
agents/        One package per agent: mimir (the Warden) + pi, planner,
               researcher, critic, evaluation, novelty, reviewer, reflection
library/       The knowledge layer: ingest/ (scouts, pipeline, fetcher, quality),
               trust/ (the trust gate), corpus/ (pgvector search), graph/ (Neo4j)
harness/       Event loop: dispatch (+ discovery pump), router, curator, main
state/         Postgres client (the event bus + corpus + workflow state)
memory/ skills/  Zep episodic memory + lessons store
api/           FastAPI command center (knowledge, gate, scout, mimir, agentlab,
               bench, trace, snapshot, stream/ws)
ops/           bootstrap, corpus seeder, Mimir first-light runner
migrations/    SQL migrations (001 baseline · 002 trigger fix · 003 discovery cursors)
docs/          Design + this technical overview (start at docs/INDEX.md)
web/           Next.js dashboard (the floorplan + inspectors)
tests/         pytest suite
```

## Setup

```bash
# 0. pyenv + Node 20 (via nvm) installed.
make pyenv          # creates the `labfoundry` virtualenv
make install        # pip sync requirements.txt (top-level packages on pythonpath)
playwright install chromium   # once — rung-2 web fetch for JS-only pages

cp .env.example .env # set ZEP_API_KEY, GITHUB_TOKEN, etc.
make infra          # docker compose up -d (Postgres+pgvector, Neo4j, SearXNG:8081)
make migrate        # apply SQL migrations into the labfoundry DB
make web-install    # npm install in web/ (Node 20)

# models
ollama pull nomic-embed-text      # corpus embeddings (768-d) — required
# + your reasoning/workhorse/fast tier models (see harness/router.py)
```

## Run

```bash
make bootstrap      # one-shot: seed the lab
make dev            # api on :8503 + web on :8088
make harness        # the dispatcher loop (separate terminal; MIMIR_LOOP=on, runs forever)
```

Unattended operation uses systemd `--user` units — `labfoundry-api`,
`labfoundry-harness`, `labfoundry-web` (auto-restart). The dashboard's live
WebSocket connects straight to the API on `:8503` (Next dev rewrites don't proxy
WS); override with `NEXT_PUBLIC_WS_URL` if needed.

## Quality

```bash
make check          # ruff lint + format check (CI parity)
make tests          # pytest (DB-integration tests skip without a local DB)
```

CI (`.github/workflows/ci.yml`) runs lint + version-bump check, then tests with
coverage. The `version` file is bumped on every PR.

## Docs

- **[docs/TECHNICAL_OVERVIEW.md](docs/TECHNICAL_OVERVIEW.md)** — current-state
  engineering reference (architecture, scouts, Mimir's gates, the Library, API,
  dashboard, data model). Start here.
- Design docs (intent) live in [`docs/`](docs/) — index at
  [`docs/INDEX.md`](docs/INDEX.md); e.g. [`MIMIR_WARDEN_SCOPE.md`](docs/MIMIR_WARDEN_SCOPE.md),
  [`KNOWLEDGE_LAYER_SCOPE.md`](docs/KNOWLEDGE_LAYER_SCOPE.md).

## License

Personal project; no license yet.
