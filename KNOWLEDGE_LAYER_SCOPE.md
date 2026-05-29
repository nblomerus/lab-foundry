# Knowledge Layer — Backend Scope

The research lab needs a real **knowledge substrate**: a place to store papers,
media, datasets, and research material, retrievable by the agents and linked
into the evidence graph. This scopes the missing backend.

## What exists today

| Piece | Status | Where |
|---|---|---|
| Source fetching (arXiv, web, HN, Reddit) | ✅ | `boardroom/research/fetcher.py`, `schemas.py` source enum |
| Live web search / navigation | ✅ | SearXNG service + fetcher (Researcher uses it directly) |
| Findings (text) in Postgres | ✅ | `findings` table |
| Knowledge graph (Neo4j) | ✅ partial | `boardroom_knowledge` — only `Claim`/`Finding`/`CriticVerdict` |
| Episodic agent memory | ✅ | Zep (not a research corpus) |
| **RAG corpus / vector store** | ❌ **to build** | — |
| **Embed + chunk ingest pipeline** | ❌ **to build** | — |
| **KG nodes for Paper/Dataset/Media/Source** | ❌ **to build** | — |
| **Librarian / ingest agent** | ❌ **to build** | — |

> **Web search stays.** The RAG corpus is the *durable library*; live web
> search remains a first-class tool so the Researcher can navigate fresh
> material that isn't (yet) ingested. Ingestion *feeds* the corpus; it does
> not replace live retrieval.

## Target architecture

```
                 ┌──────────── Ingestion ────────────┐
  arXiv ─┐       │ fetch → clean → chunk → embed      │
  repos ─┼──────▶│ → upsert RAG  + extract → KG       │
  datasets┘      └───────────────┬───────────────────┘
                                 │
        ┌────────────────────────┼───────────────────────┐
        ▼                        ▼                         ▼
   RAG corpus              Knowledge graph            (Postgres
   (vector store)          (Neo4j: Paper,              findings,
   papers/media/           Dataset, Source,            claims …)
   datasets, chunks        Author + evidence)
        ▲                        ▲
        └──── retrieve ──────────┘
                   │
              Researcher  ──(also)──▶ live web search (SearXNG)
```

## Components to build

### 1. Vector store (RAG corpus)
- **Choice:** `pgvector` on the existing Postgres (no new service; one extension).
  Alternative: Qdrant if we outgrow it. Recommend pgvector to start.
- **Tables:**
  - `documents` — id, kind (`paper|media|dataset|web|note`), title, authors,
    source_url, doi/arxiv_id, published_at, ingested_at, license, raw_uri
  - `chunks` — id, document_id, ordinal, text, embedding `vector(N)`, token_count
  - `datasets` — id, name, url, modality, size, task, license, notes
- **Migration:** `009_knowledge_corpus.sql` (CREATE EXTENSION vector; tables; ivfflat index on `chunks.embedding`).

### 2. Embed + chunk pipeline
- Chunker: ~800-token windows, 100 overlap, section-aware for papers.
- Embeddings: local model first (e.g. `bge`/`nomic` via Ollama) to stay free;
  pluggable provider behind one interface.
- Idempotent upsert keyed by `document_id + ordinal` + content hash.

### 3. Retrieval tool (MCP)
- New tools in `boardroom_knowledge` (or a `boardroom_corpus` server):
  - `corpus_search(query, k, kind?)` → top-k chunks with doc metadata + scores
  - `corpus_get_document(id)` → full doc + chunk list
  - `list_datasets(task?)` → dataset registry rows
- Researcher recipe gains a retrieval step before/alongside live web search.

### 4. Knowledge-graph extension (Neo4j)
- New node labels + constraints in `boardroom_knowledge/tools.py`:
  - `(:Paper {id, arxiv_id, doi, title, year})`
  - `(:Dataset {id, name, modality, task})`
  - `(:Source {url, kind})`  · `(:Author {name})`
- New edges:
  - `(Finding)-[:CITES]->(Paper)`
  - `(Claim)-[:USES]->(Dataset)`
  - `(Paper)-[:FROM]->(Source)` · `(Paper)-[:BY]->(Author)`
  - `(Document)` in RAG ↔ `(Paper)` in KG linked by shared id/doi
- `merge_paper(...)`, `merge_dataset(...)`, `link_finding_cites_paper(...)`.

### 5. Librarian / ingest agent
- New handler/loop: on a new `source.discovered` event (or a scheduled sweep),
  the Librarian: fetch → chunk → embed → upsert RAG → extract entities → MERGE
  into KG. Non-fatal, mirrors the existing graph-sink pattern.
- Emits `document.ingested` so the Flow graph can pulse the ingest edge.

### 6. API surface (for the Flow page + Research OS)
- `GET /knowledge/stats` → corpus counts (docs by kind, chunks, datasets) +
  KG counts (papers, datasets, citations). Feeds the new Flow nodes.
- Extend `/trace/graph/*` (already added) for Paper/Dataset traversal.

## Phasing
1. **Corpus MVP** — pgvector migration + chunk/embed + `corpus_search` tool +
   `/knowledge/stats`. Researcher can retrieve. (Unblocks the Flow UI's RAG node with real counts.)
2. **KG extension** — Paper/Dataset/Source nodes + CITES/USES edges + merges,
   wired into `task_completed` like the existing finding→claim sink.
3. **Librarian agent** — automated ingest loop + `document.ingested` event.
4. **Media/datasets** — non-text modalities, dataset registry, license tracking.

## Flow-page implication
- New left-hand **Knowledge** column with three nodes: **Ingestion → RAG Corpus
  → Knowledge Graph**, each showing live counts from `/knowledge/stats`.
- **Web search** kept as a live source feeding the Researcher (separate from the corpus).
- Until Phase 1 ships, the RAG/KG nodes render in a **"planned"** state with zeroed counts.
