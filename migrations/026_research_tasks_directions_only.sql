-- 026_research_tasks_directions_only.sql
-- Research tasks belong to DIRECTIONS only. A stock-take found non-direction claims (mission/finding,
-- e.g. the claim #65 zombie) accumulating un-advanceable thin_corpus research tasks — the old leak was
-- the now-neutralized market-era `pi` path. The live paths (planner, closure ladder) already filter to
-- directions; this is a defensive BEFORE-INSERT guard that silently SKIPS a department='research' task
-- whose claim isn't a direction, so the leak can never recur regardless of source. Idempotent.

CREATE OR REPLACE FUNCTION public.research_tasks_directions_only() RETURNS trigger AS $$
BEGIN
    -- Skip only the PROVEN leak: a research task on a MISSION (the agenda frame) or a FINDING (a synthesis
    -- output) claim — neither is ever a thing to "research". hypothesis/subquestion sub-claims under a
    -- direction remain taskable, so this never blocks a legitimate flow.
    IF NEW.department = 'research' AND NEW.claim_id IS NOT NULL
       AND (SELECT claim_kind FROM public.claims WHERE id = NEW.claim_id)
           IN ('mission'::public.claim_kind, 'finding'::public.claim_kind) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_research_tasks_directions_only ON public.tasks;
CREATE TRIGGER trg_research_tasks_directions_only
    BEFORE INSERT ON public.tasks
    FOR EACH ROW EXECUTE FUNCTION public.research_tasks_directions_only();
