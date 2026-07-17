-- Phase 9: contradiction detection metadata
-- Adds suspect_* columns to insights and principles + dedicated contradiction_events
-- audit table. ALTER TABLE statements are NOT idempotent in SQLite (no IF NOT EXISTS
-- on columns), so the migration runner wraps each ALTER in its own try/except --
-- failures on already-present columns are expected on re-runs.

ALTER TABLE insights ADD COLUMN suspect_of INTEGER REFERENCES insights(id);
ALTER TABLE insights ADD COLUMN suspect_reason TEXT;
ALTER TABLE insights ADD COLUMN suspect_score REAL;
ALTER TABLE insights ADD COLUMN suspect_detected_at TEXT;

ALTER TABLE principles ADD COLUMN suspect_of INTEGER REFERENCES principles(id);
ALTER TABLE principles ADD COLUMN suspect_reason TEXT;
ALTER TABLE principles ADD COLUMN suspect_score REAL;
ALTER TABLE principles ADD COLUMN suspect_detected_at TEXT;

-- Audit table: back-pointer for fast "who flagged whom" queries + raw entailment data.
-- Row recorded each time a NEW save flags an OLDER row as suspect.
CREATE TABLE IF NOT EXISTS contradiction_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT NOT NULL DEFAULT (datetime('now')),
    new_kind        TEXT NOT NULL,            -- 'insight' or 'principle'
    new_id          INTEGER NOT NULL,
    old_kind        TEXT NOT NULL,
    old_id          INTEGER NOT NULL,
    cosine_sim      REAL NOT NULL,
    entail_score    REAL NOT NULL,            -- 0.0-1.0, contradiction probability
    haiku_response  TEXT,                     -- raw model response for audit
    UNIQUE(new_kind, new_id, old_kind, old_id)
);
CREATE INDEX IF NOT EXISTS idx_contradiction_old ON contradiction_events(old_kind, old_id);
CREATE INDEX IF NOT EXISTS idx_contradiction_new ON contradiction_events(new_kind, new_id);

INSERT OR IGNORE INTO schema_version (version) VALUES (9);
