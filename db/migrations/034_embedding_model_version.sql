-- 034 - Embedding model-version stamping (docs/architecture.md REV 4 item 4)
--
-- WHY: embeddings are only comparable within the SAME model. If the embedding
-- model is ever swapped (e.g. all-MiniLM-L6-v2 -> a larger model), cosine
-- similarity between a new vector and an old one is meaningless — silent recall
-- degradation with no signal. Stamping the producing model's identifier on
-- every embedding row makes a model migration DETECTABLE (mixed versions in the
-- table) and lets a backfill target exactly the stale-version rows.
--
-- ADDITIVE ONLY. No existing table/row is dropped or rewritten. Existing rows
-- get NULL embedding_model_version (pre-stamp, model-unknown — correct, not a
-- gap: they were all produced by the single historical model).
--
-- Timestamp convention: N/A (no timestamp column added).
--
-- Idempotency: cmd_migrate skips this file if schema_version already has 34.
-- ADD COLUMN has no IF NOT EXISTS; caller tolerates "duplicate column name",
-- mirroring the migration-026/031/032 pattern.

-- ---------------------------------------------------------------------------
-- claim_embeddings.embedding_model_version: identifier of the model that
-- produced this vector (e.g. "sentence-transformers/all-MiniLM-L6-v2", from
-- embed.EMBEDDING_MODEL). Stamped by claim_layer.persist_claims on every
-- claim-embedding write; NULL for rows written before this migration.
-- ---------------------------------------------------------------------------
ALTER TABLE claim_embeddings ADD COLUMN embedding_model_version TEXT;

-- ---------------------------------------------------------------------------
-- insights/principles store their embedding INLINE (insights.embedding /
-- principles.embedding BLOB columns, migrations 004 + 006) rather than in a
-- dedicated embeddings table. We stamp those inline stores the same way so a
-- future model swap is detectable corpus-wide, not just for the claim pool.
-- ---------------------------------------------------------------------------
ALTER TABLE insights   ADD COLUMN embedding_model_version TEXT;
ALTER TABLE principles ADD COLUMN embedding_model_version TEXT;

INSERT OR IGNORE INTO schema_version (version) VALUES (34);
