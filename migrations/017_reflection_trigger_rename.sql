-- 017: revive the dissent→reflection trigger — it was dead at pre-rename vocabulary.
--
-- trigger_reflection_on_dissent (001) still matched the LEGACY invocation names
-- ('adversary.kill_verdict', 'auditor.slop_score') and the legacy event name
-- ('thesis.invalidated'); the live lab emits 'critic.kill_verdict',
-- 'evaluation.slop_score', and 'claim.invalidated'. Result: ZERO reflection.requested
-- events ever — the entire dissent→lesson channel (agents/reflection) was unreachable
-- dead code while its handler sat registered.
--
-- Same shape, current names (legacy kept for old rows' sake on the event arm).

CREATE OR REPLACE FUNCTION public.trigger_reflection_on_dissent() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    had_dissent BOOLEAN;
BEGIN
    IF NEW.status NOT IN ('completed', 'failed') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    had_dissent := EXISTS (
        SELECT 1 FROM events
        WHERE emitted_by_run_id = NEW.id
          AND event_type IN ('audit.slop_detected', 'claim.invalidated', 'thesis.invalidated')
    ) OR (
        NEW.invocation_type IN ('critic.kill_verdict', 'evaluation.slop_score',
                                'adversary.kill_verdict', 'auditor.slop_score')
        AND NEW.status = 'completed'
    );

    IF had_dissent THEN
        INSERT INTO events (
            event_type, target_type, target_id, payload,
            emitted_by_run_id, dedup_key
        )
        VALUES (
            'reflection.requested',
            'agent_run',
            NEW.id,
            jsonb_build_object('invocation_type', NEW.invocation_type),
            NEW.id,
            'reflect-' || NEW.id::TEXT
        )
        ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING;
    END IF;

    RETURN NEW;
END;
$$;
