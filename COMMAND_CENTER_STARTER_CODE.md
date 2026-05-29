# Command Center: Starter Code Examples

**Purpose**: Concrete, copy-paste-ready code to bootstrap the backend implementation.

---

## 1. Models to Add (boardroom/api/models.py)

Add this to the end of the file:

```python
from enum import Enum
from typing import Optional, Literal

# ================================================================
# COMMAND CENTER: Query Request/Response
# ================================================================

class QueryIntent(str, Enum):
    """Natural language query classification."""
    BLOCKERS = "blockers"
    AGENT_STATUS = "agent_status"
    CLAIM_STRENGTH = "claim_strength"
    BUDGET_QUERY = "budget"
    PHASE_STATUS = "phase"
    EVIDENCE_QUALITY = "evidence"
    AGENT_DETAIL = "agent_detail"
    CLAIM_DETAIL = "claim_detail"
    TASKS_PENDING = "tasks"
    METRICS_OVERVIEW = "metrics"
    HEALTH_CHECK = "health"
    VERDICTS_RECENT = "verdicts"


class QuerySource(BaseModel):
    """Citation for a claim in the response."""
    title: str
    url: Optional[str] = None
    snippet: Optional[str] = None


class QueryRequest(BaseModel):
    """User's natural language query."""
    query: str
    context: Optional[dict] = None


class QueryResponse(BaseModel):
    """Structured response with cited data."""
    query: str
    answer: str
    sources: list[QuerySource]
    follow_up_queries: list[str]
    confidence: float
    executed_at: str
```

---

## 2. Query Router Skeleton (boardroom/api/query_router.py)

Create this new file:

```python
"""
LabFoundry Command Center query router.

Classify natural language queries → dispatch to handlers → format responses.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, Request

from boardroom.api.models import (
    QueryIntent, QueryRequest, QueryResponse, QuerySource
)

log = logging.getLogger("command_center")
router = APIRouter(prefix="/api/command-center", tags=["command-center"])


class QueryClassifier:
    """Classify queries and extract entities."""
    
    INTENT_KEYWORDS = {
        QueryIntent.BLOCKERS: [
            "blocker", "stuck", "failing", "error", "problem", "issue", 
            "alert", "what's wrong", "what went wrong", "wrong"
        ],
        QueryIntent.HEALTH_CHECK: [
            "health", "how is", "doing", "overall", "status", "how are we"
        ],
        QueryIntent.BUDGET_QUERY: [
            "budget", "cost", "spend", "money", "runway", "cap", "how much"
        ],
        QueryIntent.AGENT_STATUS: [
            "agent", "running", "who is", "status", "active", "working"
        ],
    }
    
    @staticmethod
    def classify(query: str, context: Optional[dict] = None) -> tuple[QueryIntent, dict]:
        """Classify intent and extract entities."""
        query_lower = query.lower()
        
        # 1. Classify by keywords
        intent = QueryIntent.METRICS_OVERVIEW  # default
        for candidate, keywords in QueryClassifier.INTENT_KEYWORDS.items():
            if any(kw in query_lower for kw in keywords):
                intent = candidate
                break
        
        # 2. Extract entities
        entities = QueryClassifier.extract_entities(query)
        
        # 3. Apply context overrides
        if context:
            entities.update(context)
        
        return intent, entities
    
    @staticmethod
    def extract_entities(query: str) -> dict:
        """Extract claim IDs, agent names, dates."""
        entities = {
            "claim_ids": [],
            "agent_names": [],
            "date_range": None,
            "request_type": "summary",
        }
        
        # Claim IDs: "claim 42" or "#42"
        claim_ids = re.findall(r'(?:claim\s+)?#?(\d{1,4})', query)
        entities["claim_ids"] = [int(cid) for cid in claim_ids]
        
        # Agent names
        valid_agents = ["pi", "planner", "researcher", "knowledge_scout",
                        "evaluation", "critic", "phase_adjudicator", 
                        "reflection", "curator"]
        entities["agent_names"] = [a for a in valid_agents 
                                   if a.lower() in query.lower()]
        
        # Date ranges
        date_match = re.search(r'last\s+(\d+)\s+(hours?|days?|weeks?)', query)
        if date_match:
            entities["date_range"] = (int(date_match.group(1)), date_match.group(2))
        elif "today" in query.lower():
            entities["date_range"] = (1, "days")
        elif "this week" in query.lower():
            entities["date_range"] = (7, "days")
        
        return entities


# ================================================================
# HANDLERS (stub implementations)
# ================================================================

async def handler_blockers(pool: asyncpg.Pool, entities: dict) -> dict:
    """Return top blockers."""
    async with pool.acquire() as conn:
        # Invalidated claims (last 48h)
        invalidated = await conn.fetch("""
            SELECT c.id, c.statement, cv.reasoning, cv.created_at
            FROM claims c
            JOIN critic_verdicts cv ON cv.id = c.invalidated_by_verdict_id
            WHERE c.invalidated_at > NOW() - INTERVAL '48 hours'
            ORDER BY cv.created_at DESC
            LIMIT 5
        """)
    
    blockers = []
    for row in invalidated:
        blockers.append({
            "type": "invalidated_claim",
            "id": row["id"],
            "statement": row["statement"],
            "reason": row["reasoning"],
            "age_hours": (datetime.now(timezone.utc) - row["created_at"]).total_seconds() / 3600,
        })
    
    return {
        "blockers": blockers,
        "confidence": 0.95 if blockers else 0.5,
    }


async def handler_budget(pool: asyncpg.Pool, entities: dict) -> dict:
    """Return budget status."""
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT SUM(total_cost_usd) FROM cost_tracking"
        )
        today = await conn.fetchrow(
            "SELECT total_cost_usd FROM cost_tracking WHERE day = CURRENT_DATE"
        )
    
    BUDGET_TOTAL = 500.0  # Configurable
    remaining = BUDGET_TOTAL - (total or 0)
    
    return {
        "total_budget": BUDGET_TOTAL,
        "total_spend": total or 0,
        "remaining": max(0, remaining),
        "utilization_pct": min(100, ((total or 0) / BUDGET_TOTAL) * 100),
        "today_spend": today["total_cost_usd"] if today else 0,
        "confidence": 1.0,
    }


async def handler_health_check(pool: asyncpg.Pool, entities: dict) -> dict:
    """Return overall lab health."""
    async with pool.acquire() as conn:
        # Research progress: findings this week
        findings = await conn.fetchval(
            "SELECT COUNT(*) FROM findings WHERE created_at > NOW() - INTERVAL '7 days'"
        )
        # Task completion
        tasks_completed = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status = 'completed' AND created_at > NOW() - INTERVAL '7 days'"
        )
        tasks_total = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE created_at > NOW() - INTERVAL '7 days'"
        )
        # Agent error rate
        agent_errors = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_runs WHERE error IS NOT NULL AND started_at > NOW() - INTERVAL '24 hours'"
        )
        agent_total = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_runs WHERE started_at > NOW() - INTERVAL '24 hours'"
        )
    
    # Simple scoring
    research_progress = min(100, (findings or 0) / 10.0)
    task_completion = ((tasks_completed or 0) / max(1, tasks_total or 1)) * 100
    system_health = 100 - min(100, ((agent_errors or 0) / max(1, agent_total or 1)) * 100)
    
    overall = (research_progress * 0.4 + task_completion * 0.3 + system_health * 0.3)
    
    return {
        "research_progress": research_progress,
        "task_completion": task_completion,
        "system_health": system_health,
        "overall_health": overall,
        "confidence": 0.8,
    }


async def handler_agent_status(pool: asyncpg.Pool, entities: dict) -> dict:
    """Return active agents and their status."""
    async with pool.acquire() as conn:
        agents = await conn.fetch("""
            SELECT DISTINCT agent_name,
              COUNT(*) as runs_24h,
              COUNT(*) FILTER (WHERE error IS NOT NULL) as errors,
              MAX(started_at) as last_run_at
            FROM agent_runs
            WHERE started_at > NOW() - INTERVAL '24 hours'
            GROUP BY agent_name
            ORDER BY agent_name
        """)
    
    agent_data = []
    for row in agents:
        error_rate = row["errors"] / max(1, row["runs_24h"])
        agent_data.append({
            "name": row["agent_name"],
            "runs_today": row["runs_24h"],
            "error_rate": error_rate,
            "last_run": row["last_run_at"].isoformat() if row["last_run_at"] else None,
        })
    
    return {
        "agents": agent_data,
        "active_count": len([a for a in agent_data if a["error_rate"] < 0.2]),
        "total_count": len(agent_data),
        "confidence": 0.9,
    }


# Dispatcher
HANDLERS = {
    QueryIntent.BLOCKERS: handler_blockers,
    QueryIntent.BUDGET_QUERY: handler_budget,
    QueryIntent.HEALTH_CHECK: handler_health_check,
    QueryIntent.AGENT_STATUS: handler_agent_status,
}


# ================================================================
# RESPONSE FORMATTING
# ================================================================

def format_blockers_response(data: dict, query: str) -> QueryResponse:
    """Format blockers to markdown response."""
    blockers = data.get("blockers", [])
    
    lines = ["## Top Blockers\n"]
    for i, b in enumerate(blockers, 1):
        lines.append(f"{i}. **Claim #{b['id']} invalidated**")
        lines.append(f"   - Reason: {b['reason']}")
        lines.append(f"   - Age: {b['age_hours']:.1f} hours ago\n")
    
    if not blockers:
        lines.append("No blockers detected. Great job! 🎉")
    
    sources = [
        QuerySource(
            title="Critic verdicts (last 48h)",
            url="/api/verdicts",
            snippet=f"{len(blockers)} claims invalidated"
        )
    ]
    
    return QueryResponse(
        query=query,
        answer="\n".join(lines),
        sources=sources,
        follow_up_queries=[
            "Why was claim #{} invalidated?".format(blockers[0]["id"]) if blockers else "What's our health?",
            "How do I fix these?",
        ],
        confidence=data.get("confidence", 0.9),
        executed_at=datetime.now(timezone.utc).isoformat(),
    )


def format_budget_response(data: dict, query: str) -> QueryResponse:
    """Format budget to markdown response."""
    answer = f"""## Budget Status

- **Total Budget**: ${data['total_budget']:.2f}
- **Total Spent**: ${data['total_spend']:.2f}
- **Remaining**: ${data['remaining']:.2f}
- **Utilization**: {data['utilization_pct']:.1f}%
- **Today's Spend**: ${data['today_spend']:.2f}

Runway: {data['remaining'] / max(1, data['today_spend'])} days at current rate"""
    
    return QueryResponse(
        query=query,
        answer=answer,
        sources=[
            QuerySource(
                title="Cost tracking",
                url="/api/cost",
                snippet=f"Total: ${data['total_spend']:.2f}, Remaining: ${data['remaining']:.2f}"
            )
        ],
        follow_up_queries=["Can we increase the cap?", "What's the cost per finding?"],
        confidence=data.get("confidence", 1.0),
        executed_at=datetime.now(timezone.utc).isoformat(),
    )


def format_health_response(data: dict, query: str) -> QueryResponse:
    """Format health check to markdown response."""
    overall = data.get("overall_health", 0)
    color = "🔴" if overall < 50 else "🟡" if overall < 75 else "🟢"
    
    answer = f"""{color} ## Lab Health

- **Research Progress**: {data['research_progress']:.1f}%
- **Task Completion**: {data['task_completion']:.1f}%
- **System Health**: {data['system_health']:.1f}%
- **Overall**: {overall:.1f}%"""
    
    return QueryResponse(
        query=query,
        answer=answer,
        sources=[],
        follow_up_queries=["What's blocking us?", "Agent status?", "Budget?"],
        confidence=data.get("confidence", 0.8),
        executed_at=datetime.now(timezone.utc).isoformat(),
    )


def format_agent_status_response(data: dict, query: str) -> QueryResponse:
    """Format agent status to markdown response."""
    agents = data.get("agents", [])
    
    lines = ["## Agent Status\n"]
    for agent in agents:
        error_icon = "❌" if agent["error_rate"] > 0.1 else "✅"
        lines.append(f"{error_icon} **{agent['name']}**")
        lines.append(f"   - Runs (24h): {agent['runs_today']}")
        lines.append(f"   - Error rate: {agent['error_rate']*100:.0f}%")
        lines.append(f"   - Last run: {agent['last_run'] or 'never'}\n")
    
    return QueryResponse(
        query=query,
        answer="\n".join(lines),
        sources=[],
        follow_up_queries=["Which agent is having errors?", "What's the latest error?"],
        confidence=data.get("confidence", 0.9),
        executed_at=datetime.now(timezone.utc).isoformat(),
    )


# ================================================================
# MAIN ENDPOINT
# ================================================================

@router.post("/query")
async def query(request: QueryRequest, http_request: Request) -> QueryResponse:
    """POST /api/command-center/query"""
    pool: asyncpg.Pool = http_request.app.state.pool
    
    log.info(f"Query: {request.query}")
    
    try:
        # 1. Classify intent
        intent, entities = QueryClassifier.classify(request.query, request.context)
        log.debug(f"Intent: {intent}, Entities: {entities}")
        
        # 2. Dispatch to handler
        if intent not in HANDLERS:
            return QueryResponse(
                query=request.query,
                answer="❌ I don't understand that query. Try: 'What are our blockers?'",
                sources=[],
                follow_up_queries=["What are our blockers?", "How is the lab doing?"],
                confidence=0.0,
                executed_at=datetime.now(timezone.utc).isoformat(),
            )
        
        handler = HANDLERS[intent]
        raw_data = await handler(pool, entities)
        
        # 3. Format response
        if intent == QueryIntent.BLOCKERS:
            response = format_blockers_response(raw_data, request.query)
        elif intent == QueryIntent.BUDGET_QUERY:
            response = format_budget_response(raw_data, request.query)
        elif intent == QueryIntent.HEALTH_CHECK:
            response = format_health_response(raw_data, request.query)
        elif intent == QueryIntent.AGENT_STATUS:
            response = format_agent_status_response(raw_data, request.query)
        else:
            response = QueryResponse(
                query=request.query,
                answer=str(raw_data),
                sources=[],
                follow_up_queries=[],
                confidence=raw_data.get("confidence", 0.5),
                executed_at=datetime.now(timezone.utc).isoformat(),
            )
        
        log.info(f"Query resolved with confidence {response.confidence}")
        return response
    
    except Exception as e:
        log.exception(f"Query failed: {e}")
        return QueryResponse(
            query=request.query,
            answer=f"❌ Query failed: {str(e)}",
            sources=[],
            follow_up_queries=["What are our blockers?", "How is the lab doing?"],
            confidence=0.0,
            executed_at=datetime.now(timezone.utc).isoformat(),
        )


@router.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {"ok": True, "service": "command-center"}
```

---

## 3. Update main.py

Add these lines to `boardroom/api/main.py`:

```python
# Near the top with other imports
from boardroom.api import query_router

# In the app setup section (after line 89, with other routers):
app.include_router(query_router.router)
```

Complete diff:
```python
# Line 34-35: Add import
from boardroom.api import snapshot, stream, bench, debug, trace, query_router

# Line 89-94: Add to app setup
app.include_router(snapshot.router)
app.include_router(stream.router)
app.include_router(bench.router)
app.include_router(debug.router)
app.include_router(trace.router)
app.include_router(query_router.router)  # ← ADD THIS LINE
```

---

## 4. Test Script (test_query_router.sh)

Create and run to test locally:

```bash
#!/bin/bash

BASE_URL="http://localhost:8503/api/command-center"

echo "Testing Command Center API..."

# Test 1: Health check
echo -e "\n=== Test 1: Health Endpoint ==="
curl -s "$BASE_URL/health" | jq .

# Test 2: What are our blockers?
echo -e "\n=== Test 2: Blockers Query ==="
curl -s -X POST "$BASE_URL/query" \
  -H "Content-Type: application/json" \
  -d '{"query": "What are our blockers?"}' | jq .

# Test 3: Budget query
echo -e "\n=== Test 3: Budget Query ==="
curl -s -X POST "$BASE_URL/query" \
  -H "Content-Type: application/json" \
  -d '{"query": "How much budget do we have left?"}' | jq .

# Test 4: Health check query
echo -e "\n=== Test 4: Health Check Query ==="
curl -s -X POST "$BASE_URL/query" \
  -H "Content-Type: application/json" \
  -d '{"query": "How is the lab doing?"}' | jq .

# Test 5: Agent status
echo -e "\n=== Test 5: Agent Status Query ==="
curl -s -X POST "$BASE_URL/query" \
  -H "Content-Type: application/json" \
  -d '{"query": "Who is working right now?"}' | jq .

echo -e "\n=== Done ==="
```

Run with:
```bash
chmod +x test_query_router.sh
./test_query_router.sh
```

---

## 5. Next Steps (In Order)

1. **Add models** → Edit `boardroom/api/models.py`, add the QueryIntent/QueryRequest/QueryResponse classes
2. **Create router** → Create `boardroom/api/query_router.py` with the code above
3. **Update main** → Edit `boardroom/api/main.py` to include the router
4. **Start API** → `uvicorn boardroom.api.main:app --reload --port 8503`
5. **Test** → Run `test_query_router.sh` or use curl
6. **Iterate** → Add more handlers as needed

---

## 6. Common Errors & Solutions

### Error: "No module named 'boardroom.api.query_router'"
**Solution**: Make sure you created the file and it's in the right place: `boardroom/api/query_router.py`

### Error: "AttributeError: 'QueryIntent' has no attribute 'BLOCKERS'"
**Solution**: You didn't add the `QueryIntent` enum to `models.py`. Make sure it's there.

### Error: "Database query returned no rows"
**Solution**: That's fine! The code handles empty results gracefully. Check your test data.

### Error: "Query returned confidence 0.0"
**Solution**: Check the logs for the actual error. The handler might have crashed.

---

## 7. Performance Tips

1. **Batch queries**: Instead of multiple round-trips to DB, fetch all needed data in one query
2. **Use indexes**: Ensure indexes exist (see COMMAND_CENTER_DATA_SCHEMA.md)
3. **Cache results**: For metrics that don't change frequently, cache for 5 minutes
4. **Monitor latency**: Add timing logs to track query performance

```python
import time
start = time.time()
# ... do work ...
elapsed = time.time() - start
log.info(f"Query took {elapsed:.3f}s")
```

---

End of starter code. Ready to implement!
