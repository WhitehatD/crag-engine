-- v3.5 / Phase 8 — Alignment / Staging Tier
--
-- Subagent-saved insights land in insights_staged (visible only to the
-- operator workflow) before reaching the canonical insights table.
--
-- Routing logic (enforced in engine_daemon /save_insight):
--   role=subagent → ALWAYS stages
--   role=coordinator → direct save (current behavior)
--   role=operator → direct save (manual UI saves)
--   role=NULL + no source_file + no epic_tag + no session_id → stages
--     (low-provenance defensive)
--
-- Decay coupling: pending staged rows older than 7 days auto-reject with
-- reason "stale_pending" (cron via engine-cli decay).
--
-- See docs/architecture.md §4.

CREATE TABLE IF NOT EXISTS insights_staged (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,
  type TEXT,
  tags TEXT,
  project TEXT,
  source_file TEXT,
  role TEXT,                       -- 'subagent' | 'coordinator' | 'operator'
  session_id TEXT,
  epic_tag TEXT,
  spawned_by_agent_id TEXT,        -- which subagent saved it
  spawned_by_task TEXT,            -- the task description
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  reviewed_at TEXT,
  reviewed_by TEXT,                -- operator name (from OIDC session)
  decision TEXT NOT NULL DEFAULT 'pending',  -- 'pending'|'approved'|'rejected'|'auto-approved'|'auto-rejected'
  decision_reason TEXT,
  promoted_to_insight_id INTEGER   -- FK to insights.id when approved
);

CREATE INDEX IF NOT EXISTS idx_staged_decision ON insights_staged(decision);
CREATE INDEX IF NOT EXISTS idx_staged_role ON insights_staged(role);
CREATE INDEX IF NOT EXISTS idx_staged_created_at ON insights_staged(created_at);
CREATE INDEX IF NOT EXISTS idx_staged_promoted ON insights_staged(promoted_to_insight_id);

INSERT OR IGNORE INTO schema_version (version) VALUES (21);
