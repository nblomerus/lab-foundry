-- =========================================================================
-- 014 — Lessons dedupe + decay (Real-Lab operating model, Phase 0)
--
-- Closes the learning loop's failure modes:
--   * dedupe — a near-duplicate lesson shouldn't spam the table; instead it
--     should earn promotion pressure on the original (trigram similarity).
--   * decay — probationary lessons that never earn a supportive application
--     get retired so the curator's top-5 layer isn't crowded by dead advice.
--
-- Touches only the lessons table (from 002) — independent of 009/010.
-- =========================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Fast fuzzy lookup of near-duplicate lesson text per invocation type.
CREATE INDEX IF NOT EXISTS idx_lessons_text_trgm
    ON lessons USING gin (lesson_text gin_trgm_ops);

-- =========================================================================
-- FUNCTION: decay_lessons()
-- Retire probationary lessons that have sat for 14 days without earning a
-- single supportive application. Returns the retired ids for telemetry.
-- (Confidence decay of stale *active* lessons is deliberately deferred — it
-- needs calibration; see REAL_LAB_OPERATING_MODEL.md §8.)
-- =========================================================================

CREATE OR REPLACE FUNCTION decay_lessons() RETURNS TABLE(
    lesson_id BIGINT,
    action TEXT
) AS $$
BEGIN
    RETURN QUERY
    WITH stale AS (
        SELECT l.id
        FROM lessons l
        LEFT JOIN lesson_applications la
               ON la.lesson_id = l.id AND la.outcome = 'supportive'
        WHERE l.status = 'probationary'
          AND l.created_at < NOW() - INTERVAL '14 days'
        GROUP BY l.id
        HAVING COUNT(la.id) = 0
    ),
    retired AS (
        UPDATE lessons l
        SET status = 'retired',
            retired_at = NOW(),
            retired_reason = 'decayed: 14d probationary with 0 supportive applications'
        FROM stale
        WHERE l.id = stale.id
        RETURNING l.id
    )
    SELECT retired.id, 'decayed'::TEXT FROM retired;
END;
$$ LANGUAGE plpgsql;
