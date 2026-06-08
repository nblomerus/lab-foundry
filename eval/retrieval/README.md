# Retrieval evaluation harness

The corpus read path (`library.corpus.corpus_search`) is how Ariadne reads the
world. It had **zero** evaluation. This harness gives it recall@k / nDCG / MRR and,
more importantly, shows **where it fails** — the empirical basis for the
"port the full rag-bench hybrid retrieval" decision.

## Run it

```bash
set -a; . ./.env; set +a            # DATABASE_URL + OLLAMA_URL
PY=/home/nicholas/.pyenv/versions/labfoundry/bin/python

# one-time: freeze a gold set sampled from the live corpus (deterministic per seed)
$PY -m eval.retrieval.evaluate build --n 60 --seed 7

# evaluate the current dense-only path against the frozen gold set
$PY -m eval.retrieval.evaluate run --k 20
```

This is an **eval driver** (like `ops/mimir_firstlight.py`): it intentionally hits
the live corpus + Ollama. It is **not** a pytest test (the suite must not touch live
:5432). The gold set (`goldset.jsonl`) is frozen so reruns are comparable.

## Method — known-item retrieval (auto-labelled)

Sample real certified/queryable documents and build queries that *should* retrieve
that exact document; the relevant doc is known (it is the source), so labelling is
free. Three probes target different failure modes:

| probe   | query                                   | tests |
|---------|-----------------------------------------|-------|
| title   | the document's title                    | semantic recall (dense's strength) |
| passage | a sentence from the document's own text | passage recall |
| lexical | a distinctive term/acronym from title   | exact-token recall (where BM25 wins, dense misses) |

Known-item is a **conservative lower bound** — a query may legitimately retrieve
other on-topic docs, but only the exact source is credited. Good for a baseline and
for tracking regressions/improvements.

## Baseline — dense-only path (2026-06-06, 60-doc gold set, k=20)

```
  title      n=60   R@1=0.22  R@5=0.25  R@10=0.25  R@20=0.27  MRR=0.230  nDCG@10=0.234
  passage    n=57   R@1=0.21  R@5=0.26  R@10=0.30  R@20=0.32  MRR=0.239  nDCG@10=0.253
  lexical    n=60   R@1=0.00  R@5=0.02  R@10=0.02  R@20=0.02  MRR=0.003  nDCG@10=0.006
  OVERALL    n=177  R@1=0.14  R@5=0.18  R@10=0.19  R@20=0.20  MRR=0.156  nDCG@10=0.163
```

**Retrieval is far from "perfect."** A paper's *exact title* fails to retrieve that
paper in the top-20 ~73% of the time; a rare exact token essentially never does.

### Diagnosis (proven, not guessed)

1. **Embeddings are healthy.** Self-retrieval is exact: `distance(stored_embedding,
   reembed(own chunk text)) = 0.0000`, and querying with a chunk's own full text
   returns its doc at rank 1 / distance 0.0. No model or embedding-space bug.
2. **nomic task prefixes would *hurt*.** The corpus was embedded prefix-less; adding
   `search_query:` moves the query *away* from the documents (CrossGET global rank
   120 → 260). Do **not** re-embed for prefixes.
3. **ANN tuning does not fix it.** `ef_search=40` is below the candidate pool
   (`max(4k,32)=80`), but raising `ef_search=500` / pool=200 leaves title R@20 at
   0.27 — the target genuinely sits behind 40+ other docs in embedding space for
   short queries.
4. **It is structural: dense cannot rank short / exact-token queries.** "CrossGET",
   "NACHOS", "FLAIRR-TS" are rare tokens BM25 ranks #1 instantly but dense buries
   under topically-similar chunks. → the fix is **lexical fusion (BM25 ⊕ dense)**,
   i.e. the rag-bench hybrid port, implementable on the existing pgvector store via
   Postgres `tsvector`/`ts_rank` (no second store, no re-embed).

## Hybrid (implemented 2026-06-06) — BM25 ⊕ dense via RRF

`corpus_search(hybrid=True)` (now the default) RRF-fuses the dense ANN with a BM25
(Postgres FTS over `idx_chunks_fts`, migration `004`) ranking. Same frozen gold set,
k=20:

```
                dense R@20   hybrid R@20      dense MRR   hybrid MRR
  title            0.27         1.00            0.230       0.625
  passage          0.32         0.96            0.239       0.642
  lexical          0.02         0.42            0.003       0.139
  OVERALL          0.20         0.79            0.156       0.466
```

**4× overall recall; exact-title retrieval went 0.27 → 1.00.** Rare exact tokens
that dense missed entirely (CrossGET, NACHOS, FLAIRR-TS, LLM-Agents) are now
retrieved. Reproduce: `... evaluate run --mode dense` vs `--mode hybrid`.

The residual `lexical` misses are all *common* words (generalization, ChatGPT,
Reinforcement) — under-specified single-term queries that should not uniquely
resolve to one paper. That is a gold-set probe artifact, not a retrieval defect
(tighten `_distinctive_term` to prefer low-DF tokens if you want a cleaner signal).

## Latency (fixed 2026-06-06)

The first cut recomputed `to_tsvector(text)` per matched row in `ts_rank`, so a
common single-term query (~12k–50k matches) cost ~1.6–1.7 s. Fixed with:
- a materialized `chunks.tsv` column (migration `004`) kept fresh by a trigger, so
  `ts_rank` reads a precomputed vector; GIN moved to `tsv`, and
- a candidate-scan cap (`LEX_SCAN_CAP`) before ranking — rare/distinctive terms
  match under the cap (no loss), common terms get an approximate lexical arm (fine,
  dense covers them).

Result: lexical arm **~1700 ms → ~6–40 ms**; total hybrid SQL **~40 ms** worst-case,
flat across term commonality; recall unchanged (R@20 0.80). Remaining `corpus_search`
latency is the Ollama embed call (~10–600 ms, shared-GPU variable) — common to dense
and hybrid, out of scope here.

## Remaining

- **Cross-encoder rerank** — the third rag-bench stage (`rag_bench/core/retriever.py`)
  to lift R@1 / precision once recall is solid (current hybrid R@1 ≈ 0.20–0.35).
