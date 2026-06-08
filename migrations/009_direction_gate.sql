-- 009_direction_gate.sql — the PRIORITY GATE on Ariadne's directions.
--
-- Human-approval promotion: Ariadne proposes + scores directions; a human approves/holds/
-- rejects each one here BEFORE any execution agent (Planner, Stage 2) may act on it. A
-- direction with no row is implicitly 'pending'. Only status='approved' directions become
-- active research — and a budget cap limits how many run at once.
--
-- Keyed by claim_id with ON DELETE CASCADE so a superseded/retired direction's gate row
-- vanishes with it (the agenda stays coherent + the move stays reversible).
--
-- Idempotent + additive — safe to apply via psql against the live DB.

CREATE TABLE IF NOT EXISTS public.direction_gate (
    claim_id   bigint PRIMARY KEY REFERENCES public.claims(id) ON DELETE CASCADE,
    status     text NOT NULL DEFAULT 'pending',   -- pending | approved | held | rejected
    note       text,
    decided_by text,                              -- 'human' (dashboard) | 'auto' (future)
    decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS direction_gate_status_idx ON public.direction_gate (status);
