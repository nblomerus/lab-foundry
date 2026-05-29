-- =========================================================================
-- DEEPSEEK BALANCE LOG — periodic snapshots of the authoritative remaining
-- balance from DeepSeek's /user/balance. DeepSeek exposes no usage-history
-- API, so spend is derived from the drop in balance between snapshots
-- (source-accurate, vs. estimating from token counts).
-- =========================================================================

CREATE TABLE IF NOT EXISTS deepseek_balance_log (
    id BIGSERIAL PRIMARY KEY,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_balance NUMERIC(10,4) NOT NULL,
    topped_up     NUMERIC(10,4),
    granted       NUMERIC(10,4)
);

CREATE INDEX IF NOT EXISTS idx_ds_balance_recent ON deepseek_balance_log(recorded_at DESC);
