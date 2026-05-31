-- =========================================================================
-- 011 — Agent-run expectations (Real-Lab operating model, Phase 0)
--
-- Every agent loop's PLAN step records what it expected; its REFLECT step
-- records what actually happened. The expectation→outcome diff is the raw
-- material the lessons loop learns from. PI per-hypothesis goals live in a
-- richer claim_goals table (later migration); non-PI agents use these light
-- columns on their run row.
--
-- Independent of the PI/agent-request migrations (009/010) — touches only
-- agent_runs (from 001), so it applies cleanly even before those exist.
-- =========================================================================

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS expectation    TEXT,
    ADD COLUMN IF NOT EXISTS outcome        TEXT,
    ADD COLUMN IF NOT EXISTS expectation_met BOOLEAN;

-- Find runs whose expectation hasn't been reconciled yet (the reflect step
-- and the lessons judge both scan this).
CREATE INDEX IF NOT EXISTS idx_agent_runs_open_expectation
    ON agent_runs(started_at DESC)
    WHERE expectation IS NOT NULL AND outcome IS NULL;
