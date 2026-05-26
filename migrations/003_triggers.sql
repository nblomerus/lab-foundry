-- 003_triggers.sql
-- Triggers that turn database state changes into events on the bus.
-- These are how 'inserting a task' or 'creating a thesis' naturally wakes
-- the right handler without the inserting code needing to know about events.

BEGIN;

-- =========================================================================
-- TRIGGER: emit 'task.created' when a pending task is inserted.
-- =========================================================================

CREATE OR REPLACE FUNCTION emit_task_created() RETURNS TRIGGER AS $$
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
            'thesis_id',  NEW.thesis_id
        ),
        'taskcreate-' || NEW.id::text
    )
    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_emit_task_created
    AFTER INSERT ON tasks
    FOR EACH ROW
    EXECUTE FUNCTION emit_task_created();


-- =========================================================================
-- TRIGGER: emit 'queue.empty' when the last pending research task transitions.
-- Lets the Planner wake up to refill.
-- =========================================================================

CREATE OR REPLACE FUNCTION emit_queue_empty_if_drained() RETURNS TRIGGER AS $$
DECLARE
    remaining INT;
BEGIN
    IF OLD.status <> 'pending' OR NEW.status = 'pending' THEN
        RETURN NEW;
    END IF;
    SELECT COUNT(*) INTO remaining
    FROM tasks
    WHERE department = NEW.department AND status = 'pending';

    IF remaining = 0 THEN
        INSERT INTO events (event_type, target_type, payload, dedup_key)
        VALUES (
            'queue.empty',
            'queue',
            jsonb_build_object('department', NEW.department),
            'queueempty-' || NEW.department || '-' || EXTRACT(EPOCH FROM NOW())::bigint::text
        )
        ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_emit_queue_empty
    AFTER UPDATE ON tasks
    FOR EACH ROW
    EXECUTE FUNCTION emit_queue_empty_if_drained();

COMMIT;
