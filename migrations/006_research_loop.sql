-- 006_research_loop.sql
-- Agentic researcher loop: per-task inquiry plan, per-page evidence,
-- experiment runs, and a fetched-page cache (the self-hosted retrieval moat).
--
-- Each table is read-mostly after its task finishes; together they form the
-- audit trail the Debug research-tree view dissects step-by-step.

BEGIN;

-- =========================================================================
-- RESEARCH INQUIRIES  (one row per planning pass; multiple per task on iteration)
-- =========================================================================

CREATE TABLE research_inquiries (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL REFERENCES tasks(id),
    iteration INT NOT NULL DEFAULT 1,
    question TEXT NOT NULL,
    -- [{q: str, sources: [str], why: str}]
    sub_questions JSONB NOT NULL,
    -- [{kind: str, params: object, why: str}]
    proposed_experiments JSONB NOT NULL DEFAULT '[]',
    plan_run_id BIGINT REFERENCES agent_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_inquiries_task ON research_inquiries(task_id, iteration);

-- =========================================================================
-- EVIDENCE  (per-page extractions; the raw research material the agent reads)
-- =========================================================================

CREATE TABLE evidence (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL REFERENCES tasks(id),
    inquiry_id BIGINT REFERENCES research_inquiries(id),
    sub_question_idx INT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    -- Verbatim quote from the page. The model's claim is allowed to interpret,
    -- but the quote keeps it honest about what the page actually says.
    quote TEXT NOT NULL,
    claim TEXT NOT NULL,
    stance TEXT NOT NULL CHECK (stance IN ('supports', 'refutes', 'neutral')),
    confidence NUMERIC(3, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    extract_run_id BIGINT REFERENCES agent_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_evidence_task ON evidence(task_id, created_at);
CREATE INDEX idx_evidence_inquiry ON evidence(inquiry_id);

-- =========================================================================
-- EXPERIMENT RUNS  (the "do something" layer: pricing, demand signal, repo growth)
-- =========================================================================

CREATE TABLE experiment_runs (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL REFERENCES tasks(id),
    inquiry_id BIGINT REFERENCES research_inquiries(id),
    kind TEXT NOT NULL,
    params JSONB NOT NULL,
    result JSONB,
    error TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    interpretation TEXT,
    interpret_run_id BIGINT REFERENCES agent_runs(id),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_experiments_task ON experiment_runs(task_id);

-- =========================================================================
-- FETCH CACHE  (URL -> extracted markdown; the cheap-first rung of the ladder)
-- =========================================================================

CREATE TABLE fetch_cache (
    url TEXT PRIMARY KEY,
    -- Cleaned markdown/text after extraction. Truncated to ~50 KB.
    content TEXT NOT NULL,
    extractor TEXT NOT NULL,   -- 'trafilatura' | 'bs4' | 'plain'
    status_code INT NOT NULL,
    bytes_fetched INT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_fetch_cache_expires ON fetch_cache(expires_at);

COMMIT;
