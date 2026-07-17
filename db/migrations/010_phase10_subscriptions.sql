-- Phase 10: SSE broadcast — track active subscribers + emitted events
-- (mostly diagnostic — the subscriber registry is in-memory; this table just
-- gives us a queryable log of broadcast events for /admin/broadcast_stats.)

CREATE TABLE IF NOT EXISTS broadcast_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    emitted_at       TEXT NOT NULL DEFAULT (datetime('now')),
    kind             TEXT NOT NULL,           -- 'insight_saved' | 'principle_distilled' | 'promote_global' | 'contradiction'
    payload          TEXT NOT NULL,           -- JSON of {insight_id, project, type, tags, ...}
    subscriber_count INTEGER DEFAULT 0        -- how many SSE clients received it
);
CREATE INDEX IF NOT EXISTS idx_broadcast_emitted ON broadcast_events(emitted_at DESC);

INSERT OR IGNORE INTO schema_version (version) VALUES (10);
