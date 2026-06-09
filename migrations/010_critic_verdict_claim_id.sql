-- 010_critic_verdict_claim_id.sql — finish the thesis_id -> claim_id rename.
--
-- Every other table had its thesis_id column renamed to claim_id (baked into the
-- 001 dump), but critic_verdicts kept the legacy thesis_id column — while ALL the
-- code that touches it already expects claim_id:
--   * state.client.create_critic_verdict  INSERTs claim_id
--   * state.client.get_critic_verdict      SELECT * -> CriticVerdict(claim_id=...)
--   * api.bench                            SELECT id ... WHERE claim_id=$1
-- so the critic's verdict path was broken end-to-end (it would raise
-- UndefinedColumn). This brings the physical column in line with the code.
-- (api.snapshot._dissent, which read av.thesis_id, is updated in the same change.)
--
-- Idempotent + metadata-only (a column/constraint rename rewrites no rows) — safe
-- to apply via psql against the live DB even with data present.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'critic_verdicts'
          AND column_name = 'thesis_id'
    ) THEN
        ALTER TABLE public.critic_verdicts RENAME COLUMN thesis_id TO claim_id;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'adversary_verdicts_thesis_id_fkey'
    ) THEN
        ALTER TABLE public.critic_verdicts
            RENAME CONSTRAINT adversary_verdicts_thesis_id_fkey TO critic_verdicts_claim_id_fkey;
    END IF;
END $$;
