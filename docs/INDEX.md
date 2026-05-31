# Design docs

Reading order for understanding LabFoundry's design. These are working design
documents (dense, opinionated); the code is the source of truth where they differ.

## Start here
1. [REAL_LAB_OPERATING_MODEL.md](REAL_LAB_OPERATING_MODEL.md) — the lab operating model: PI decomposition, the deliberative planner, the novelty/quality gate + peer-review panel, the shared termination model, and the expectation→outcome→lessons loop.
2. [AGENT_OPERATING_MODEL.md](AGENT_OPERATING_MODEL.md) — the two interaction planes (ungated knowledge vs gated delegation) and the common multi-step agent contract.
3. [AGENT_INTERACTION_SCOPE.md](AGENT_INTERACTION_SCOPE.md) — agent-to-agent delegation: the `agent.request`/`agent.reply` bus, the allow-list, and the guardrails.

## The knowledge layer (the Library + Mimir)
4. [KNOWLEDGE_LAYER_SCOPE.md](KNOWLEDGE_LAYER_SCOPE.md) — the knowledge-substrate backend scope (corpus, vector store, KG nodes, ingest).
5. [MIMIR_WARDEN_SCOPE.md](MIMIR_WARDEN_SCOPE.md) — Mimir, Warden of the Library: the five stores, the Context Graph, the Librarian, the trust/certification model, the acquisition (pull) path, and retrieval.

## Command center (dashboard API + UI)
6. [COMMAND_CENTER_API_SPEC.md](COMMAND_CENTER_API_SPEC.md) — the FastAPI surface + TypeScript types.
7. [COMMAND_CENTER_DATA_SCHEMA.md](COMMAND_CENTER_DATA_SCHEMA.md) — the data schema behind the command center.
8. [COMMAND_CENTER_BACKEND_INTEGRATION.md](COMMAND_CENTER_BACKEND_INTEGRATION.md) — backend integration patterns.
9. [COMMAND_CENTER_STARTER_CODE.md](COMMAND_CENTER_STARTER_CODE.md) — scaffolding/templates.

## Archive
[archive/](archive/) holds superseded docs kept for history: the pre-pivot
`Old_ARCHITECTURE.md` (the "boardroom" v1) and `New_direction.md` (the pivot
brainstorm). Not maintained.
