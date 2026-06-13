-- 021_document_retrieval_tracking.sql — when a document last surfaced in a corpus_search result.
--
-- Powers ops.decay_corpus (Tier 5 pollution cleanup): the corpus carries a large low-trust mass
-- (≈40% github at web_unknown, much of it noise). The retrieval gate already EXCLUDES
-- trust_state='decayed' (library/corpus/tools.py), but nothing ever SET that state — 'decayed' was
-- a designed-but-unused trust state. This column lets the decay sweep spare docs that are actually
-- being used: a stale, low-trust doc that has never been retrieved (or not in a long while) is
-- demoted to 'decayed' (excluded from search) without deleting anything — reversible by re-ingest.
--
-- Best-effort stamped by library.corpus.tools._track_retrieval (throttled to ≤1×/day/doc), so the
-- read path adds at most one cheap throttled UPDATE per search. Additive + idempotent.

ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS last_retrieved_at timestamptz;

-- Partial index: the decay sweep scans for never/long-unretrieved docs; most rows are NULL early on.
CREATE INDEX IF NOT EXISTS idx_documents_last_retrieved ON public.documents (last_retrieved_at);
