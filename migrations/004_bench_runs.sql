-- =========================================================================
-- BENCH RUNS — persisted model-comparison results (read-only playground).
-- Lets past comparisons be retrieved/inspected later; not part of the live
-- control loop.
-- =========================================================================

CREATE TABLE IF NOT EXISTS bench_runs (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    invocation_type TEXT NOT NULL,
    tier TEXT,
    thesis_id BIGINT,
    context_note TEXT,
    prompt_tokens INT,
    prompt_preview TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    results JSONB NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_bench_runs_recent ON bench_runs(created_at DESC);
