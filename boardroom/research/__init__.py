"""
Self-hosted research stack.

`fetcher.web_fetch` is the single rung of the retrieval ladder we run today:
httpx GET → trafilatura extraction → Postgres-backed cache, with per-domain
politeness. `loop.run_research_task` orchestrates the agentic researcher
(plan → extract → experiment → synthesize → gap_check), and `experiments/`
holds the "do something" operations the loop can dispatch.
"""
