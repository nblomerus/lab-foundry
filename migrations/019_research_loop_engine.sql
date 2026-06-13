-- 019: research-loop engine substrate.
--
-- Two artifacts that turn the emergent loop into one source of truth:
--
--   * direction_stage_v  — the ONE place a direction's STAGE is derived (topic → scored →
--     held/passed → approved → review → proposal → experiments → finding → article →
--     concluded), plus the single human-readable BLOCKER for why it isn't moving. Before
--     this view the stage was re-derived independently in the pacemaker, the closure ladder,
--     the planner, the API, and the web UI — drift-prone. Every reader now selects from here.
--
--   * direction_transitions — an append-only audit of every lifecycle status change written
--     through state.advance_direction(): graduate | conclude | gap | retire | supersede |
--     reopen. Before this, status writes were scattered across six writers with a free-text
--     invalidation_reason, so "why did this direction die?" was not queryable. The transition
--     column makes gap-vs-retire-vs-supersede a first-class, queryable distinction.
--
-- Additive and idempotent. claims.status has NO check constraint (verified live) — legal
-- transitions are enforced in Python (state._LEGAL_DIRECTION_TRANSITIONS), not the DB.

CREATE OR REPLACE VIEW public.direction_stage_v AS
SELECT
  c.id        AS claim_id,
  c.statement,
  c.status::text AS claim_status,
  dg.status   AS gate_status,
  (SELECT da.verdict FROM public.direction_adjudications da
     WHERE da.claim_id = c.id ORDER BY da.created_at DESC LIMIT 1) AS adjudication,
  EXISTS (SELECT 1 FROM public.research_documents rd
            WHERE rd.claim_id = c.id AND rd.kind = 'lit_review' AND rd.status = 'final') AS has_review,
  EXISTS (SELECT 1 FROM public.research_documents rd
            WHERE rd.claim_id = c.id AND rd.kind = 'proposal'  AND rd.status = 'final') AS has_proposal,
  EXISTS (SELECT 1 FROM public.research_documents rd
            WHERE rd.claim_id = c.id AND rd.kind = 'article'   AND rd.status = 'final') AS has_article,
  (SELECT count(*) FROM public.experiment_runs e JOIN public.tasks t ON t.id = e.task_id
     WHERE t.claim_id = c.id AND e.status = 'completed') AS experiments_done,
  (SELECT count(*) FROM public.experiment_runs e JOIN public.tasks t ON t.id = e.task_id
     WHERE t.claim_id = c.id AND e.status NOT IN ('completed','failed','killed')) AS experiments_inflight,
  EXISTS (SELECT 1 FROM public.tasks t WHERE t.claim_id = c.id) AS has_tasks,
  (SELECT count(*) FROM public.research_findings rf WHERE rf.direction_claim_id = c.id) AS findings,
  -- The single derived stage label — the ladder the UI, API, doctor, and pulse all share.
  -- Terminal statuses win first: an invalidated/merged direction that still carries an old
  -- 'hold' adjudication must read as terminal, not 'held'.
  CASE
    WHEN c.status = 'concluded' THEN 'concluded'
    WHEN c.status = 'invalidated' THEN 'invalidated'
    WHEN c.status = 'merged' THEN 'merged'
    WHEN EXISTS (SELECT 1 FROM public.research_documents rd
                   WHERE rd.claim_id = c.id AND rd.kind = 'article' AND rd.status = 'final') THEN 'article'
    WHEN (SELECT count(*) FROM public.research_findings rf WHERE rf.direction_claim_id = c.id) > 0 THEN 'finding'
    WHEN (SELECT count(*) FROM public.experiment_runs e JOIN public.tasks t ON t.id = e.task_id
            WHERE t.claim_id = c.id AND e.status = 'completed') > 0 THEN 'experiments'
    WHEN EXISTS (SELECT 1 FROM public.research_documents rd
                   WHERE rd.claim_id = c.id AND rd.kind = 'proposal' AND rd.status = 'final') THEN 'proposal'
    WHEN EXISTS (SELECT 1 FROM public.research_documents rd
                   WHERE rd.claim_id = c.id AND rd.kind = 'lit_review' AND rd.status = 'final') THEN 'review'
    WHEN dg.status = 'approved' THEN 'approved'
    WHEN EXISTS (SELECT 1 FROM public.direction_adjudications da WHERE da.claim_id = c.id AND da.verdict = 'pass') THEN 'passed'
    WHEN EXISTS (SELECT 1 FROM public.direction_adjudications da WHERE da.claim_id = c.id AND da.verdict = 'hold') THEN 'held'
    WHEN EXISTS (SELECT 1 FROM public.direction_scores ds WHERE ds.claim_id = c.id) THEN 'scored'
    ELSE 'proposed'
  END AS stage,
  -- The blocker: the one reason this direction is parked, surfaced to lab_doctor / the UI.
  -- The evidence-cap literal (9) mirrors ARIADNE_EVIDENCE_CAP's default; kept a literal here
  -- (rather than a session GUC) for simplicity — if the env is retuned, update both.
  CASE
    WHEN c.status IN ('proposed','tested','weakly_supported','replicated')
         AND EXISTS (SELECT 1 FROM public.direction_adjudications da WHERE da.claim_id = c.id AND da.verdict = 'hold')
         AND dg.status IS DISTINCT FROM 'approved'
      THEN 'held by adjudicator'
    WHEN c.status IN ('proposed','tested','weakly_supported','replicated')
         AND (SELECT count(*) FROM public.experiment_runs e JOIN public.tasks t ON t.id = e.task_id
                WHERE t.claim_id = c.id AND e.status = 'completed') >= 9
      THEN 'evidence cap reached'
    ELSE NULL
  END AS blocker
FROM public.claims c
LEFT JOIN public.direction_gate dg ON dg.claim_id = c.id
WHERE c.claim_kind = 'direction';

CREATE TABLE IF NOT EXISTS public.direction_transitions (
  id                bigserial PRIMARY KEY,
  claim_id          bigint NOT NULL REFERENCES public.claims (id) ON DELETE CASCADE,
  from_status       text,
  to_status         text NOT NULL,
  transition        text NOT NULL,   -- graduate | conclude | gap | retire | supersede | reopen
  reason            text,
  decided_by        text,            -- synthesis | reflect | closure | deliberate | human | auto
  payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by_run_id  bigint,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_direction_transitions_claim ON public.direction_transitions (claim_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_direction_transitions_kind  ON public.direction_transitions (transition);
