-- 007_agent_sessions.sql
-- Session/Step framework: groups multiple agent_runs into one logical
-- multi-step execution (researcher v2 today; every reworked agent next).
--
-- Adds:
--   - agent_sessions: one row per handler invocation
--   - agent_runs.session_id / step_name / parent_step_id / step_order /
--     fallback_attempts: link a run to its session, name the step, record
--     intermediate provider failures
--   - events.session_id: lets StreamHub fan step.* / session.* events
--     to a per-session subscriber without joining through agent_runs
--   - notify_event() trigger now includes session_id in the NOTIFY payload
--     so the WebSocket fanout can filter cheaply
--
-- Backfill: existing agent_runs / events have session_id = NULL. That's fine;
-- the trace UI only renders sessioned runs. Legacy rows show up in /debug as
-- before.

BEGIN;

-- =========================================================================
-- AGENT_SESSIONS
-- =========================================================================

CREATE TABLE agent_sessions (
    id BIGSERIAL PRIMARY KEY,
    handler_name TEXT NOT NULL,
    triggered_by_event_id BIGINT REFERENCES events(id),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed')),
    mode TEXT NOT NULL DEFAULT 'live'
        CHECK (mode IN ('live', 'replay')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    error TEXT
);

CREATE INDEX idx_agent_sessions_recent ON agent_sessions(started_at DESC);
CREATE INDEX idx_agent_sessions_handler ON agent_sessions(handler_name, started_at DESC);
CREATE INDEX idx_agent_sessions_event ON agent_sessions(triggered_by_event_id);

-- =========================================================================
-- AGENT_RUNS — session linkage + fallback attempt log
-- =========================================================================

ALTER TABLE agent_runs
    ADD COLUMN session_id BIGINT REFERENCES agent_sessions(id),
    ADD COLUMN step_name TEXT,
    ADD COLUMN parent_step_id BIGINT REFERENCES agent_runs(id),
    ADD COLUMN step_order INT,
    -- [{provider, model, error, latency_ms}] per provider that failed before
    -- the winning model in _invoke_with_fallback. Empty when no fallback fired.
    ADD COLUMN fallback_attempts JSONB NOT NULL DEFAULT '[]';

CREATE INDEX idx_agent_runs_session ON agent_runs(session_id, step_order);

-- =========================================================================
-- EVENTS — session linkage so StreamHub can filter without joins
-- =========================================================================

ALTER TABLE events
    ADD COLUMN session_id BIGINT REFERENCES agent_sessions(id);

CREATE INDEX idx_events_session ON events(session_id, emitted_at);

-- =========================================================================
-- notify_event() — now includes session_id in the NOTIFY payload so the
-- WebSocket fanout can filter and skip the re-fetch for step.*/session.*
-- events (whose payload already carries everything the UI needs).
-- =========================================================================

CREATE OR REPLACE FUNCTION notify_event() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('events', json_build_object(
        'id', NEW.id,
        'type', NEW.event_type,
        'target_type', NEW.target_type,
        'target_id', NEW.target_id,
        'session_id', NEW.session_id
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMIT;
