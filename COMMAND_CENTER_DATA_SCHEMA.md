# Command Center Data Schema & SQL Reference

**Purpose**: Document the database tables used by the Command Center query system, with optimized queries for each metric.

---

## 1. Core Tables (Existing Schema)

### 1.1 `company_state` (singleton)
Current organization status and research configuration.

```sql
CREATE TABLE company_state (
    id INT PRIMARY KEY DEFAULT 1,
    current_phase phase,              -- 'frame'|'hypothesize'|'experiment'|'validate'|'write'|'submit'
    phase_started_at TIMESTAMPTZ,
    deadline TIMESTAMPTZ,
    problem_statement TEXT,
    stance TEXT,
    success_criterion TEXT,
    thesis TEXT,                      -- Primary claim under investigation
    niche TEXT,                       -- Research focus
    audience TEXT,                    -- Target publication venue
    charter TEXT,                     -- Research plan (can be JSON)
    paused BOOLEAN,
    paused_reason TEXT
);
```

**Query**: Get current state for phase indicator + progress calculation
```sql
SELECT * FROM company_state WHERE id = 1;
```

---

### 1.2 `claims` (formerly `theses`)
Research claims/hypotheses with evidence chain.

```sql
CREATE TABLE claims (
    id BIGSERIAL PRIMARY KEY,
    statement TEXT NOT NULL,          -- The actual claim
    status claim_status,              -- 'proposed'|'tested'|'weakly_supported'|'replicated'|'invalidated'|'merged'
    confidence NUMERIC(3,2),          -- 0.0–1.0
    confidence_prev NUMERIC(3,2),
    parent_id BIGINT REFERENCES claims(id),
    created_by_run_id BIGINT,
    invalidated_at TIMESTAMPTZ,
    invalidated_by_verdict_id BIGINT,
    invalidation_reason TEXT,
    last_evidence_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);

-- Key indexes
CREATE INDEX idx_claims_active ON claims(status)
  WHERE status IN ('proposed', 'tested', 'weakly_supported', 'replicated');
CREATE INDEX idx_claims_confidence ON claims(confidence DESC)
  WHERE status IN ('proposed', 'tested', 'weakly_supported', 'replicated');
```

**Use cases**:
- List active claims (claim_strength handler)
- Count claims in each status (research progress)
- Find recently invalidated claims (blockers, verdicts)

---

### 1.3 `findings`
Research evidence, one per discovery.

```sql
CREATE TABLE findings (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT REFERENCES tasks(id),
    claim_id BIGINT REFERENCES claims(id),
    source TEXT,                     -- 'HN'|'Reddit'|'Web'|'Paper', etc.
    url TEXT,
    title TEXT,
    summary TEXT,
    relevance_score NUMERIC(3,1),   -- 1–10
    why_it_matters TEXT,
    audit_score NUMERIC(3,2),       -- 0–1
    audit_verdict TEXT,             -- 'pass'|'slop'|'stale'|NULL (pending)
    supports_thesis BOOLEAN,
    created_at TIMESTAMPTZ
);

-- Key indexes
CREATE INDEX idx_findings_thesis ON findings(claim_id, created_at DESC);
CREATE INDEX idx_findings_audit_verdict ON findings(audit_verdict, created_at DESC);
CREATE INDEX idx_findings_created ON findings(created_at DESC);
CREATE INDEX idx_findings_high_signal ON findings(claim_id, relevance_score DESC)
  WHERE audit_verdict = 'pass' AND relevance_score >= 8;
```

**Use cases**:
- Count findings by audit verdict (evidence quality, slop rate)
- Recent findings (metrics overview)
- Evidence chain for claim (claim detail handler)
- High-signal findings by claim (claim strength)

---

### 1.4 `critic_verdicts` (formerly `adversary_verdicts`)
Critic's verdicts: kill, weaken, or sustain decisions.

```sql
CREATE TABLE critic_verdicts (
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES claims(id),
    verdict TEXT NOT NULL,           -- 'invalidated'|'weakly_supported'|'replicated'
    confidence NUMERIC(3,2),         -- 0.0–1.0
    reasoning TEXT,
    cited_finding_ids BIGINT[],      -- Array of finding IDs
    first_pass_verdict TEXT,
    first_pass_reasoning TEXT,
    revised BOOLEAN,
    created_at TIMESTAMPTZ,
    run_id BIGINT
);

-- Key index
CREATE INDEX idx_critic_verdicts_created ON critic_verdicts(created_at DESC);
```

**Use cases**:
- Recent verdicts (verdicts handler, blockers)
- Verdict history for claim (claim detail)

---

### 1.5 `agent_runs`
Observability: each agent invocation.

```sql
CREATE TABLE agent_runs (
    id BIGSERIAL PRIMARY KEY,
    department TEXT,                 -- 'research'|'evaluation'|'execution', etc.
    agent_name TEXT,                 -- 'researcher'|'critic'|'planner'|etc.
    invocation_type TEXT,            -- 'researcher_explore'|'critic_evaluate', etc.
    model_tier model_tier,           -- 'reasoning'|'workhorse'|'fast'|'code'
    model_name TEXT,                 -- 'claude-opus', 'gpt-4', etc.
    triggered_by_event_id BIGINT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    status TEXT,                     -- 'running'|'completed'|'failed'
    input_token_count INT,
    output_token_count INT,
    cost_usd NUMERIC(8,4),
    error TEXT,
    langfuse_trace_id TEXT,
    input_summary TEXT,
    output_summary TEXT
);

-- Key indexes
CREATE INDEX idx_agent_runs_recent ON agent_runs(started_at DESC);
CREATE INDEX idx_agent_runs_agent_name ON agent_runs(agent_name, started_at DESC);
```

**Use cases**:
- Agent status (active agents, error rates)
- System health (error rate calculation)
- Cost tracking (tokens spent by tier)
- Agent detail (specific agent's runs)

---

### 1.6 `tasks`
Work units in the research pipeline.

```sql
CREATE TABLE tasks (
    id BIGSERIAL PRIMARY KEY,
    objective_id BIGINT REFERENCES objectives(id),
    claim_id BIGINT REFERENCES claims(id),
    department TEXT,
    task_type TEXT,                  -- 'research'|'evaluate'|'synthesize', etc.
    description TEXT,
    payload JSONB,
    priority INT,                    -- 1–10
    status task_status,              -- 'pending'|'running'|'completed'|'failed'|'halted'
    claimed_by TEXT,                 -- Agent name
    created_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result JSONB,
    halt_reason TEXT
);

-- Key indexes
CREATE INDEX idx_tasks_pending ON tasks(priority DESC, created_at)
  WHERE status = 'pending';
CREATE INDEX idx_tasks_running ON tasks(started_at)
  WHERE status = 'running';
CREATE INDEX idx_tasks_status_created ON tasks(status, created_at DESC);
```

**Use cases**:
- Task count by status (metrics overview)
- Stalled tasks (blockers)
- Task completion rate (research progress)
- Pending queue (tasks handler)

---

### 1.7 `cost_tracking`
Daily budget tracking.

```sql
CREATE TABLE cost_tracking (
    day DATE PRIMARY KEY,
    total_cost_usd NUMERIC(8,4),
    reasoning_calls INT,
    workhorse_calls INT,
    fast_calls INT,
    code_calls INT,
    cap_reached BOOLEAN
);

-- Key index
CREATE INDEX idx_cost_tracking_day ON cost_tracking(day DESC);
```

**Use cases**:
- Budget status (budget handler)
- Cost per finding (evidence quality)
- Daily spend trend (metrics overview)
- Cap alerts (blockers, real-time alerts)

---

### 1.8 `slop_rate_by_claim` (Materialized View)
Rolling 24-hour slop rate per claim.

```sql
CREATE MATERIALIZED VIEW slop_rate_by_claim AS
SELECT
    f.claim_id,
    COUNT(CASE WHEN f.audit_verdict = 'slop' THEN 1 END)::FLOAT / NULLIF(COUNT(*), 0) AS slop_rate,
    COUNT(*) AS window_size,
    MAX(f.created_at) AS latest
FROM findings f
WHERE f.created_at > NOW() - INTERVAL '24 hours'
GROUP BY f.claim_id
HAVING COUNT(*) >= 5;

CREATE UNIQUE INDEX idx_slop_rate_claim ON slop_rate_by_claim(claim_id);
```

**Use case**: Fast slop rate lookups (evidence quality, blockers).

---

### 1.9 `phase_transitions` (Audit Log)
Record of phase changes.

```sql
CREATE TABLE phase_transitions (
    id BIGSERIAL PRIMARY KEY,
    from_phase phase,
    to_phase phase,
    reason TEXT,
    cited_finding_ids BIGINT[],
    cited_claim_ids BIGINT[],
    proposed_by_run_id BIGINT,
    forced BOOLEAN,
    decided_at TIMESTAMPTZ
);
```

**Use case**: Phase status handler (current phase, history).

---

## 2. Query Patterns by Handler

### 2.1 Research Progress (0–100%)

```sql
-- Formula: (findings_pct * 0.4) + (completion_rate * 0.3) + (claims_advanced * 0.3)

-- Findings this week
SELECT 
  COUNT(*) as count,
  COUNT(*) FILTER (WHERE audit_verdict = 'pass') as pass,
  COUNT(*) FILTER (WHERE audit_verdict = 'slop') as slop
FROM findings
WHERE created_at > NOW() - INTERVAL '7 days';

-- Task completion rate (this week)
SELECT 
  COUNT(*) FILTER (WHERE status = 'completed') as completed,
  COUNT(*) as total,
  COUNT(*) FILTER (WHERE status IN ('failed', 'halted')) as failed
FROM tasks
WHERE created_at > NOW() - INTERVAL '7 days';

-- Claims advanced
SELECT 
  COUNT(*) FILTER (WHERE status IN ('weakly_supported', 'replicated')) as advanced,
  COUNT(*) as total
FROM claims
WHERE status IN ('proposed', 'tested', 'weakly_supported', 'replicated');
```

---

### 2.2 System Health (0–100%)

```sql
-- Agent error rate (24h)
SELECT 
  COUNT(*) as total_runs,
  COUNT(*) FILTER (WHERE status = 'running') as running,
  COUNT(*) FILTER (WHERE error IS NOT NULL) as errored
FROM agent_runs
WHERE started_at > NOW() - INTERVAL '24 hours';

-- Active agents (last 30 min)
SELECT COUNT(DISTINCT agent_name) as active
FROM agent_runs
WHERE started_at > NOW() - INTERVAL '30 minutes';

-- Expected agents (hardcoded)
-- 9 core: pi, planner, researcher, knowledge_scout, evaluation, critic, phase_adjudicator, reflection, curator
```

---

### 2.3 Evidence Health (0–100%)

```sql
-- Slop rate (overall, rolling 24h)
SELECT 
  COUNT(*) FILTER (WHERE audit_verdict = 'slop')::FLOAT / NULLIF(COUNT(*), 0) as slop_rate
FROM findings
WHERE created_at > NOW() - INTERVAL '24 hours';

-- Audit pass rate
SELECT 
  COUNT(*) FILTER (WHERE audit_verdict = 'pass') as pass,
  COUNT(*) as total
FROM findings
WHERE created_at > NOW() - INTERVAL '24 hours';

-- Evidence diversity (claims with evidence)
SELECT 
  COUNT(DISTINCT f.claim_id) as with_evidence,
  COUNT(DISTINCT c.id) as total_active
FROM claims c
LEFT JOIN findings f ON f.claim_id = c.id
WHERE c.status IN ('proposed', 'tested', 'weakly_supported', 'replicated');
```

---

### 2.4 Budget Tracking

```sql
-- Daily spend (last 30 days)
SELECT day, total_cost_usd, cap_reached
FROM cost_tracking
WHERE day >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY day DESC;

-- Total spend
SELECT SUM(total_cost_usd) as total FROM cost_tracking;

-- Cost per finding (this week)
SELECT 
  (SELECT SUM(total_cost_usd) FROM cost_tracking WHERE day >= CURRENT_DATE - INTERVAL '7 days') as week_cost,
  COUNT(*) as findings_week
FROM findings
WHERE created_at > NOW() - INTERVAL '7 days';
```

---

### 2.5 Blockers & Attention Items

```sql
-- Invalidated claims (last 48h)
SELECT 
  c.id, c.statement, cv.verdict, cv.reasoning,
  cv.created_at,
  COUNT(f.id) as evidence_count
FROM claims c
JOIN critic_verdicts cv ON cv.id = c.invalidated_by_verdict_id
LEFT JOIN findings f ON f.claim_id = c.id
WHERE c.invalidated_at > NOW() - INTERVAL '48 hours'
GROUP BY c.id, cv.id
ORDER BY cv.created_at DESC;

-- Agent errors (last 4h)
SELECT 
  agent_name, COUNT(*) as error_count,
  MAX(error) as latest_error,
  MAX(started_at) as last_run
FROM agent_runs
WHERE started_at > NOW() - INTERVAL '4 hours'
GROUP BY agent_name
HAVING COUNT(*) FILTER (WHERE error IS NOT NULL) > 0
ORDER BY error_count DESC;

-- Stalled tasks (pending > 12h or running > 8h)
SELECT 
  id, task_type, status,
  EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, created_at)))/3600 as hours_elapsed
FROM tasks
WHERE (status = 'pending' AND created_at < NOW() - INTERVAL '12 hours')
   OR (status = 'running' AND started_at < NOW() - INTERVAL '8 hours')
ORDER BY created_at ASC;

-- Budget exceeded
SELECT day, total_cost_usd, cap_reached
FROM cost_tracking
WHERE cap_reached OR total_cost_usd > 50.0
ORDER BY day DESC;
```

---

## 3. Optimized Index Checklist

These indexes should exist on a production instance:

```sql
-- Claims
CREATE INDEX idx_claims_active ON claims(status) 
  WHERE status IN ('proposed', 'tested', 'weakly_supported', 'replicated');
CREATE INDEX idx_claims_confidence ON claims(confidence DESC);
CREATE INDEX idx_claims_invalidated_at ON claims(invalidated_at DESC);

-- Findings
CREATE INDEX idx_findings_claim_created ON findings(claim_id, created_at DESC);
CREATE INDEX idx_findings_audit_created ON findings(audit_verdict, created_at DESC);
CREATE INDEX idx_findings_created ON findings(created_at DESC);
CREATE INDEX idx_findings_supports ON findings(supports_thesis)
  WHERE audit_verdict = 'pass';

-- Agent runs
CREATE INDEX idx_agent_runs_started ON agent_runs(started_at DESC);
CREATE INDEX idx_agent_runs_agent_name ON agent_runs(agent_name, started_at DESC);
CREATE INDEX idx_agent_runs_error ON agent_runs(error IS NOT NULL, started_at DESC);

-- Tasks
CREATE INDEX idx_tasks_status ON tasks(status, created_at DESC);
CREATE INDEX idx_tasks_pending ON tasks(priority DESC, created_at)
  WHERE status = 'pending';
CREATE INDEX idx_tasks_running ON tasks(started_at)
  WHERE status = 'running';

-- Cost tracking
CREATE INDEX idx_cost_tracking_day ON cost_tracking(day DESC);
CREATE INDEX idx_cost_tracking_cap ON cost_tracking(cap_reached)
  WHERE cap_reached = true;

-- Critic verdicts
CREATE INDEX idx_critic_verdicts_created ON critic_verdicts(created_at DESC);
CREATE INDEX idx_critic_verdicts_claim ON critic_verdicts(claim_id, created_at DESC);
```

---

## 4. Query Performance Targets

| Query | Target | Actual |
|-------|--------|--------|
| Count findings (7d) | < 50ms | _ms |
| Task completion rate | < 50ms | _ms |
| Slop rate | < 100ms | _ms |
| Agent error rate | < 100ms | _ms |
| All blockers | < 300ms | _ms |
| Full health check | < 1s | _ms |

**Optimization tactics**:
1. Ensure indexes are present and analyzed
2. Use `VACUUM ANALYZE` after large data loads
3. Enable query logging: `log_min_duration_statement = 500`
4. Use `EXPLAIN ANALYZE` on slow queries
5. Consider materialized views for frequent rolling-window calculations

---

## 5. Data Migration Notes

When migrating from old schema (theses → claims, etc.):

```sql
-- Data should already be migrated by 008_labfoundry_reontology.sql
-- Verify post-migration:

-- Check enum values
SELECT DISTINCT status FROM claims LIMIT 5;
-- Should show: proposed, tested, weakly_supported, replicated, invalidated, merged

-- Check FK references
SELECT COUNT(*) FROM claims WHERE invalidated_by_verdict_id IS NOT NULL;
-- Should match number of invalidated claims

-- Refresh materialized view
REFRESH MATERIALIZED VIEW CONCURRENTLY slop_rate_by_claim;
```

---

## 6. Example: Build a Full Metrics Query

Combining multiple tables for complete metrics snapshot:

```sql
SELECT 
  -- Company state
  (SELECT current_phase FROM company_state WHERE id = 1) as current_phase,
  (SELECT deadline FROM company_state WHERE id = 1) as deadline,
  
  -- Claims
  (SELECT COUNT(*) FROM claims 
   WHERE status IN ('proposed', 'tested', 'weakly_supported', 'replicated')) as active_claims,
  (SELECT COUNT(*) FROM claims WHERE status = 'invalidated') as invalidated_claims,
  
  -- Findings (7 days)
  (SELECT COUNT(*) FROM findings WHERE created_at > NOW() - INTERVAL '7 days') as findings_week,
  (SELECT COUNT(*) FROM findings WHERE created_at > NOW() - INTERVAL '7 days' AND audit_verdict = 'pass') as findings_pass,
  (SELECT COUNT(*) FROM findings WHERE created_at > NOW() - INTERVAL '7 days' AND audit_verdict = 'slop') as findings_slop,
  
  -- Tasks (7 days)
  (SELECT COUNT(*) FROM tasks WHERE created_at > NOW() - INTERVAL '7 days') as tasks_total,
  (SELECT COUNT(*) FROM tasks WHERE created_at > NOW() - INTERVAL '7 days' AND status = 'completed') as tasks_completed,
  
  -- Agent runs (24h)
  (SELECT COUNT(DISTINCT agent_name) FROM agent_runs WHERE started_at > NOW() - INTERVAL '24 hours') as agents_active,
  (SELECT COUNT(*) FILTER (WHERE error IS NOT NULL) FROM agent_runs WHERE started_at > NOW() - INTERVAL '24 hours') as agents_errors,
  
  -- Budget
  (SELECT SUM(total_cost_usd) FROM cost_tracking) as total_spend,
  (SELECT SUM(total_cost_usd) FROM cost_tracking WHERE day >= CURRENT_DATE - INTERVAL '7 days') as spend_week,
  (SELECT COALESCE(total_cost_usd, 0) FROM cost_tracking WHERE day = CURRENT_DATE) as spend_today;
```

---

## 7. Troubleshooting Slow Queries

```sql
-- Find slow queries (enabled in postgresql.conf)
SELECT query, calls, mean_time, max_time
FROM pg_stat_statements
WHERE mean_time > 100
ORDER BY mean_time DESC;

-- Analyze specific query
EXPLAIN ANALYZE
SELECT COUNT(*) FROM findings
WHERE created_at > NOW() - INTERVAL '24 hours';

-- Check index usage
SELECT schemaname, tablename, indexname, idx_scan
FROM pg_stat_user_indexes
ORDER BY idx_scan DESC;

-- Rebuild fragmented index
REINDEX INDEX idx_findings_created;
```

---

## 8. Data Retention & Archival

```sql
-- Archive old findings (> 6 months)
-- Move to archive table, then delete
INSERT INTO findings_archive
SELECT * FROM findings WHERE created_at < NOW() - INTERVAL '6 months';

DELETE FROM findings WHERE created_at < NOW() - INTERVAL '6 months';

-- Keep agent_runs for 3 months (audit trail)
DELETE FROM agent_runs WHERE started_at < NOW() - INTERVAL '3 months';
```

---

End of data schema reference.
