-- Phase A (2026-05-20): recall filter telemetry
--
-- Tracks how many embedding candidates were present BEFORE the
-- `superseded_by IS NULL` filter versus AFTER it on every recall call.
-- The ratio gives the "recall_filter_pct" KPI on /api/data_quality:
--   recall_filter_pct = (candidates_pre - candidates_post) / candidates_pre × 100
--
-- When this stays near 0 the supersede graph is healthy.
-- Rising values indicate many stale/superseded insights are still being
-- pulled into embedding search before the filter, hinting that the corpus
-- needs pruning or that arena adjudication is missing.
--
-- Fire-and-forget inserts from engine_daemon._do_recall (async thread pool)
-- so recall latency is unaffected.

CREATE TABLE IF NOT EXISTS recall_filter_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT    NOT NULL,               -- ISO8601 UTC (T-separated)
    project                 TEXT,                           -- scoped project or NULL for global
    candidates_pre_filter   INTEGER NOT NULL DEFAULT 0,     -- len(emb_rows) before WHERE superseded_by IS NULL
    candidates_post_filter  INTEGER NOT NULL DEFAULT 0,     -- len(emb_rows) after filter
    query_fingerprint       TEXT                            -- sha256[:16] of query for correlation
);

CREATE INDEX IF NOT EXISTS idx_rfe_ts      ON recall_filter_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_rfe_project ON recall_filter_events(project, ts DESC);

-- Schema version bump
INSERT OR IGNORE INTO schema_version (version) VALUES (14);
