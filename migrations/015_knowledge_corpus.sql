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
