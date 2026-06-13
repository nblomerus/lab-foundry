-- 018: research_documents — the written artifacts of the traditional research arc.
--
-- The lab ran the research METHOD implicitly (deliberation ≈ topic choice, claim_goals
-- ≈ hypotheses, synthesis ≈ findings) but produced no readable artifacts between the
-- direction statement and the finding. This table holds the scholar agent's documents,
-- one arc per direction:
--
--   lit_review  — corpus-grounded literature review (citation-graded like deliberation)
--   proposal    — research questions + hypotheses (measurable thresholds) + method plan
--   article     — the final IMRaD write-up (full article on concluded; research note on
--                 a mixed/inconclusive finding at the evidence cap)
--
-- One CURRENT document per (direction, kind): a re-write supersedes (status) rather
-- than duplicating. Bodies are markdown; citations is the graded list of corpus titles
-- the document grounds in; meta carries kind-specific structure (the proposal's RQs and
-- hypotheses live here so the experiment designer can consume them mechanically).

CREATE TABLE IF NOT EXISTS public.research_documents (
    id                 bigserial PRIMARY KEY,
    claim_id           bigint NOT NULL REFERENCES public.claims (id) ON DELETE CASCADE,
    kind               text   NOT NULL CHECK (kind IN ('lit_review', 'proposal', 'article')),
    title              text   NOT NULL,
    body_md            text   NOT NULL,
    meta               jsonb  NOT NULL DEFAULT '{}'::jsonb,
    citations          jsonb  NOT NULL DEFAULT '[]'::jsonb,
    status             text   NOT NULL DEFAULT 'final' CHECK (status IN ('draft', 'final', 'superseded')),
    created_by_run_id  bigint,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_research_documents_claim ON public.research_documents (claim_id, kind, id DESC);
