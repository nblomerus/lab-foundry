-- 008_direction_scores.sql — Ariadne's DECISION FRAMEWORK scores per direction.
--
-- One row per direction claim: the nine 1–5 dimensions she scores, plus the derived
-- composite + priority (computed in agents.ariadne.scoring, not by the LLM). Keyed by
-- claim_id with ON DELETE CASCADE so deleting a direction claim removes its scores —
-- preserving the "Ariadne's writes are surgically reversible" property
-- (DELETE FROM claims WHERE claim_kind IN ('mission','direction') cascades here).
--
-- Idempotent + additive — safe to apply via psql against the live DB.

CREATE TABLE IF NOT EXISTS public.direction_scores (
    claim_id              bigint PRIMARY KEY REFERENCES public.claims(id) ON DELETE CASCADE,
    novelty               smallint,
    feasibility           smallint,
    evidence_availability smallint,
    paper_potential       smallint,
    reviewer_interest     smallint,
    technical_depth       smallint,
    differentiation       smallint,
    cost_efficiency       smallint,
    lab_alignment         smallint,
    composite             numeric(4,2) NOT NULL,
    priority              text NOT NULL,          -- high | medium | low
    rationale             text,
    created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS direction_scores_priority_idx
    ON public.direction_scores (composite DESC);
