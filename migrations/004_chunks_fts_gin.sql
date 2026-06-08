-- 004_chunks_fts_gin.sql
-- Materialized full-text vector (chunks.tsv) — the BM25 arm of hybrid retrieval
-- (library/corpus/tools.py _search_hybrid RRF-fuses dense ANN + ts_rank(tsv)).
--
-- WHY a stored column (not an expression index): the dense-only read path misses
-- short / exact-token queries badly (eval/retrieval baseline: exact-title R@20=0.27,
-- rare-token ≈0.02). Hybrid fixes that, but ranking with ts_rank(to_tsvector(text))
-- recomputes the tsvector per matched row — ~1.6 s on common single-term queries.
-- Storing the tsvector once lets ts_rank read it directly (typical query <20 ms).
-- A trigger keeps tsv fresh on insert/text-update; the GIN serves both @@ and rank.
--
-- POPULATED DB: ADD COLUMN is instant (nullable, no rewrite); backfill existing
-- rows out-of-band in batches (ops/backfill_chunks_tsv.py) and build the GIN
-- CONCURRENTLY. FRESH/EMPTY DB: this whole file is instant and the trigger fills
-- tsv as the corpus seed inserts chunks. All statements are idempotent.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv tsvector;

DROP TRIGGER IF EXISTS trg_chunks_tsv ON chunks;
CREATE TRIGGER trg_chunks_tsv BEFORE INSERT OR UPDATE OF text ON chunks
    FOR EACH ROW EXECUTE FUNCTION
        tsvector_update_trigger(tsv, 'pg_catalog.english', text);

CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (tsv);

-- Superseded: the earlier expression index (recomputed to_tsvector per query row).
DROP INDEX IF EXISTS idx_chunks_fts;
