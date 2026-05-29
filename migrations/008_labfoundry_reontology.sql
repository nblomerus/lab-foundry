-- LabFoundry re-ontology: Boardroom → research lab domain
-- 2026-05-28
--
-- Renames:
-- - theses → claims (table + concept)
-- - claim field → statement (avoid redundancy claims.claim)
-- - killed_* fields → invalidated_*
-- - adversary_verdicts → critic_verdicts
-- - phase enum: exploration/convergence/commitment/execution → frame/hypothesize/experiment/validate/write/submit
-- - thesis_status enum → claim_status, with expanded machine: proposed/tested/weakly_supported/replicated/invalidated/merged
-- - All foreign keys updated
-- - Materialized view slop_rate_by_thesis → slop_rate_by_claim

-- (a) Rename table and columns
ALTER TABLE theses RENAME TO claims;
ALTER TABLE claims RENAME COLUMN claim TO statement;
ALTER TABLE claims RENAME COLUMN killed_at TO invalidated_at;
ALTER TABLE claims RENAME COLUMN killed_by_verdict_id TO invalidated_by_verdict_id;
ALTER TABLE claims RENAME COLUMN kill_reason TO invalidation_reason;

-- (b) Rename foreign keys in dependent tables
ALTER TABLE tasks RENAME COLUMN thesis_id TO claim_id;
ALTER TABLE findings RENAME COLUMN thesis_id TO claim_id;
ALTER TABLE objectives RENAME COLUMN thesis_id TO claim_id;
ALTER TABLE phase_transitions RENAME COLUMN cited_thesis_ids TO cited_claim_ids;

-- (c) Rename verdict table
ALTER TABLE adversary_verdicts RENAME TO critic_verdicts;
-- NOTE: claims.invalidated_by_verdict_id was already renamed in (a) above; no
-- further rename is needed here. A prior dead self-rename at this spot
-- (RENAME COLUMN x TO x) errored "already exists" and aborted a fresh
-- docker-entrypoint-initdb.d run (ON_ERROR_STOP=1), even though `make migrate`
-- (psql without ON_ERROR_STOP) tolerated it. Removed.

-- (d) Recreate phase enum (Postgres doesn't support direct rename of enum values)
ALTER TYPE phase RENAME TO _phase_old;
CREATE TYPE phase AS ENUM ('frame', 'hypothesize', 'experiment', 'validate', 'write', 'submit');
ALTER TABLE company_state ALTER COLUMN current_phase TYPE phase USING (
  CASE current_phase::text
    WHEN 'exploration' THEN 'frame'::phase
    WHEN 'convergence' THEN 'hypothesize'::phase
    WHEN 'commitment'  THEN 'experiment'::phase
    WHEN 'execution'   THEN 'validate'::phase
    ELSE 'frame'::phase
  END
);
DROP TYPE _phase_old;

-- (e) Recreate thesis_status enum → claim_status with expanded machine
ALTER TYPE thesis_status RENAME TO _thesis_status_old;
CREATE TYPE claim_status AS ENUM (
  'proposed',         -- created, no evidence
  'tested',           -- has evidence from completed tasks
  'weakly_supported', -- survived first critic pass (watch verdict)
  'replicated',       -- reproduction re-run succeeded (sandbox phase)
  'invalidated',      -- critic kill verdict
  'merged'            -- absorbed into another claim
);
ALTER TABLE claims ALTER COLUMN status TYPE claim_status USING (
  CASE status::text
    WHEN 'active'    THEN 'proposed'::claim_status
    WHEN 'killed'    THEN 'invalidated'::claim_status
    WHEN 'merged'    THEN 'merged'::claim_status
    WHEN 'promoted'  THEN 'weakly_supported'::claim_status
    ELSE 'proposed'::claim_status
  END
);
DROP TYPE _thesis_status_old;

-- (f) Rename materialized view
DROP MATERIALIZED VIEW IF EXISTS slop_rate_by_thesis;
CREATE MATERIALIZED VIEW slop_rate_by_claim AS
  SELECT
    f.claim_id,
    COUNT(CASE WHEN f.audit_verdict = 'slop' THEN 1 END)::float / NULLIF(COUNT(*), 0) AS slop_rate,
    COUNT(*) AS window_size,
    MAX(f.created_at) AS latest
  FROM findings f
  WHERE f.created_at > NOW() - INTERVAL '24 hours'
  GROUP BY f.claim_id
  HAVING COUNT(*) >= 5;

-- (g) Update phase_transitions constraints/references if needed
-- (phase_transitions.cited_claim_ids is BIGINT[], which already works fine)

-- (h) Update comment/constraints on company_state to reflect research domain
-- (these are semantic-only; no schema changes)
COMMENT ON COLUMN company_state.problem_statement IS 'Research mandate: the problem space the lab is investigating';
COMMENT ON COLUMN company_state.thesis IS 'Primary claim under investigation';
COMMENT ON COLUMN company_state.niche IS 'Research question or focus area';
COMMENT ON COLUMN company_state.audience IS 'Target publication venue';
COMMENT ON COLUMN company_state.charter IS 'Research plan and methodology';
