-- 020: data-realism signal on experiments + findings.
--
-- The lab was running 93.8% synthetic experiments (make_classification / np.random / torch.randn),
-- so 8 of 9 findings were "inconclusive: synthetic only." This makes realism a first-class,
-- queryable signal so the loop can discount synthetic-only findings, escalate them to a real-data
-- confirmation (the confirm_real_data transition), and surface "% real" in the UI + lab_doctor.
--
--   experiment_runs.data_realism      — 'real' | 'builtin' | 'synthetic' (static-classified at interpret)
--   experiment_runs.realism_mismatch  — the plan named a real dataset but the run did not use one
--   research_findings.data_realism    — worst-case realism across the finding's grounding experiments
--
-- Additive + idempotent (mirrors migration 011). No backfill — pre-020 rows stay NULL and are
-- treated as 'synthetic' (conservative) by readers until re-run.

ALTER TABLE public.experiment_runs
    ADD COLUMN IF NOT EXISTS data_realism text,
    ADD COLUMN IF NOT EXISTS realism_mismatch boolean NOT NULL DEFAULT false;

ALTER TABLE public.research_findings
    ADD COLUMN IF NOT EXISTS data_realism text;

CREATE INDEX IF NOT EXISTS experiment_runs_realism_idx ON public.experiment_runs (data_realism);
