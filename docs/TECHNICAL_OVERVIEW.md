# LabFoundry — Technical Overview

> Current-state engineering reference (as of v0.1.15). The `*_SCOPE.md` files in
> this directory are *design* docs (intent); this document describes what is
> **actually built and running**. Where they differ, this wins.

LabFoundry is an autonomous AI research lab. The long-term goal: give it a seed
problem and it runs itself — a Principal Investigator frames directions and
hypotheses (**claims**), researchers gather grounded evidence, review gates
decide what's promoted, and the lab writes **lessons** that sharpen the next
round.

**What is live today** is the *knowledge substrate*: a fleet of discovery
**scouts** feeds **Mimir** (the Warden), which trust- and quality-gates every
source into **the Library** (a queryable pgvector corpus + Neo4j graph). The
research workflow (PI/Planner/Researcher/Critic/…) is **built but dormant**,
gated off by `KNOWLEDGE_CORE_ONLY=1` so the lab fills a strong base before
research begins.

---

## 1. Architecture at a glance

```
                          ┌─────────── discovery (scouts) ───────────┐
   arXiv  Web  GitHub  HF-datasets  OpenML
     │     │     │        │           │      each pages its own cursor
     └─────┴─────┴────────┴───────────┘      (refresh newest / deepen)
                   │  source.discovered
                   ▼
                ╔══════╗   TRUST gate  (deterministic ladder + LLM tie-breaker)
                ║ MIMIR ║  QUALITY gate (substance / no error-walls)
                ╚══════╝   → certify + embed   |   → quarantine / reject
                   │ document.ingested
                   ▼
              ┌─────────┐  pgvector corpus (chunks, 768-d)  + Neo4j context graph
              │ LIBRARY │  semantic search (corpus_search)
              └─────────┘
                   │ (when Ariadne wakes)
                   ▼
      PI → Planner → Researchers → Critic/Eval/Novelty → Reviewer → Library
      (research workflow — built, dormant under KNOWLEDGE_CORE_ONLY)
```

Everything is an **event-driven loop over a Postgres event bus**. There is no
RPC mesh: agents are async handlers; the dispatcher routes `events` rows to them
via `LISTEN/NOTIFY`. Mutations and the events they emit share a transaction, so
consumers wake reliably.

**Processes** (systemd `--user` units):

| Service | Port | Role |
|---|---|---|
| `labfoundry-harness` | — | the dispatcher: routes events to handlers, runs the watchdog + the discovery pump |
| `labfoundry-api` | 8503 | FastAPI command center (REST + `/ws/events` WebSocket) |
| `labfoundry-web` | 8088 | Next.js dashboard (the floorplan + inspectors) |

**Infra** (`docker compose`): Postgres 16 + pgvector (`:5432`), Neo4j 5
(`:7687/:7474`), SearXNG (`:8081`). Local inference via Ollama (`:11434`).

---

## 2. The dispatcher & the discovery pump (`harness/`)

`harness/dispatch.py::Dispatcher` is the core loop:

- **Event routing** — `LISTEN events`; each NOTIFY spawns a bounded handler task
  (`max_concurrent_handlers`). Handlers are registered per `event_type` in
  `harness/main.py`. Cooldowns, an urgent-event bypass, and a slop-pause gate
  sit in front.
- **Watchdog** (every 5 min) — reconciles lessons, refreshes the slop view,
  reaps orphaned tasks, and (only when Ariadne is *active*) emits a gentle
  agenda-tracking sweep.
- **Discovery pump** (`_discovery_pump_loop`) — the base-building driver while
  Ariadne is dark. It is **condition-driven, not interval-driven**: whenever the
  intake backlog (pending `source.discovered` / `document.parsed`) drops below a
  low-water mark, it fires the next discovery slice. So intake runs *continuously*
  and never idles between ticks, bounded by backpressure (it waits while the
  backlog is healthy) plus a short min-gap. Tunables: `LIBRARY_PUMP_LOW_WATER`,
  `_CHECK_SECONDS`, `_MIN_GAP_SECONDS`.

`KNOWLEDGE_CORE_ONLY=1` registers **only** the intake handlers (Mimir + scouts +
Library); the research-workflow handlers stay unregistered. `ariadne_active()`
(= NOT core-only) is the switch that flips discovery between aggressive
base-building and agenda-tracking.

---

## 3. Scouts — stateful discovery (`library/ingest/scouts.py`, `agents/mimir/collectors.py`)

Scouts are **pure source-finders**: each queries one source's API and returns
`SourceDescriptor`s (kind, source_kind, canonical_key, url, title). They never
touch the DB or emit events — emission + dedupe is the collector's job.

| Scout | source_kind | finds | paginates via |
|---|---|---|---|
| arXiv | `arxiv` | papers (newest-first) | `start` offset |
| Web | `web` | open-web pages (SearXNG) | `pageno` |
| GitHub | `github` | repos by stars | `page` |
| HuggingFace | `dataset` | datasets by downloads | — (refresh-only) |
| OpenML | `openml` | benchmark datasets by name | `offset` |

**Stateful discovery (migration 003)** is what stops scouts re-fetching the same
data:

- **`discovery_cursors`** — a per-source pagination cursor. `run_discovery_sweep`
  asks `state.discovery_offset(...)` for an offset that *alternates*: REFRESH
  (offset 0, every `LIBRARY_REFRESH_AFTER_S` ≈ 2h, to catch new submissions) and
  DEEPEN (advance the offset, wrap past `LIBRARY_MAX_OFFSET`, walking the
  back-catalogue). A scout therefore keeps surfacing *new* material instead of
  the same newest-N.
- **`discovery_seen`** — the novelty ledger / "is this new?" gate.
  `state.discovery_filter_new(...)` surfaces a candidate only if it's **absent
  from the corpus** (`document_exists`) **and** not attempted within
  `LIBRARY_RETRY_AFTER_S` (≈ 12h), recording each attempt. So a source that
  failed to ingest retries on a schedule — not every sweep (the old spin) and not
  never (the old dedup trap).

**Topic selection (`plan_sweep`)**: when Ariadne is dark, the sweep rotates a
wide slice of a ~48-topic AI/ML frontier taxonomy (aggressive base-building);
when she's active, it tracks her active **claims** (her agenda) plus a light
frontier top.

**Mimir → scout direction (`agents/mimir/focus.py`)**: discovery isn't only
scouts → Mimir. The *standing* focus flows down via `plan_sweep` (agenda → scout
topics). `request_focus(state, topics=…, requester="pi")` is the explicit **push**
lever — the PI directs the next sweep at specific topics (a directed
`library.sweep_requested`), the broad-topic counterpart to `request_acquire()`'s
single-source pull.

Per-source rate discipline: arXiv is globally serialized to ≥3.5s/call with a
15-min backoff on failure and an 8s fast-fail (it tarpits abusers); GitHub/arXiv
pace between topics; SearXNG/HF/OpenML are generous.

---

## 4. Mimir — the Warden: two gates (`agents/mimir/`, `library/trust/`, `library/ingest/`)

Every discovered source clears **two independent gates** before it enters the
Library. Both are mostly deterministic (cheap, scalable); one bounded LLM call is
reserved for a single ambiguous boundary.

### Ingest pipeline (`library/ingest/pipeline.py`)
`source.discovered → stage_source → classify_trust → (certify) → embed_and_finalize`

1. **Resolve full text** per source_kind:
   - arXiv → ar5iv HTML full body, abstract fallback;
   - GitHub → repo metadata (stars/forks/language/topics/license) **+ README**
     via the GitHub API (not JS-page scraping — we store the README+metadata,
     not code);
   - HF dataset → hub metadata (task categories, modalities, size, downloads,
     likes) + schema + sample rows via the datasets-server;
   - OpenML → description + qualities (instances/features/classes/target);
   - web/other → `web_fetch` (httpx + trafilatura, with a Playwright rung-2
     fallback for JS-only pages), Postgres-backed `fetch_cache`.
2. **QUALITY gate** (`library/ingest/quality.py`) — reject thin bodies
   (< `MIN_QUALITY_CHARS` = 500) and known error/wall pages (dataset-viewer
   errors, JS/cookie/login walls, 404s, rate-limit pages). Applied to **all**
   sources before a document is created, so hollow rows never enter the corpus.
3. **Parse + chunk** (deterministic; `parser.py` + `chunker.py`). Zero chunks →
   skip (no document).
4. **TRUST gate** (`library/trust/classify.py`) — a deterministic ladder:
   `peer_reviewed` (resolving DOI) > `preprint` (arXiv) > `official_repo` (active
   GitHub w/ releases) > `web_reputable` (curated hosts incl. wikipedia,
   huggingface.co, openml.org, *.gov/.edu) > `web_unknown`. Hard-gates above the
   ladder: a blocked **license** or a **retracted** source → quarantined. The
   lone `web_unknown` boundary may consult **one LLM tie-breaker** (capped at
   `web_reputable`).
5. **Finalize** — on approve: embed chunks (Ollama `nomic-embed-text`, 768-d),
   write vectors, best-effort MERGE into the Neo4j graph, flip
   `documents.queryable`, emit `document.ingested`. **Guard:** a doc that embeds
   0 chunks is left non-queryable (never a hollow "queryable" row).

Turned-away sources are auditable: `mimir.ingest_blocked` (trust) and
`library.ingest_rejected` (quality) carry their reasons.

The **acquire/pull path** (`agents/mimir/acquire.py`) is the demand side: an
allow-listed agent (PI / Researcher / Novelty) asks Mimir for a specific source;
Mimir caps, resolves, dedupes, and runs it through the same gates.

---

## 5. The Library (`library/corpus/`, `library/graph/`)

- **Corpus** — Postgres `documents` + `chunks` (pgvector `embedding`), plus
  `datasets`. `corpus_search(q, k)` embeds the query and runs a pgvector ANN over
  chunks → ranked hits with title, snippet, trust tier, score. This powers the
  dashboard's "Search the corpus" box (semantic, across titles/abstracts/full
  text/README+metadata).
- **Context graph** — Neo4j (Paper/Dataset/Finding/CITES). Populated best-effort
  on ingest; the KG extension is partial.

Live snapshot (illustrative): ~23k arXiv papers (a ~21.8k seed + continuous
intake), ~2k GitHub repos, web pages, and a growing dataset/openml set — all
quality-gated, hollow-doc count 0.

---

## 6. Research workflow (built, dormant)

Registered only when `KNOWLEDGE_CORE_ONLY` is off. Event-driven, same bus:

| Agent | Wakes on | Emits |
|---|---|---|
| **Ariadne** (PI) | exploration kickoff; claim confidence/invalidation; phase/budget | `claim.created`, phase decisions |
| **Planner** | `queue.empty` | `task.created` |
| **Researchers** | `task.created` | `finding.high_signal`, `task.completed`, `acquire.requested` |
| **Critic** | `finding.high_signal` | `claim.invalidated` |
| **Evaluation** | `task.completed` | slop score (entry gate) |
| **Novelty / Reviewer / Adjudicator** | review/promotion/phase | promotion + phase writes |

Phases: **frame → hypothesize → experiment → validate → write → submit**, each
budgeted; a watchdog forces a transition past 1.5× budget. Per-invocation model
routing (`harness/router.py`) swaps local↔cloud tiers without touching agent
code; recipes are assembled by `harness/curator.py`.

---

## 7. API (`api/`, FastAPI on :8503)

| Route | Purpose |
|---|---|
| `GET /knowledge/stats` | corpus + graph + memory counts |
| `GET /knowledge/recent` | latest ingests |
| `GET /knowledge/search?q=&k=` | **semantic corpus search** |
| `GET /knowledge/mimir` | Mimir Warden panel: at-a-glance, trust ladder, today's intake funnel, source mix, recent certifications, requests |
| `GET /knowledge/scout?kind=` | per-scout view: in-corpus, added-today, last-searched topics, recent findings (+ snippets) |
| `GET /knowledge/gate[?kind=]` | the intake gate — admitted / blocked-trust / rejected-quality + reasons; `kind` scopes it to one scout |
| `GET /agentlab/*`, `POST /agentlab/run` | Agent Lab: run any agent in isolation (LLM dry-run or live Mimir paths) + per-agent test suites |
| `GET /bench/*` | model-comparison bench over recipes |
| `GET /trace/*`, `/snapshot`, `/debug/*` | run traces, lab snapshot, debug |
| `WS /ws/events` | live event stream (Postgres NOTIFY fan-out) |

---

## 8. Dashboard (`web/`, Next.js on :8088)

The **Floorplan** (`web/app/components/Floorplan.tsx`) renders the lab as rooms:

- **Active rooms** (scouts, Mimir, Library) have rich drill-downs; **dormant**
  ones (Ariadne, Planner, Critic, Gate, Ops, …) show a minimal status panel.
- **Flow animations** are activity-gated, **per source kind** — a web
  `source.discovered` pulses only the Web flow — so the diagram reflects real
  traffic, not a perpetual loop. Subtle Mimir→scout "focus" lanes show direction.
- **Per-agent gates**: each scout's door opens *its* gate slice
  (`/knowledge/gate?kind=`); Mimir's opens the full intake gate. "Turned away
  (why)" lists trust blocks + quality rejections with reasons.
- **Scout panels** show recent findings (paper titles / URLs+summaries / repos /
  dataset ids) + last-searched topics, pulled durably from the corpus.
- **Mimir Warden panel** mirrors `/knowledge/mimir`; the **Library** panel has
  the semantic **corpus search** box.

> Live-stream note: the browser connects the WebSocket **directly to the API**
> (`:8503`), because Next.js dev rewrites proxy HTTP but not WS upgrades.
> Override with `NEXT_PUBLIC_WS_URL` / `NEXT_PUBLIC_WS_PORT`.

---

## 9. Data model (key tables)

| Table | Holds |
|---|---|
| `events` | the bus — every event, status, dedup_key; LISTEN/NOTIFY source of truth |
| `documents` / `chunks` | the corpus (kind, source_kind, canonical_key, trust_tier, status, queryable; chunk text + 768-d `embedding`) |
| `datasets` | dataset rows linked to documents |
| `claims` / `tasks` / `findings` / `critic_verdicts` | the research workflow state |
| `discovery_cursors` | per-(source,topic) pagination cursor (migration 003) |
| `discovery_seen` | novelty ledger — surfaced sources + attempts (migration 003) |
| `certifications` | immutable per-document trust decisions |
| `company_state` / `phase_transitions` / lessons tables | mission/phase + learning loop |

Migrations: `001_schema.sql` (baseline incl. corpus), `002` (trigger fix),
`003_discovery_cursors.sql`. Applied via `make migrate`.

---

## 10. Operating it

```bash
make infra            # Postgres+pgvector, Neo4j, SearXNG
make migrate          # apply migrations/*.sql
make install          # python deps (pyenv labfoundry env); playwright install chromium (once)
make web-install      # web deps (Node 20)
make bootstrap        # seed the lab
make dev              # api :8503 + web :8088
make harness          # the dispatcher loop (MIMIR_LOOP=on)
```

Unattended: the `labfoundry-{api,harness,web}` systemd `--user` units (auto-restart).

**Key env** (`.env`): `MIMIR_LOOP=on`, `KNOWLEDGE_CORE_ONLY=1`,
`LIBRARY_SCOUTS=arxiv,web,github,dataset,openml`, `SEARXNG_URL=http://localhost:8081`,
`DATABASE_URL`, `GITHUB_TOKEN`, `ZEP_API_KEY`. Discovery tunables:
`LIBRARY_AGGRESSIVE_TOPICS/_PER_TOPIC`, `LIBRARY_REFRESH_AFTER_S`,
`LIBRARY_RETRY_AFTER_S`, `LIBRARY_PUMP_LOW_WATER`, `ARXIV_MIN_INTERVAL_S`,
`WEB_FETCH_PLAYWRIGHT`.

Always run Python via the pyenv `labfoundry` env. Don't run the DB-integration
tests against the live `:5432` (the `db` fixture truncates/seeds).

---

## 11. Current state & known gaps

- **Live & continuous**: scouts → Mimir (trust + quality) → Library, driven by
  the backpressure pump; semantic corpus search; the full dashboard.
- **Dormant**: the research workflow (PI/Planner/Researcher/Critic/…), behind
  `KNOWLEDGE_CORE_ONLY`. `request_focus()` is ready for when Ariadne wakes.
- **Partial**: the Neo4j context graph (best-effort on ingest); HF datasets are
  refresh-only (no offset pagination); corpus search is vector-only (no KG/entity
  lookup yet); Retraction-Watch coverage of journal retractions is a follow-up.
