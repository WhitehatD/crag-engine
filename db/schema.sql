-- crag-engine -- SQLite memory backend
-- BASELINE schema ONLY (11 tables: sessions, insights, principles, event queue,
-- token ledger, ...). The LIVE schema = this baseline + every migration in
-- db/migrations/*.sql applied via `engine-cli.py migrate` (24 tables at v24:
-- entity_links, contradiction_events, arena_events, insights_staged,
-- operator_audit_log, falsifiers, grounding_queue, recall_timings, ...).
-- Need the current full schema (e.g. for a test fixture)? Dump it read-only:
--   python -c "import sqlite3; c=sqlite3.connect('file:db/engine.db?mode=ro',uri=True); print('\n'.join(r[0] for r in c.execute(\"SELECT sql FROM sqlite_master WHERE sql IS NOT NULL\")))"

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Sessions: diary entries from each CC session
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT    NOT NULL,
    date        TEXT    NOT NULL,               -- YYYY-MM-DD
    accomplished TEXT,                           -- bullet list
    files_changed TEXT,                          -- bullet list
    commits     TEXT,                            -- bullet list
    decisions   TEXT,                            -- bullet list
    problems    TEXT,                            -- bullet list
    next_steps  TEXT,                            -- bullet list
    duration    TEXT,                            -- e.g. "~2 hours"
    raw_markdown TEXT,                           -- full original markdown for export
    created_at  TEXT    DEFAULT (datetime('now'))
);

-- Insights: key learnings, decisions, and patterns (with confidence scoring)
CREATE TABLE IF NOT EXISTS insights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT,                            -- NULL = global insight
    type            TEXT    NOT NULL DEFAULT 'decision', -- decision, pattern, bug-fix, architecture, tool
    content         TEXT    NOT NULL,
    context         TEXT,                            -- what prompted this insight
    tags            TEXT,                            -- comma-separated
    status          TEXT    DEFAULT 'active',        -- active, stale, invalidated, archived
    source_file     TEXT,                            -- file this insight references
    supersedes      INTEGER,                         -- ID of insight this replaces
    verified_at     TEXT,                            -- last verification timestamp
    confidence      REAL    DEFAULT 0.5,             -- 0.0-1.0 confidence score
    verify_count    INTEGER DEFAULT 0,               -- total verification attempts
    verify_streak   INTEGER DEFAULT 0,               -- consecutive successful verifications
    promoted_to     INTEGER,                         -- FK to principles.id if promoted
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now')),
    embedding       BLOB,                             -- Phase 2: float32 bytes, 384 dims (all-MiniLM-L6-v2)
    last_recalled_at TEXT                             -- Phase 4: precomputed from recall_events for fast decay queries
);

-- Principles: distilled, high-confidence knowledge
CREATE TABLE IF NOT EXISTS principles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT,
    content         TEXT    NOT NULL,                 -- the distilled principle
    source_insights TEXT,                             -- comma-separated insight IDs
    confidence      REAL    DEFAULT 0.9,
    tags            TEXT,
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now'))
);

-- Project context: current state of each managed project
CREATE TABLE IF NOT EXISTS project_context (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    project                 TEXT    NOT NULL UNIQUE,
    status                  TEXT    DEFAULT 'active',  -- active, paused, completed
    current_branch          TEXT,
    last_session_date       TEXT,
    architecture_decisions  TEXT,
    known_issues            TEXT,
    backlog                 TEXT,
    updated_at              TEXT    DEFAULT (datetime('now'))
);

-- Plans: task lists with status tracking
CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT    NOT NULL,
    task_number INTEGER NOT NULL,
    description TEXT    NOT NULL,
    status      TEXT    DEFAULT 'pending',       -- pending, in_progress, completed, blocked
    blocked_reason TEXT,
    created_at  TEXT    DEFAULT (datetime('now')),
    updated_at  TEXT    DEFAULT (datetime('now')),
    UNIQUE(project, task_number)
);

-- Event queue: pending tasks from webhooks, crons, CI
CREATE TABLE IF NOT EXISTS pending_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT,                                -- NULL = system-level event
    source      TEXT    NOT NULL,                     -- webhook, cron, ci, manual
    event_type  TEXT    NOT NULL,                     -- ci_failure, health_check, cve_alert, deploy_failure, custom
    summary     TEXT    NOT NULL,                     -- human-readable one-liner
    payload     TEXT,                                 -- full JSON payload from source
    priority    TEXT    NOT NULL DEFAULT 'normal',    -- critical, high, normal, low
    status      TEXT    NOT NULL DEFAULT 'pending',   -- pending, claimed, completed, failed, expired
    claimed_by  TEXT,                                 -- session ID that picked this up
    result      TEXT,                                 -- outcome summary after completion
    created_at  TEXT    DEFAULT (datetime('now')),
    claimed_at  TEXT,
    completed_at TEXT,
    expires_at  TEXT                                  -- auto-expire stale events
);

-- Token ledger: cost tracking per session
CREATE TABLE IF NOT EXISTS token_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT,                             -- Claude session ID
    project         TEXT    NOT NULL,
    task_summary    TEXT,
    tokens_in       INTEGER DEFAULT 0,
    tokens_out      INTEGER DEFAULT 0,
    cache_hits      INTEGER DEFAULT 0,
    cache_misses    INTEGER DEFAULT 0,
    rtk_savings_pct REAL    DEFAULT 0,
    headroom_savings_pct REAL DEFAULT 0,
    wall_time_sec   INTEGER DEFAULT 0,
    model           TEXT,                             -- opus, sonnet, haiku
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    DEFAULT (datetime('now'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_sessions_project     ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_date        ON sessions(date);
CREATE INDEX IF NOT EXISTS idx_insights_project     ON insights(project);
CREATE INDEX IF NOT EXISTS idx_insights_type        ON insights(type);
CREATE INDEX IF NOT EXISTS idx_insights_confidence  ON insights(confidence);
CREATE INDEX IF NOT EXISTS idx_principles_project   ON principles(project);
CREATE INDEX IF NOT EXISTS idx_plans_project        ON plans(project);
CREATE INDEX IF NOT EXISTS idx_plans_status         ON plans(status);
CREATE INDEX IF NOT EXISTS idx_events_status        ON pending_events(status);
CREATE INDEX IF NOT EXISTS idx_events_project       ON pending_events(project);
CREATE INDEX IF NOT EXISTS idx_events_priority      ON pending_events(priority);
CREATE INDEX IF NOT EXISTS idx_ledger_project       ON token_ledger(project);
CREATE INDEX IF NOT EXISTS idx_ledger_date          ON token_ledger(created_at);

-- ============================================================
-- Phase 1 foundations (schema v3)
-- ============================================================

-- FTS5 virtual table (content-table mode -- mirrors insights)
CREATE VIRTUAL TABLE IF NOT EXISTS insights_fts USING fts5(
    content, tags, context, type,
    content='insights',
    content_rowid='id'
);

-- Triggers to keep FTS in sync with insights table
CREATE TRIGGER IF NOT EXISTS insights_ai AFTER INSERT ON insights BEGIN
  INSERT INTO insights_fts(rowid, content, tags, context, type)
    VALUES (new.id, new.content, COALESCE(new.tags,''), COALESCE(new.context,''), new.type);
END;

CREATE TRIGGER IF NOT EXISTS insights_au AFTER UPDATE ON insights BEGIN
  INSERT INTO insights_fts(insights_fts, rowid, content, tags, context, type)
    VALUES ('delete', old.id, old.content, COALESCE(old.tags,''), COALESCE(old.context,''), old.type);
  INSERT INTO insights_fts(rowid, content, tags, context, type)
    VALUES (new.id, new.content, COALESCE(new.tags,''), COALESCE(new.context,''), new.type);
END;

CREATE TRIGGER IF NOT EXISTS insights_ad AFTER DELETE ON insights BEGIN
  INSERT INTO insights_fts(insights_fts, rowid, content, tags, context, type)
    VALUES ('delete', old.id, old.content, COALESCE(old.tags,''), COALESCE(old.context,''), old.type);
END;

-- Recall Ledger -- tracks which insights are retrieved
CREATE TABLE IF NOT EXISTS recall_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_id   INTEGER REFERENCES insights(id),
    principle_id INTEGER REFERENCES principles(id),
    session_id   TEXT,
    query        TEXT,
    hit_rank     INTEGER,
    recalled_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_recall_insight  ON recall_events(insight_id);
CREATE INDEX IF NOT EXISTS idx_recall_session  ON recall_events(session_id);
CREATE INDEX IF NOT EXISTS idx_recall_ts       ON recall_events(recalled_at);

-- Normalized Tags
CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS insight_tags (
    insight_id INTEGER NOT NULL REFERENCES insights(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (insight_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_insight_tags_tag ON insight_tags(tag_id);

-- ============================================================
-- Phase 2 Semantic Embeddings (schema v4)
-- ============================================================

-- Partial index for insights that have embeddings (avoids scanning unembedded rows)
CREATE INDEX IF NOT EXISTS idx_insights_embedded
    ON insights(id) WHERE embedding IS NOT NULL;

INSERT OR IGNORE INTO schema_version (version) VALUES (3);
INSERT OR IGNORE INTO schema_version (version) VALUES (4);

-- ============================================================
-- Phase 4 Lifecycle & Hygiene (schema v5)
-- ============================================================

-- Index for last_recalled_at (used by decay command)
CREATE INDEX IF NOT EXISTS idx_insights_last_recall ON insights(last_recalled_at);

-- Trigger to keep last_recalled_at synced when new recall events fire
CREATE TRIGGER IF NOT EXISTS recall_update_last AFTER INSERT ON recall_events
WHEN NEW.insight_id IS NOT NULL
BEGIN
    UPDATE insights SET last_recalled_at = NEW.recalled_at WHERE id = NEW.insight_id;
END;

INSERT OR IGNORE INTO schema_version (version) VALUES (5);
