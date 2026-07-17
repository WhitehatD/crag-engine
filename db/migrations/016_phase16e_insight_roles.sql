-- Phase 16-E: insight role + epic tagging + session provenance
-- Enables agent-role attribution, epic-level grouping, and session-level
-- recall analysis (Phase 17 empirical validation feed).
--
-- Columns:
--   role        - who saved the insight: 'coordinator'|'subagent'|'operator'|NULL
--                 NULL = saved before this migration (legacy rows)
--   epic_tag    - optional freeform epic/sprint label, e.g. 'phase-16', 'vps-migration'
--   session_id  - the CLAUDE_SESSION_ID or equivalent at save time;
--                 used by Phase 17 to correlate saves with token_ledger rows.
--
-- Migration is idempotent: "duplicate column name" errors are expected if
-- any column was added manually; the schema_version INSERT OR IGNORE ensures
-- the migration is not re-applied on a second run.

ALTER TABLE insights ADD COLUMN role       TEXT;
ALTER TABLE insights ADD COLUMN epic_tag   TEXT;
ALTER TABLE insights ADD COLUMN session_id TEXT;

-- Index for Phase 17 join: token_ledger.session_id <-> insights.session_id
CREATE INDEX IF NOT EXISTS idx_insights_session_id ON insights (session_id)
    WHERE session_id IS NOT NULL;

INSERT OR IGNORE INTO schema_version (version) VALUES (16);
