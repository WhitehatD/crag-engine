-- Phase 11: token_ledger cache token split
-- Allows callers to record fresh vs cached input tokens separately,
-- enabling accurate cost calculation without relying on Headroom extrapolation.
--
-- Backward-compatible: new columns default to 0.
-- When fresh_input_tokens > 0, cost_analysis uses it instead of tokens_in.
-- When 0 (old rows), the Headroom-projection path applies.

ALTER TABLE token_ledger ADD COLUMN cache_read_tokens  INTEGER DEFAULT 0;
ALTER TABLE token_ledger ADD COLUMN cache_write_tokens INTEGER DEFAULT 0;
ALTER TABLE token_ledger ADD COLUMN fresh_input_tokens INTEGER DEFAULT 0;

INSERT OR IGNORE INTO schema_version (version) VALUES (11);
