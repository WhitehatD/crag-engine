-- Phase 6: semantic search for principles + recall dedup
ALTER TABLE principles ADD COLUMN embedding BLOB;
ALTER TABLE recall_events ADD COLUMN fingerprint TEXT;
CREATE INDEX IF NOT EXISTS idx_recall_events_fingerprint ON recall_events(fingerprint);

-- Schema version bump
INSERT OR IGNORE INTO schema_version (version) VALUES (6);
