# LabFoundry

An **autonomous AI research lab**. Given a seed problem, it runs itself like a lab:
a Principal Investigator decomposes the mission into directions and hypotheses
(**claims**); researchers gather grounded evidence; a novelty/quality gate and a
peer-review panel decide what gets promoted; and the lab writes **lessons** that
sharpen the next round. You set the mission and watch.

Everything runs locally on Ollama by default; the model router swaps to cloud
tiers per-invocation without touching agent code.

> Status: actively evolving. The research loop (Researcher / Evaluation / Critic /
> Planner) and the knowledge substrate (the Library + Librarian ingest) are built;
> the full PI harness, the novelty/peer-review gate, and **Mimir** (the Library's
> trust warden) are partially built — see the design docs in [`docs/`](docs/).

## How it works

The harness is an event-driven loop over a Postgres event bus (`LISTEN/NOTIFY`).
Stateless agents react to events; every high-signal result clears a
novelty/quality gate before it advances, and dissent feeds the lessons store.

| Lab role | Agent | Owns |
|---|---|---|
| Principal Investigator | **PI** | the agenda: mission → directions → hypotheses (claims) + per-claim goals |
| Lab manager | **Planner** | the deliberative schedule — what to work on next |
| Methods reviewer | **Evaluation** | substance + groundedness; the entry gate (kills slop) |
| Adversarial reviewer | **Critic** | "can I break this claim?" |
| Related-work reviewer | **Novelty** | "has someone already shown this?" |
| Area chair | **Reviewer** | applies panel consensus; owns the single promotion write |
| Phase chair | **Adjudicator** | advances the phase only when evidence warrants |
| Head librarian | **Mimir** | governs the Library: source/document provenance + trust + certification |
| Data curator | **Librarian** | ingests papers/datasets into the corpus (chunk → embed → graph) |

Phases: **frame → hypothesize → experiment → validate → write → submit** — each
budgeted; a watchdog forces a transition past 1.5× budget.

**The Library** (the knowledge substrate) is a pgvector corpus + a Neo4j context
graph, governed by Mimir. See [docs/MIMIR_WARDEN_SCOPE.md](docs/MIMIR_WARDEN_SCOPE.md)
and [docs/REAL_LAB_OPERATING_MODEL.md](docs/REAL_LAB_OPERATING_MODEL.md).

## Stack

| Layer | Choice |
|---|---|
| Database | Postgres 16 + **pgvector** (event bus, source of truth, vector corpus) |
| Memory | Zep (episodic narrative + Graphiti knowledge graph) |
| Graph | Neo4j 5 (claims/findings + corpus context graph) |
| Tracing | Langfuse |
| Local inference | Ollama (per-tier model routing in code) |
| Embeddings | `nomic-embed-text` (768-d), local |
| Tool protocol | MCP (Model Context Protocol) |
| Backend | FastAPI + asyncpg + WebSocket |
| Frontend | Next.js + React + Tailwind |
| Packaging | pyenv + uv + Make; `src/` layout |

## Repository layout

```
src/labfoundry/          The Python package
├── harness/             Event loop: dispatch, router, curator, session, main
├── handlers/            One handler per event type
├── research/            Researcher loop + librarian (ingest) + fetchers
├── audit/ adversarial/ planner/   Evaluation / Critic / Planner loops
├── state/ memory/ skills/         Postgres / Zep / lessons clients
├── api/                 FastAPI command center
└── mcp_servers/         labfoundry_state / _research / _knowledge / _corpus
migrations/              SQL migrations (001 … 015_knowledge_corpus)
docs/                    Design docs (start at docs/INDEX.md)
deploy/systemd/          User units for unattended operation
web/                     Next.js dashboard
tests/                   pytest suite
```

## Setup

```bash
# 0. pyenv + Node 20 (via nvm) installed.
make pyenv          # creates the `labfoundry` virtualenv
make install        # uv pip sync requirements.txt
pip install -e .    # editable install of the src/ package

cp .env.example .env # set ZEP_API_KEY etc.
make infra          # docker compose up -d (Postgres+pgvector, Neo4j, SearXNG)
make migrate        # apply SQL migrations into the labfoundry DB
make web-install    # npm install in web/

# models
ollama pull qwen2.5:14b           # CODE tier
ollama pull nomic-embed-text      # corpus embeddings (768-d)
# + your reasoning/workhorse/fast tier models (see src/labfoundry/harness/router.py)
```

## Run

```bash
make bootstrap      # one-shot: seed the lab + kickoff
make dev            # api on :8503 + web on :8088
make harness        # the autonomous loop (separate terminal; runs forever)
```

Unattended operation uses the systemd units in [`deploy/systemd/`](deploy/systemd/)
(`labfoundry-api`, `labfoundry-harness`, `labfoundry-liveness`).

## Quality

```bash
make check          # ruff lint + format check (CI parity)
make tests          # pytest (DB-integration tests skip without a local DB)
```

CI (`.github/workflows/ci.yml`) runs lint + version-bump check, then tests with
coverage. The `version` file is bumped on every PR.

## Docs

Design docs live in [`docs/`](docs/) — start with [`docs/INDEX.md`](docs/INDEX.md).

## License

Personal project; no license yet.
