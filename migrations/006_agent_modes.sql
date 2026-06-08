-- 006_agent_modes.sql
-- The per-agent mode dial — readiness Stage 4 AND the debug control plane.
--
-- A per-agent activation seam, replacing the all-or-nothing KNOWLEDGE_CORE_ONLY block:
--   off       — handler not invoked (PAUSE: debug / safety / "not ready yet")
--   shadow    — read-only; not run via the dispatcher (no structural write-suppression
--               for event handlers yet — realized per-agent, e.g. Ariadne's firstlight)
--   advisory  — runs + writes, outputs flagged for human review
--   active    — runs normally
-- The dispatcher runs an agent only when its mode is advisory|active; off|shadow are
-- suppressed (suppression_reason = 'agent_<mode>'). An explicit row OVERRIDES the
-- KNOWLEDGE_CORE_ONLY default (that's the decoupling). Idempotent.
CREATE TABLE IF NOT EXISTS public.agent_modes (
    agent_name text PRIMARY KEY,
    mode       text NOT NULL DEFAULT 'active'
               CHECK (mode IN ('off', 'shadow', 'advisory', 'active')),
    note       text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
