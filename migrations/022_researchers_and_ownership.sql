-- 022_researchers_and_ownership.sql
-- Researcher identity + ownership — the lab's experiments become attributable artifacts.
--
-- Until now "the researcher" was a stateless role: each task was claimed by an ephemeral
-- `researcher-<uuid>` worker and nothing linked an experiment to a person. This migration
-- introduces a persistent, named ROSTER of full-stack researchers (ML engineer + software
-- engineer + scientist) and an ownership chain:
--
--     claims.researcher_id  (Ariadne assigns a direction's owner at approval)
--        └─ tasks.researcher_id        (inherited from the claim, for atomic claim/filter)
--             └─ experiment_runs.researcher_id  (set when the owner AUTHORS the experiment)
--
-- So every experiment is linked to a SPECIFIC researcher who designed, ran, and interpreted it.
-- Idempotent (re-runnable): IF NOT EXISTS / ON CONFLICT throughout.

-- The roster. persona is a short bio; the full system prompt is composed at runtime
-- (agents/researcher/identity.py) from name + specialty + persona, so this stays lean.
CREATE TABLE IF NOT EXISTS public.researchers (
    id         bigserial PRIMARY KEY,
    name       text NOT NULL UNIQUE,                       -- the persona's name (e.g. 'Daedalus')
    persona    text NOT NULL DEFAULT '',                   -- 1-2 sentence bio / voice
    specialty  text NOT NULL DEFAULT '',                   -- assignment tag (matched to a direction's field)
    model      text,                                        -- optional per-researcher model override (else lab default)
    status     text NOT NULL DEFAULT 'active'
               CHECK (status IN ('active', 'paused', 'retired')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Ownership columns (nullable until assigned; SET NULL on researcher delete so history survives a
-- roster change — an experiment is never cascade-deleted because its author was retired).
ALTER TABLE public.claims
    ADD COLUMN IF NOT EXISTS researcher_id bigint REFERENCES public.researchers(id) ON DELETE SET NULL;
ALTER TABLE public.tasks
    ADD COLUMN IF NOT EXISTS researcher_id bigint REFERENCES public.researchers(id) ON DELETE SET NULL;
ALTER TABLE public.experiment_runs
    ADD COLUMN IF NOT EXISTS researcher_id bigint REFERENCES public.researchers(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS claims_researcher_id_idx          ON public.claims(researcher_id);
CREATE INDEX IF NOT EXISTS tasks_researcher_id_idx           ON public.tasks(researcher_id);
CREATE INDEX IF NOT EXISTS experiment_runs_researcher_id_idx ON public.experiment_runs(researcher_id);

-- Default roster — a small set of named, full-stack researchers with distinct leanings (each owns
-- assigned directions end-to-end; the specialty only biases assignment). Extend/edit via
-- ops.researchers. ON CONFLICT keeps re-runs and manual edits intact.
INSERT INTO public.researchers (name, specialty, persona) VALUES
    ('Daedalus', 'systems-optimization',
     'A builder''s builder — from-scratch torch models, optimization dynamics, architecture ablations, and the engineering to make a run reproducible.'),
    ('Hypatia', 'statistics-calibration',
     'A mathematician''s eye — estimators, uncertainty, calibration, and the statistical discipline to tell a real signal from noise.'),
    ('Heron', 'llm-retrieval-eval',
     'An applied experimentalist — LLM behaviour, retrieval and reranking, honest benchmarking on real data with measurable thresholds.')
ON CONFLICT (name) DO NOTHING;
