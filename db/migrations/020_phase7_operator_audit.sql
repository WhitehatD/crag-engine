-- Phase 7 — operator_audit_log table
--
-- Every operator-initiated mutating action on the engine is logged here.
-- This is the audit trail for: arena verdicts, manual supersede, FP-clear,
-- promote_insight, distill, drift fixes, decay actions, etc.
--
-- Schema:
--   id            AUTOINCREMENT
--   created_at    ISO timestamp (default now)
--   actor         operator | coordinator | subagent | hook
--   action        canonical action name (e.g. mark_fp, supersede, promote,
--                 distill, decay, arena_verdict, drift_resolve)
--   target_class  insight | principle | session | entity | pair | contradiction
--   target_id     string identifier (composite for pairs: "loser/winner")
--   payload       JSON blob — input parameters
--   result        JSON blob — what happened (ok, ids touched, error?)
--   note          optional free-text reason
--   session_id    operator's session id (provenance)
--
-- Indexes: created_at desc (newest first), action (for triage filter),
-- target_class+target_id (object-page audit trail).

CREATE TABLE IF NOT EXISTS operator_audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    actor        TEXT NOT NULL DEFAULT 'operator',
    action       TEXT NOT NULL,
    target_class TEXT,
    target_id    TEXT,
    payload      TEXT,    -- JSON
    result       TEXT,    -- JSON
    note         TEXT,
    session_id   TEXT
);

CREATE INDEX IF NOT EXISTS idx_oal_created_at  ON operator_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_oal_action      ON operator_audit_log(action);
CREATE INDEX IF NOT EXISTS idx_oal_target      ON operator_audit_log(target_class, target_id);

INSERT OR IGNORE INTO schema_version (version) VALUES (20);
