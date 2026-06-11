-- 015_direction_adjudications.sql — the INDEPENDENT novelty/impact gate.
--
-- Ariadne's gate floors (novelty/impact/paper_potential) are SELF-ASSESSED by the same
-- LLM call that authored the direction — no external check, so self-scores ceiling-cluster
-- (novelty never below 3) and the same topic cluster recurs unpenalized. This adds an
-- independent adjudicator (agents/novelty) that scores each direction against the ACTUAL
-- nearest prior art (the corpus) and the lab's OWN prior directions (anti-rut), and the gate
-- (harness/ariadne_pace) requires its verdict='pass' — not just the proposer's self-score.
--
-- One row per direction. verdict is derived deterministically (handler-side) from the
-- independent scores + flags, mirroring how composite is computed not LLM-set. Additive +
-- idempotent. A LEFT JOIN in the gate means an UNadjudicated direction never auto-approves
-- while adjudication is required (the desired fail-safe).
CREATE TABLE IF NOT EXISTS public.direction_adjudications (
    claim_id            bigint PRIMARY KEY REFERENCES public.claims (id) ON DELETE CASCADE,
    novelty_independent smallint,
    impact_independent  smallint,
    is_novel            boolean NOT NULL DEFAULT false,
    is_impactful        boolean NOT NULL DEFAULT false,
    redundant           boolean NOT NULL DEFAULT false,   -- re-treads a prior lab direction (a rut)
    redundant_note      text,
    verdict             text NOT NULL,                    -- pass | hold
    rationale           text,
    nearest_prior_art   jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_by_run_id   bigint,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_direction_adjudications_verdict
    ON public.direction_adjudications (verdict);
