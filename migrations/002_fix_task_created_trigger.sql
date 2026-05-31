-- Fix: emit_task_created() referenced NEW.thesis_id, but migration 008 renamed
-- tasks.thesis_id -> claim_id without updating this trigger function. Every
-- pending-task INSERT then raised `record "new" has no field "thesis_id"`, so no
-- task could ever be created and the autonomous loop flatlined. Repoint to
-- claim_id (the task.created consumer reads task.claim_id, not this payload key,
-- so renaming the key to claim_id is safe and matches the post-008 ontology).
--
-- CREATE OR REPLACE is idempotent: applies cleanly on a fresh boot (after the
-- 001 baseline) and patches the existing/live DB via `make migrate`.
CREATE OR REPLACE FUNCTION public.emit_task_created() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.status <> 'pending' THEN
        RETURN NEW;
    END IF;
    INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
    VALUES (
        'task.created',
        'task',
        NEW.id,
        jsonb_build_object(
            'department', NEW.department,
            'task_type',  NEW.task_type,
            'priority',   NEW.priority,
            'claim_id',   NEW.claim_id
        ),
        'taskcreate-' || NEW.id::text
    )
    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING;
    RETURN NEW;
END;
$$;
