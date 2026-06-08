-- 005_claim_kind_and_goals.sql
-- Storage for the research PI ("Ariadne"): the agenda tree + per-hypothesis goals.
-- Locked spec: docs/REAL_LAB_OPERATING_MODEL.md (the mission→direction→hypothesis→
-- subquestion tree on claims.parent_id + a claim_kind discriminator — NO new tree
-- table) and docs/AGENT_OPERATING_MODEL.md (the claim_goals schema). The design doc
-- numbered these 009/011; this repo's consolidated baseline is 001, so they land as 005.
--
-- The research PI does not exist yet (Stage 0 neutralized the market-PI). This is the
-- storage it will write in shadow mode. Existing claims are all hypotheses (the design:
-- a hypothesis "= today's claim"), so claim_kind backfills to 'hypothesis' and the
-- current status-only readers are unaffected. All statements are idempotent so a fresh
-- install's `make migrate` and a direct apply to the live DB both behave.
--
-- FOLLOW-UP (Stage 3, when the PI starts creating mission/direction rows): the eight
-- readers that select active claims by status alone (state/client.py) must add an
-- explicit `claim_kind IN ('hypothesis','subquestion')` filter, else they'd treat
-- directions as hypotheses. Latent until those rows exist.

-- claim_kind — the agenda-tree discriminator.
--   mission       (parent_id NULL; projection of company_state.problem_statement)
--   └ direction   (the novelty unit)
--     └ hypothesis  (unit of belief; confidence + claim_goals; = today's claim)
--       └ subquestion (promoted only when load-bearing)
DO $$ BEGIN
    CREATE TYPE public.claim_kind AS ENUM ('mission', 'direction', 'hypothesis', 'subquestion');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE public.claims
    ADD COLUMN IF NOT EXISTS claim_kind public.claim_kind NOT NULL DEFAULT 'hypothesis';

CREATE INDEX IF NOT EXISTS idx_claims_kind ON public.claims (claim_kind);

-- claim_goals — per-hypothesis PI goals; history preserved (one row per goal-set).
CREATE TABLE IF NOT EXISTS public.claim_goals (
    id              bigserial PRIMARY KEY,
    claim_id        bigint NOT NULL REFERENCES public.claims(id),
    expectation     text NOT NULL,           -- what evidence would confirm it
    kill_condition  text NOT NULL,           -- what would refute it
    novelty_target  text,                    -- why it'd be publishable / not already done
    next_milestone  text,                    -- the concrete next proof
    priority_hint   text,                    -- the PI's only scheduling lever, consumed by the Planner
    status          text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'met', 'missed', 'revised')),
    set_by_run_id   bigint,
    set_at          timestamptz NOT NULL DEFAULT now(),
    reviewed_at     timestamptz,
    outcome         text                     -- filled at reflect: what actually happened
);
CREATE INDEX IF NOT EXISTS claim_goals_claim ON public.claim_goals (claim_id);
