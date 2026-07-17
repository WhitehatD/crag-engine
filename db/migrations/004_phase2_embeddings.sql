-- Migration 004: Phase 2 Semantic Embeddings
-- Adds embedding column to insights for vector search

ALTER TABLE insights ADD COLUMN embedding BLOB;  -- float32 bytes, 384 dims, 1536 bytes

-- Partial index for insights that have embeddings (avoids scanning unembedded rows)
CREATE INDEX IF NOT EXISTS idx_insights_embedded
    ON insights(id) WHERE embedding IS NOT NULL;

INSERT OR IGNORE INTO schema_version (version) VALUES (4);
