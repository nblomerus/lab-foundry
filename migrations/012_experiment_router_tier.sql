-- 012_experiment_router_tier.sql — make the new EXPERIMENT router tier
-- representable in the database.
--
-- The Quartermaster's coding loop (designs/debugs sandboxed ML experiments,
-- led by DeepSeek) routes through a new Tier.EXPERIMENT in harness/router.py.
-- Two DB surfaces were never widened for it, so every experiments.design /
-- .debug invoke crashed:
--   1. agent_runs.model_tier is the `model_tier` enum {reasoning,workhorse,
--      fast,code} — the router records each call's tier, so an "experiment"
--      tier raised InvalidTextRepresentationError.
--   2. cost_tracking tracks per-tier daily call counts in <tier>_calls columns
--      (router: _calls_today / _record_cost, col = f"{tier.value}_calls") —
--      the missing experiment_calls raised UndefinedColumnError.
--
-- Both are additive + idempotent — safe to apply on the live DB.
-- (ALTER TYPE ... ADD VALUE must run outside a transaction block; migrations
--  are applied statement-at-a-time via `psql < file`, so this is fine.)

ALTER TYPE public.model_tier ADD VALUE IF NOT EXISTS 'experiment';

ALTER TABLE public.cost_tracking
    ADD COLUMN IF NOT EXISTS experiment_calls integer DEFAULT 0 NOT NULL;
