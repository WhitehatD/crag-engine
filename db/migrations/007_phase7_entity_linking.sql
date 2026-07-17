-- Phase 7: entity linking for cross-project entity-anchored recall
CREATE TABLE IF NOT EXISTS entity_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_id      INTEGER REFERENCES insights(id),
    principle_id    INTEGER REFERENCES principles(id),
    entity          TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    raw_match       TEXT,
    extracted_at    TEXT DEFAULT (datetime('now')),
    CHECK ((insight_id IS NOT NULL) OR (principle_id IS NOT NULL)),
    UNIQUE(insight_id, principle_id, entity, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_entity_links_entity ON entity_links(entity, entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_links_insight ON entity_links(insight_id);
CREATE INDEX IF NOT EXISTS idx_entity_links_principle ON entity_links(principle_id);

-- Schema version bump
INSERT OR IGNORE INTO schema_version (version) VALUES (7);
