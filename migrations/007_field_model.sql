-- 007_field_model.sql — Ariadne's Domain-Expert FIELD MODEL.
--
-- The AI/ML research landscape as MAINTAINED trend states over the context-graph
-- concepts (METHOD/TASK/DATASET). Two orthogonal signals per concept:
--   * total_papers   — all-time prominence (the saturation axis)
--   * velocity       — SHARE-normalized growth across the last two paper cohorts
--                      (recent_share / prior_share - 1), so corpus-growth doesn't
--                      make everything look "rising"
-- → trend_state ∈ emerging | hot | stable | saturated | declining.
--
-- Re-derivable (rebuilt by ops.field_model_build from Neo4j), so NOT precious-tier.
-- Idempotent + additive — safe to apply via psql against the live DB.

CREATE TABLE IF NOT EXISTS public.field_model (
    id            bigserial PRIMARY KEY,
    concept_kind  text NOT NULL,                    -- METHOD | TASK | DATASET
    concept_key   text NOT NULL,
    concept_name  text NOT NULL,
    total_papers  integer NOT NULL DEFAULT 0,       -- all-time prominence
    recent_papers integer NOT NULL DEFAULT 0,       -- count in the latest cohort
    prior_papers  integer NOT NULL DEFAULT 0,       -- count in the preceding cohort
    velocity      numeric(7,3) NOT NULL DEFAULT 0,  -- share-normalized growth
    trend_state   text NOT NULL,                    -- emerging|hot|stable|saturated|declining
    recent_window text,                             -- e.g. '2606'
    prior_window  text,                             -- e.g. '2605'
    computed_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (concept_kind, concept_key)
);

CREATE INDEX IF NOT EXISTS field_model_trend_idx ON public.field_model (trend_state, total_papers DESC);
CREATE INDEX IF NOT EXISTS field_model_kind_idx  ON public.field_model (concept_kind, velocity DESC);
