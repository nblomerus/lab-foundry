-- 001_initial.sql
-- Boardroom: autonomous AI-native company schema.
-- Phase machine + theses + event bus + memory pointers + friction primitives.

BEGIN;

-- =========================================================================
-- ENUMS
-- =========================================================================

CREATE TYPE phase AS ENUM (
    'exploration',
    'convergence',
    'commitment',
    'execution'
);

CREATE TYPE thesis_status AS ENUM (
    'active',
    'promoted',
    'killed',
    'merged'
);

CREATE TYPE task_status AS ENUM (
    'pending',
    'running',
    'completed',
    'failed',
    'halted'
);

CREATE TYPE model_tier AS ENUM (
    'reasoning',
    'workhorse',
    'fast',
    'code'
);

CREATE TYPE event_status AS ENUM (
    'pending',
    'consumed',
    'failed',
    'suppressed'
);

-- =========================================================================
-- COMPANY STATE (singleton)
-- =========================================================================

CREATE TABLE company_state (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    problem_statement TEXT NOT NULL,
    stance TEXT,
    success_criterion TEXT,
    current_phase phase NOT NULL DEFAULT 'exploration',
    phase_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bootstrap_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deadline TIMESTAMPTZ NOT NULL,
    thesis TEXT,
    niche TEXT,
    audience TEXT,
    charter TEXT,
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    paused_reason TEXT
);

-- =========================================================================
-- THESES (the unit of strategic reasoning)
-- =========================================================================

CREATE TABLE theses (
    id BIGSERIAL PRIMARY KEY,
    claim TEXT NOT NULL,
    status thesis_status NOT NULL DEFAULT 'active',
    parent_id BIGINT REFERENCES theses(id),
    confidence NUMERIC(3,2) NOT NULL DEFAULT 0.50 CHECK (confidence BETWEEN 0 AND 1),
    confidence_prev NUMERIC(3,2),
    created_by_run_id BIGINT,
    killed_at TIMESTAMPTZ,
    killed_by_verdict_id BIGINT,
    kill_reason TEXT,
    last_evidence_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_theses_active ON theses(status) WHERE status = 'active';
CREATE INDEX idx_theses_confidence ON theses(confidence DESC) WHERE status = 'active';

-- =========================================================================
-- OBJECTIVES (used in execution phase; exploration drives off theses)
-- =========================================================================

CREATE TABLE objectives (
    id BIGSERIAL PRIMARY KEY,
    week_start DATE NOT NULL,
    objective TEXT NOT NULL,
    success_criteria TEXT NOT NULL,
    rationale TEXT,
    thesis_id BIGINT REFERENCES theses(id),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    created_by_run_id BIGINT
);

-- =========================================================================
-- TASKS (claimable work units)
-- =========================================================================

CREATE TABLE tasks (
    id BIGSERIAL PRIMARY KEY,
    objective_id BIGINT REFERENCES objectives(id),
    thesis_id BIGINT REFERENCES theses(id),
    department TEXT NOT NULL,
    task_type TEXT NOT NULL,
    description TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    priority INT NOT NULL DEFAULT 5,
    status task_status NOT NULL DEFAULT 'pending',
    claimed_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result JSONB,
    halt_reason TEXT
);

CREATE INDEX idx_tasks_pending ON tasks(priority DESC, created_at) WHERE status = 'pending';
CREATE INDEX idx_tasks_running ON tasks(started_at) WHERE status = 'running';

-- =========================================================================
-- FINDINGS (research outputs, with audit)
-- =========================================================================

CREATE TABLE findings (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL REFERENCES tasks(id),
    thesis_id BIGINT REFERENCES theses(id),
    source TEXT,
    url TEXT,
    title TEXT,
    summary TEXT NOT NULL,
    relevance_score NUMERIC(3,1) NOT NULL CHECK (relevance_score BETWEEN 1 AND 10),
    why_it_matters TEXT,
    audit_score NUMERIC(3,2),
    audit_verdict TEXT,
    supports_thesis BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_findings_thesis ON findings(thesis_id, created_at DESC);
CREATE INDEX idx_findings_high_signal
    ON findings(thesis_id, relevance_score DESC)
    WHERE audit_verdict = 'pass' AND relevance_score >= 8;

-- =========================================================================
-- ADVERSARY VERDICTS (kill / weaken decisions, two-pass reflection)
-- =========================================================================

CREATE TABLE adversary_verdicts (
    id BIGSERIAL PRIMARY KEY,
    thesis_id BIGINT NOT NULL REFERENCES theses(id),
    verdict TEXT NOT NULL,
    confidence NUMERIC(3,2) NOT NULL,
    reasoning TEXT NOT NULL,
    cited_finding_ids BIGINT[] NOT NULL,
    first_pass_verdict TEXT,
    first_pass_reasoning TEXT,
    revised BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id BIGINT
);

-- =========================================================================
-- AGENT RUNS (observability, model-tier accounting)
-- =========================================================================

CREATE TABLE agent_runs (
    id BIGSERIAL PRIMARY KEY,
    department TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    invocation_type TEXT NOT NULL,
    model_tier model_tier NOT NULL,
    model_name TEXT NOT NULL,
    triggered_by_event_id BIGINT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    input_token_count INT,
    output_token_count INT,
    cost_usd NUMERIC(8,4),
    error TEXT,
    langfuse_trace_id TEXT,
    input_summary TEXT,
    output_summary TEXT
);

CREATE INDEX idx_agent_runs_recent ON agent_runs(started_at DESC);

-- =========================================================================
-- EVENT BUS (the spine of the event-driven harness)
-- =========================================================================

CREATE TABLE events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    target_type TEXT,
    target_id BIGINT,
    emitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    emitted_by_run_id BIGINT REFERENCES agent_runs(id),
    status event_status NOT NULL DEFAULT 'pending',
    consumed_at TIMESTAMPTZ,
    consumed_by_handler TEXT,
    consumed_run_id BIGINT REFERENCES agent_runs(id),
    suppression_reason TEXT,
    dedup_key TEXT,
    UNIQUE (event_type, target_type, target_id, dedup_key)
);

CREATE INDEX idx_events_pending ON events(emitted_at) WHERE status = 'pending';
CREATE INDEX idx_events_recent ON events(emitted_at DESC);

CREATE OR REPLACE FUNCTION notify_event() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('events', json_build_object(
        'id', NEW.id,
        'type', NEW.event_type,
        'target_type', NEW.target_type,
        'target_id', NEW.target_id
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_notify_event AFTER INSERT ON events
    FOR EACH ROW EXECUTE FUNCTION notify_event();

-- =========================================================================
-- PHASE TRANSITIONS (audit log; no rollback, append only)
-- =========================================================================

CREATE TABLE phase_transitions (
    id BIGSERIAL PRIMARY KEY,
    from_phase phase NOT NULL,
    to_phase phase NOT NULL,
    reason TEXT NOT NULL,
    cited_finding_ids BIGINT[] NOT NULL DEFAULT '{}',
    cited_thesis_ids BIGINT[] NOT NULL DEFAULT '{}',
    proposed_by_run_id BIGINT REFERENCES agent_runs(id),
    forced BOOLEAN NOT NULL DEFAULT FALSE,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================================================================
-- COOLDOWNS (friction layer; per invocation+target)
-- =========================================================================

CREATE TABLE cooldowns (
    id BIGSERIAL PRIMARY KEY,
    invocation_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id BIGINT NOT NULL,
    cooldown_until TIMESTAMPTZ NOT NULL,
    set_by_run_id BIGINT REFERENCES agent_runs(id),
    UNIQUE (invocation_type, target_type, target_id)
);

-- Partial index predicates can't use NOW() (not IMMUTABLE). A plain B-tree on
-- cooldown_until is enough to make the "WHERE cooldown_until > NOW()" filter cheap
-- after the unique (invocation_type, target_type, target_id) lookup.
CREATE INDEX idx_cooldowns_until ON cooldowns(cooldown_until);

-- =========================================================================
-- COST TRACKING (daily caps)
-- =========================================================================

CREATE TABLE cost_tracking (
    day DATE PRIMARY KEY,
    total_cost_usd NUMERIC(8,4) NOT NULL DEFAULT 0,
    reasoning_calls INT NOT NULL DEFAULT 0,
    workhorse_calls INT NOT NULL DEFAULT 0,
    fast_calls INT NOT NULL DEFAULT 0,
    code_calls INT NOT NULL DEFAULT 0,
    cap_reached BOOLEAN NOT NULL DEFAULT FALSE
);

-- =========================================================================
-- MEMORY POINTERS (bridge to Zep; Postgres owns IDs, Zep owns content)
-- =========================================================================

CREATE TABLE memory_pointers (
    id BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    zep_session_id TEXT NOT NULL,
    zep_message_uuid TEXT,
    summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entity_type, entity_id, zep_message_uuid)
);

CREATE INDEX idx_memory_pointers_entity ON memory_pointers(entity_type, entity_id);

-- =========================================================================
-- USER OVERRIDES (rare; the "I do nothing" escape hatch)
-- =========================================================================

CREATE TABLE user_overrides (
    id BIGSERIAL PRIMARY KEY,
    override_type TEXT NOT NULL,
    target_type TEXT,
    target_id BIGINT,
    note TEXT,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================================================================
-- SLOP RATE (rolling 24h window, refreshed by watchdog)
-- =========================================================================

CREATE MATERIALIZED VIEW slop_rate_by_thesis AS
SELECT
    f.thesis_id,
    COUNT(*) FILTER (WHERE f.audit_verdict = 'slop')::FLOAT
        / NULLIF(COUNT(*), 0) AS slop_rate,
    COUNT(*) AS window_size,
    MAX(f.created_at) AS latest
FROM findings f
WHERE f.created_at > NOW() - INTERVAL '24 hours'
GROUP BY f.thesis_id
HAVING COUNT(*) >= 5;

CREATE UNIQUE INDEX idx_slop_rate_thesis ON slop_rate_by_thesis(thesis_id);

COMMIT;
