-- 014_research_findings.sql — the terminal SYNTHESIS step.
--
-- The loop framed paper-shaped directions and ran reproducible experiments, but
-- nothing read ACROSS a direction's experiments to assemble a defensible result —
-- it dead-ended at "confidence moved + corpus grew". This adds the missing terminal:
-- once a direction has accumulated enough completed experiments, the synthesis agent
-- composes them into a paper-shaped FINDING (claim + method + numbers + limitations +
-- so-what), persisted here, graduated as a `finding` claim, and ingested into the
-- Library so it compounds and feeds Ariadne's next deliberation.
--
-- Additive + idempotent. `ALTER TYPE ... ADD VALUE` must run outside a transaction
-- block; the migration runner pipes each file straight to psql (per-statement
-- autocommit), so this is safe (mirrors 012's model_tier 'experiment' add).
ALTER TYPE public.claim_kind ADD VALUE IF NOT EXISTS 'finding';

-- One row per synthesized finding for a direction. direction_claim_id is the
-- direction it concludes; finding_claim_id is the claim_kind='finding' node minted
-- for it (graph lineage + status). grounded_in is the list of experiment ids/keys the
-- finding rests on (its reproducibility basis); ingested_doc_id backlinks the Library
-- doc the finding became. n_experiments = how many completed runs it was built from,
-- so the trigger only re-synthesizes when materially more evidence has accumulated.
CREATE TABLE IF NOT EXISTS public.research_findings (
    id                 bigserial PRIMARY KEY,
    direction_claim_id bigint NOT NULL REFERENCES public.claims (id) ON DELETE CASCADE,
    finding_claim_id   bigint REFERENCES public.claims (id) ON DELETE SET NULL,
    headline           text NOT NULL,
    claim_text         text NOT NULL,
    supported          text NOT NULL,          -- supported | refuted | mixed | inconclusive
    method             text,
    key_numbers        text,
    limitations        text,
    so_what            text,
    next_step          text,
    confidence         real,
    n_experiments      int NOT NULL DEFAULT 0,
    grounded_in        jsonb NOT NULL DEFAULT '[]'::jsonb,
    ingested_doc_id    bigint,
    created_by_run_id  bigint,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_research_findings_direction
    ON public.research_findings (direction_claim_id, created_at DESC);
