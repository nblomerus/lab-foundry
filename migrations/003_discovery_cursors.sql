-- 003_discovery_cursors.sql
-- Stateful scout discovery (MIMIR_WARDEN_SCOPE §3).
--
-- The old sweep was stateless: every run re-fetched each topic's newest-N at
-- offset 0, so it kept pulling the SAME data (wasteful, and it rate-limited us at
-- arXiv) and re-surfaced sources that failed to ingest forever. These two tables
-- give each scout a memory:
--
--   discovery_cursors — per (source_kind, topic): how deep we've paged and when
--   we last refreshed the newest. The sweep alternates REFRESH (offset 0, catch
--   new submissions) with DEEPEN (advance the offset, walk the back-catalogue),
--   so a scout never re-fetches the same slice.
--
--   discovery_seen — the novelty ledger: every (source_kind, canonical_key) a
--   scout has surfaced, with attempt count + last attempt. The "is this new?"
--   gate skips anything already in the corpus or attempted within the retry
--   window, so failed sources retry on a schedule (not never, not forever).

CREATE TABLE IF NOT EXISTS discovery_cursors (
    source_kind       text NOT NULL,
    topic             text NOT NULL,
    offset_n          integer NOT NULL DEFAULT 0,
    last_refreshed_at timestamptz,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_kind, topic)
);

CREATE TABLE IF NOT EXISTS discovery_seen (
    source_kind     text NOT NULL,
    canonical_key   text NOT NULL,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_attempt_at timestamptz NOT NULL DEFAULT now(),
    attempts        integer NOT NULL DEFAULT 1,
    PRIMARY KEY (source_kind, canonical_key)
);

CREATE INDEX IF NOT EXISTS discovery_seen_retry_idx
    ON discovery_seen (source_kind, last_attempt_at);
