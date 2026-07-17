-- WS4 — Recall timing persistence (schema v24)
--
-- The /query/slo p99 latency SLI was computed from an in-memory 200-entry ring
-- buffer (_recall_slow_log) that resets on every daemon restart. That is a
-- no-black-box violation: an SLO metric with no persistent source records —
-- after a restart the p99 is fabricated from whatever the buffer has refilled.
--
-- This migration adds a durable, append-only timing log. The daemon writes ONE
-- row per /recall (cheap best-effort INSERT, non-blocking, AFTER the response
-- timing is known). /query/slo computes p50/p95/p99 from this table over the
-- report window, falling back to the ring buffer only when the table is empty
-- (with a `source` field stating which was used).
--
-- Doctrine: every displayed number is a measurement traceable to source
-- records. recall_timings IS that source of record for latency SLIs.

CREATE TABLE IF NOT EXISTS recall_timings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,          -- UTC ISO timestamp of the recall
    duration_ms REAL NOT NULL,          -- measured end-to-end recall latency (ms)
    project     TEXT,                   -- recall project scope (nullable)
    topk        INTEGER                 -- requested topk (nullable)
);

CREATE INDEX IF NOT EXISTS idx_recall_timings_ts
    ON recall_timings(ts);

-- Record migration
INSERT OR IGNORE INTO schema_version (version) VALUES (24);
