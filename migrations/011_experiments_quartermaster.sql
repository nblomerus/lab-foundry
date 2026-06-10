-- 011_experiments_quartermaster.sql — give experiment_runs everything the
-- sandboxed-experiment + Quartermaster lanes need, and widen the status set.
--
-- The researcher now generates a self-contained Python experiment that the
-- Quartermaster schedules into an isolated Docker container (CPU or GPU), with
-- per-experiment budgets, heartbeats, and active kill. Results + a researcher's
-- narrative note flow back into the direction's confidence AND into Mimir as a
-- first-party Library document.
--
-- Idempotent + additive (ADD COLUMN IF NOT EXISTS) — safe to apply on the live DB.

ALTER TABLE public.experiment_runs
    ADD COLUMN IF NOT EXISTS code                text,                              -- the generated script (NULL for the legacy data-probe kinds)
    ADD COLUMN IF NOT EXISTS wall_clock_budget_s integer NOT NULL DEFAULT 600,
    ADD COLUMN IF NOT EXISTS mem_budget_mb       integer NOT NULL DEFAULT 2048,
    ADD COLUMN IF NOT EXISTS requires_gpu        boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS gpu_mem_mb          integer,                           -- VRAM budget when requires_gpu
    ADD COLUMN IF NOT EXISTS priority            integer NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS worker              text,                              -- the lf-exp-<id> container name
    ADD COLUMN IF NOT EXISTS heartbeat_at        timestamptz,                       -- last liveness beat from the runner
    ADD COLUMN IF NOT EXISTS killed_at           timestamptz,
    ADD COLUMN IF NOT EXISTS kill_reason         text,
    ADD COLUMN IF NOT EXISTS resource_usage      jsonb,                             -- peak mem/cpu/vram, exit code, …
    ADD COLUMN IF NOT EXISTS provenance          jsonb,                             -- image digest + seed + code hash (the reproducibility basis)
    ADD COLUMN IF NOT EXISTS dataset_refs        jsonb,                             -- content-hashes of input/output datasets
    ADD COLUMN IF NOT EXISTS researcher_notes    text,                              -- narrative commentary surfaced to Ariadne
    ADD COLUMN IF NOT EXISTS ingested_doc_id     bigint REFERENCES public.documents(id) ON DELETE SET NULL;

-- Widen the status CHECK to admit 'queued' (proposed, awaiting a compute slot)
-- and 'killed' (terminated by the Quartermaster). The constraint name is stable
-- from 001; drop-and-recreate is the only way to alter a CHECK.
ALTER TABLE public.experiment_runs DROP CONSTRAINT IF EXISTS experiment_runs_status_check;
ALTER TABLE public.experiment_runs
    ADD CONSTRAINT experiment_runs_status_check
    CHECK (status = ANY (ARRAY['pending', 'queued', 'running', 'completed', 'failed', 'killed']));

-- The Quartermaster sweeps these constantly; index the hot status lookups.
CREATE INDEX IF NOT EXISTS experiment_runs_status_idx ON public.experiment_runs (status);
CREATE INDEX IF NOT EXISTS experiment_runs_queued_priority_idx
    ON public.experiment_runs (priority DESC, started_at) WHERE status = 'queued';
