# Reasoning layer — context-graph concept extraction

The corpus graph is Ariadne's "why it matters" backbone, but it shipped as a flat
provenance stub: `Paper-[:FROM]->Source` only (31k papers, one edge type). She could
not traverse **paper → method → paper** or reason about novelty gaps. This builds the
reasoning structure: each paper's **methods / datasets / tasks**, projected as typed
edges so the Field Model is queryable.

## Approach — per-paper extraction (not per-chunk)

rag-bench's `entity_extractor` extracts (entity, relation, entity) triples per CHUNK
via a local LLM — ~1.7M calls for our corpus, impractical. We extract **per PAPER**:
ONE schema-guided LLM call (Ollama, `format=json`) over title + lead excerpt →
`{methods, datasets, tasks}`. That captures a paper's headline concepts at ~1/45th
the cost (**~31k calls for the whole corpus** — a feasible background job), which is
what the Field Model needs. Schema (labels + relations) mirrors rag-bench's
`graph_store` so a per-chunk enrichment pass could deepen the same graph later.

- `library/graph/extract.py` — `extract_paper_concepts()` (LLM) + `project_paper_concepts()` (Neo4j MERGE).
- Projection (name-keyed, distinct from the id-keyed `:Dataset` registry):
  `(:Paper)-[:USES]->(:METHOD)`, `-[:EVALUATED_ON]->(:DATASET)`, `-[:ADDRESSES]->(:TASK)`.

## Run it (slice + measurement)

```bash
set -a; . ./.env; set +a
python -m eval.graph.extract_slice --n 30   # sample, extract, project, measure
```

Reads the live corpus (Postgres), writes concept nodes/edges to Neo4j. An eval/build
DRIVER (like `ops/mimir_firstlight`), not a pytest. Idempotent per paper.

## Slice result (2026-06-06, 30 papers, qwen2.5:14b)

```
BEFORE: 0 concept nodes/edges (flat Paper-[:FROM]->Source only)
AFTER:  51 METHOD, 21 DATASET, 37 TASK nodes;
        57 USES, 22 EVALUATED_ON, 39 ADDRESSES edges;
        coverage 28/30 (93%)
Traversal (paper→method→paper): "contrastive learning" shared by ~6 papers,
        "retrieval-augmented generation"/"fine-tuning"/"code generation" by ~2 each.
```

Extraction quality is good (e.g. NEFTune → `USES:NEFTune, finetuning`;
`EVALUATED_ON:AlpacaEval, ShareGPT, OpenPlatypus`). The 2 uncovered papers are a
pure-math and a management paper with no ML methods — correctly empty.

## Scale — the full-corpus backfill

`ops/extract_concepts_backfill.py` runs extraction over the whole corpus, **resumable
+ idempotent** (a `p.concepts_extracted` marker per paper — even 0-concept ones — so a
re-run skips done papers and a death costs nothing; re-running also picks up new papers
the discovery pump added). GPU-bound at ~2.5 s/paper (concurrency gives no speedup), so
~23k papers ≈ **~15–16 h**; the graph grows incrementally and is usable throughout.

```bash
set -a; . ./.env; set +a
PYTHONUNBUFFERED=1 nohup python -u -m ops.extract_concepts_backfill --limit 0 \
  > /tmp/labfoundry_concept_backfill.log 2>&1 & disown
# monitor: cypher "MATCH (p:Paper) WHERE p.concepts_extracted RETURN count(p)"
```

It shares the GPU with the discovery pump's embedder, so both run slower while active.

### Further limits
- **Simplifications vs rag-bench:** per-paper (headline concepts) not per-chunk;
  paper→concept edges, not full entity↔entity triples (USES/OUTPERFORMS between
  models/methods). A later per-chunk enrichment pass can add those.
- **Next for the Field Model:** novelty-gap query (methods/datasets with few papers in
  a topic), concept canonicalization (merge "fine-tuning"/"finetuning"), and exposing
  a `kg_*` read tool Ariadne calls during direction-setting.
- **Cosmetic:** the Neo4j driver logs "relationship type not in DB" warnings on the
  first BEFORE measurement (before any edges exist) — harmless; grep them out.
