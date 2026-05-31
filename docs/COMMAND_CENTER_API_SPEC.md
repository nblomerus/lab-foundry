# Command Center API & Component Specification

This document provides the exact interfaces, props, and contracts for all new command center components.

---

## Frontend Type Definitions

### QuerySource
```typescript
export interface QuerySource {
  type: "claim" | "finding" | "event" | "metric" | "agent" | "dissent";
  id: number;
  reference: string;
  confidence?: number;
}
```

**Fields:**
- `type`: Category of the source (must be one of the 6 types listed)
- `id`: Database ID for the referenced object
- `reference`: Human-readable description (e.g., "Finding #42: Market size data shows...")
- `confidence`: Optional confidence score 0.0-1.0 if applicable

### QueryResponse
```typescript
export interface QueryResponse {
  query: string;
  answer: string;
  sources: QuerySource[];
  follow_up_queries: string[];
  confidence: number;
  executed_at: string;
  processing_time_ms?: number;
  model_used?: string;
}
```

**Fields:**
- `query`: Echo of the original query string
- `answer`: Markdown-formatted response (supports `**bold**`, `_italic_`, `\n` newlines)
- `sources`: Array of citations supporting the answer
- `follow_up_queries`: Array of 2-3 suggested follow-up questions
- `confidence`: Overall confidence in answer (0.0-1.0)
- `executed_at`: ISO8601 timestamp when query was processed
- `processing_time_ms`: Optional time in milliseconds to generate response
- `model_used`: Optional model name (e.g., "claude-opus", "claude-3.5-sonnet")

---

## Component Props & Contracts

### CommandQuery Component

**File:** `web/app/components/CommandQuery.tsx`

**Props Interface:**
```typescript
export interface CommandQueryProps {
  snap: Snapshot;
  onSuggestionClick?: (query: string) => void;
}
```

**Usage:**
```tsx
<CommandQuery
  snap={snapshot}
  onSuggestionClick={(q) => console.log(`User clicked: ${q}`)}
/>
```

**Rendering Details:**
- Card layout with responsive max-height (max-h-[600px])
- Grid width: `lg:col-span-5` (takes 5 of 12 columns on large screens)
- Input field: rounded-2xl, blue focus ring, placeholder text
- Submit button: blue background, send icon, disabled when loading/empty
- Loading state: spinner + "Analyzing org state..." message
- Error state: red alert box with icon and error text
- Response display: markdown rendering, expandable sources, follow-up buttons
- Empty state: brain icon + helpful text when no response yet

**Keyboard Interaction:**
- Enter key submits query (Shift+Enter does not submit)
- Esc clears error state (optional enhancement)

**API Call:**
```typescript
fetch("/api/query", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ query: "user input" })
})
```

**Expected Response:** QueryResponse type

**Error Handling:**
- Network errors display as "Query failed: [error message]"
- HTTP errors (non-200) show detail.message or generic error
- Graceful fallback if response is malformed

---

### QuerySuggestions Component

**File:** `web/app/components/QuerySuggestions.tsx`

**Props Interface:**
```typescript
export interface QuerySuggestionsProps {
  snap: Snapshot;
  onSelectQuery: (query: string) => void;
}
```

**Usage:**
```tsx
<QuerySuggestions
  snap={snapshot}
  onSelectQuery={(q) => setSelectedQuery(q)}
/>
```

**Rendering Details:**
- Card layout with responsive scrolling (max-h-[200px] overflow-y-auto)
- Grid width: `lg:col-span-5` (paired next to CommandQuery)
- Title: "Suggested queries" with Lightbulb icon
- Subtitle: "Based on current org state"
- Each suggestion is a button with:
  - Full-width layout
  - Query text on left
  - Reason badge on right (color-coded)
  - Hover state: border blue-300, bg blue-50
  - Transition effect
- Max 6 suggestions shown
- If no suggestions: "No suggestions at this time"

**Smart Suggestion Logic:**

1. **Always suggest:**
   - "What's our current progress?" (blue)

2. **Claims-based:**
   - If `active_claim_count > 5`: "What are our highest confidence claims?" (blue)
   - If `invalidated_claim_count > 0`: "Why were our killed claims rejected?" (amber)

3. **Task-based:**
   - If `pending_tasks > 10`: "What's our priority task queue?" (amber)

4. **Failure-based:**
   - If `failed_runs_today > 0`: "What failed today and why?" (red)
   - If `slop_today > 0`: "What audit slop was flagged today?" (red)

5. **Dissent-based:**
   - If `dissent.length > 0`: "What contradicts our thesis?" (amber)

6. **Findings-based:**
   - If `findings_today > 0`: "What high-signal findings did we get today?" (green)

7. **Agent-based:**
   - If any `org_roles.running_count > 0`: "Which agents are running right now?" (green)

8. **Deadline-based:**
   - If `days_remaining < 5`: "Are we on track to meet our deadline?" (amber)

9. **Budget-based:**
   - If `cost.cap_reached`: "Have we hit our budget limit?" (red)
   - Else: "How much have we spent today?" (default)

10. **Phase-specific:**
    - exploration: "What's the landscape of candidate niches?" (blue)
    - convergence: "Which claims have the strongest support?" (blue)
    - commitment: "What evidence supports our thesis?" (blue)
    - execution: "How is the customer validation progressing?" (green)

**Return value:** Up to 6 suggestions, ordered by relevance

**Callback behavior:**
- On click: calls `onSelectQuery(queryString)`
- Parent should submit the query (in this case, CommandQuery does auto-submit)

---

### OrganizationScope Component

**File:** `web/app/components/OrganizationScope.tsx`

**Props Interface:**
```typescript
export interface OrganizationScopeProps {
  state: CompanyState;
  stats: Stats;
  orgRoles: OrgRole[];
}
```

**Usage:**
```tsx
<OrganizationScope
  state={snapshot.state}
  stats={snapshot.stats}
  orgRoles={snapshot.org_roles}
/>
```

**Rendering Details:**
- Full-width banner below header
- Blue-tinted design (blue-50/50 background, blue-200 border)
- Responsive: stacked on mobile, horizontal on lg+ screens
- Left side:
  - Globe icon in blue box
  - h3: "Organization-wide view"
  - Subtitle: "Complete state across all research teams and agents"
- Right side (flexbox with 3 items):
  - "X agents active" (gray text with Users icon)
  - "Y runs today" (blue badge)
  - Phase badge with smart coloring:
    - Red if `failed_runs_today > findings_today` (unhealthy)
    - Amber if `failed_runs_today > 0` (caution)
    - Green otherwise (healthy)

**Calculations:**
```typescript
activeRoles = orgRoles.filter(r => r.running_count > 0).length
totalRunsToday = orgRoles.reduce((sum, r) => sum + r.runs_today, 0)
```

**Non-interactive:** No click handlers, display only

---

## Page Layout Changes

### Before (original page.tsx)
```typescript
<div className="space-y-6">
  <Header />
  <StatsGrid />
  <section className="grid grid-cols-1 gap-6 lg:grid-cols-12">
    <WorkflowLoop /> {/* various sizes */}
    <DissentPanel /> 
    <ClaimsPanel />
    <TaskQueuePanel />
    <FindingsPanel />
    <TelemetryPanel />
    <SkillPanel />
    <EventStream />
  </section>
</div>
```

### After (new page.tsx)
```typescript
<div className="space-y-6">
  <Header />
  <OrganizationScope /> {/* NEW */}
  <StatsGrid />
  <section className="grid grid-cols-1 gap-6 lg:grid-cols-12">
    {/* Left side: existing panels */}
    <WorkflowLoop /> 
    <DissentPanel /> 
    <ClaimsPanel />
    <TaskQueuePanel />
    <FindingsPanel />
    
    {/* Right side: NEW command interface (5 cols) */}
    <CommandQuery />
    <QuerySuggestions />
    
    {/* Bottom: existing panels */}
    <TelemetryPanel />
    <SkillPanel />
    <EventStream />
  </section>
</div>
```

**Grid Changes:**
- CommandQuery: `lg:col-span-5`
- QuerySuggestions: `lg:col-span-5`
- These stack on top of each other on lg+ (right side)
- On mobile (md and below): full width, standard flow

---

## HTTP API Contract

### Endpoint: POST /api/query

**Request Body:**
```typescript
{
  query: string;
  context_window?: number;      // optional, default ~50
  include_sources?: boolean;    // optional, default true
}
```

**Response (200 OK):**
```typescript
{
  query: string;                      // echo input
  answer: string;                     // markdown formatted answer
  sources: QuerySource[];             // citations
  follow_up_queries: string[];        // 2-3 suggestions
  confidence: number;                 // 0.0-1.0
  executed_at: string;                // ISO8601 timestamp
  processing_time_ms?: number;
  model_used?: string;
}
```

**Error Responses:**

**400 Bad Request:**
```json
{
  "detail": "Query string cannot be empty"
}
```

**500 Internal Server Error:**
```json
{
  "detail": "Failed to process query: [reason]"
}
```

**Constraints:**
- Query string must be non-empty (trim whitespace)
- Query should be < 1000 characters
- Response timeout recommended: 10 seconds
- Context window default: 50 items max from each category
- Include_sources: when false, sources array should be empty but still present

---

## Data Flow Diagram

```
User input in CommandQuery
  |
  v
fetch("/api/query", { query })
  |
  v
Backend gathers Snapshot context
  |
  +-- state (CompanyState)
  +-- active_claims (List[Claim])
  +-- invalidated_claims (List[Claim])
  +-- recent_findings (List[Finding])
  +-- dissent (List[DissentItem])
  +-- org_roles (List[OrgRole])
  +-- recent_runs (List[AgentRun])
  +-- stats (Stats)
  +-- cost (Cost)
  +-- telemetry (List[TelemetryDay])
  |
  v
Route to Analyzer (StateAnalyzer, ClaimAnalyzer, etc.)
  |
  v
Generate answer with Claude API
  |
  v
Extract sources from context
  |
  v
Generate follow-up questions
  |
  v
Calculate confidence
  |
  v
Return QueryResponse
  |
  v
Display in CommandQuery component
  |
  +-- Render answer as markdown
  +-- Show sources in expandable section
  +-- Display follow-up suggestion buttons
  |
  v
User can click follow-up or type new query
```

---

## Implementation Priority

### Phase 1: MVP (1-2 days)
- [x] Create CommandQuery, QuerySuggestions, OrganizationScope components
- [x] Update page layout
- [x] Add TypeScript types
- [ ] Create `/api/query` endpoint (basic implementation)
- [ ] Simple query handler that returns hardcoded response for testing

### Phase 2: Core (3-5 days)
- [ ] Implement real query handler with Claude API
- [ ] Build query intent detection
- [ ] Implement source citation logic
- [ ] Add follow-up suggestion generation
- [ ] Implement all 6+ query type analyzers

### Phase 3: Polish (2-3 days)
- [ ] Add query caching
- [ ] Optimize context window efficiency
- [ ] Add query history
- [ ] Implement analytics
- [ ] Performance testing

### Phase 4: Enhancement (ongoing)
- [ ] Voice input
- [ ] Query templates
- [ ] Role-based suggestions
- [ ] Slack integration
- [ ] Export to markdown/PDF

---

## Testing Guidance

### Unit Tests (Components)
```typescript
// CommandQuery.test.tsx
- Renders input field and button
- Displays loading state when fetching
- Shows error message on failure
- Displays response with answer, sources, follow-ups
- Submit on Enter key
- Clear input after submit

// QuerySuggestions.test.tsx
- Generates correct suggestions for org state
- Limits to max 6 suggestions
- Color-codes badges correctly
- Calls onSelectQuery when button clicked

// OrganizationScope.test.tsx
- Calculates active agent count
- Sums runs today
- Colors phase badge based on health
```

### Integration Tests
```typescript
// Full flow
- User types "What are our blockers?"
- Component fetches /api/query
- Response displays correctly
- Follow-up buttons work
- Click follow-up re-submits query
```

### API Tests
```python
# test_query_endpoint.py
- POST /api/query with valid query returns 200
- Response has all required fields
- Sources are correctly formatted
- Follow-ups are reasonable suggestions
- Confidence is 0.0-1.0
- Invalid queries return 400
```

---

## Common Query Examples & Expected Structure

### Query: "What's our current progress?"
```
Answer: "Organization is in convergence phase (day 5 of 30)..."
Sources:
  - type: "metric", id: 1, reference: "Current phase: convergence"
  - type: "metric", id: 2, reference: "Active claims: 8"
  - type: "metric", id: 3, reference: "High-signal findings: 12 today"
Follow-ups:
  - "Which claims have the strongest support?"
  - "What contradicts our thesis?"
  - "What blockers do we face?"
Confidence: 0.95
```

### Query: "What failed today?"
```
Answer: "3 agents failed today: HN_SCRAPER (timeout)..."
Sources:
  - type: "event", id: 156, reference: "HN_SCRAPER failed: timeout after 30s"
  - type: "event", id: 157, reference: "CLAIM_EVALUATOR failed: schema error"
  - type: "event", id: 159, reference: "FINDING_AUDITOR failed: model error"
Follow-ups:
  - "How should we fix the HN_SCRAPER timeout?"
  - "What's causing schema validation errors?"
  - "Can we reduce token counts in the auditor?"
Confidence: 0.92
```

### Query: "Which agents are running?"
```
Answer: "5 out of 8 agents active: Researcher (2), Claim Adjudicator (1)..."
Sources:
  - type: "agent", id: "researcher", reference: "Researcher: 2 running"
  - type: "agent", id: "adjudicator", reference: "Claim Adjudicator: 1 running"
  - type: "agent", id: "critic", reference: "Critic: 1 running"
  - type: "agent", id: "auditor", reference: "Auditor: 1 running"
Follow-ups:
  - "What are the researchers working on?"
  - "Show me task queue for pending research"
  - "Which agent is struggling most?"
Confidence: 0.99
```

---

## Configuration & Environment

### Frontend Configuration
- Base API URL: `/api` (relative, proxied by Next.js)
- Fetch timeout: 10 seconds (recommended)
- Loading state debounce: 200ms (recommended)

### Backend Configuration
- Claude API integration: Use existing api client
- Context window: Default 50 items per category
- Timeout: 8-10 seconds per query
- Cache: Optional, beneficial for common patterns
- Model: claude-3.5-sonnet (or appropriate tier)

---

## Notes for Implementation

1. **Markdown Formatting:** Answer supports `**bold**`, `_italic_`, `\n` newlines. No other markdown (lists, tables) needed for MVP.

2. **Source Colors:** Map source types to badge colors:
   - claim → blue
   - finding → blue
   - metric → green
   - dissent → amber
   - event → default
   - agent → blue

3. **Confidence Interpretation:**
   - 0.9+: Very confident
   - 0.7-0.9: Confident
   - 0.5-0.7: Moderate
   - <0.5: Low confidence (flag with amber color)

4. **Follow-up Generation:** Should be actionable, specific, and flow naturally from the answer.

5. **Source Reference:** Should be concise but complete (max 100 chars ideally).

6. **Error Messages:** Be helpful and suggest next steps (e.g., "Try asking about a specific claim").

7. **Query Intent:** Use NLU to detect if query is about progress, blockers, agents, claims, findings, or budget.

8. **Context Efficiency:** Limit snapshot data passed to query handler:
   - Recent 20 findings (not all)
   - Recent 5 dissent items
   - Recent 5 failed runs
   - All active claims (limit to 20?)
   - Current org_roles (full list)
   - Latest telemetry (last 7 days)

---

## Troubleshooting

**"Query failed: 404 Not Found"**
- Backend hasn't implemented `/api/query` endpoint yet
- Check FastAPI app has the route registered

**"Query failed: [timeout]"**
- Claude API call is taking too long
- Check network connectivity
- Consider reducing context window

**No suggestions showing**
- Org state may not match suggestion thresholds
- Add fallback suggestions (status, progress always shown)
- Check snap data is populated

**Sources not displaying**
- Ensure QueryResponse.sources is populated
- Check source type matches one of the 6 allowed values
- Verify reference is non-empty string

**Markdown not rendering**
- Answer component does basic string replacement (** for bold, _ for italic, \n for breaks)
- Complex markdown (lists, tables) not supported yet
- Keep answers simple and prose-based

---

This specification provides everything needed to implement the backend `/api/query` endpoint and verify the frontend components work correctly.
