-- Migration 005: Phase 4 Lifecycle
-- Adds optional tracking for decay and re-verification flags.

-- No new tables required — decay reads from recall_events, writes to insights.confidence
-- Adding precomputed last_recalled_at for performance (avoid recurring LEFT JOIN at decay time)

ALTER TABLE insights ADD COLUMN last_recalled_at TEXT;
CREATE INDEX IF NOT EXISTS idx_insights_last_recall ON insights(last_recalled_at);

-- Backfill last_recalled_at from recall_events
UPDATE insights
SET last_recalled_at = (
    SELECT MAX(recalled_at) FROM recall_events WHERE insight_id = insights.id
)
WHERE EXISTS (SELECT 1 FROM recall_events WHERE insight_id = insights.id);

-- Trigger to keep last_recalled_at synced when new recall events fire
CREATE TRIGGER IF NOT EXISTS recall_update_last AFTER INSERT ON recall_events
WHEN NEW.insight_id IS NOT NULL
BEGIN
    UPDATE insights SET last_recalled_at = NEW.recalled_at WHERE id = NEW.insight_id;
END;

-- Schema version bump
INSERT OR IGNORE INTO schema_version (version) VALUES (5);
