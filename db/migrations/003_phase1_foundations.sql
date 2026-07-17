-- Migration 003: Phase 1 foundations
-- Adds: FTS5 full-text index, recall ledger, normalized tags, schema v3

-- ============================================================
-- 1. FTS5 virtual table (content-table mode — mirrors insights)
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS insights_fts USING fts5(
    content, tags, context, type,
    content='insights',
    content_rowid='id'
);

-- Backfill FTS from existing active insights
INSERT OR IGNORE INTO insights_fts(rowid, content, tags, context, type)
SELECT id,
       content,
       COALESCE(tags, ''),
       COALESCE(context, ''),
       type
FROM insights
WHERE status = 'active';

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

-- ============================================================
-- 2. Recall Ledger — tracks which insights are retrieved
-- ============================================================
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

-- ============================================================
-- 3. Normalized Tags
-- ============================================================
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
-- 4. Schema version bump
-- ============================================================
INSERT OR IGNORE INTO schema_version (version) VALUES (3);
