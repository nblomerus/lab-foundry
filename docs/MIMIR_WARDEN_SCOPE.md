# Mimir, Warden of the Library — Design

> **Status:** EXTENDS `KNOWLEDGE_LAYER_SCOPE.md`; obeys the two-plane model
> (`AGENT_OPERATING_MODEL.md` / `AGENT_INTERACTION_SCOPE.md`) and all four locked
> decisions. Produced by a **7-dimension design fan-out** (datamodel ·
> context-graph · librarian · trust · requests · retrieval · identity), each put
> through an adversarial review against the **live codebase**. Every verdict was
> *ship-with-fixes*; **the binding fixes are folded in below at the exact spot
> they apply (⚠️ Review fix callouts) and listed in §11.** This doc is a DESIGN
> DOC in the house style of `REAL_LAB_OPERATING_MODEL.md` — no code is built yet.
> NONE of the Library exists today: no pgvector, no `documents`/`chunks`/
> `datasets`, no embedder, no Librarian, no Mimir, no corpus tools. The Neo4j
> projection carries only `Claim`/`Finding`/`CriticVerdict`. This is the spec to
> make the Library real.

---

## 0. Framing — what Mimir is and is NOT

The diagram names **Mimir, Warden of Knowledge**: *governs trust, approvals, and
certification; guards The Library; powers = approve · block · certify; focus =
provenance, evidence, and paper-grade trust.* We operationalize Mimir as a
**governor over a librarian, plus a pull path** — the lab's **library staff**, a
separate branch of government from the claim-promotion panel already in
`REAL_LAB_OPERATING_MODEL.md §0`.

| Lab role | LabFoundry agent | Owns |
|---|---|---|
| Head librarian / Provenance officer | **Mimir** (`mimir/loop.py`, new — thin governor) | The **corpus's trust**: source + document certification, trust tiers, ingest approve/block, acquisition-request vetting. Owns the single trust write; the Librarian never self-certifies. |
| Data curator / Lab archivist | **Librarian** (`research/librarian/loop.py`, new — Plane-1 doer) | The **ingest pipeline**: fetch→clean→chunk→embed→extract→upsert RAG + MERGE KG. Writes corpus docs + Paper/Dataset/Source/Author nodes. Acts only on Mimir-approved ingests. |

### The separation of powers (locked decision 2 — state it explicitly)

The lab now has **two gates that must never collide**:

```
INPUT GATE  (corpus / provenance)          OUTPUT GATE  (claims / promotion)
   Mimir (Head Librarian)                     Evaluation → Critic → Novelty → Reviewer → Adjudicator
   Q: "can this DOCUMENT enter/be cited?"      Q: "is this CLAIM true / novel / promotable?"
   subject:  documents, sources, datasets      subject:  claims, findings
   verbs:    approve · block · certify · decay  verbs:    pass(entry) · vote · promote · hold · reject · merge
   tables:   documents.trust_*, certifications  tables:   critic_verdicts, gate_reviews, claims.status
   fires on: acquire request / document.ingested fires on: claim.promotion_candidate
```

**Why no collision (the load-bearing invariant).** A document being
`peer_reviewed` says nothing about whether a *claim citing it* is true (you can
cite a great paper to support a wrong claim, or a weak source to support a right
one). Conversely a claim being `replicated` says nothing about whether its
sources belong in the corpus. The two governance loops are **about different
objects** and therefore cannot overlap. Verified against live code: the panel
writes `critic_verdicts` + `claims.status` (`handlers/critic.py:188-228`),
`get_active_claims` (`state/client.py:128`) is `SELECT * FROM claims WHERE status
IN (...)` — claims-scoped, so the new corpus tables cannot pollute it. Mimir's
`document_status` values (`quarantined`/`certified`/`blocked`) are **disjoint**
from `claim_status` (`proposed/tested/weakly_supported/replicated/invalidated/
merged`, migration 008).

**The single, one-directional touch-point.** Novelty's `recall_prior_art` step
(`REAL_LAB §3`) may **read** a prior-art document's `trust_tier` to weight prior
art — a `peer_reviewed` prior-art hit is stronger evidence of "already done" than
a `web_unknown` blog. This is **read-only and one-way**: Novelty consumes trust;
Mimir never consumes a claim verdict. **Forbidden the other direction:** a panel
REJECT must NOT quarantine the cited documents. Assert this asymmetry in a test
(panel verdict writes never touch `documents.trust_*`).

### The Library legend holds
> *RAG finds relevant information · the Context Graph explains why it matters ·
> Mimir decides whether it can be trusted.* §1 builds the **what is relevant**
> (corpus + vector + KG nodes); §2 builds the **why it matters** (the lab's own
> cognition as graph citizens — first-class, per locked decision 4); §3–§4 build
> the **doer + judge**; §5 the **pull path**; §6 the **read path** that serves it
> all back to the agents.

---

## 1. The Library — five stores → three backends

The five diagram stores collapse onto exactly **three backends already in the
stack — no new service**. Postgres is the spine; Neo4j carries the graph
projections; the filesystem holds raw blobs that don't belong in a DB.

| Diagram store | Backend | Concrete home | Status |
|---|---|---|---|
| **Raw Store** (PDFs/papers/code/logs) | Postgres + filesystem | `fetch_cache` (existing, URL→cleaned text, TTL'd) + new `documents.raw_uri` (path/URL to the original blob; bytes on disk, not PG) | fetch_cache ✅; `raw_uri` pointer NEW |
| **Vector Index** (embeddings/chunks) | Postgres + pgvector | NEW `chunks` (`text` + `embedding vector(768)`) | NEW |
| **Structured Store** (dataset/experiment/claim ledgers) | Postgres | NEW `documents`, NEW `datasets`; EXISTING `experiment_runs`, `claims`, `findings` | partly ✅, corpus tables NEW |
| **Result Store** (metrics/figures/tables/artifacts) | Postgres pointer + filesystem | DEFERRED to a later migration — nothing produces durable artifacts today; only `experiment_runs.result` exists | ⚠️ **no real home yet — deferred** |
| **Context Graph** (entities/relationships/decisions/provenance/temporal) | Neo4j | EXISTING Claim/Finding/CriticVerdict + NEW Paper/Dataset/Source/Author (§1) + NEW AgentRun/Interaction/Decision/Certification (§2) | partly ✅ |

> ⚠️ **Review fix — the live image cannot run this migration.** The demo Postgres
> is `postgres:16-alpine`; `vector` is **not** in `pg_available_extensions` (only
> pgcrypto, pg_trgm, uuid-ossp). `CREATE EXTENSION vector` raises `ERROR: could
> not open extension control file`. The image **must** flip to
> `pgvector/pgvector:pg16` (already referenced — commented — at
> `docker-compose.yml:74` for the Zep store) in **both** `docker-compose.yml:3`
> AND `docker-compose.demo.yml:13` (service `postgres-demo`). This is not a soft
> pre-req: both compose files mount `./migrations:/docker-entrypoint-initdb.d:ro`,
> so on every **fresh-volume boot** Postgres runs every `migrations/*.sql` inside
> initdb — and the demo stack has **no `make migrate` path at all**, it relies
> SOLELY on the initdb.d mount. Without the swap the demo corpus can never be
> created. Validate `pg_available_extensions` contains `vector` post-swap. The
> base is volume-compatible (same PG 16), but validate on the demo volume first.

> ⚠️ **Review fix — no embedding model is pulled.** Ollama has only generative
> models. The chunk pipeline needs `ollama pull nomic-embed-text` (768-d).
> Without it every ingest's embed step fails and chunks land with NULL embeddings,
> silently degrading retrieval to keyword-only. Add a non-fatal embed probe to
> `main.py::_preflight` (`~line 63`) that POSTs `/api/embeddings` with
> `EMBED_MODEL` and logs **DEGRADED** (mirroring the Neo4j/Zep non-fatal blocks).
> Someone must ACT on that log.

### Migration `015_knowledge_corpus.sql` — the storage layer

**Number 015 is the honest non-colliding choice.** Files on disk are
`001-008, 011, 014`; `009/010/012/013` are RESERVED-but-unbuilt in
`REAL_LAB_OPERATING_MODEL.md §6` (009 `pi_directions`/`claim_goals`, 010
`agent_requests`, 012 `gate_reviews`, 013 `loop_terminations`); 014 already
landed. Migrations run by lexicographic glob (`Makefile:113`). 015 is the next
free integer above the highest applied file and leaves the reserved band intact.
The stale `KNOWLEDGE_LAYER_SCOPE §56` `009` is rejected — it collides with the
reserved `pi_directions` migration.

> ⚠️ **Review fix — non-idempotent DDL hard-fails the SECOND `make migrate`.**
> The Makefile migrate target loops `for f in migrations/*.sql` and re-applies
> **every** file on every invocation — there is NO `schema_migrations` tracking
> table. Postgres has no `CREATE TYPE IF NOT EXISTS`, so bare `CREATE TYPE
> document_kind …` raises `ERROR: type already exists` on the second run and rolls
> back the whole migration. 014/011 deliberately use the `IF NOT EXISTS` /
> guarded-`DO` convention; 015 MUST match it: wrap each `CREATE TYPE` in
> `DO $$ BEGIN … EXCEPTION WHEN duplicate_object THEN NULL; END $$;` and use
> `CREATE TABLE/INDEX IF NOT EXISTS` everywhere.

> ⚠️ **Review fix — `result_artifacts` is pulled OUT of 015.** The Result Store
> is explicitly deferred (nothing produces artifacts today). A `UNIQUE` on a
> nullable `content_hash` is dead schema that complicates 015's idempotency story
> for zero current benefit. It lands in a separate later migration when the first
> artifact producer exists. 015 is strictly Raw/Vector/Structured.

```sql
-- 015_knowledge_corpus.sql
-- The Library's storage layer: Raw/Vector/Structured stores in Postgres.
-- Mimir owns the trust_* columns + enums (semantics in §4); the Librarian owns
-- the content columns (§3). Numbered 015: 009-013 RESERVED (REAL_LAB §6), 014
-- last applied. Runs by lexicographic glob AND via docker-entrypoint-initdb.d on
-- fresh-volume boot — so the pgvector image swap is MANDATORY first (see §1 fix).
-- RE-RUN-SAFE: guarded CREATE TYPE + IF NOT EXISTS everywhere (014 convention).
-- PRE-REQ: image = pgvector/pgvector:pg16, and `ollama pull nomic-embed-text`.

BEGIN;
CREATE EXTENSION IF NOT EXISTS vector;        -- fails loudly on plain postgres:16-alpine

-- ---- ENUMS (guarded; Postgres has no CREATE TYPE IF NOT EXISTS) ------------
DO $$ BEGIN CREATE TYPE document_kind AS ENUM
   ('paper','media','dataset','web','code','note');
   EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- Mimir's ingest verdict (corpus admission). DISJOINT from claim_status.
DO $$ BEGIN CREATE TYPE document_status AS ENUM
   ('quarantined','certified','blocked');
   EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- The ordered trust LADDER (semantics owned by §4). Bottom rung is quarantine.
DO $$ BEGIN CREATE TYPE trust_tier AS ENUM
   ('quarantined','user_asserted','web_unknown','web_reputable',
    'official_repo','preprint','peer_reviewed');
   EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- Certification LIFECYCLE, orthogonal to tier (semantics owned by §4).
DO $$ BEGIN CREATE TYPE trust_state AS ENUM
   ('provisional','certified','decayed','quarantined');
   EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---- DOCUMENTS  (Structured Store: corpus registry + Mimir trust columns) --
CREATE TABLE IF NOT EXISTS documents (
    id              BIGSERIAL PRIMARY KEY,         -- surrogate; events.target_id-shaped
    kind            document_kind NOT NULL,
    title           TEXT,
    authors         TEXT[]   NOT NULL DEFAULT '{}',  -- denormalized; Author nodes in KG
    source_kind     TEXT     NOT NULL,             -- 'arxiv'|'openml'|'github'|'web'|...
    source_url      TEXT,
    canonical_key   TEXT     NOT NULL,             -- deterministic dedupe key (doi>arxiv_id>canon URL)
    doi             TEXT,
    arxiv_id        TEXT,
    published_at    TIMESTAMPTZ,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    license         TEXT,
    raw_uri         TEXT,                          -- Raw Store pointer (disk/object/URL)
    content_hash    TEXT,                          -- sha256(normalized raw bytes); idempotency key
    queryable       BOOLEAN NOT NULL DEFAULT FALSE,  -- Librarian flips after embed/upsert succeeds
    parse_run_id    BIGINT REFERENCES agent_runs(id),
    -- ---- Mimir-owned trust columns (datamodel owns the COLUMNS; §4 the SEMANTICS) --
    status          document_status NOT NULL DEFAULT 'quarantined',
    trust_tier      trust_tier  NOT NULL DEFAULT 'web_unknown',
    trust_state     trust_state NOT NULL DEFAULT 'provisional',
    provenance      JSONB       NOT NULL DEFAULT '{}',  -- fetcher, acquisition_request_id, checks, dedupe verdict
    certified_by_run_id  BIGINT REFERENCES agent_runs(id),
    certified_at         TIMESTAMPTZ,
    last_trust_review_at TIMESTAMPTZ,
    -- decay signals (refreshed by the Librarian on re-fetch; consumed by decay_trust §4)
    retracted        BOOLEAN NOT NULL DEFAULT FALSE,
    last_source_push TIMESTAMPTZ,
    -- ---- dedupe / idempotency --------------------------------------------
    CONSTRAINT uq_documents_canonical UNIQUE (source_kind, canonical_key),
    CONSTRAINT ck_documents_doi   CHECK (doi   <> ''),
    CONSTRAINT ck_documents_arxiv CHECK (arxiv_id <> '')
);
-- content_hash dedupe (exact bytes); partial so many NULLs coexist.
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_content_hash
    ON documents(content_hash) WHERE content_hash IS NOT NULL;
-- ⚠️ Review fix — UNIQUE(doi)/UNIQUE(arxiv_id) over-constrain a mixed corpus and
-- collapse empty strings onto one row. Use partial unique indexes + the CHECKs above.
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_doi
    ON documents(doi)      WHERE doi      IS NOT NULL AND doi      <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_arxiv
    ON documents(arxiv_id) WHERE arxiv_id IS NOT NULL AND arxiv_id <> '';
CREATE INDEX IF NOT EXISTS idx_documents_kind   ON documents(kind);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
-- Hot retrieval path: certified, non-blocked corpus, by trust.
CREATE INDEX IF NOT EXISTS idx_documents_trust  ON documents(trust_tier, trust_state);

-- ---- CHUNKS  (Vector Index) ---------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal       INT NOT NULL,                  -- 0-based position within doc
    text          TEXT NOT NULL,
    embedding     vector(768),                   -- nomic-embed-text; NULL until embed runs
    embed_model   TEXT,                          -- lets a future re-embed migrate cleanly
    token_count   INT,
    content_hash  TEXT NOT NULL,                 -- sha256(text); idempotent re-chunk + re-embed skip
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_chunks_doc_ordinal_hash UNIQUE (document_id, ordinal, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
-- ✅ Decision (LOCKED) — HNSW pinned + created INLINE here. Unlike ivfflat (which
-- needs training rows and silently mis-recalls on an empty table, forcing a
-- deferred reindex), HNSW builds incrementally and is VALID on an empty table —
-- so the vector index ships in 015 itself, no `make reindex-corpus` step. Cosine
-- ops match nomic-embed-text's normalized vectors (the <=> operator in §6).
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
-- Query-time recall knob (per session, set by labfoundry_corpus._get_pool):
--   SET hnsw.ef_search = 40;
-- Build cost on a slowly-growing corpus is negligible; if a large backfill ever
-- makes inline build slow, raise maintenance_work_mem for that one load.

-- ---- DATASETS  (Structured Store: dataset registry) ----------------------
CREATE TABLE IF NOT EXISTS datasets (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    url          TEXT,
    modality     TEXT,                           -- 'tabular'|'text'|'image'|'audio'|...
    task         TEXT,                           -- 'classification'|'regression'|...
    size         TEXT,
    license      TEXT,
    notes        TEXT,
    document_id  BIGINT REFERENCES documents(id),  -- nullable: a dataset is a trust-judgeable corpus citizen
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_datasets_name UNIQUE (name)
);
CREATE INDEX IF NOT EXISTS idx_datasets_task ON datasets(task);

-- ---- CERTIFICATIONS  (append-only Mimir decision ledger — §4) -------------
-- Mirrors the critic_verdicts(append) + claims.status(denormalized) pattern.
CREATE TABLE IF NOT EXISTS certifications (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES documents(id),
    decision      TEXT NOT NULL CHECK (decision IN ('approve','block','certify','decay','recertify')),
    from_tier     trust_tier,                    -- NULL on first approve
    to_tier       trust_tier  NOT NULL,
    to_state      trust_state NOT NULL,
    signals       JSONB  NOT NULL DEFAULT '{}',  -- the deterministic classify_trust signals dict
    used_llm      BOOLEAN NOT NULL DEFAULT FALSE,
    reasons       TEXT   NOT NULL,               -- verbatim; surfaced to requester on block
    decided_by_run_id BIGINT REFERENCES agent_runs(id),  -- NULL for automatic decay
    requested_by  TEXT,                          -- 'researcher'|'pi'|'evaluation'|'librarian'|'watchdog'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_certifications_doc ON certifications(document_id, created_at DESC);

-- ---- trust_rank() — stable ordinal for the retrieval floor + rerank (§4,§6) --
-- ⚠️ Review fix — explicit CASE, NOT array_position(enum_range()). An IMMUTABLE
-- positional function silently corrupts indexed ordering if a tier is ever
-- inserted mid-ladder via ALTER TYPE ... ADD VALUE BEFORE. Tiers may only be
-- APPENDED; pinning integers here makes that safe.
CREATE OR REPLACE FUNCTION trust_rank(t trust_tier) RETURNS INT
  LANGUAGE sql IMMUTABLE AS $$ SELECT CASE t
    WHEN 'quarantined'    THEN 0 WHEN 'user_asserted' THEN 1
    WHEN 'web_unknown'    THEN 2 WHEN 'web_reputable'  THEN 3
    WHEN 'official_repo'  THEN 4 WHEN 'preprint'        THEN 5
    WHEN 'peer_reviewed'  THEN 6 END $$;

COMMIT;
```

**Idempotency contract.**
- `documents`: upsert keyed on `(source_kind, canonical_key)` — `INSERT … ON
  CONFLICT (source_kind, canonical_key) DO NOTHING RETURNING id` — collapses
  re-fetches deterministically. `content_hash` is the exact-bytes backstop; `doi`/
  `arxiv_id` are semantic dedupe (two URLs for one arXiv paper → one document).
  Mimir trust columns are NEVER overwritten by re-ingest — only by a §4 certify
  path. The Librarian must normalize empty doi/arxiv `''`→NULL before insert (the
  CHECKs reject `''`).
- `chunks`: upsert on `(document_id, ordinal, content_hash)`; the hash lets the
  embed step skip unchanged text. Re-chunking deletes chunks (CASCADE) and
  re-inserts.
- `datasets`: upsert on `name`.

> ⚠️ **Review fix — `documents.id` is a BIGSERIAL surrogate, NOT a hash.** The
> librarian dimension proposed a deterministic `_doc_id` hash; a hash does not fit
> signed 64-bit `events.target_id` safely and risks collision (silent corpus
> merge). The deterministic dedupe lives in the separate `UNIQUE(source_kind,
> canonical_key)` column; `id` stays a clean BIGINT FK-shaped value. Mimir's
> pull-path dedupe (§5) checks the same UNIQUE, not a hashed id.

### Neo4j EXTERNAL-knowledge nodes (Paper/Dataset/Source/Author)

Add to `labfoundry_knowledge/tools.py::ensure_constraints()` (called from
`main.py:104-105`). `Paper.id == documents.id` — **same identity, no join
table**, exactly the contract the graph already uses for `Claim.id`/`Finding.id`.

```cypher
CREATE CONSTRAINT paper_id    IF NOT EXISTS FOR (p:Paper)   REQUIRE p.id   IS UNIQUE;
CREATE CONSTRAINT dataset_id  IF NOT EXISTS FOR (d:Dataset) REQUIRE d.id   IS UNIQUE;
CREATE CONSTRAINT source_url  IF NOT EXISTS FOR (s:Source)  REQUIRE s.url  IS UNIQUE;
CREATE CONSTRAINT author_name IF NOT EXISTS FOR (a:Author)  REQUIRE a.name IS UNIQUE;
CREATE INDEX     paper_doi    IF NOT EXISTS FOR (p:Paper)   ON (p.doi);
```

- `(:Paper {id=documents.id, doi, arxiv_id, title, year, trust_tier})`
- `(:Dataset {id=datasets.id, name, modality, task})`
- `(:Source {url, kind})` · `(:Author {name})`
- `(Finding)-[:CITES]->(Paper)` · `(Claim)-[:USES]->(Dataset)` ·
  `(Paper)-[:FROM]->(Source)` · `(Paper)-[:BY]->(Author)`

New inline write functions (mirror the existing `merge_*` pattern at
`tools.py:77-194`; **never** exposed over MCP; best-effort + swallowed like
`graph_sink.py`):

```python
async def merge_paper(id:int, doi, arxiv_id, title, year, trust_tier:str,
                      source_url, authors:list[str]) -> None: ...
async def merge_dataset(id:int, name:str, modality, task) -> None: ...
async def link_finding_cites_paper(finding_id:int, paper_id:int, created_at) -> None: ...
async def link_claim_uses_dataset(claim_id:int, dataset_id:int, created_at) -> None: ...
```

> ⚠️ **Review fix — split `ensure_constraints()` to avoid a merge-conflict
> hotspot.** This dimension (corpus nodes) and §2 (cognition nodes) both append
> Cypher to the one `ensure_constraints()`. Split it into
> `ensure_corpus_constraints()` + `ensure_cognition_constraints()` called
> sequentially from `main.py:105`. Both reuse the single `_get_driver()` singleton
> (`tools.py:36`); neither redefines the other's labels.

### Raw Store fidelity + images / media (LOCKED)

**Papers keep their full bytes.** `fetch_cache` truncates extracted text to ~50KB
(`fetcher.py`) — fine for an HTML page, lossy for a paper's body + figures. For
`kind='paper'` the Librarian writes the **full original blob** (the PDF) to
`raw_uri` (filesystem/object store — **never** Postgres) and parses from *that*;
`fetch_cache` stays the cheap dedupe/extract cache. The 50KB cap never gates a
paper's text or its figure extraction.

**Images / media are first-class corpus citizens — governed now, pixel-embedded
later.** A figure, chart, diagram, or media file gets a real `documents` row
(`kind='media'`), its blob in `raw_uri`, a **Mimir trust tier**, and a
`(:Paper|:Source)` graph link — so it is provenance-tracked and trust-judged
exactly like text. What it does **not** get in v1 is a **multimodal/pixel
embedding**: media is retrievable by its **text surface** — title, caption,
alt-text, the figure caption + surrounding paper context, and optional OCR for
charts/tables — which is chunked + embedded into the *same* text `chunks` index
like everything else. So images **enter the Library now, are trust-judged now, and
are findable now** via their text.

> A CLIP-style *visual* embedding index is a deliberate phase-later. It needs a
> second embedder and a `vector(N)` of a different dimension — and the
> **pinned-per-corpus dim rule** (§3) forbids mixing dims in one index — so it
> lands as its own `media_chunks` table + `labfoundry_corpus` visual-search path
> when there's a real need, not bolted into the text index. Tracked as out-of-v1,
> not unsolved.

---

## 2. The Context Graph — the lab's cognition as queryable graph citizens

> **LOCKED (decision 4).** The Library records not just external papers but the
> lab's **own cognition** — agent interactions, decisions, findings — as
> first-class, queryable graph citizens. This is the *"Context Graph explains why
> it matters"* layer; it complements RAG's *"what is relevant"*. It gets its own
> top-level section, not a sub-bullet.

### Backend boundary (binding): Neo4j is curated cognition; Zep stays episodic

The Context Graph is a **deterministic, schema'd Neo4j projection** of the
`events`/`agent_runs` tables — **NOT** delegated to Graphiti. `recall_graph`
returns open-ended dicts with no stable ids (`memory/client.py:258`) and can't
answer *"which exact Decision cited Finding 412 and what did it supersede"*; only
the Neo4j projection (already keyed on `Claim.id`/`Finding.id`) can.

- **Decisions** write to **BOTH**: a typed `(:Decision)` node → Neo4j, and a
  one-line narrative → the relevant existing Zep session (`claims-lifecycle`/
  `dissent`/`phase-transitions` — already written by `critic.py`/
  `phase_transition.py`, so **no new Zep session is needed for cognition**).
- **Interactions** write to **Neo4j ONLY** — an `agent.request` is structured by
  construction; narrating every delegation to Zep would flood the ~5 req/min
  thread cap.

### New node families (additive — do not redefine Claim/Finding/CriticVerdict)

```
(:AgentRun   {id, department, agent_name, invocation_type, session_id, status, started_at})
(:Interaction{id, kind:'delegation'|'pipeline', from_role, to_role, intent, status, depth,
              parent_run_id, payload_hash, created_at})
(:Decision   {id, kind, actor, rationale, temporal_state:'active'|'superseded', created_at})
```

`:Decision.kind ∈ {pi_decompose, pi_spawn, pi_kill, gate_promote, gate_hold,
gate_reject, gate_merge, phase_transition, critic_verdict, mimir_approve,
mimir_block, mimir_certify, mimir_decay}`. The `mimir_*` kinds are **owned by §4
semantically**; this section owns only their graph projection. A `critic_verdict`
Decision co-exists with the existing `:CriticVerdict` node — the Decision is the
*cognition view* (uniform actor+rationale+temporal_state), the CriticVerdict node
keeps its evidence-grounding role; we link them `(:Decision)-[:REALIZED_AS]->
(:CriticVerdict)` rather than duplicate. A `mimir_certify` Decision links
`(:Decision)-[:CERTIFIES]->(:Paper)` (the §1 anchor) and is the same shape as a
`(:Certification)` ledger row projected — coordinate the exact node label with §4
(use `(:Decision{kind:'mimir_*'})` as the canonical cognition citizen; a separate
`(:Certification)` is redundant and dropped).

### New edges (the "why it matters" topology)

| Edge | From → To | Carries | Written by |
|---|---|---|---|
| `:REQUESTED {intent, status, depth}` | `(:AgentRole)→(:AgentRole)` | the Plane-2 chord | universal hook on `agent.request`/`agent.reply` |
| `:PRODUCED_BY` | `(:Finding)→(:AgentRun)` | provenance of a finding | inline at the researcher write |
| `:DECIDED_BY` | `(:Decision)→(:AgentRun)` | who/which run made the call | inline in each producer |
| `:ABOUT` | `(:Decision)→(:Claim)` | what the decision concerns | inline |
| `:CITES` | `(:Decision)→(:Finding)` | evidence the decision rests on | inline (from `cited_*_ids`) |
| `:SUPERSEDES` | `(:Decision)→(:Decision)` | temporal override chain | replay-reconstructed (see fix) |
| `:REALIZED_AS` | `(:Decision)→(:CriticVerdict)` | dedupe with existing verdict node | inline in `critic.py` |
| `:CITES` *(corpus)* | `(:Finding)→(:Paper)` | source document | inline; no-op guard if `paper_id` null |

> ⚠️ **Review fix — the chord is role→role, not run→run.** The spec wanted
> `(:AgentRun)-[:REQUESTED]->(:AgentRun)`, sourced from
> `event['emitted_by_run_id']`/`consumed_run_id`. But `consumed_run_id` is a
> **dead column** (`migrations/001_initial.sql:217`) — `_mark_consumed` only ever
> writes `consumed_by_handler` (a string), and the `agent.request` payload is
> **role-keyed** (`AGENT_INTERACTION_SCOPE.md:28`) with no target run at request
> time (the target run doesn't exist yet). So the run→run chord is unbuildable.
> Model `REQUESTED` as **role→role** (matching the scope doc's "chord across the
> ring, Critic→Researcher" viz). If run→run is ever wanted, "requests writes
> `consumed_run_id`" becomes an explicit cross-dimension contract with §5 — not an
> assumption here.

### Schema bootstrap — extend `ensure_cognition_constraints()` (the §1-split sink)

```cypher
CREATE CONSTRAINT agentrun_id    IF NOT EXISTS FOR (a:AgentRun)    REQUIRE a.id IS UNIQUE;
CREATE CONSTRAINT interaction_id IF NOT EXISTS FOR (i:Interaction) REQUIRE i.id IS UNIQUE;
CREATE CONSTRAINT decision_id    IF NOT EXISTS FOR (d:Decision)    REQUIRE d.id IS UNIQUE;
CREATE INDEX      decision_at    IF NOT EXISTS FOR (d:Decision)    ON (d.created_at);
```
`:Interaction.id` = the Postgres `events.id`; `:Decision.id` = a synthetic
`"kind:pk"` (e.g. `"critic_verdict:418"`) — both stable, so MERGE is idempotent
and replay-safe.

### The inline projection sink (how it's written — no blocking, no new transport)

**(1) Interactions — ONE universal hook, NOT a second handler.**
`dispatch.register()` enforces single-handler-per-event (`dispatch.py:115`), and
`agent.request` is already owned by the (designed) `handle_agent_request`. So we
hook, not register.

> ⚠️ **Review fix — the hook goes in `_process_event`, not `_mark_consumed`.**
> `_mark_consumed`'s signature is `(self, event_id, handler_name, result)`
> (`dispatch.py:230`) — the full `event` dict and `session` are **not in scope**
> there, so the proposed call would `NameError`. Place the hook in
> `_process_event` where `event` and `session` live (`~line 173`).

> ⚠️ **Review fix — "every event flows through `_mark_consumed`" is FALSE.**
> Suppressed events (`no_handler`/`cooldown`/`cost_cap`/`slop_pause`,
> `dispatch.py:180-194`) go to `_mark_suppressed`; handler exceptions go to
> `_mark_failed`. A delegation rejected by a cost/cooldown gate, or a handler that
> raises — exactly what an operator most wants on the chord viz — would be
> invisible. Project from a `finally`-block after the try/except in
> `_process_event` (`~lines 206-226`), tagging `status` from which of
> consumed/failed/suppressed ran. (Or scope the chord to "successful only" and
> say so — we choose the `finally` so refusals are visible.)

> ⚠️ **Review fix — the projection must NOT sit on the handler hot path.** A
> Neo4j round-trip inside the `async with self._handler_sem` (`dispatch.py:201`)
> holds one of only 4 handler slots for its full latency; under
> `MAX_CONCURRENT_HANDLERS=4` a slow/queued Neo4j throttles real research. And
> `asyncio.create_task` fire-and-forget re-introduces the unbounded concurrency
> the semaphore exists to prevent (`dispatch.py:98-105`). **Use a bounded,
> decoupled sink:** push projection jobs onto an `asyncio.Queue` drained by a
> single dedicated worker task **outside** the handler semaphore, with a short
> per-write driver timeout. Bounded AND off the critical path.

> ⚠️ **Review fix — suppress projection on replay.** Replay sessions emit real
> event rows (`session.py:107-140`, `mode='replay'`) that flow through
> `_process_event`. Hard-guard the sink: skip when `session.mode=='replay'`
> (reachable via the dispatcher session contextvar, `dispatch.py:119-128`),
> matching how Router cost-tracking already special-cases replay.

**(2) AgentRuns — projected lazily, id-only.**

> ⚠️ **Review fix — no per-event Postgres SELECT.** `:AgentRun`'s descriptive
> fields (`department`/`agent_name`/`invocation_type`) live on `agent_runs`
> (`001_initial.sql:182-184`), not on the event row. A synchronous lookup per
> consumed event is a second DB query on the hot path. Project `:AgentRun` with
> **id only** (defensive MERGE) and let the replay/backfill projector batch-join
> `agent_runs` for the descriptive fields. Defensive MERGE means edge endpoints
> exist regardless of order.

**(3) Decisions — inline in producers, exactly where graph writes already live:**
- `critic.py:197` (beside `merge_critic_verdict_challenged_claim`): add
  `merge_decision(kind='critic_verdict', actor='critic', rationale=verdict.
  reasoning, cited_finding_ids=verdict.cited_finding_ids, about_claim_id=claim_id,
  run_id=run_id)` + a `REALIZED_AS` edge.
- `phase_transition.py:~274` (inside the charter txn): after the
  `phase_transitions` insert, `merge_decision(kind='phase_transition',
  actor='pi', rationale=decision.reasoning, cited_claim_ids=payload['cited_claim_ids'])`.
- Future `pi/loop.py`, `gate/loop.py`: `merge_decision(kind='pi_decompose'|
  'gate_promote'|…)` at the persist spot.
- Mimir (§4): `merge_decision(kind='mimir_certify'|'mimir_block'|…)` — projection
  contract only.

> ⚠️ **Review fix — `PRODUCED_BY` must point at the RESEARCHER run, not the
> evaluator.** At `task_completed.py:~294` the `run_id` in scope is the
> **Evaluation** (slop-scorer) run (`task_completed.py:268/246`), not the
> researcher run that produced the finding. Wiring `PRODUCED_BY` there falsely
> attributes every finding to the evaluator. Resolve the producing run from the
> finding's own provenance, or project `PRODUCED_BY` where the researcher writes
> the finding.

> ⚠️ **Review fix — `created_at` is a `datetime`, not a `str`.** Existing inline
> calls pass `created_at=claim.created_at`/`finding.created_at` — Python
> `datetime` objects (`critic.py:209`, `task_completed.py:304`), while the
> `merge_decision` signature declared `created_at:str`. A `str`-vs-`datetime`
> comparison in `context_graph_state_as_of` then silently returns wrong results.
> Standardize on a Neo4j temporal (`datetime($x)`) everywhere and convert
> explicitly at the call site; make `state_as_of` compare like-for-like.

> ⚠️ **Review fix — `SUPERSEDES`/`temporal_state` is DERIVED, not a stored mutable
> flag.** Flipping a prior decision's `temporal_state` requires a read-before-write
> against a best-effort store that may be stale/down (Neo4j is non-fatal,
> `main.py:101-108`); a dropped prior write breaks the chain and `state_as_of`
> returns two "active" decisions. Make `state_as_of` compute "latest
> non-superseded ABOUT claim with `created_at<=ts`" directly from `created_at`
> ordering + explicit `SUPERSEDES` edges, and reconstruct `SUPERSEDES`
> authoritatively in the **replay projector** from Postgres (the source of truth).
> Treat any inline `supersedes_pk` as a best-effort hint only.

### The "why it matters" read tools (Plane 1, ungated) — `labfoundry_knowledge/server.py`

```python
async def context_graph_explain(target_kind: Literal['claim','finding'], target_id:int) -> dict:
    """The decision + interaction + provenance chain that explains 'why this matters'.
    For a claim: every Decision ABOUT it (active first), each with its CITES findings,
    DECIDED_BY run, SUPERSEDES ancestor; plus the delegation Interactions whose runs
    produced the cited findings. For a finding: PRODUCED_BY run, CITES paper, and every
    Decision that CITES it."""

async def context_graph_state_as_of(claim_id:int, ts:str) -> dict:
    """Decisions ABOUT claim_id with created_at<=ts, excluding any SUPERSEDED by an
    earlier-than-ts decision → the lab's belief state at T."""

async def context_graph_interactions(run_id:int|None=None, since:str|None=None,
                                     limit:int=50) -> list[dict]:
    """The chords: REQUESTED edges (from_role,to_role,intent,status,depth). Feeds the
    viz's animated arcs across the ring (delegation=animated, pipeline=static)."""
```
Exposed via `mcp.tool()` like the existing three readers; `merge_*` stays
internal. `context_graph_explain` is the multi-hop superset over the existing
`get_claim_evidence_chain`/`get_finding_influence`.

### Backfill / replay
`scripts/project_context_graph.py` replays `events` + `agent_runs` (ordered by id)
through `merge_interaction`/`merge_agent_run`/`merge_decision` — idempotent
(stable MERGE keys), authoritatively reconstructs `SUPERSEDES`, and batch-enriches
`:AgentRun` descriptive fields. An **optional** index `idx_events_type_session ON
events(event_type, session_id)` speeds the scan — confirm the query shape first;
it partly overlaps the existing `idx_events_session` (`007_agent_sessions.sql:64`),
so only add it if the projector filters by `event_type` first.

---

## 3. The Librarian — ingest pipeline / data agent

The Librarian is the corpus **doer**, a fifth peer loop
(`src/labfoundry/research/librarian/loop.py`) that mirrors the researcher's
recipes-dict + `curator.build()` + `router.invoke(session=, step_name=)` skeleton
so `/trace` renders ingest as a DAG. It governs **nothing** — it writes rows in
`trust_state='provisional'` and waits for Mimir.

### Package layout (mirror the researcher)
```
src/labfoundry/research/librarian/{__init__,loop,chunker,embedder,schemas}.py
src/labfoundry/research/fetcher.py             # + search_arxiv, fetch_openml, fetch_github_repo
src/labfoundry/handlers/librarian.py           # handle_source_discovered, handle_ingest_approved
```

### Steps & recipes — two LLM recipes only

| step | kind | recipe? | tier | budget | wake/parent |
|---|---|---|---|---|---|
| fetch | Python (`_SOURCE_FETCHERS`) | no | — | — | start of phase A |
| **parse** | `librarian.parse` | **yes** | CODE | 16_000 | child of session root; skipped for clean trafilatura pages |
| chunk | Python (`chunker.plan`) | no | — | — | between calls |
| embed | Python (`embedder.embed_many`) | no | — | — | phase B only |
| **extract_entities** | `librarian.extract_entities` | **yes** | CODE | 12_000 | fan-out from `parse_run_id` |
| upsert/MERGE | Python (state + neo4j) | no | — | — | phase B only |

Chunk and embed are deterministic Python invoked **between** router calls, never
routed: routing exists to pick an LLM tier and burn the cap; chunking is
tokenizer arithmetic and embedding is a fixed-model call with no tier choice.
Both recipes are `Tier.CODE` (local-first `qwen2.5-coder`, same rationale as
`researcher.extract_evidence`). ROUTE additions (`router.py:ROUTE`, after the
existing block): `"librarian.parse": Tier.CODE`, `"librarian.extract_entities":
Tier.CODE`. No `DAILY_CAPS` change — both ride the existing `Tier.CODE:500`. Embed
calls are local-only and **never** cap-counted.

> ⚠️ **Review fix — extract_entities is the real cost driver, not embed.**
> `Tier.CODE:500/day` is shared with `researcher.extract_evidence` +
> `adversary.extract_counter`. `librarian.extract_entities` fanning out per
> chunk-batch on a 60-chunk paper can burn 5–15 CODE calls **per document**; an
> N-paper sweep can exhaust the CODE cap and degrade researcher evidence
> extraction to local-only. **Bound the batch size** and profile before enabling
> the sweep; consider a per-day ingest cap.

### The orchestrator — split at Mimir (the mechanical encoding of "never self-certify")

Phase A (fetch→parse→chunk-plan→persist `trust_state='provisional'`, emit
`document.parsed`) runs on the wake event. **Phase B** (embed→upsert chunks→
extract_entities→MERGE KG→flip `queryable`→emit `document.ingested`) runs **ONLY**
when the dispatcher delivers `mimir.ingest_approved`. Making the embed/upsert half
a *separate handler invocation triggered by Mimir's event* turns the trust gate
into a hard control-flow boundary, not a politeness convention — and a blocked
source costs one parse call, not 60 embed calls.

```python
LIBRARIAN_ENABLED = os.environ.get("LIBRARIAN_LOOP","").lower() in {"v1","on"}  # default OFF

async def run_ingest_phase_a(source, dispatcher, *, triggered_by_event_id):
    fetched = await _fetch_source(source, state)
    if not fetched or not fetched.content.strip():
        return {"skipped": True, "reason": "empty/blocked"}
    parsed, parse_run_id = await _maybe_parse(fetched, source, curator, router, session, ...)
    doc_id, is_new = await state.upsert_document(           # ON CONFLICT(source_kind,canonical_key) DO NOTHING RETURNING id
        kind=source.kind, source_kind=source.kind, canonical_key=source.canonical_key,
        title=parsed.title, authors=parsed.authors, source_url=fetched.url,
        doi=parsed.doi, arxiv_id=parsed.arxiv_id, raw_uri=source.url,
        trust_state="provisional", parse_run_id=parse_run_id)
    if not is_new:
        return {"document_id": doc_id, "deduped": True}
    plan = chunker.plan(parsed)   # rag-bench PaperChunker: equation/table/section-aware (see "Chunker" below)
    await state.stage_chunk_plan(doc_id, plan)                    # text+ordinal+hash, NO vectors
    await state.emit_corpus_event("document.parsed", target_type="document", target_id=doc_id,
                                  payload={"kind": source.kind, "n_chunks": len(plan),
                                           "title": parsed.title, "url": fetched.url,
                                           "parse_run_id": parse_run_id})
    return {"document_id": doc_id, "n_chunks": len(plan), "awaiting": "mimir"}

async def run_ingest_phase_b(document_id, dispatcher, *, triggered_by_event_id):
    doc = await state.get_document(document_id)
    if doc.trust_state == "quarantined":                          # Mimir blocked it
        return {"skipped": True, "reason": "blocked"}
    plan = await state.get_chunk_plan(document_id)
    pending = [c for c in plan if not await state.chunk_has_vector(document_id, c.ordinal, c.hash)]
    vectors = await embedder.embed_many([c.text for c in pending])  # vector(768)
    await state.upsert_chunks(document_id, pending, vectors)        # UNIQUE(document_id,ordinal,content_hash)
    entities = await _extract_entities(plan, doc, curator, router, session, parent=doc.parse_run_id)
    await _merge_kg(doc, entities)                                 # best-effort, swallowed like graph_sink
    await state.set_document_queryable(document_id, True)          # Librarian flips MECHANICAL queryable;
    await state.emit_corpus_event("document.ingested", target_type="document", target_id=document_id,
                                  payload={"kind": doc.kind, "n_chunks": len(plan),
                                           "trust_tier": doc.trust_tier})
    return {"document_id": document_id, "queryable": True}
```

> ⚠️ **Review fix — `state.emit_event(...)` DOES NOT EXIST.** Every event in the
> repo is emitted by a bespoke method inlining `INSERT INTO events … ON CONFLICT
> DO NOTHING`; the only `emit_event` is `Session.emit_event` — keyword-only and it
> writes **no `dedup_key`** (`session.py:107`). Add a real
> `StateClient.emit_corpus_event(event_type, *, target_type, target_id, payload,
> dedup_key)` helper that does a UNIQUE-keyed inline INSERT (matching
> `router._emit_cap_hit` / `complete_task`), so `document.parsed`/`ingested` get
> sweep idempotency. This is the right cleanup (the codebase inlines everywhere).

> ⚠️ **Review fix — `document.kind` does NOT touch the findings source Literal.**
> Extending `research/schemas.py:93` `FindingOut.source` reaches into the OUTPUTS
> path the gate consumes (`loop.py:300` synth prompt; duplicated
> `handlers/researcher.py:43`) — a locked-decision-2 violation. Define a SEPARATE
> `DocumentKind = Literal['paper','dataset','code','web','media','note']` in the
> new `librarian/schemas.py`. The `documents.kind` enum (§1) is the DB-side
> contract; the findings Literal stays exactly as-is.

> ⚠️ **Review fix — `claim_task` cannot claim the event's source.** The librarian
> spec conflated "claim a 'library' task with SKIP LOCKED" with "the source rides
> the event payload". `claim_task(department='library')` returns an *arbitrary*
> pending task ordered by priority, not the source the `source.discovered` event
> is about, and there is no library-task producer. **Drop the task-claim framing.**
> `source.discovered` carries the source inline in its payload;
> `handle_source_discovered` runs phase A directly on THAT source (deduped by the
> `UNIQUE(source_kind, canonical_key)` upsert) — exactly how `graph_sink` handles
> `claim.created` off `target_id`.

### Chunker — vendor rag-bench's `PaperChunker` (LOCKED: reuse, don't reinvent)
The Librarian's `chunker.plan` is **not new code** — it adapts the battle-tested
`PaperChunker` from rag-bench
(`/home/nicholas/workspace/rag-bench/rag_bench/core/chunker.py`), already
purpose-built for arXiv/ML papers and solving the hard parts the earlier
"~800-token tiktoken" sketch hand-waved:
- **equation-aware** — protects `$$…$$`, `\[…\]`, `\begin{equation|align|gather}`
  from being split (placeholder swap → restore);
- **table-aware** — keeps table rows with their header;
- **acronym expansion** — first occurrence per chunk (`MIPS` → `Maximum Inner
  Product Search (MIPS)`);
- **section blocklist** — drops references/acknowledgments noise (`SECTION_BLOCKLIST`);
- **contextual prefix** — prepends `"{title} — {section}"` per chunk for embedding
  recall (a real win);
- **pluggable `ChunkingStrategy`** (`rag_bench/core/strategies/`) — splitter swappable.

**Adaptation (the only work):** vendor `chunker.py` + `core/strategies/` + the two
constants (`MIN_CHUNK_LENGTH=100`, `SECTION_BLOCKLIST`) into
`research/librarian/chunker.py` (vendor, not a cross-repo import), and map its
`ChunkData` output → the Librarian chunk-plan rows (`{ordinal, text, content_hash}`
for §1's `chunks`; `section` into `provenance`). **Supersedes the earlier
`target_tokens=800`/tiktoken note:** rag-bench is **char-based** (`chunk_size=1024`,
`overlap=128`, recursive strategy) — proven, and ~1024 chars ≈ 250 tokens sits well
inside nomic-embed-text's window; keep a token target only as an optional strategy
config. **Companion reuse (Phase 2/3 — flag now, evaluate then):** rag-bench's
`core/ingest.py` (paper → sections/acronyms/metadata) is the natural basis for
`librarian.parse`, and `core/entity_extractor.py` for `librarian.extract_entities`
+ the §1 Paper/Author KG nodes.

### Embedder — deterministic, pinned, lock-aware
`embedder.embed_many(texts) -> list[list[float]]` (768-d, asserts dim against
`chunks.embedding` at import). Provider pinned per corpus — you cannot mix
`vector(768)` from nomic with `vector(1536)` from OpenAI in one index, so the
provider is PINNED, never fall-through-chained like chat.

> ⚠️ **Review fix — embed must respect VRAM, not just budget.** A private
> `asyncio.Semaphore(4)` bypasses the router's `GPULock` (`router.py:359-411`,
> `max_in_flight=4`), so a sweep storm can drive concurrent `/api/embeddings`
> loading the embed model onto a GPU alongside chat models on the 8GB 2070
> SUPER — a realistic VRAM OOM. Either route embed concurrency through the SHARED
> GPULock, or set the embedder semaphore to 1–2 AND **batch** (the
> `/api/embeddings` `input` field accepts a list — 20–60 chunks per HTTP call, not
> per-chunk). Pin the embed model to the larger GPU. (See also §6 — the retrieval
> read path's query-embed has the same constraint.)

### New fetchers (`fetcher.py`, alongside the existing search fetchers)
`search_arxiv` (Atom API; returns arxiv_id/title/authors/abstract/pdf_url —
`ttl_for` already gives arxiv.org 30d), `fetch_openml` (JSON; → Dataset node +
`datasets` row), `fetch_github_repo` (reuses the token pattern from
`experiments/gh_search_trend.py`). They register into `_SOURCE_FETCHERS =
{"arxiv":…, "openml":…, "github":…, "web": web_fetch_one}`; the existing
`search_web`/`search_hacker_news`/`search_reddit` are untouched (web search stays
first-class).

### Watchdog sweep (mirror `_check_phase_budget`, `dispatch.py:530`)
`_check_ingest_sweep` (`LIBRARIAN_SWEEP_HOURS`, default 24) re-discovers standing
`ingest_sources` whose last sweep is stale, emitting one `source.discovered` per
due source with `dedup_key=sweep-{source}-{hourbucket}` (date-bucket dedup,
identical to `budget-{phase}`). Standing-sources-only for v1 — autonomous crawling
risks corpus bloat that floods Mimir.

> ⚠️ **Review fix — cite only BUILT loops as the rollout pattern.** `PI_LOOP` /
> `PI_SWEEP_HOURS` do not exist in code. Mirror the real gates: `RESEARCHER_LOOP`
> (`researcher.py:73`), `ADVERSARY_LOOP` (`critic.py:165`), `AUDITOR_LOOP`
> (`task_completed.py:242`), `PLANNER_LOOP` (`queue_empty.py:163`); and
> `_check_phase_budget`'s date-bucket dedup for the sweep.

### Termination fit
The Librarian is **bounded and non-iterative** (one source → one document →
done); no "iterate?" decision, so it does not need the designed
`harness/termination.py` weighted-stop model. The only runaway is the sweep
re-discovering thousands of sources — bounded by the sweep cadence + per-source
`dedup_key` + `max_concurrent_handlers` + the embedder semaphore.

---

## 4. Mimir — trust tiers + certification

Mimir is a **thin governor** peer to Reviewer/Adjudicator, not a multi-step
researcher loop. "Paper-grade trust" is an ordered, **deterministic-first** ladder
computed from cheap, falsifiable signals, with a thin LLM tie-breaker ONLY for the
ambiguous `web_reputable`/`web_unknown` boundary.

### The trust ladder + lifecycle (enums in §1; semantics here)
`trust_tier` (ordered): `quarantined < user_asserted < web_unknown <
web_reputable < official_repo < preprint < peer_reviewed`. `trust_state`
(lifecycle, orthogonal): `provisional → certified`, decay → `decayed`, BLOCK →
`quarantined`. `documents.trust_*` is the **denormalized current** verdict (hot
path); `certifications` is the **immutable history** (audit + temporal state) —
exactly the `critic_verdicts`(append) + `claims.status`(denormalized) pattern.

> ⚠️ **Review fix — the "mirrors claim_status" analogy was overstated.**
> `claim_status` (`008:48-55`) is an **unordered state machine**, not an ordered
> ladder; no ordered enum exists in the repo to mirror. The append-ledger +
> denormalized-column pattern is the real precedent (`critic_verdicts`); the
> ordered ladder is new, which is why `trust_rank` pins explicit integers (§1).

### `classify_trust` — the deterministic gate (zero tokens, ~95% of ingests)
```python
def classify_trust(meta: DocMeta) -> TrustClassification:   # pure, no I/O; probes pre-resolved
    if meta.doi and _doi_resolves(meta.doi):    return TC('peer_reviewed', …, needs_llm=False)
    if meta.arxiv_id or _host_is(url,'arxiv.org'): return TC('preprint', …, needs_llm=False)
    if _host_is(url,'github.com'):
        m = meta.github_repo_meta
        if m and m.has_release and (now-m.last_push).days<365:
            return TC('official_repo', …, needs_llm=False)
        return TC('web_unknown', …, needs_llm=False)
    if _domain_reputable(url):                  return TC('web_reputable', …, needs_llm=False)
    return TC('web_unknown', …, needs_llm=True)   # the ONE place an LLM is allowed in
```
**License is a hard gate, not a tier signal:** `none`/`all-rights-reserved`/
`noindex` forces **BLOCK** regardless of tier. Reuse `fetcher._looks_blocked`
(a Cloudflare challenge page is auto-BLOCK) and seed `_domain_reputable` from the
`fetcher._TTL_RULES` host list + `*.gov`/`*.edu` suffix rules — do not
re-implement domain parsing.

> ⚠️ **Review fix — DOI/arXiv resolution is the Librarian's job, pre-resolved into
> `DocMeta`.** A `doi.org` HEAD inside `classify_trust` is a hang risk on the
> ingest path. Resolve behind `fetcher.HTTP_TIMEOUT` + `fetch_cache` on the
> Librarian side so `classify_trust` stays pure-and-fast.

### APPROVE vs CERTIFY — the evidence gate
APPROVE admits a doc at its provisional tier (**caps at `web_reputable`**). CERTIFY
is a **separate evidence-gated elevation** that unlocks the top tiers and requires
a third-party-verifiable identifier — this is the literal meaning of "paper-grade
trust", not a model's say-so.

| To reach | Required (all must hold) |
|---|---|
| `certified` @ any tier | `content_hash` · `source_url` · license permits retention |
| `certified` @ `preprint` | above + `arxiv_id` resolves |
| `certified` @ `peer_reviewed` | above + `doi` resolves at a known venue |

BLOCK = **quarantine, never DELETE** (preserves the dedupe signal + audit trail;
reversible by re-cert). The acquisition-request reply (§5) carries the reason
verbatim.

### The recipe + control flow
```python
RECIPES["mimir.certify"] = Recipe(invocation_type="mimir.certify", agent="mimir",
    total_budget=6_000, use_cold_path=True, recall_sessions=["library-provenance"],
    recall_k=4, output_schema="MimirVerdict", task_data_builder=_build_certify_task_data)

class MimirVerdict(BaseModel):                  # ONLY used when needs_llm=True
    decision: Literal["approve","block"]
    tier: Literal["user_asserted","web_unknown","web_reputable"]  # LLM may NOT set top-3
    reasons: str = Field(..., min_length=20)
```
`run_mimir_certify`: (1) `classify_trust`; if `not needs_llm` → write
deterministically, `used_llm=False`, no LLM call. (2) license hard-gate → BLOCK if
fails. (3) if `needs_llm` → ONE WORKHORSE invoke, re-assert `tier <=
web_reputable` server-side. (4) APPROVE → set `trust_tier`, `trust_state=
'provisional'`, `status='certified'`, append `certifications(decision='approve')`,
emit `mimir.ingest_approved`. (5) CERTIFY is a separate later transition. (6)
BLOCK → `trust_tier='quarantined'`, `status='blocked'`, append
`certifications(decision='block')`, emit `mimir.ingest_blocked` (carries
`reasons`).

> ⚠️ **Review fix — `mimir.certify` MUST be in `router.py:ROUTE` or it
> ValueErrors.** `router.invoke` does `tier = ROUTE.get(invocation_type); if tier
> is None: raise ValueError` (`router.py:491-493`) — there is no default. Add
> exactly `ROUTE['mimir.certify'] = Tier.WORKHORSE`. Pick ONE invocation name and
> use it in the Recipe, ROUTE, and prose (the trust spec was internally
> inconsistent — `mimir.certify` vs `mimir.adjudicate_trust`). The deterministic
> ~95% path never calls the router, so WORKHORSE's 4000/day cap (~$0.0006/call) is
> not a threat to the protected REASONING 50/day cap.

> ⚠️ **Review fix — `agent='mimir'` needs a `SYSTEM_PROMPTS` + `TOOLS_BY_AGENT`
> entry or `curator.build` KeyErrors.** `curator.py:366` does
> `SYSTEM_PROMPTS[recipe.agent]` as a hard subscript; there is no `'mimir'` key.
> Add `SYSTEM_PROMPTS['mimir']` (the persona card in §7) AND `TOOLS_BY_AGENT
> ['mimir']` (must include the corpus read server so the relevance pre-screen can
> run). See §7/§8.

### Trust DECAY / re-certification
A `decay_trust()` SQL function (no LLM) demotes `certified` docs to `decayed` on
**retraction only** (`documents.retracted`), writing `certifications(decision=
'decay', decided_by_run_id=NULL)`. `decayed` docs drop out of the default
retrieval floor until a `recertify`.

> ✅ **Decision (LOCKED) — decay fires on retraction ONLY; repo-staleness dropped.**
> The earlier "`official_repo` with `last_source_push` > 18 months → decay" rule is
> removed: an 18-month-quiet repo is, far more often than not, a *finished, stable*
> library, not an abandoned one — staleness is too noisy a demotion signal and
> would silently sink good tools. `last_source_push` is still captured on
> `documents` (cheap, useful for display + a future, better signal) — it just no
> longer drives `decay_trust()`. Retraction is an unambiguous, externally-asserted
> fact and is the only automatic demotion trigger.

> ⚠️ **Review fix — throttle decay; it is NOT urgent and the watchdog ticks every
> 5 min.** Even retraction-only, a full-table `decay_trust()` scan in
> `_watchdog_loop` would run every 300s while the intent is weekly
> (`TRUST_DECAY_HOURS=168`). Gate it with a `_last_trust_decay_tick` guard like
> `_reconcile_lessons_if_due` (`dispatch.py:446-461`).

### How trust feeds retrieval (the floor + rerank — contract owed to §6)
The default floor and the rerank weight are owned here as a WHERE/ORDER contract;
§6 owns the `corpus_search` tool that applies them.

> ⚠️ **Review fix — pin ONE canonical predicate; default a COLD corpus low.** The
> trust spec gave two divergent WHERE clauses. Canonical:
> `WHERE trust_rank(d.trust_tier) >= trust_rank($min_trust) AND d.trust_state NOT
> IN ('quarantined','decayed')`. And a `web_reputable` default starves a young
> corpus (mostly `web_unknown`/`provisional`), so the Researcher silently falls
> back to live web and the corpus never demonstrates value. Default `min_trust` to
> `web_unknown` until the corpus crosses a seed threshold; expose `min_trust` as a
> caller arg. Quarantined/decayed are never in default results.

---

## 5. The pull path — acquisition requests (agents ask, Mimir suses out)

The pull path is a **thin specialization of the already-designed Plane-2
delegation bus** (migration 010 `agent_requests`), NOT a new mechanism.
Requesting ingestion is unambiguously Plane 2: it **spends money** (fetch+embed),
**mutates the shared corpus** every agent reads, and is **abuse-prone** — exactly
what the Plane-2 guardrails (allow-list, depth≤3, dedupe/cooldown, budget,
provenance) exist to bound. Reading the corpus stays ungated Plane 1.

| Action | Plane | Why |
|---|---|---|
| `corpus_search`, `kg_evidence`, `context_graph_explain` | 1 (ungated) | read the substrate; no spend, no mutation, no target agent |
| **`agent.request{to:mimir, intent:acquire}`** | **2 (gated)** | directed at Mimir; causes fetch+embed spend; mutates the shared corpus; abuse-prone |

### One new intent on the existing bus (no new transport, no new event)

> ⚠️ **Review fix — there is NO `acquisition.requested` event and NO per-intent
> handler.** The identity dimension invented a first-class `acquisition.requested`
> event + COOLDOWNS row. The live architecture has exactly ONE delegation event
> `agent.request` routed by a SINGLE `handle_agent_request` that fans out
> internally by `to`/`intent` (`AGENT_INTERACTION_SCOPE.md:42-51`). The dispatcher
> only ever sees `event_type='agent.request'`, and `_is_cooled_down` keys on
> `(event_type, target_type)` — a COOLDOWNS row for `acquisition.requested` is
> **dead** (never matches). Acquisition is `agent.request{intent:'acquire'}`
> consumed by `handle_agent_request`; the acquire branch lives INSIDE that handler.

```
agent.request { from, to:'mimir', intent:'acquire', payload:AcquireRequest, parent_run_id, depth, budget }
agent.reply   { from:'mimir', to, request_id, status, result }
```

```python
class AcquireRequest(BaseModel):
    kind: Literal["paper","dataset","url","repo"]
    url: str|None=None; doi: str|None=None; arxiv_id: str|None=None
    dataset_name: str|None=None; query: str|None=None
    why: str = Field(..., min_length=30)   # cheap pre-LLM abuse filter (the novelty_rationale trick)
    claim_id: int|None=None                # the claim/direction this serves (read-only relevance)
    @model_validator(mode="after")
    def _one_identifier_per_kind(self): ...
```

**Reply status vocab (disjoint from the gate's promote/hold/reject/merge):**

| status | meaning | follow-on |
|---|---|---|
| `approved` | vetted; ingest dispatched | `mimir.ingest_approved` already emitted; reply carries `request_id`, tier |
| `rejected` | failed relevance / trust pre-screen | reply carries the contrastive `reason` |
| `already_have` | dedupe hit | reply carries `document_id`; requester pivots to `corpus_get_document` |
| `rate_limited` | per-agent cap or per-content cooldown hit | reply carries `retry_after` |

> ⚠️ **Review fix — `AcquireRequest.kind` must map to `documents.kind`.** `url`/
> `repo` are not document kinds; the corpus enum has `web`/`media`/`note`. Map at
> the `mimir.ingest_approved` boundary (`url→web`, `repo→code`); never let `repo`
> reach `documents.kind` unmapped.

### New allow-list rows (`AGENT_INTERACTION_SCOPE.md` matrix)

| From → To | intent | why |
|---|---|---|
| Researcher → Mimir | `acquire` | "I need this paper/dataset to ground a sub-question" |
| PI → Mimir | `acquire` | "this direction needs a primary source we don't have" |
| Novelty (`agent='evaluation'`) → Mimir | `acquire` | "prior-art candidate found on the web — ingest it for durable novelty checks" |

Add `acquire` to the §35 verb list explicitly; add Mimir as a valid `to` target in
`handle_agent_request`'s role validation (not only the `.md`) and to the
`AGENT_OPERATING_MODEL.md` per-agent "Delegates to (Plane 2)" union. Mimir is
**never a `from`** for `acquire`.

### Adjudication — async, bounded, never awaits the Librarian
```
handle_agent_request(intent='acquire'):
  0. allow-list + from≠to (existing Plane-2 guards)
  1. DETERMINISTIC GUARDS (no LLM — suppress floods before they cost a call):
       a. per-agent daily cap  (count agent_requests WHERE from=$1 AND intent='acquire'
                                 AND created_at::date=CURRENT_DATE >= MIMIR_ACQUIRE_CAP_PER_AGENT) → rate_limited
       b. per-content cooldown  (in-handler SELECT on cooldowns; key = blake2b(kind,identifier)) → rate_limited
  2. DEDUPE (no LLM) → find_corpus_duplicate(content_hash, doi, arxiv_id, title) → already_have
  3. TRUST PRE-SCREEN (no LLM) → classify_trust domain/host → if BLOCK tier → rejected
  4. mimir.certify (ONE LLM step only if needs_llm) over relevance + corpus_search(why) preview + tier
  5. PERSIST + REPLY (async, NO Librarian await):
       approve → emit mimir.ingest_approved{request_id, kind→documents.kind, identifier, trust_tier, claim_id}
                 + reply{approved} + set_cooldown(blake2b(kind,identifier), 21600)
       block   → reply{rejected, reason}
```

> ⚠️ **Review fix — the per-content cooldown is in-handler ONLY; delete the
> `dispatch.py:50` COOLDOWNS edit.** `_is_cooled_down` keys on the event's
> `target_type/target_id` (which for `agent.request` is the recipient `'mimir'`,
> never a content hash) and bails unless `target_id` is non-NULL (`dispatch.py:
> 277`), while `cooldowns.target_id` is `BIGINT NOT NULL` — a targetless acquire
> can't satisfy it. Worse, the single shared `agent.request` handler means a
> dispatch-layer cooldown would suppress `investigate`/`challenge`/`verify` too.
> Do the cooldown purely in-handler via `dispatcher.set_cooldown('mimir.acquire',
> 'acquire_key', hash, 21600)` + a direct SELECT. The Plane-2 native
> `(from,to,intent,payload-hash)` dedupe (owed by migration 010 +
> `handle_agent_request`) is the other half.

> ⚠️ **Review fix — the cooldown hash must be deterministic and signed-63-bit
> safe.** Never use builtin `hash()` (unstable across processes; overflows signed
> BIGINT). Use `int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(),
> 'big') & 0x7FFFFFFFFFFFFFFF`. A collision merely over-suppresses one unrelated
> acquire for the window — fail-safe.

> ⚠️ **Review fix — keep acquire targets out of the `claim` namespace.** Mimir/
> Librarian targets stay in `{source, document, dataset, acquisition_request}`,
> never `target_type='claim'` — reinforcing the separation of powers and keeping
> claim-keyed readers free of acquisition noise.

### Async no-hang contract
- **block / already_have / rate_limited** → `agent.reply` emitted **inside** the
  handler, synchronously. Fast path, no wait.
- **approve** → handler emits `mimir.ingest_approved` AND replies `{approved}`
  immediately; the requester does **not** wait for ingest — it polls
  `corpus_search` or reacts to `document.ingested` later.
- **Timeout**: a missing reply within `ACQUIRE_REPLY_TIMEOUT_S` (default 120s) is
  treated as `unknown` → proceed without the source, never block. Because Mimir's
  handler is bounded (seconds), a timeout means Mimir is **down**, not slow.

> ⚠️ **Review fix — the whole pull path is INERT until migration 010 +
> `handle_agent_request` ship.** No `agent_requests` table, no `agent.request`
> registration exists today. Feature-gate the acquire branch registration on
> `hasattr(dispatcher,'request_agent')` (the Planner-v3 degrade pattern), and make
> `find_corpus_duplicate` catch asyncpg `UndefinedTable` → return None (let the
> Librarian's idempotent upsert backstop) so it degrades instead of crashing
> pre-009/010. 015 (trust DDL) lands first and must NOT FK or depend on
> `agent_requests`; the **certify-on-ingest** path
> (`document.ingested`→`mimir.certify`) is the part that works standalone.

> ⚠️ **Review fix — under the cost cap, acquires are silently suppressed with no
> reply.** Do NOT add `agent.request` to `URGENT_EVENTS` (acquisition is
> cost-bearing and must respect the cap that URGENT bypasses). The consequence:
> `_is_cost_capped` marks `agent.request` `suppressed` with no reply
> (`dispatch.py:190-192`). The requester's `ACQUIRE_REPLY_TIMEOUT_S` fail-open
> MUST treat silence as "unknown/proceed", never as block — a hard requirement on
> the requester loops.

> ⚠️ **Review fix — `mimir.adjudicate_request` retier to FAST (or justify
> WORKHORSE).** WORKHORSE ∈ PREMIUM_TIERS (`router.py:206`) leads with paid
> DeepSeek; at 25/day × 3 requesters = up to 75 premium calls/day on top of the
> load that already drove the cap 800→4000. The deterministic pre-checks strip the
> abuse cases, so the LLM step is a bounded judgment like
> `phase_adjudicator.check`/`evaluation.relevance_verify` (both FAST). Route the
> acquire-adjudication LLM step to `Tier.FAST`. (Per-document `mimir.certify` stays
> WORKHORSE — trust calibration warrants it.)

---

## 6. Retrieval layer — RAG tools, rerank, context builder, agent integration

The read path ships as plain async functions in a **NEW `labfoundry_corpus` MCP
server**, consumed Plane-1-style by **direct in-process import** (the live
pattern: `research/loop.py:28` and `critic.py:199` import plain async functions;
nobody calls these over MCP stdio). We reject extending `labfoundry_knowledge` —
that server owns the Neo4j driver and only Cypher; corpus retrieval owns a
different resource (the asyncpg pgvector pool + an embedder) and must be a
**separate failure domain**.

### Tools (Plane-1, ungated)
```python
# labfoundry_corpus/tools.py
async def corpus_search(query:str, k:int=8, *, kind:str|None=None,
                        min_trust:str|None=None, kg_expand:bool=False) -> list[RetrievedChunk]:  # labfoundry_corpus
async def build_context(query:str, *, k:int=12, max_tokens:int=3000,
                        kind:str|None=None, min_trust:str|None=None) -> ContextBlock: ...
async def corpus_get_document(document_id:int) -> DocumentDetail: ...
async def list_datasets(task:str|None=None) -> list[DatasetRow]: ...

# ADDED to labfoundry_knowledge/tools.py (Cypher, existing driver):
async def kg_prior_claims(statement:str, k:int=10) -> list[dict]   # NEW: CONTAINS over Claim.statement + Paper.title
async def kg_evidence(claim_id:int, limit:int=20) -> list[dict]    # alias of get_claim_evidence_chain
async def kg_finding_influence(finding_id:int) -> dict             # alias of get_finding_influence
```
The `kg_evidence`/`kg_finding_influence` renames **keep the old names as aliases**
so `trace.py:248` doesn't break (`get_claim_critics`, also imported there, is left
untouched).

### Pipeline (inside `corpus_search`)
1. **embed** query → 768-d vec via Ollama.
2. **ANN** over chunks — pure index scan, candidate pool `N=max(4*k,32)`:
   ```sql
   SELECT c.id, c.document_id, c.ordinal, c.text, c.token_count,
          d.kind, d.title, d.source_url, d.trust_tier, d.ingested_at,
          (c.embedding <=> $1) AS distance
   FROM chunks c JOIN documents d ON d.id = c.document_id
   WHERE ($2::text IS NULL OR d.kind = $2)
     AND d.status = 'certified' AND d.queryable        -- Mimir gate + Librarian flag
     AND trust_rank(d.trust_tier) >= trust_rank($3::trust_tier)   -- §4 canonical floor
     AND d.trust_state NOT IN ('quarantined','decayed')
   ORDER BY c.embedding <=> $1 LIMIT $4;
   ```
3. **KG expansion** (opt-in `kg_expand`, off by default — costs a Neo4j hop).
4. **rerank** (Python, over the N candidates):
   `score = 0.60*sim + 0.30*trust_w + 0.10*recency`, where `trust_w =
   TRUST_WEIGHT[tier]` and `recency = exp(-age_days/180)`. If `tier` is missing
   (write race) default `trust_w=0.4` (treat as unverified, never certified).
   Weights are module constants — trust at 0.30 must not turn retrieval into a
   pure authority filter.
5. return `RetrievedChunk[]` with the component breakdown for `/trace` debugging.

> ⚠️ **Review fix — the pgvector codec MUST be registered on the pool.** Copying
> `api/main.py:38`'s jsonb-only `_init_conn` is insufficient: asyncpg has no native
> `vector` type, so binding `list[float]` as `$1` in `ORDER BY c.embedding <=> $1`
> fails at runtime — the single most load-bearing SQL in the dimension. `_get_pool`
> init MUST also `await pgvector.asyncpg.register_vector(conn)` (add `pgvector` as a
> dep), OR render the query vector as a `$1::vector` text literal `'[…]'`. State
> which.

> ⚠️ **Review fix — query embeddings bypass GPULock and cost_tracking.** The
> read-path `_embed_query` using a private httpx client never acquires the
> `GPULock` (`router.py:372-410`) and never persists an `agent_runs`/`cost_tracking`
> row — so the highest-frequency new GPU op is uncapped (VRAM thrash, same as §3's
> ingest embed) and invisible to `/debug`. Route query-embed through the SAME
> GPULock (or pin to a dedicated GPU), and either record a lightweight
> `invocation_type='corpus.embed'` 0-cost-but-counted `agent_runs` row or document
> the deliberate omission. (Same fix as §3 — embed is ONE shared constraint across
> ingest + read.)

> ⚠️ **Review fix — confirm the Ollama embed endpoint shape.** Legacy
> `POST /api/embeddings {model, prompt}` → `['embedding']` is the deprecated path;
> modern Ollama prefers `POST /api/embed {model, input}` → `['embeddings'][0]`. Pin
> the deployed version's endpoint and guard both keys — a wrong shape returns 200
> with a different JSON and silently yields a wrong-length vector.

### Context builder
```python
class ProvenanceSpan(BaseModel):
    char_start:int; char_end:int; document_id:int; chunk_id:int; ordinal:int
    title:str|None; source_url:str|None; trust_tier:str; score:float
class ContextBlock(BaseModel):
    text:str; spans:list[ProvenanceSpan]; total_tokens:int; dropped:int
```
`build_context` greedily fills **whole chunks** (never mid-chunk) until
`max_tokens`, emitting each as `[#i] {text}\n` and recording its
`(char_start,char_end)`. The `[#i]` markers let the LLM cite by index; `spans[i]`
resolves index→`chunk_id`→`document_id` so a downstream finding can MERGE a real
`(:Finding)-[:CITES]->(:Paper)` edge (§1/§2 own the merge). Deterministic — no LLM
call inside. Degenerate case: a single chunk exceeding `max_tokens` returns empty
`text` with `dropped>0`; the caller must handle it.

### Agent integration
**Researcher (unblock).** Add per-sub-question corpus recall **alongside** web
search at `research/loop.py:533-545`: corpus chunks become pre-fetched
`FetchedPage`-shaped items (`extractor='corpus'`, `url='corpus://doc/{id}'`,
`from_cache=True`) flowing into the **existing** `extract_evidence` step — uniform
provenance, **zero new LLM call**, no recipe/schema change. Web search stays
first-class. (The dashboard must special-case the `corpus://` URL scheme or it
renders a dead link.)

> ⚠️ **Review fix — drop the `TOOLS_BY_AGENT` edit; it's dead code.** `tool_names`
> is set on `BuiltPrompt` (`curator.py:394`) but the router NEVER reads it — the
> model payload is pure structured-output JSON (`response_format`/`format`), no
> `tools=`. Editing `TOOLS_BY_AGENT` neither enables nor gates `corpus_search`, and
> the stated rationale ("so the tools surface for the agent") implies LLM
> tool-calling, the exact Plane-2 path the Plane-1 thesis rejects. The real
> integration is the direct import in `research/loop.py`/`novelty/loop.py`. (Keep
> `TOOLS_BY_AGENT['mimir']`/`['librarian']` entries only because `curator.build`
> subscripts them and would KeyError without — declarative, not functional.)

**Novelty (unblock GATE).** `novelty/loop.py::recall_prior_art` calls BOTH
`corpus_search(claim.statement, kind='paper', min_trust='web_unknown')` AND
`kg_prior_claims(claim.statement, k)`, unioned/deduped. This corpus_search over
ingested papers IS the pgvector half of the precondition `REAL_LAB §3` names for
lifting the Novelty-solo-REJECT restriction.

> ⚠️ **Review fix — use REAL_LAB's exact toggle names; the lift is gated on a
> non-zero papers count.** The precondition is `pgvector` AND a Paper node
> (`REAL_LAB:186`), and it unblocks lifting the **Novelty-solo-REJECT** restriction
> — distinct from flipping `NOVELTY_LOOP=v2` (`REAL_LAB:180`) and `GATE_LOOP=v2`
> (`REAL_LAB:193`). corpus_search supplies only the pgvector half over `documents`;
> the Paper node is §1's. The solo-REJECT lift MUST be gated on a non-zero
> `/knowledge/stats` papers count, not merely "the tool exists" — until ingest has
> admitted papers, `corpus_search` returns `[]` and Novelty honestly degrades to
> lexical+KG (never a silent pass).

### `/knowledge/stats` + Flow page
New router `src/labfoundry/api/knowledge.py` (registered `main.py:35/93`):
```json
{ "corpus": {"documents_by_kind": {...}, "chunks": 3211, "datasets": 4,
             "docs_by_trust_tier": {...}},
  "graph":  {"papers": 12, "datasets": 4, "citations": 31},
  "ingest": {"certified": 52, "provisional": 3, "blocked": 1} }
```
Corpus = SQL `GROUP BY`; graph = extend `trace.py:graph_stats` (`~line 212`) with
`Paper`/`Dataset`/`CITES` counts (+ `AgentRun`/`Interaction`/`Decision` from §2),
using the existing try/except→`unavailable` pattern. Feeds the Flow page's left
**Knowledge** column (Ingestion → RAG Corpus → Knowledge Graph), each node showing
live counts; renders "planned" with zeros until ingest ships. Add `GET
/trace/graph/document/{id}` for Paper traversal.

---

## 7. Harness & identity — roles / routing / cadence / termination / lessons / Zep / ENV

### Mimir's profile persona card (the diagram card)
```
Name   : Mimir
Title  : Warden of Knowledge
Role   : Governs trust, approvals, and certification of the Library's INPUTS.
         Vets every acquisition request and certifies every ingested document
         and source; assigns a trust tier. The judge over the Librarian's work.
Guards : The Library (the corpus + its provenance) — Raw Store, Vector Index,
         Structured Store, Result Store, and the external sources feeding them.
Powers : approve · block · certify  (+ assign trust_tier, + decay/re-certify)
Focus  : provenance, evidence quality, source reputation, dedupe, paper-grade
         trust of INPUTS. Never claim promotion.
```

### System prompts (`curator.py:SYSTEM_PROMPTS`, terse style of `pi`/`critic`)
```python
"mimir": (
  "You are Mimir, Warden of Knowledge — the lab's head librarian and provenance "
  "officer. You govern what enters the Library: you certify sources and documents, "
  "assign a trust tier, and approve or block ingestion and acquisition requests. "
  "You judge provenance, source reputation, relevance, and duplication — never the "
  "truth of the lab's own claims (that is the review panel's job, not yours). A "
  "low-trust or duplicate source contaminates every finding that cites it, so you "
  "are conservative: a reasoned BLOCK is cheaper than laundering a bad source into "
  "the corpus. You decide; the Librarian fetches and ingests on your approval."
),
"librarian": (
  "You are the Librarian — the lab's data curator and archivist. You fetch, clean, "
  "chunk, embed, and ingest approved sources into the corpus and knowledge graph, "
  "and you extract entities (Paper/Dataset/Source/Author) faithfully. You never "
  "decide what is trustworthy — you ingest what Mimir has approved and surface what "
  "you find. Precision over volume; a mis-extracted citation is worse than none."
),
```

### Tool groups (`curator.py:TOOLS_BY_AGENT`)
- `'mimir'`: `['labfoundry-state','labfoundry-knowledge','labfoundry-corpus']`
  (needs the corpus read server for the §5 relevance pre-screen).
- `'librarian'`: `['labfoundry-state','labfoundry-research','labfoundry-knowledge']`.

> ⚠️ **Review fix — `labfoundry-events`/`labfoundry-memory` are phantom MCP
> groups.** Only THREE servers exist (`labfoundry-state`, `-research`,
> `-knowledge`) plus the new `-corpus`. Do not list `labfoundry-events`/
> `labfoundry-memory` as tool surfaces. And `labfoundry-knowledge` today exposes
> only Neo4j claim/finding merges — the Paper/Dataset/corpus tools must GROW (§1/
> §6) before librarian tools resolve; the group is not ready today.

> ⚠️ **Review fix — Librarian narration contradiction.** The identity spec had the
> Librarian narrate to Zep but gave it no memory tool. **Resolution:** only **Mimir**
> narrates to the `library-provenance` Zep session (it's the judge); the Librarian
> writes only `agent_runs.expectation/outcome` (the light non-PI Lessons path). No
> `labfoundry-memory` tool needed on either (Zep narration is an inline
> `memory_client` call from the loop, not an LLM tool).

### Routing (`router.py:ROUTE`)
```python
"mimir.certify":              Tier.WORKHORSE,   # per-document trust; deterministic ~95% skips the LLM entirely
"mimir.adjudicate_request":   Tier.FAST,        # acquire vetting after deterministic pre-checks (review-fix retier)
"librarian.parse":            Tier.CODE,        # strict-JSON cleanup of messy fetched text
"librarian.extract_entities": Tier.CODE,        # per-chunk-batch fan-out (bound the batch — §3 cost fix)
```
No embed/chunk in ROUTE (not chat invocations). No `DAILY_CAPS` change.

> ⚠️ **Review fix — `MIMIR_DAILY_BUDGET` counts from `agent_runs`, NOT a
> `cost_tracking.mimir_calls` column.** `cost_tracking` is per-TIER keyed by `day`
> (`reasoning_calls`/`workhorse_calls`/…); a per-AGENT column would double-count
> (Mimir's WORKHORSE calls also land in `workhorse_calls`) and the router has no
> per-agent hook at cost time. Count Mimir cloud calls in the loop's pre-invoke
> check from `agent_runs WHERE agent_name='mimir' AND model_name NOT LIKE local AND
> created_at::date=CURRENT_DATE`. No migration. Also: the **global** `cap_reached`
> flag (`router.py:948-955`) already suppresses Mimir at dispatch before its loop
> runs, so the deterministic-degrade path is reachable ONLY via `MIMIR_DAILY_BUDGET`
> (which must be `<<` 4000 to matter — 200 is fine); under a global cap, certify
> simply defers to the next recert sweep.

### Cadence / wakes (event + periodic)
| Agent | Wake | Source | Throttle |
|---|---|---|---|
| Librarian | `source.discovered` | fetcher/sweep/vetted pull dispatch | per-domain ingest cooldown ≈3600s |
| Librarian | `mimir.ingest_approved` | Mimir approved → phase B | per-document ≈600s |
| Librarian | `library.sweep_requested` | watchdog `LIBRARIAN_SWEEP_HOURS=24` | date-bucket dedup |
| Mimir | `agent.request{intent:'acquire'}` | Researcher/PI/Novelty pull (§5) | in-handler cap + per-content cooldown |
| Mimir | `document.ingested` | Librarian finished → certify | per-document ≈600s |
| Mimir | watchdog decay | `TRUST_DECAY_HOURS=168` | `_last_trust_decay_tick` guard (§4 fix) |

`dispatch.py` wiring: add COOLDOWNS rows for `source.discovered`/
`mimir.ingest_approved`/`document.ingested` (keyed on their real event types +
`target_type`). Add **NOTHING** to `URGENT_EVENTS` — ingest/certify are never
urgent, and an URGENT ingest would bypass the cost gate (the carve-out hazard).
`mimir.ingest_blocked`/`library.sweep_requested` self-throttle via their own
`dedup_key` at the INSERT. (The acquire cooldown is in-handler, NOT a COOLDOWNS
row — §5 fix.)

### Termination fit
Mimir and the Librarian are **degenerate single-decision loops** (`max_rounds=1`).
They adopt only the always-on safety gates: `HUMAN_STOP` (`company_state.paused`
halts ingest+certify), `BUDGET_CAP` (shared cap + `MIMIR_DAILY_BUDGET`; no URGENT
path exists precisely so this can't be bypassed), `TIMEOUT`/`ERROR_FLOOR` (a hung
fetch must release its handler semaphore), `MAX_ITERATIONS=1`. They do NOT use
`KILL_CONDITION`/`GOAL_MET`/`PEER_REVIEW_*`/`DIMINISHING_RETURNS`.

> ⚠️ **Review fix — `harness/termination.py` + migration 013 are designed, not
> built.** Mark the termination-model fit as a forward dependency, as `REAL_LAB`
> does for migrations 009–013, not a live integration.

### Lessons + Zep
Both reflect **lightly** via `agent_runs.expectation/outcome` (the non-PI path) —
no dedicated reflect harness. Mimir's expectation on a certify ("domain X is
reputable → tier=trusted"); outcome filled if a later finding from that doc gets
slop-flagged → a tactical lesson ("domain X produces low-relevance material —
pre-filter"). The existing `reflect.judge_applications` + watchdog `reconcile`/
`decay` machinery picks these up unchanged.

**New canonical Zep session `library-provenance`** added to `ZEP_SESSIONS` in
**both** `main.py:54` AND the mirror at `api/bench.py:66` (currently 5 sessions:
`claims-lifecycle, phase-transitions, pi-deliberations, dissent, charter`). Any
`recall_sessions=["library-provenance"]` recipe MUST have the session pre-created
or it recalls nothing silently. (§2's cognition projection needs **no** new Zep
session — Decisions reuse the existing sessions for their prose half.)

> ⚠️ **Review fix — the `theses-lifecycle` recall bug is ALREADY FIXED.** Cite the
> register-before-use rule generically ("any `recall_sessions` name must be in
> `main.py:54` + `bench.py:66` before first use"), not as a live bug at
> `adversarial/loop.py:367` (now reads `['claims-lifecycle','dissent']`). The
> dual-list edit is still correct; ideally `bench.py` imports the list from
> `main.py` instead of mirroring it.

### ENV gates
| Var | Default | Meaning |
|---|---|---|
| `MIMIR_LOOP` | `off` | `v1` enables certify + acquire vetting (off until the corpus/pgvector exists) |
| `LIBRARIAN_LOOP` | `off` | `v1` enables the ingest loop |
| `LIBRARIAN_SWEEP_HOURS` | `24` | standing-source re-scan cadence |
| `TRUST_DECAY_HOURS` | `168` | trust-decay re-cert cadence (weekly) |
| `MIMIR_DAILY_BUDGET` | `200` | Mimir's own daily cloud-call ceiling (counted from `agent_runs`) |
| `MIMIR_ACQUIRE_CAP_PER_AGENT` | `25` | per-agent daily acquire ceiling (anti-spam, pre-LLM) |
| `ACQUIRE_REPLY_TIMEOUT_S` | `120` | requester treats a missing reply as "unknown" and proceeds |
| `EMBED_MODEL` / `CORPUS_EMBED_MODEL` | `nomic-embed-text` | pinned 768-d local embed model |

All default off/conservative, matching the `*_LOOP` default-off-until-validated
pattern.

---

## 8. Consolidated NEW artifacts

### Migrations
| # | File | Owns | Note |
|---|---|---|---|
| 015 | `015_knowledge_corpus.sql` | `document_kind`/`document_status`/`trust_tier`/`trust_state` enums; `documents`/`chunks`/`datasets`/`certifications`; `trust_rank()`; corpus indexes **incl. the inline HNSW vector index** | re-run-safe (guarded DO + IF NOT EXISTS); requires the pgvector image swap |
| 010 (designed) | `010_agent_requests.sql` | `agent_requests` table; index `(from_role,intent,created_at)`; accepts `intent='acquire'` + JSONB payload | PREREQUISITE for the pull path; lands separately |
| later | `0NN_result_store.sql` | `result_artifacts` (deferred Result Store) | when the first artifact producer exists |

### New files
`src/labfoundry/research/librarian/{__init__,loop,chunker,embedder,schemas}.py` ·
`src/labfoundry/handlers/librarian.py` · `src/labfoundry/mimir/{loop,trust}.py` ·
`src/labfoundry/handlers/mimir.py` · `src/labfoundry/novelty/loop.py` (joint with gate work) ·
`src/labfoundry/mcp_servers/labfoundry_corpus/{tools,server}.py` ·
`src/labfoundry/api/knowledge.py` · `scripts/project_context_graph.py`.

### Roles
`mimir` (governor) + `librarian` (doer) added to `SYSTEM_PROMPTS` +
`TOOLS_BY_AGENT` (`curator.py:73/310`). `AGENT_OPERATING_MODEL.md` promotes the
"Librarian (planned)" row to a built Plane-1 writer and adds Mimir as governor +
the only `acquire` delegation target.

### Recipes
`mimir.certify` (WORKHORSE) · `mimir.adjudicate_request` (FAST) · `librarian.parse`
(CODE) · `librarian.extract_entities` (CODE). Plus deterministic non-recipes:
`chunker.plan`, `embedder.embed_many`.

### ROUTE
`mimir.certify→WORKHORSE`, `mimir.adjudicate_request→FAST`,
`librarian.parse→CODE`, `librarian.extract_entities→CODE`.

### Events (with COOLDOWNS / URGENT_EVENTS)
| Event | target_type | COOLDOWNS | URGENT? |
|---|---|---|---|
| `source.discovered` | `source` | `librarian.ingest`, ≈3600s per-domain | no |
| `document.parsed` | `document` | self-throttled via dedup_key | no |
| `mimir.ingest_approved` | `document` | `librarian.ingest`, ≈600s | no |
| `mimir.ingest_blocked` | `document` | self-throttled (per requester+target) | no |
| `document.ingested` | `document` | `mimir.certify`, ≈600s | no |
| `library.sweep_requested` | `ingest_source` | date-bucket dedup_key | no |
| `agent.request{intent:acquire}` | `mimir` | in-handler cap+cooldown (NOT a COOLDOWNS row) | **no** (cost-bearing) |

### MCP tools
**`labfoundry_corpus`** (NEW): `corpus_search`, `build_context`,
`corpus_get_document`, `list_datasets`. **`labfoundry_knowledge`** (extend, Cypher):
`kg_prior_claims` (new), `kg_evidence`/`kg_finding_influence` (aliases),
`context_graph_explain`/`context_graph_state_as_of`/`context_graph_interactions`
(new §2 readers). Internal (never MCP): `merge_paper`/`merge_dataset`/
`link_finding_cites_paper`/`link_claim_uses_dataset`/`merge_agent_run`/
`merge_interaction`/`merge_decision`.

### State methods (`state/client.py`)
`emit_corpus_event` (real UNIQUE-keyed INSERT helper) · `upsert_document` (ON
CONFLICT `(source_kind,canonical_key)`) · `stage_chunk_plan`/`get_chunk_plan` ·
`upsert_chunks`/`chunk_has_vector` · `set_document_queryable`/`get_document` ·
`register_dataset` · `append_certification` · `find_corpus_duplicate` (catches
`UndefinedTable`→None) · `count_acquires_today`. Plus Pydantic `Document`/`Chunk`/
`Dataset` models.

### ENV
`MIMIR_LOOP`, `LIBRARIAN_LOOP`, `LIBRARIAN_SWEEP_HOURS`, `TRUST_DECAY_HOURS`,
`MIMIR_DAILY_BUDGET`, `MIMIR_ACQUIRE_CAP_PER_AGENT`, `ACQUIRE_REPLY_TIMEOUT_S`,
`EMBED_MODEL`/`CORPUS_EMBED_MODEL`.

### Infra
Flip `postgres:16-alpine` → `pgvector/pgvector:pg16` in `docker-compose.yml:3` +
`docker-compose.demo.yml:13`. `ollama pull nomic-embed-text`. Add the embed
preflight probe to `main.py::_preflight`.

---

## 9. Build phasing — ordered by "smallest change that makes the Library real"

**Phase 0 — make the substrate runnable (infra + DDL).**
1. **pgvector image swap** in both compose files + `ollama pull nomic-embed-text`
   + `_preflight` embed probe. Nothing works until the extension and model exist.
2. **`015_knowledge_corpus.sql`** (re-run-safe): enums, `documents`/`chunks`/
   `datasets`/`certifications`, `trust_rank()`, indexes **incl. the inline HNSW
   vector index**. The Library is dead without it.

**Phase 1 — the read path lights up (proves the substrate).**
3. **`labfoundry_corpus/tools.py`** — `_get_pool` (with the pgvector codec
   registered), `Embedder` (GPULock-aware), `corpus_search`, `build_context`.
   Unit-testable against a hand-seeded `documents`/`chunks`.
4. **`/knowledge/stats`** — lights the Flow Knowledge column with real counts.

**Phase 2 — the doer (half a loop, no judge yet).**
5. **`embedder.py` + `chunker.py`** — deterministic, unit-testable without the
   harness; assert dim==768 at import.
6. **`librarian/loop.py` phase A + `librarian.parse`** behind `LIBRARIAN_LOOP=v1`
   — a `documents` row + chunk-plan + `document.parsed` flowing. STOP: phase B is
   dead until Mimir exists.
7. **`search_arxiv`** — first real source (richest metadata, friendliest API,
   already TTL'd).

**Phase 3 — the judge (close the trust gate).**
8. **`mimir/trust.py` `classify_trust`** + `015`'s trust columns already present
   → deterministic certify with zero tokens. **`mimir/loop.py` `mimir.certify`**
   (+ ROUTE + `SYSTEM_PROMPTS['mimir']` + `TOOLS_BY_AGENT['mimir']`) +
   `handle_document_ingested`.
9. **Librarian phase B + KG merges** — now that `mimir.ingest_approved` exists.
   (The HNSW vector index already shipped inline in 015 — no separate build step.)

**Phase 4 — the cognition layer (the locked first-class Context Graph).**
10. **§2 projection**: `ensure_cognition_constraints`, the bounded queue worker
    sink in `_process_event`, inline `merge_decision` in `critic.py`/
    `phase_transition.py`, `context_graph_explain`/`state_as_of`/`interactions`
    readers, and `scripts/project_context_graph.py` backfill.

**Phase 5 — the pull path (last; depends on Plane 2).**
11. **`kg_prior_claims` + Novelty `recall_prior_art`** — the gating dependency for
    lifting Novelty-solo-REJECT (gated on a non-zero papers count).
12. **migration 010 + `handle_agent_request` acquire branch** + the allow-list
    rows + `mimir.adjudicate_request` (FAST). Feature-gated; inert until 010 ships.
13. **watchdog sweeps** (`_check_ingest_sweep`, decay) + standing `ingest_sources`.

### What to build first
**The pgvector image swap, `ollama pull nomic-embed-text`, and migration 015.**
Without the extension, the model, and the tables there is no Library to govern,
retrieve from, or ingest into — every other artifact in this doc imports from a
schema that does not exist. The single smallest change that makes the Library
*real* is `015` landing on a `pgvector/pgvector:pg16` image: it creates the
corpus, the trust columns, and the ledger in one transaction, and it is the hard
ordering dependency that gates `MIMIR_LOOP`/`LIBRARIAN_LOOP` ever going on.

---

## 10. Decisions

### Resolved (locked)
| Decision | Resolution |
|---|---|
| Backends | Five stores → three existing backends (Postgres+pgvector, Neo4j, filesystem). No new service. |
| Separation of powers | Mimir governs INPUTS (`documents`/trust); the panel governs OUTPUTS (`claims`). Zero shared write target. Touch-point = Novelty reads `trust_tier` (read-only, one-way). |
| Mimir's form | Governor (judge) over a Librarian (doer) + a Plane-2 pull path. The Librarian never self-certifies — enforced by the phase-A/phase-B event boundary. |
| Context Graph | FIRST-CLASS: the lab's own cognition (AgentRun/Interaction/Decision) is a deterministic Neo4j projection, NOT Graphiti. Its own §2. |
| Migration number | `015` (009–013 reserved-unbuilt; 014 last applied). |
| Embedding | `nomic-embed-text`, `vector(768)`, pinned per-corpus; local + free; provider swappable behind a Protocol. |
| Vector index | **HNSW pinned, created INLINE in 015** (`vector_cosine_ops`, m=16 / ef_construction=64; query knob `hnsw.ef_search`). HNSW builds on an empty table → no deferred reindex step; ivfflat rejected (cold-start mis-recall). |
| Trust decay | **Retraction-only** (`documents.retracted`). Repo-staleness (18mo) dropped — too noisy (a quiet repo is usually finished, not abandoned). `last_source_push` still captured, no longer a trigger. |
| Raw Store + media | Papers keep **full blob** in `raw_uri` (50KB `fetch_cache` cap never gates a paper). Images/media = first-class `kind='media'` corpus citizens (blob + trust tier + KG node), retrieved via **text surface** (caption/alt/OCR); pixel/visual embedding deferred to a future `media_chunks` index. |
| Corpus server name | `labfoundry_corpus` (group `labfoundry-corpus`) — first labfoundry-branded MCP server. |
| Trust model | Ordered `trust_tier` ladder + orthogonal `trust_state`; deterministic-first `classify_trust` (~95% zero-token); APPROVE caps at `web_reputable`, CERTIFY unlocks top tiers via a verifiable identifier; BLOCK = quarantine-not-delete; append-only `certifications` ledger. |
| Pull path | One new `acquire` intent on the existing `agent.request` bus; async non-blocking adjudication; in-handler cap + cooldown (NOT a dispatch COOLDOWNS row). |
| Retrieval | New `labfoundry_corpus` server, in-process import (Plane-1); embed→ANN→filter→rerank(0.6 sim/0.3 trust/0.1 recency)→whole-chunk context builder with per-span provenance. |
| Result Store | Deferred to a later migration; only `experiment_runs.result` exists today. |

### Still-open
| Question | Owner / lean |
|---|---|
| `min_trust` cold-corpus floor: corpus-size-aware vs static `web_unknown` default. | retrieval+trust — lean: `web_unknown` until a seed threshold. |
| Where the `_domain_reputable` allowlist lives (env vs `reputable_domains` table vs hardcoded), and who curates it. | trust+Director. |
| Does a finding's `trust_tier` prior cap a claim below `replicated`? | gate — must be a Reviewer-consulted read-only prior, never a Mimir write. |
| `authors TEXT[]` on `documents` AND `:Author` nodes — keep the denormalization for cheap display? | datamodel — lean: keep. |
| Re-fetch of a CHANGED document: re-emit `document.parsed` (re-certify) vs trust-tier-only refresh? | trust+librarian — lean: content change → re-certify. |
| Pipeline-edge granularity in §2: sample/collapse high-frequency `claim.confidence_changed` interactions, or chart delegation chords only? | context-graph + viz owner. |

---

## 11. Corrections applied from adversarial review (binding)

1. **pgvector image swap is HARD, on both stacks** — `postgres:16-alpine` has no
   `vector`; both compose files mount migrations at `docker-entrypoint-initdb.d`
   and the demo stack has no `make migrate` fallback, so the swap to
   `pgvector/pgvector:pg16` in `docker-compose.yml:3` + `docker-compose.demo.yml:13`
   is mandatory before any fresh-volume boot (§1).
2. **No embed model pulled** — non-fatal `_preflight` probe + `ollama pull
   nomic-embed-text`; corpus degrades to keyword-only, not booting broken (§1).
3. **015 made re-run-safe** — guarded `DO $$…duplicate_object…$$` for every
   `CREATE TYPE` + `IF NOT EXISTS` everywhere (the `Makefile` re-applies all files;
   no tracking table) (§1).
4. **UNIQUE(doi)/UNIQUE(arxiv_id) → partial unique indexes + empty-string CHECKs**
   — bare table UNIQUEs over-constrain a mixed corpus and collapse `''` rows (§1).
5. **`result_artifacts` pulled out of 015** into a later Phase-2 migration; 015 is
   strictly Raw/Vector/Structured (§1).
6. **HNSW pinned + created INLINE in 015** — ivfflat cold-starts badly (needs
   training rows, mis-recalls on an empty table); HNSW builds incrementally and is
   valid on an empty table, so the index ships in 015 directly and the deferred
   `make reindex-corpus` step is eliminated (Director-locked) (§1).
7. **`documents.id` is a BIGSERIAL surrogate**, deterministic dedupe in a separate
   `UNIQUE(source_kind, canonical_key)` — a hashed id risks BIGINT overflow/
   collision (§1).
8. **`ensure_constraints()` split** into corpus + cognition halves to avoid a
   merge-conflict hotspot (§1/§2).
9. **The REQUESTED chord is role→role, not run→run** — `consumed_run_id` is a dead
   column and the `agent.request` payload is role-keyed (§2).
10. **The §2 projection hook goes in `_process_event`'s `finally`-block, not
    `_mark_consumed`** — the latter has no `event` in scope and is bypassed by
    suppressed/failed events (the chord-completeness premise was false) (§2).
11. **The §2 projection is a bounded queue worker OFF the handler semaphore** — an
    in-semaphore Neo4j await or `create_task` fire-and-forget both break the
    swarm's concurrency bound (§2).
12. **Replay projection suppressed** (`session.mode=='replay'` hard guard) (§2).
13. **`:AgentRun` projected id-only** (no per-event Postgres SELECT); descriptive
    fields backfilled by the replay projector (§2).
14. **`PRODUCED_BY` points at the researcher run, not the evaluator's** (§2).
15. **`created_at` standardized as a Neo4j temporal** everywhere (existing call
    sites pass `datetime`, not `str`) so `state_as_of` compares like-for-like (§2).
16. **`SUPERSEDES`/temporal_state is query-derived + replay-reconstructed**, not a
    stored mutable flag depending on read-before-write against a best-effort store
    (§2).
17. **`state.emit_event` does not exist** — add a real UNIQUE-keyed
    `emit_corpus_event` helper for `document.parsed`/`document.ingested` (§3).
18. **`document.kind` gets a separate `DocumentKind` Literal** — extending the
    findings `source` Literal would pollute the OUTPUTS path (locked-decision-2
    violation) (§3).
19. **Drop the task-claim framing** — `source.discovered` carries the source
    inline; `handle_source_discovered` runs phase A directly (claim_task returns an
    arbitrary task, not the event's source) (§3).
20. **Embed respects VRAM via GPULock + batching**, not a private semaphore that
    bypasses the in-flight cap — applies to BOTH ingest embed (§3) and read-path
    query embed (§6).
21. **`librarian.extract_entities` batch is bounded** — it (not embed) is the real
    CODE-cap cost driver, shared with the researcher (§3).
22. **Cite only BUILT loops** (`RESEARCHER_LOOP`/`ADVERSARY_LOOP`/`AUDITOR_LOOP`/
    `PLANNER_LOOP`) as the rollout pattern; `PI_LOOP`/`PI_SWEEP_HOURS` don't exist
    (§3).
23. **`mimir.certify` added to `router.py:ROUTE`** (`WORKHORSE`) or it ValueErrors;
    one canonical invocation name (§4).
24. **`SYSTEM_PROMPTS['mimir']` + `TOOLS_BY_AGENT['mimir']` added** or
    `curator.build` KeyErrors on the hard subscript (§4/§7).
25. **`decay_trust()` throttled** with a `_last_trust_decay_tick` guard (the
    watchdog ticks every 5 min, the intent is weekly) (§4).
26. **trust_rank pins explicit integers (CASE), not `array_position`** — a
    positional IMMUTABLE corrupts under `ALTER TYPE … ADD VALUE BEFORE`; the
    "mirrors claim_status" analogy was overstated (claim_status is unordered) (§4).
27. **One canonical retrieval floor predicate; default a cold corpus to
    `web_unknown`** — two divergent WHEREs existed; a `web_reputable` default
    starves a young corpus (§4/§6).
28. **No `acquisition.requested` event / no per-intent handler / delete the
    `dispatch.py:50` COOLDOWNS edit** — acquisition is `agent.request{intent:
    'acquire'}` fanned out inside the single `handle_agent_request`; the
    per-content cooldown is in-handler only (a content-hash COOLDOWNS row is dead,
    and would also suppress investigate/challenge/verify) (§5).
29. **The cooldown hash is deterministic + signed-63-bit safe** (`blake2b
    digest_size=8 & 0x7FFF…`), never builtin `hash()` (§5).
30. **`AcquireRequest.kind` mapped to `documents.kind`** at the
    `mimir.ingest_approved` boundary (no unmapped `url`/`repo` reaching the enum)
    (§5).
31. **Acquire targets stay out of the `claim` namespace** (`{source, document,
    dataset, acquisition_request}` only) (§5).
32. **The pull path is feature-gated + inert until migration 010 +
    `handle_agent_request`**; `find_corpus_duplicate` catches `UndefinedTable`;
    015 must not FK `agent_requests`; certify-on-ingest works standalone (§5).
33. **`agent.request` NOT in URGENT_EVENTS**; the requester's
    `ACQUIRE_REPLY_TIMEOUT_S` fail-open MUST treat cost-cap silence as
    "unknown/proceed", never block (§5).
34. **`mimir.adjudicate_request` retiered WORKHORSE→FAST** — the post-pre-check
    judgment is bounded; WORKHORSE is the paid premium tier and would stack up to
    75 paid calls/day (§5).
35. **pgvector codec registered on the corpus pool** — copying the jsonb-only
    `_init_conn` makes the core ANN bind fail at runtime (§6).
36. **Query embeddings routed through GPULock + made observable** (a counted
    `agent_runs` row or a documented omission) — the highest-frequency new GPU op
    was uncapped and invisible (§6).
37. **Ollama embed endpoint shape pinned** (`/api/embed {input}` →
    `['embeddings'][0]` vs legacy) with both keys guarded (§6).
38. **`TOOLS_BY_AGENT` for `corpus_search` is dead code** — the router never reads
    `tool_names`; the real integration is the direct in-process import (the Mimir/
    Librarian entries are kept only because `curator.build` subscripts them) (§6).
39. **Use REAL_LAB's exact toggle names** (`NOVELTY_LOOP=v2` vs `GATE_LOOP=v2`);
    the solo-REJECT lift is gated on the pgvector + Paper-node precondition AND a
    non-zero `/knowledge/stats` papers count, not "the tool exists" (§6).
40. **`MIMIR_DAILY_BUDGET` counts from `agent_runs`, not a `cost_tracking` column**
    (per-tier table, would double-count); the global `cap_reached` flag already
    suppresses Mimir upstream, so the deterministic-degrade is reachable only via
    `MIMIR_DAILY_BUDGET << 4000` (§7).
41. **Only Mimir narrates to Zep** (`library-provenance`); the Librarian writes
    `agent_runs.expectation/outcome` — resolves the "narrate with no memory tool"
    contradiction. `labfoundry-events`/`labfoundry-memory` are phantom MCP groups;
    `labfoundry-knowledge` must GROW corpus tools before librarian tools resolve
    (§7).
42. **`library-provenance` added to BOTH `main.py:54` and `bench.py:66`**; the
    `theses-lifecycle` recall bug is already fixed — cite the register-before-use
    rule generically (§7).
43. **Termination model is a forward dependency** on the designed
    `harness/termination.py` + migration 013, not a live integration (§7).
