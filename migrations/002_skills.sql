-- 002_skills.sql
-- Boardroom: skill-improvement layer.
-- Lessons accumulated from dissent, applied via curator, retired by outcomes.
-- Tool description versioning for MCP server evolution.

BEGIN;

-- =========================================================================
-- ENUMS
-- =========================================================================

CREATE TYPE lesson_status AS ENUM (
    'probationary',
    'active',
    'retired'
);

CREATE TYPE lesson_source AS ENUM (
    'reflection',
    'audit_pattern',
    'adversary_pattern',
    'user_injected',
    'tool_misuse'
);

-- =========================================================================
-- LESSONS (the unit of accumulated skill)
-- =========================================================================

CREATE TABLE lessons (
    id BIGSERIAL PRIMARY KEY,
    applies_to_invocation TEXT NOT NULL,
    applies_when JSONB NOT NULL DEFAULT '{}',
    lesson_text TEXT NOT NULL,
    rationale TEXT,

    derived_from_run_id BIGINT REFERENCES agent_runs(id),
    derived_via lesson_source NOT NULL,
    confidence NUMERIC(3,2) NOT NULL DEFAULT 0.40 CHECK (confidence BETWEEN 0 AND 1),

    supersedes BIGINT REFERENCES lessons(id),
    superseded_by BIGINT REFERENCES lessons(id),

    status lesson_status NOT NULL DEFAULT 'probationary',
    promotion_run_count INT NOT NULL DEFAULT 0,
    contradiction_run_count INT NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    retired_reason TEXT
);

CREATE INDEX idx_lessons_active_by_invocation
    ON lessons(applies_to_invocation, confidence DESC)
    WHERE status IN ('probationary', 'active');

-- =========================================================================
-- LESSON APPLICATIONS (which lessons were in which run's context)
-- This is the substrate for outcome correlation.
-- =========================================================================

CREATE TABLE lesson_applications (
    id BIGSERIAL PRIMARY KEY,
    lesson_id BIGINT NOT NULL REFERENCES lessons(id),
    agent_run_id BIGINT NOT NULL REFERENCES agent_runs(id),
    outcome TEXT,
    outcome_judged_at TIMESTAMPTZ,
    outcome_judged_by_run_id BIGINT REFERENCES agent_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_lesson_applications_pending
    ON lesson_applications(lesson_id)
    WHERE outcome IS NULL;

CREATE INDEX idx_lesson_applications_lesson_outcome
    ON lesson_applications(lesson_id, outcome);

-- =========================================================================
-- TOOL DESCRIPTION VERSIONS (MCP tool evolution)
-- =========================================================================

CREATE TABLE tool_description_versions (
    id BIGSERIAL PRIMARY KEY,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    version INT NOT NULL,
    description TEXT NOT NULL,
    parameters_schema JSONB NOT NULL,
    derived_from_lesson_id BIGINT REFERENCES lessons(id),
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (server_name, tool_name, version)
);

CREATE UNIQUE INDEX idx_tool_versions_current
    ON tool_description_versions(server_name, tool_name)
    WHERE is_current;

-- =========================================================================
-- TRIGGER: emit reflection event when an agent_run completes with dissent
-- A "dissent run" = run that produced audit.slop_detected, thesis.invalidated,
-- OR the run *is* a critic finalizing a non-pass verdict.
-- =========================================================================

CREATE OR REPLACE FUNCTION trigger_reflection_on_dissent() RETURNS TRIGGER AS $$
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
          AND event_type IN ('audit.slop_detected', 'thesis.invalidated')
    ) OR (
        NEW.invocation_type IN ('adversary.kill_verdict', 'auditor.slop_score')
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
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_reflection_on_dissent
    AFTER UPDATE ON agent_runs
    FOR EACH ROW
    EXECUTE FUNCTION trigger_reflection_on_dissent();

-- =========================================================================
-- VIEW: active lessons per invocation (the curator's primary query)
-- =========================================================================

CREATE VIEW active_lessons_by_invocation AS
SELECT
    l.id,
    l.applies_to_invocation,
    l.applies_when,
    l.lesson_text,
    l.confidence,
    l.status,
    l.promotion_run_count,
    l.contradiction_run_count
FROM lessons l
WHERE l.status IN ('probationary', 'active')
ORDER BY l.applies_to_invocation, l.confidence DESC, l.promotion_run_count DESC;

-- =========================================================================
-- FUNCTION: reconcile_lessons()
-- Called periodically by the watchdog. Promotes / retires based on outcomes.
-- =========================================================================

CREATE OR REPLACE FUNCTION reconcile_lessons() RETURNS TABLE(
    lesson_id BIGINT,
    action TEXT,
    new_status lesson_status
) AS $$
BEGIN
    RETURN QUERY
    WITH lesson_stats AS (
        SELECT
            l.id,
            l.status,
            COUNT(*) FILTER (WHERE la.outcome = 'supportive') AS supportive,
            COUNT(*) FILTER (WHERE la.outcome = 'contradicting') AS contradicting,
            COUNT(*) FILTER (WHERE la.outcome IS NOT NULL) AS judged
        FROM lessons l
        LEFT JOIN lesson_applications la ON la.lesson_id = l.id
        WHERE l.status IN ('probationary', 'active')
        GROUP BY l.id, l.status
    ),
    promotions AS (
        UPDATE lessons l
        SET status = 'active',
            promoted_at = NOW(),
            promotion_run_count = ls.supportive::INT,
            confidence = LEAST(0.95, l.confidence + 0.10)
        FROM lesson_stats ls
        WHERE l.id = ls.id
          AND l.status = 'probationary'
          AND ls.supportive >= 5
          AND ls.contradicting <= 1
        RETURNING l.id, 'promoted'::TEXT AS action, l.status
    ),
    retirements AS (
        UPDATE lessons l
        SET status = 'retired',
            retired_at = NOW(),
            retired_reason = format('contradicted by %s runs vs %s supportive',
                                    ls.contradicting, ls.supportive),
            contradiction_run_count = ls.contradicting::INT
        FROM lesson_stats ls
        WHERE l.id = ls.id
          AND ls.contradicting >= 3
          AND ls.contradicting > ls.supportive
        RETURNING l.id, 'retired'::TEXT AS action, l.status
    )
    SELECT * FROM promotions
    UNION ALL
    SELECT * FROM retirements;
END;
$$ LANGUAGE plpgsql;

COMMIT;
