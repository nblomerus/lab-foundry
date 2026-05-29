# LabFoundry Command Center: Backend Integration Specification

**Author**: Nicholas Blomerus  
**Date**: 2026-05-29  
**Status**: Production Implementation Plan  
**Audience**: Backend engineers, DevOps

---

## Executive Summary

This document specifies the complete backend integration for the LabFoundry Command Center frontend (built in the previous phase). The Command Center provides an AI-native interface to the research lab's operational state through:

1. **Health Metrics** — Real-time org-wide KPIs (research progress, system health, evidence quality, budget)
2. **AI Query Interface** — Natural language questions answered with cited data
3. **Real-time Updates** — Live agent status, new challenges, and changing priorities

**Implementation scope**: 3 core systems
- **Metrics Engine** (SQL-based health calculations)
- **Query Router** (natural language → SQL dispatcher)
- **Real-time Adapter** (WebSocket integration)

**Estimated effort**: 5–7 engineer-days for production quality

---

## 1. System Architecture

### 1.1 Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend: CommandQuery + QuerySuggestions + OrganizationScope   │
└────────────────────────┬────────────────────────────────────────┘
                         │
        ┌────────────────┴────────────────┐
        │                                 │
   REST API                        WebSocket (live)
   (polling)                        (push)
        │                                 │
┌───────▼──────────────────────────────────▼──────────────────────┐
│              LabFoundry API Server (FastAPI)                     │
├────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Query Router (natural language dispatcher)              │   │
│  │  - Parse query intent + extract entities               │   │
│  │  - Route to specialized metric/data queries            │   │
│  │  - Compile response with markdown formatting + sources │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────┬──────────────────────────────────┐   │
│  │ Metrics Engine       │  Real-time Adapter              │   │
│  │ ────────────────     │  ──────────────────             │   │
│  │ • Research Progress  │  • Agent status changes         │   │
│  │ • System Health      │  • New verdicts/audit findings  │   │
│  │ • Evidence Health    │  • Budget threshold breaches    │   │
│  │ • Budget Tracking    │  • Priority shifts              │   │
│  │ • Attention Items    │  • Phase transitions            │   │
│  └──────────────────────┴──────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Data Layer: asyncpg connection pool + SQL queries       │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────┘
                         │
                ┌────────▼────────┐
                │   PostgreSQL    │
                │  (Boardroom DB) │
                └─────────────────┘
```

### 1.2 Key Principles

1. **SQL-first metrics** — No caching layer; direct queries to ensure real-time accuracy
2. **Stateless query router** — Each request self-contained; no session state
3. **Markdown formatting** — Responses are structured but human-readable (no JSON blobs)
4. **Extensible classification** — Query intents mapped to reusable metric functions
5. **Graceful degradation** — Missing data returns partial results with confidence flags

---

## 2. Health Metrics: Calculation & Source Data

All metrics are calculated on-demand from the operational event stream. Refresh interval for UI: **5 minutes** (or on WebSocket push).

### 2.1 Research Progress %

**Purpose**: Overall advancement through the research pipeline.

**Formula**:
```
research_progress = (
  (findings_generated / target_findings) * 40% +
  (task_completion_rate) * 30% +
  (claims_weakly_supported_or_better / claims_total) * 30%
)
clamped to [0, 100]
```

**SQL Queries**:

```sql
-- Finding generation (last 7 days)
SELECT 
  COUNT(*) as findings_this_week,
  COUNT(*) FILTER (WHERE audit_verdict = 'pass') as high_signal,
  COUNT(*) FILTER (WHERE audit_verdict = 'slop') as low_signal
FROM findings
WHERE created_at > NOW() - INTERVAL '7 days';

-- Task completion rate (last 7 days)
SELECT 
  COUNT(*) FILTER (WHERE status = 'completed') as completed,
  COUNT(*) FILTER (WHERE status = 'failed') as failed,
  COUNT(*) as total_attempted
FROM tasks
WHERE created_at > NOW() - INTERVAL '7 days';

-- Claim strength distribution
SELECT 
  COUNT(*) FILTER (WHERE status IN ('proposed', 'tested')) as proposed_or_tested,
  COUNT(*) FILTER (WHERE status IN ('weakly_supported', 'replicated')) as supported_or_replicated,
  COUNT(*) as total_active
FROM claims
WHERE status IN ('proposed', 'tested', 'weakly_supported', 'replicated');
```

**Targets** (configurable in `company_state.charter` or hardcoded):
- `target_findings`: 100 per week (adjust per research scope)
- Task completion rate: 70%+
- Claims advanced to supported: 50%+ of active claims

**Confidence flags**:
- Low if fewer than 10 findings this week
- Low if no tasks completed in last 3 days

---

### 2.2 System Health

**Purpose**: Infrastructure & agent operational status.

**Formula**:
```
system_health = (
  (100 - error_rate_pct) * 30% +
  (active_agents / expected_agents) * 30% +
  (running_agents / active_agents) * 20% +
  (1 - stale_agent_pct) * 20%
)
clamped to [0, 100]
```

**SQL Queries**:

```sql
-- Agent error rate (last 24h)
SELECT 
  COUNT(*) as total_runs,
  COUNT(*) FILTER (WHERE status = 'running') as running,
  COUNT(*) FILTER (WHERE error IS NOT NULL) as errored,
  COUNT(DISTINCT agent_name) as unique_agents
FROM agent_runs
WHERE started_at > NOW() - INTERVAL '24 hours';

-- Agent freshness (last 30 min)
SELECT 
  COUNT(DISTINCT agent_name) as active_last_30m,
  COUNT(DISTINCT agent_name) FILTER (WHERE started_at > NOW() - INTERVAL '30 minutes') as started_last_30m
FROM agent_runs
WHERE started_at > NOW() - INTERVAL '24 hours';

-- Stale agents (no run in last 8 hours)
SELECT 
  COUNT(DISTINCT agent_name) as stale_agents
FROM (
  SELECT DISTINCT agent_name
  FROM agent_runs ar
  WHERE NOT EXISTS (
    SELECT 1 FROM agent_runs ar2
    WHERE ar2.agent_name = ar.agent_name
    AND ar2.started_at > NOW() - INTERVAL '8 hours'
  )
  AND ar.started_at > NOW() - INTERVAL '24 hours'
) t;

-- Expected agents (hardcoded list or derive from invocation_type)
-- Expected: pi, planner, researcher, knowledge_scout, evaluation, critic, phase_adjudicator, reflection, curator = 9 core agents
```

**Thresholds**:
- Error rate > 20% → 🔴 red
- Error rate 10–20% → 🟡 amber  
- Error rate < 10% → 🟢 green
- Any stale agent (no run > 8h) → amber
- All agents stale → red

**Confidence flags**:
- Low if running for < 1 hour
- Medium if running < 24 hours

---

### 2.3 Evidence Health

**Purpose**: Quality of research evidence and audit integrity.

**Formula**:
```
evidence_health = (
  (1 - slop_rate) * 40% +
  (audit_pass_rate) * 35% +
  (evidence_diversity_score) * 25%
)
clamped to [0, 100]
```

**SQL Queries**:

```sql
-- Slop rate (rolling 24-hour window, per-claim)
SELECT 
  slop_rate,
  window_size,
  latest
FROM slop_rate_by_claim
ORDER BY latest DESC
LIMIT 1;
-- If no materialized view row: compute directly from findings table

SELECT 
  COUNT(*) FILTER (WHERE audit_verdict = 'slop')::FLOAT / NULLIF(COUNT(*), 0) as slop_rate,
  COUNT(*) as window_size,
  MAX(created_at) as latest
FROM findings
WHERE created_at > NOW() - INTERVAL '24 hours';

-- Audit pass rate (last 24h)
SELECT 
  COUNT(*) FILTER (WHERE audit_verdict = 'pass') as pass,
  COUNT(*) FILTER (WHERE audit_verdict = 'slop') as slop,
  COUNT(*) FILTER (WHERE audit_verdict IS NULL) as pending,
  COUNT(*) as total
FROM findings
WHERE created_at > NOW() - INTERVAL '24 hours';

-- Evidence diversity: ratio of claims with evidence to total claims
SELECT 
  COUNT(DISTINCT f.claim_id) as claims_with_evidence,
  COUNT(*) FILTER (WHERE c.status IN ('proposed', 'tested', 'weakly_supported', 'replicated')) as active_claims
FROM claims c
LEFT JOIN findings f ON f.claim_id = c.id AND f.created_at > NOW() - INTERVAL '7 days'
WHERE c.status IN ('proposed', 'tested', 'weakly_supported', 'replicated');
```

**Thresholds**:
- Slop rate > 30% → 🔴 red
- Slop rate 15–30% → 🟡 amber
- Slop rate < 15% → 🟢 green
- Audit pass rate < 50% → amber
- Evidence diversity < 40% → amber

**Confidence flags**:
- Low if fewer than 20 findings in rolling window
- Low if no claims have evidence yet

---

### 2.4 Budget Tracking

**Purpose**: Token spend, cost per finding, and runway.

**Formulas**:
```
cost_per_finding = total_cost_this_week / findings_this_week
runway_days = (budget_remaining) / (avg_daily_cost)
budget_utilization_pct = total_cost_to_date / total_budget
```

**SQL Queries**:

```sql
-- Daily cost summary (last 7 days)
SELECT 
  day,
  total_cost_usd,
  reasoning_calls,
  workhorse_calls,
  fast_calls,
  code_calls,
  cap_reached
FROM cost_tracking
WHERE day >= CURRENT_DATE - INTERVAL '7 days'
ORDER BY day DESC;

-- Total spend to date
SELECT 
  SUM(total_cost_usd) as total_spend,
  SUM(reasoning_calls + workhorse_calls + fast_calls + code_calls) as total_calls
FROM cost_tracking;

-- Cost per finding (this week)
SELECT 
  (SELECT SUM(total_cost_usd) FROM cost_tracking WHERE day >= CURRENT_DATE - INTERVAL '7 days') as week_cost,
  COUNT(*) as findings_this_week
FROM findings
WHERE created_at > NOW() - INTERVAL '7 days';

-- Token efficiency by agent (last 24h)
SELECT 
  agent_name,
  COUNT(*) as runs,
  SUM(input_token_count + output_token_count) as total_tokens,
  SUM(cost_usd) as cost,
  AVG((input_token_count + output_token_count)::FLOAT / NULLIF(GREATEST(LENGTH(output_summary), 1), 1)) as tokens_per_char_output
FROM agent_runs
WHERE started_at > NOW() - INTERVAL '24 hours'
GROUP BY agent_name
ORDER BY cost DESC;
```

**Budget configuration** (in `company_state.charter` or env var):
```json
{
  "budget": {
    "total_usd": 500.0,
    "daily_cap_usd": 50.0,
    "per_model_tier": {
      "reasoning": 0.50,
      "workhorse": 0.10,
      "fast": 0.02,
      "code": 0.05
    }
  }
}
```

**Confidence flags**:
- Low if budget data < 24 hours
- Medium if < 1 week historical data

---

### 2.5 Attention Items (Blockers & Hot Signals)

**Purpose**: High-priority issues demanding human or agent action.

**Query pattern**: Collect recent negative verdicts, error states, and anomalies.

**SQL Queries**:

```sql
-- Blocked claims (invalidated in last 48h with reasoning)
SELECT 
  c.id,
  c.statement,
  cv.verdict,
  cv.reasoning,
  cv.created_at,
  COUNT(f.id) as evidence_count
FROM claims c
JOIN critic_verdicts cv ON cv.id = c.invalidated_by_verdict_id
LEFT JOIN findings f ON f.claim_id = c.id
WHERE c.invalidated_at > NOW() - INTERVAL '48 hours'
GROUP BY c.id, cv.id
ORDER BY cv.created_at DESC
LIMIT 10;

-- Underperforming agents (error rate spike)
SELECT 
  agent_name,
  COUNT(*) as runs_last_4h,
  COUNT(*) FILTER (WHERE error IS NOT NULL) as errors,
  MAX(error) as latest_error,
  MAX(started_at) as last_run
FROM agent_runs
WHERE started_at > NOW() - INTERVAL '4 hours'
GROUP BY agent_name
HAVING COUNT(*) FILTER (WHERE error IS NOT NULL)::FLOAT / COUNT(*) > 0.2
ORDER BY agent_name;

-- Budget alerts
SELECT 
  CASE 
    WHEN cap_reached = true THEN 'daily_cap_exceeded'
    WHEN total_cost_usd > (
      SELECT COALESCE(SUM(total_cost_usd), 0)
      FROM cost_tracking
      WHERE day < CURRENT_DATE
    ) * 0.8 THEN 'week_budget_80_percent'
    ELSE NULL
  END as alert_type,
  day,
  total_cost_usd
FROM cost_tracking
WHERE cap_reached = true
OR day = CURRENT_DATE
ORDER BY day DESC;

-- Stalled tasks (pending > 12h or running > 8h)
SELECT 
  id,
  task_type,
  description,
  status,
  EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, created_at)))/3600 as hours_elapsed,
  claimed_by
FROM tasks
WHERE 
  (status = 'pending' AND created_at < NOW() - INTERVAL '12 hours')
  OR (status = 'running' AND started_at < NOW() - INTERVAL '8 hours')
ORDER BY created_at ASC
LIMIT 20;

-- High-slop claims (> 40% slop rate)
SELECT 
  sr.claim_id,
  c.statement,
  sr.slop_rate,
  sr.window_size,
  sr.latest
FROM slop_rate_by_claim sr
JOIN claims c ON c.id = sr.claim_id
WHERE sr.slop_rate > 0.4
ORDER BY sr.slop_rate DESC;
```

**Alert severity**:
- 🔴 **Critical**: Budget cap exceeded, 100% error rate, all agents stale
- 🟡 **Warning**: Budget 80%+, error rate > 20%, slop rate > 40%, tasks stalled > 24h
- 🔵 **Info**: New task dispatch, claim invalidated, phase transition proposed

---

## 3. AI Query Interface: POST /api/command-center/query

### 3.1 Endpoint Specification

**URL**: `POST /api/command-center/query`  
**Authentication**: None (internal, or add Bearer token if exposed)  
**Rate limit**: 100 queries/minute per session

### 3.2 Request Schema

```python
class QueryRequest(BaseModel):
    query: str              # Natural language question
    context: Optional[dict] = None  # Optional: { "claim_id": 42, "agent_name": "researcher" }
```

**Examples**:
- "What are our top 3 blockers?"
- "Who is working right now?"
- "Which hypothesis is weakest?"
- "How much budget do we have left?"
- "Show me the high-slop claims."
- "Why was claim #42 invalidated?"
- "What phase are we in?"

### 3.3 Response Schema

```python
class QuerySource(BaseModel):
    """Citation for a claim made in the response."""
    title: str              # "Database query result", "Critic verdict", etc.
    url: Optional[str]      # Link to raw data (e.g., /api/claims/42)
    snippet: Optional[str]  # Excerpt from source

class QueryResponse(BaseModel):
    query: str
    answer: str             # Markdown-formatted response
    sources: list[QuerySource]
    follow_up_queries: list[str]  # 2–4 suggested next questions
    confidence: float       # 0.0–1.0 (0.7+ is high-confidence)
    executed_at: str        # ISO 8601 timestamp
```

**Example response**:

```json
{
  "query": "What are our top 3 blockers?",
  "answer": "## Top 3 Blockers\n\n1. **Claim #42 invalidated by critic** (confidence: 0.95)\n   - Reason: Contradicted by 8 high-signal findings\n   - Status: Awaiting invalidation handling\n   - Age: 2 hours\n\n2. **Budget cap exceeded** (confidence: 1.0)\n   - Daily cap: $50.00\n   - Spent today: $52.30\n   - Remaining runway: 8 days at current burn rate\n\n3. **Researcher agent stalled** (confidence: 0.85)\n   - Last run: 6 hours ago\n   - Latest error: API timeout (DeepSeek)\n   - Pending tasks: 5",
  "sources": [
    {
      "title": "Critic verdict #1234",
      "url": "/api/claims/42/verdicts",
      "snippet": "Invalidated due to contradictory findings in high-quality sources."
    },
    {
      "title": "Cost tracking (today)",
      "url": "/api/cost/today",
      "snippet": "Daily cap: $50.00, Current spend: $52.30"
    },
    {
      "title": "Agent runs (researcher, last 24h)",
      "url": "/api/agents/researcher/runs",
      "snippet": "Last started: 2026-05-29T14:22:00Z, error: timeout"
    }
  ],
  "follow_up_queries": [
    "How do I invalidate claim #42?",
    "What's the error rate for researcher?",
    "Can we increase the budget cap?"
  ],
  "confidence": 0.93,
  "executed_at": "2026-05-29T20:35:17Z"
}
```

---

## 4. Query Router: Intent Classification & Dispatch

### 4.1 Intent Taxonomy

The router maps natural language queries to specialized handlers. Classification is rule-based + optional LLM fallback.

```python
class QueryIntent(str, Enum):
    BLOCKERS = "blockers"               # What's blocking progress?
    AGENT_STATUS = "agent_status"       # Who is working? What's the status?
    CLAIM_STRENGTH = "claim_strength"   # Which claims are weakest/strongest?
    BUDGET_QUERY = "budget"             # How much budget? Runway? Cost efficiency?
    PHASE_STATUS = "phase"              # What phase are we in? Progress?
    EVIDENCE_QUALITY = "evidence"       # Slop rate? Audit pass rate?
    AGENT_DETAIL = "agent_detail"       # Details on a specific agent's recent runs
    CLAIM_DETAIL = "claim_detail"       # Evidence chain for a specific claim
    TASKS_PENDING = "tasks"             # What tasks are pending/blocked?
    METRICS_OVERVIEW = "metrics"        # Give me the dashboard summary
    HEALTH_CHECK = "health"             # How is the lab doing overall?
    VERDICTS_RECENT = "verdicts"        # Recent critic decisions?
```

### 4.2 Routing Logic (Pseudocode)

```python
async def route_query(request: QueryRequest, pool: asyncpg.Pool) -> QueryResponse:
    """
    Route natural language query to handler.
    
    1. Extract intent + entities (keywords, claim IDs, agent names)
    2. Dispatch to handler
    3. Format response + fetch sources
    4. Return QueryResponse
    """
    
    # Step 1: Intent classification (rule-based heuristics)
    intent, entities = classify_query(request.query, request.context)
    
    # Step 2: Dispatch to handler
    handler_func = {
        QueryIntent.BLOCKERS: handle_blockers,
        QueryIntent.AGENT_STATUS: handle_agent_status,
        QueryIntent.CLAIM_STRENGTH: handle_claim_strength,
        QueryIntent.BUDGET_QUERY: handle_budget,
        QueryIntent.PHASE_STATUS: handle_phase,
        QueryIntent.EVIDENCE_QUALITY: handle_evidence,
        QueryIntent.AGENT_DETAIL: handle_agent_detail,
        QueryIntent.CLAIM_DETAIL: handle_claim_detail,
        QueryIntent.TASKS_PENDING: handle_tasks,
        QueryIntent.METRICS_OVERVIEW: handle_metrics,
        QueryIntent.HEALTH_CHECK: handle_health,
        QueryIntent.VERDICTS_RECENT: handle_verdicts,
    }[intent]
    
    answer_data = await handler_func(pool, entities)
    
    # Step 3: Format to markdown + gather sources
    answer_md = format_answer(answer_data)
    sources = extract_sources(answer_data)
    follow_ups = suggest_follow_ups(intent, answer_data)
    
    return QueryResponse(
        query=request.query,
        answer=answer_md,
        sources=sources,
        follow_up_queries=follow_ups,
        confidence=answer_data.get("confidence", 0.8),
        executed_at=datetime.now(timezone.utc).isoformat(),
    )
```

### 4.3 Intent Classification Rules

**Rule-based keywords** (checked in order):

| Intent | Keywords |
|--------|----------|
| `BLOCKERS` | "blocker", "stuck", "failing", "error", "problem", "issue", "alert" |
| `AGENT_STATUS` | "agent", "running", "who is", "status", "active" |
| `CLAIM_STRENGTH` | "claim", "hypothesis", "weak", "strong", "confidence", "which" |
| `BUDGET_QUERY` | "budget", "cost", "spend", "money", "runway", "cap" |
| `PHASE_STATUS` | "phase", "progress", "stage", "current" |
| `EVIDENCE_QUALITY` | "slop", "quality", "audit", "pass", "evidence" |
| `AGENT_DETAIL` | "agent" + agent_name (e.g., "researcher", "critic") |
| `CLAIM_DETAIL` | "claim" + number (e.g., "claim 42") |
| `TASKS_PENDING` | "task", "pending", "queue" |
| `METRICS_OVERVIEW` | "overview", "summary", "dashboard", "metrics", "how are we" |
| `HEALTH_CHECK` | "health", "how is", "status", "doing" |
| `VERDICTS_RECENT` | "verdict", "critic", "invalidat", "decision" |

**Fallback**: If no keywords match, use lightweight LLM classification (Claude Haiku as a prompt):
```python
classify_with_llm(query: str) -> (intent: QueryIntent, confidence: float)
```

### 4.4 Entity Extraction

```python
def extract_entities(query: str, context: Optional[dict]) -> dict:
    """Extract: claim IDs, agent names, dates, metrics."""
    entities = {
        "claim_ids": [],
        "agent_names": [],
        "date_range": None,
        "metrics": [],
    }
    
    # Extract claim IDs: "claim 42" or "#42" or just "42"
    claim_pattern = r'(?:claim\s+)?#?(\d{1,4})'
    entities["claim_ids"] = [int(m) for m in re.findall(claim_pattern, query)]
    
    # Extract agent names: "researcher", "critic", "planner", etc.
    valid_agents = ["pi", "planner", "researcher", "knowledge_scout", "evaluation",
                    "critic", "phase_adjudicator", "reflection", "curator"]
    entities["agent_names"] = [a for a in valid_agents if a.lower() in query.lower()]
    
    # Extract date ranges: "last 24h", "today", "this week", "last 7 days"
    date_match = re.search(r'last\s+(\d+)\s+(hours?|days?|weeks?)', query)
    if date_match:
        entities["date_range"] = (int(date_match.group(1)), date_match.group(2))
    
    # Override with context
    if context:
        entities.update(context)
    
    return entities
```

---

## 5. Handler Implementations

Each handler returns a dict with `data`, `confidence`, and metadata for formatting.

### 5.1 Handler: Blockers

```python
async def handle_blockers(pool: asyncpg.Pool, entities: dict) -> dict:
    """Return top 3–5 blockers ranked by severity."""
    
    async with pool.acquire() as conn:
        # 1. Invalidated claims
        invalidated = await conn.fetch("""
            SELECT c.id, c.statement, cv.verdict, cv.reasoning, cv.created_at,
                   COUNT(f.id) as evidence_count
            FROM claims c
            JOIN critic_verdicts cv ON cv.id = c.invalidated_by_verdict_id
            LEFT JOIN findings f ON f.claim_id = c.id
            WHERE c.invalidated_at > NOW() - INTERVAL '48 hours'
            GROUP BY c.id, cv.id
            ORDER BY cv.created_at DESC
            LIMIT 5
        """)
        
        # 2. Agent errors
        agent_errors = await conn.fetch("""
            SELECT agent_name, COUNT(*) as error_count,
                   MAX(error) as latest_error, MAX(started_at) as last_run
            FROM agent_runs
            WHERE started_at > NOW() - INTERVAL '4 hours'
            GROUP BY agent_name
            HAVING COUNT(*) FILTER (WHERE error IS NOT NULL)::FLOAT / COUNT(*) > 0.2
            ORDER BY error_count DESC
            LIMIT 3
        """)
        
        # 3. Budget alerts
        budget_alerts = await conn.fetch("""
            SELECT day, total_cost_usd, cap_reached
            FROM cost_tracking
            WHERE day >= CURRENT_DATE - INTERVAL '1 day'
            AND (cap_reached OR total_cost_usd > (
              SELECT COALESCE(SUM(total_cost_usd) / 7, 0)
              FROM cost_tracking
            ) * 1.5)
            ORDER BY day DESC
        """)
        
        # 4. Stalled tasks
        stalled = await conn.fetch("""
            SELECT id, task_type, description, status,
                   EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, created_at)))/3600 as hours_elapsed
            FROM tasks
            WHERE (status = 'pending' AND created_at < NOW() - INTERVAL '12 hours')
               OR (status = 'running' AND started_at < NOW() - INTERVAL '8 hours')
            ORDER BY created_at ASC
            LIMIT 3
        """)
    
    blockers = []
    
    # Compile blockers
    for row in invalidated:
        blockers.append({
            "type": "invalidated_claim",
            "id": row["id"],
            "title": f"Claim #{row['id']} invalidated",
            "reason": row["reasoning"],
            "age_hours": (datetime.now(timezone.utc) - row["created_at"]).total_seconds() / 3600,
            "severity": 0.95,
        })
    
    for row in agent_errors:
        error_rate = sum(1 for r in agent_errors if r["agent_name"] == row["agent_name"]) / max(1, row["error_count"])
        blockers.append({
            "type": "agent_error",
            "agent": row["agent_name"],
            "error_rate": error_rate,
            "latest_error": row["latest_error"],
            "severity": min(error_rate, 0.9),
        })
    
    # Sort by severity
    blockers.sort(key=lambda x: x["severity"], reverse=True)
    
    return {
        "blockers": blockers[:5],
        "confidence": 0.95 if blockers else 0.5,
        "total_blockers": len(blockers),
    }
```

### 5.2 Handler: Agent Status

```python
async def handle_agent_status(pool: asyncpg.Pool, entities: dict) -> dict:
    """Return active agents, their status, and recent runs."""
    
    async with pool.acquire() as conn:
        agents = await conn.fetch("""
            SELECT DISTINCT agent_name,
              (SELECT COUNT(*) FROM agent_runs ar2
               WHERE ar2.agent_name = ar.agent_name
               AND ar2.started_at > NOW() - INTERVAL '30 minutes') as active_runs_30m,
              (SELECT COUNT(*) FROM agent_runs ar2
               WHERE ar2.agent_name = ar.agent_name
               AND ar2.started_at > NOW() - INTERVAL '24 hours'
               AND ar2.error IS NOT NULL) as errors_24h,
              (SELECT COUNT(*) FROM agent_runs ar2
               WHERE ar2.agent_name = ar.agent_name
               AND ar2.started_at > NOW() - INTERVAL '24 hours') as total_runs_24h,
              (SELECT MAX(started_at) FROM agent_runs ar2
               WHERE ar2.agent_name = ar.agent_name) as last_run_at
            FROM agent_runs ar
            WHERE ar.started_at > NOW() - INTERVAL '24 hours'
            ORDER BY ar.agent_name
        """)
    
    agent_data = []
    for row in agents:
        error_rate = row["errors_24h"] / max(1, row["total_runs_24h"])
        agent_data.append({
            "name": row["agent_name"],
            "active": row["active_runs_30m"] > 0,
            "error_rate": error_rate,
            "runs_today": row["total_runs_24h"],
            "last_run_at": row["last_run_at"],
        })
    
    active = [a for a in agent_data if a["active"]]
    
    return {
        "agents": agent_data,
        "active_count": len(active),
        "total_count": len(agent_data),
        "confidence": 0.98,
    }
```

### 5.3 Handler: Claim Strength (Weakest/Strongest)

```python
async def handle_claim_strength(pool: asyncpg.Pool, entities: dict) -> dict:
    """Return claims ranked by strength (confidence + evidence)."""
    
    async with pool.acquire() as conn:
        claims = await conn.fetch("""
            SELECT c.id, c.statement, c.status, c.confidence,
              COUNT(f.id) as finding_count,
              COUNT(f.id) FILTER (WHERE f.supports_thesis AND f.audit_verdict = 'pass') as supporting_findings,
              COUNT(f.id) FILTER (WHERE NOT f.supports_thesis AND f.audit_verdict = 'pass') as contradicting_findings
            FROM claims c
            LEFT JOIN findings f ON f.claim_id = c.id
            WHERE c.status IN ('proposed', 'tested', 'weakly_supported', 'replicated')
            GROUP BY c.id
            ORDER BY c.confidence DESC, finding_count DESC
        """)
    
    weakest = sorted(claims, key=lambda x: (x["confidence"], -x["finding_count"]))[:5]
    strongest = sorted(claims, key=lambda x: (-x["confidence"], -x["supporting_findings"]))[:5]
    
    return {
        "weakest": [{"id": c["id"], "statement": c["statement"], "confidence": c["confidence"]} for c in weakest],
        "strongest": [{"id": c["id"], "statement": c["statement"], "confidence": c["confidence"]} for c in strongest],
        "confidence": 0.95,
    }
```

### 5.4 Handler: Budget Query

```python
async def handle_budget(pool: asyncpg.Pool, entities: dict) -> dict:
    """Return budget status, spend rate, and runway."""
    
    async with pool.acquire() as conn:
        # Total spend
        spend_data = await conn.fetchrow("""
            SELECT 
              SUM(total_cost_usd) as total_spend,
              COUNT(*) as days_tracked
            FROM cost_tracking
        """)
        
        # This week
        week_data = await conn.fetchrow("""
            SELECT 
              SUM(total_cost_usd) as week_spend,
              AVG(total_cost_usd) as daily_avg,
              MAX(cap_reached) as cap_hit
            FROM cost_tracking
            WHERE day >= CURRENT_DATE - INTERVAL '7 days'
        """)
        
        # Today
        today_data = await conn.fetchrow("""
            SELECT total_cost_usd, cap_reached
            FROM cost_tracking
            WHERE day = CURRENT_DATE
        """)
    
    total = spend_data["total_spend"] or 0.0
    week_spend = week_data["week_spend"] or 0.0
    daily_avg = week_data["daily_avg"] or 0.0
    today_spend = today_data["total_cost_usd"] or 0.0 if today_data else 0.0
    
    # Budget config (from env or hardcoded)
    BUDGET_TOTAL = float(os.environ.get("BUDGET_TOTAL", 500.0))
    BUDGET_DAILY = float(os.environ.get("BUDGET_DAILY", 50.0))
    
    remaining = BUDGET_TOTAL - total
    runway_days = remaining / max(daily_avg, 1.0) if daily_avg > 0 else 999
    utilization_pct = (total / BUDGET_TOTAL) * 100
    
    return {
        "total_budget": BUDGET_TOTAL,
        "total_spend": total,
        "remaining": remaining,
        "utilization_pct": utilization_pct,
        "daily_budget": BUDGET_DAILY,
        "today_spend": today_spend,
        "weekly_avg": daily_avg * 7,
        "runway_days": runway_days,
        "cap_hit_today": today_data and today_data["cap_reached"],
        "confidence": 1.0,
    }
```

### 5.5 Handler: Health Check

```python
async def handle_health(pool: asyncpg.Pool, entities: dict) -> dict:
    """Return overall health: research progress, system health, evidence, budget."""
    
    progress = await compute_research_progress(pool)
    sys_health = await compute_system_health(pool)
    evidence = await compute_evidence_health(pool)
    budget = await handle_budget(pool, {})
    
    overall = (progress * 0.35 + sys_health * 0.35 + evidence * 0.20 + 
               (100 - min(budget["utilization_pct"], 100)) * 0.10)
    
    return {
        "research_progress_pct": progress,
        "system_health_pct": sys_health,
        "evidence_health_pct": evidence,
        "budget_health_pct": 100 - min(budget["utilization_pct"], 100),
        "overall_health_pct": overall,
        "confidence": 0.90,
    }
```

---

## 6. Real-time Updates: WebSocket Integration

### 6.1 Architecture

Extend the existing `/ws/events` stream to push metrics updates and highlights.

```python
# In stream.py
@router.websocket("/ws/command-center")
async def ws_command_center(websocket: WebSocket):
    """
    Stream live command-center updates:
    - Metrics refresh (every 5 min or on data change)
    - Alert broadcasts (new blocker, budget cap, agent stall)
    - Phase transitions, verdict decisions
    """
    await websocket.accept()
    
    try:
        while True:
            # 1. Every 5 minutes, refresh metrics
            metrics = await compute_all_metrics(app.state.pool)
            await websocket.send_json({
                "type": "metrics_refresh",
                "data": metrics,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            
            # 2. Poll event stream for alerts
            alerts = await poll_alerts(app.state.pool)
            for alert in alerts:
                await websocket.send_json({
                    "type": "alert",
                    "severity": alert["severity"],  # "critical", "warning", "info"
                    "message": alert["message"],
                    "data": alert["data"],
                    "timestamp": alert["timestamp"],
                })
            
            await asyncio.sleep(300)  # 5-minute refresh
    
    except WebSocketDisconnect:
        pass
```

### 6.2 Metric Update Messages

**Message types sent to WebSocket**:

```python
class MetricsRefreshMessage(BaseModel):
    type: Literal["metrics_refresh"]
    data: dict  # { "research_progress": 65, "system_health": 88, ... }
    timestamp: str

class AlertMessage(BaseModel):
    type: Literal["alert"]
    severity: Literal["critical", "warning", "info"]
    message: str
    data: dict  # { "claim_id": 42, "agent": "researcher", ... }
    timestamp: str

class VerdictMessage(BaseModel):
    type: Literal["verdict"]
    claim_id: int
    verdict: str  # "invalidated", "weakly_supported", etc.
    reasoning: str
    timestamp: str

class PhaseTransitionMessage(BaseModel):
    type: Literal["phase_transition"]
    from_phase: str
    to_phase: str
    reason: str
    timestamp: str
```

**Client-side integration** (in `web/app/lib/ws.ts`):

```typescript
export class CommandCenterWS {
  private socket: WebSocket | null = null;

  connect(url: string) {
    this.socket = new WebSocket(url);
    this.socket.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      
      if (msg.type === "metrics_refresh") {
        // Update OrganizationScope + health cards
        store.dispatch(updateMetrics(msg.data));
      } else if (msg.type === "alert") {
        // Push notification + highlight in QuerySuggestions
        showAlert(msg.severity, msg.message);
        store.dispatch(addAlert(msg));
      } else if (msg.type === "verdict") {
        // Refresh claim in Theses panel
        store.dispatch(updateClaim(msg.claim_id, { status: msg.verdict }));
      }
    };
  }
}
```

---

## 7. Database Optimization & Indexing

### 7.1 Critical Indexes (Already exist; verify performance)

```sql
-- Existing indexes (from migrations)
CREATE INDEX idx_claims_active ON claims(status) 
  WHERE status IN ('proposed', 'tested', 'weakly_supported', 'replicated');

CREATE INDEX idx_findings_thesis ON findings(claim_id, created_at DESC);

CREATE INDEX idx_agent_runs_recent ON agent_runs(started_at DESC);

-- Additional recommended indexes for query performance
CREATE INDEX idx_findings_audit_verdict ON findings(audit_verdict, created_at DESC);
CREATE INDEX idx_findings_created ON findings(created_at DESC);
CREATE INDEX idx_critic_verdicts_created ON critic_verdicts(created_at DESC);
CREATE INDEX idx_agent_runs_agent_name ON agent_runs(agent_name, started_at DESC);
CREATE INDEX idx_tasks_status_created ON tasks(status, created_at DESC);
CREATE INDEX idx_cost_tracking_day ON cost_tracking(day DESC);

-- Materialized view refresh strategy
CREATE OR REPLACE FUNCTION refresh_slop_rate_by_claim()
RETURNS void AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY slop_rate_by_claim;
END;
$$ LANGUAGE plpgsql;

-- Refresh every 10 minutes (via cron or task scheduler)
-- SELECT cron.schedule('refresh-slop-rate', '*/10 * * * *', 'SELECT refresh_slop_rate_by_claim()');
```

### 7.2 Query Performance Targets

| Query | Expected Time | Data Freshness |
|-------|---------------|-----------------|
| `research_progress` | < 100ms | 5 min |
| `system_health` | < 150ms | real-time |
| `evidence_health` | < 200ms | 5 min |
| `budget_status` | < 50ms | real-time |
| `blockers` (top 5) | < 300ms | 5 min |
| `agent_status` (all agents) | < 100ms | 2 min |
| Full health check | < 1s | 5 min |

**Optimization tactics**:
- Use materialized views for rolling-window calculations (slop_rate_by_claim)
- Batch queries where possible (fetch all metrics in one round-trip)
- Add column stats: `ANALYZE findings; ANALYZE agent_runs;`
- Monitor slow queries: `log_min_duration_statement = 500` in PostgreSQL config

---

## 8. Implementation Roadmap

### Phase 1: Core Query Router (Days 1–2)

1. Add `QueryRequest` and `QueryResponse` models to `boardroom/api/models.py`
2. Create `boardroom/api/query_router.py` with intent classification and entity extraction
3. Implement simple handlers: `handle_blockers`, `handle_budget`, `handle_health`
4. Test with curl/Postman

**Acceptance criteria**:
- `POST /api/command-center/query` responds with valid QueryResponse
- "What are our blockers?" returns top 3 blockers in markdown format
- Response includes sources and follow-up queries
- Confidence score is reasonable (0.7+)

### Phase 2: Metrics Engine (Days 3–4)

1. Implement metric computation functions (research_progress, system_health, evidence_health)
2. Add handlers: `handle_phase`, `handle_evidence`, `handle_agent_status`, `handle_claim_strength`
3. Add database indexes for query optimization
4. Load test with concurrent queries

**Acceptance criteria**:
- All 12 intent types routable and responding
- Metrics update within 5 minutes of data change
- Query latency < 500ms even with 100K+ findings
- Confidence scores track data freshness

### Phase 3: Real-time Adapter (Days 5–6)

1. Extend `/ws/events` → `/ws/command-center` with metrics refresh
2. Add alert polling: blockers, budget threshold breaches, agent stalls
3. Integrate with frontend QuerySuggestions + metrics display
4. Deploy and monitor production behavior

**Acceptance criteria**:
- WebSocket connects and streams metrics every 5 minutes
- Alerts broadcast within 30 seconds of trigger
- Frontend updates without full page reload
- Metrics confidence visible to user

### Phase 4: Polish & Hardening (Days 7)

1. Error handling: malformed queries, missing data, timeout recovery
2. Logging: structured logs for audit trail + debugging
3. Documentation: API contract, query examples, troubleshooting
4. Performance tuning: slow query identification, index optimization

**Acceptance criteria**:
- All error paths tested and logged
- No unhandled exceptions bubble to client
- Documentation is executable (examples work)
- Performance benchmarks met

---

## 9. Deployment & Operations

### 9.1 Environment Variables

```bash
# .env (or systemd EnvironmentFile=)
BUDGET_TOTAL=500.0              # Total budget in USD
BUDGET_DAILY=50.0               # Daily cap
QUERY_TIMEOUT_S=10              # Max query execution time
METRICS_REFRESH_INTERVAL_S=300  # WebSocket metric push interval (5 min)
ALERT_POLL_INTERVAL_S=30        # How often to check for new alerts
CONFIDENCE_LOW_THRESHOLD=0.7    # Below this = low confidence badge
```

### 9.2 Monitoring & Alerts

**Key metrics to monitor**:
- `/api/command-center/query` response time (p50, p95, p99)
- Query router classification accuracy (via manual spot-checks)
- Database slow query log
- WebSocket connection count and message throughput
- Cost per query (API + DB)

**Alerts**:
- Response time > 2s → page ops
- Classification confidence < 0.5 → log for review
- Database CPU > 80% → trigger index refresh
- WebSocket connections > 1000 → scale considerations

### 9.3 Testing Strategy

**Unit tests**:
- Intent classification on 50+ query examples
- Entity extraction (claim IDs, agent names, dates)
- Metric computation (mock data → expected results)

**Integration tests**:
- Full query flow (request → response) with real DB
- Handler output format and source extraction
- WebSocket message serialization

**Load tests**:
- 10 concurrent queries on large dataset (1M findings)
- Verify response time stays < 500ms

---

## 10. Data Sources Reference

| Metric | Source Table(s) | Refresh |
|--------|-----------------|---------|
| Research Progress | findings, tasks, claims | 5 min |
| System Health | agent_runs | 2 min |
| Evidence Health | findings, slop_rate_by_claim | 5 min |
| Budget | cost_tracking | real-time |
| Attention Items | critic_verdicts, agent_runs, tasks, findings | 5 min |
| Agent Status | agent_runs (last 24h) | 2 min |
| Claim Evidence Chain | findings, tasks, claims | 5 min |
| Phase Status | company_state, phase_transitions | real-time |
| Task Queue | tasks (pending, running) | 5 min |

---

## 11. Success Criteria

✅ **By end of implementation**:

1. Users can ask "What are our blockers?" and get back a markdown response with sources in < 1s
2. All 12 query intents are functional and routable
3. Health metrics (research_progress, system_health, evidence_health, budget) update every 5 min
4. WebSocket alerts broadcast within 30s of trigger
5. Zero unhandled exceptions; all errors logged and recoverable
6. Documentation is complete and tested
7. Database queries optimized; latency < 500ms for all handlers

**Expected user impact**:
- Command Center replaces manual dashboard browsing
- Users answer their own questions instead of asking for reports
- Real-time awareness of blockers and phase transitions
- Budget tracking reduces overspend risk

---

## Appendix A: SQL Cheat Sheet

### Frequently Used Queries

```sql
-- All active claims with evidence summary
SELECT 
  c.id, c.statement, c.confidence,
  COUNT(f.id) as finding_count,
  COALESCE(AVG(f.audit_score), 0) as avg_audit_score,
  COALESCE(AVG(f.relevance_score), 0) as avg_relevance
FROM claims c
LEFT JOIN findings f ON f.claim_id = c.id
WHERE c.status IN ('proposed', 'tested', 'weakly_supported', 'replicated')
GROUP BY c.id
ORDER BY c.confidence DESC;

-- Recent agent errors
SELECT 
  id, agent_name, error, started_at
FROM agent_runs
WHERE error IS NOT NULL
AND started_at > NOW() - INTERVAL '24 hours'
ORDER BY started_at DESC
LIMIT 50;

-- Cost summary by day
SELECT 
  day,
  total_cost_usd,
  reasoning_calls, workhorse_calls, fast_calls, code_calls,
  cap_reached
FROM cost_tracking
WHERE day >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY day DESC;
```

---

## Appendix B: Example Queries (User-Facing)

1. **"What are our top 3 blockers?"** → `handle_blockers`
2. **"Show me recent agent errors"** → `handle_blockers` (agent_errors subset)
3. **"Who is working right now?"** → `handle_agent_status` (filtered to active)
4. **"How much budget do we have left?"** → `handle_budget`
5. **"What's our slop rate?"** → `handle_evidence`
6. **"Which claims are weakest?"** → `handle_claim_strength`
7. **"What phase are we in?"** → `handle_phase`
8. **"Why was claim #42 invalidated?"** → `handle_claim_detail` (specific claim)
9. **"Give me a health check"** → `handle_health`
10. **"What tasks are pending?"** → `handle_tasks`

---

## Appendix C: Future Extensions

1. **Trend analysis**: "Is our slop rate improving?" (compare week-over-week)
2. **Predictive alerts**: "At current burn rate, when do we run out of budget?"
3. **Agent recommendations**: "Should we prioritize researcher or critic next?"
4. **Evidence ranking**: "What's the strongest evidence for claim #42?"
5. **Narrative synthesis**: "Summarize the current research thesis in a paragraph"
6. **Custom dashboards**: Users save favorite query sets
7. **Batch queries**: "Run these 5 queries and email me the results"

---

End of specification.
