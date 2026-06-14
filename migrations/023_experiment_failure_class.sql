-- 023_experiment_failure_class.sql — a coarse failure class on experiment_runs for triage + feedback.
--
-- The session loop classifies WHY a run failed (env_missing_lib | timeout | no_result |
-- network_attempt | serialization | infeasible | genuine_bug) and surfaces the MOST INFORMATIVE
-- attempt error (a real traceback beats the generic "no JSON result"). Persisting the class lets
-- ops.experiment_audit bucket failures and lets the researcher feed capability gaps back to Ariadne.
-- Idempotent.
ALTER TABLE public.experiment_runs ADD COLUMN IF NOT EXISTS failure_class text;
CREATE INDEX IF NOT EXISTS experiment_runs_failure_class_idx ON public.experiment_runs(failure_class);
