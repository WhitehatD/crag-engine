-- Phase 4.4 — recall_feedback table
--
-- Operator-flagged signal on ranking quality.  Used by RecallExplorerPage's
-- "flag bad ranking" button.  Future use: feed into ranking-weight tuning,
-- adjudication queue, decay model (Phase 10).
--
-- Schema:
--   id          AUTOINCREMENT
--   created_at  ISO timestamp
--   query       the operator's query
--   project     scope at search time (nullable)
--   insight_id  the insight being flagged (nullable for "missing result" flag)
--   feedback    'wrong_rank' | 'irrelevant' | 'missing' | 'stale_content'
--   actual_rank rank when flagged (1-based, null if missing-result)
--   expected_rank operator's claim of where it should rank (null if irrelevant)
--   note        free-text (max 500 chars enforced in app code)
--   session_id  who flagged it (provenance)
--   role        coordinator | subagent | operator
--
-- Indexes: by query (for "show all feedback on query X"), by insight_id (for
-- the object page's audit trail), by feedback (for triage view).

CREATE TABLE IF NOT EXISTS recall_feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    query         TEXT NOT NULL,
    project       TEXT,
    insight_id    INTEGER,            -- NULL = missing-result flag
    feedback      TEXT NOT NULL,      -- wrong_rank | irrelevant | missing | stale_content
    actual_rank   INTEGER,
    expected_rank INTEGER,
    note          TEXT,
    session_id    TEXT,
    role          TEXT,
    FOREIGN KEY (insight_id) REFERENCES insights(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_recall_feedback_query      ON recall_feedback(query);
CREATE INDEX IF NOT EXISTS idx_recall_feedback_insight_id ON recall_feedback(insight_id);
CREATE INDEX IF NOT EXISTS idx_recall_feedback_feedback   ON recall_feedback(feedback);
CREATE INDEX IF NOT EXISTS idx_recall_feedback_created_at ON recall_feedback(created_at);

INSERT OR IGNORE INTO schema_version (version) VALUES (19);
