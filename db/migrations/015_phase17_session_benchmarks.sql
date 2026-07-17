-- Phase 17: session benchmark columns for token_ledger
-- Enables empirical validation of engine recall effectiveness over time.
-- Four counters logged per session via add_token_record MCP tool.
-- All default to 0 for backward compat with old rows (which had no tracking).
--
-- Definitions:
--   recall_hits    - insights recalled during this session that were actually
--                    useful (agent cited them, task wouldn't have been solved
--                    without them). Increment when recall results materially
--                    changed the agent's approach.
--   recall_misses  - queries where recall returned nothing useful (or returned
--                    nothing at all). Tracks coverage gaps.
--   repeated_errors - errors this session that matched a previously-saved
--                    insight (i.e., the engine had the answer but wasn't
--                    recalled in time, or the insight was stale).
--   novel_saves    - new insights saved this session (net new knowledge
--                    added to the corpus). Use len(save_insight calls) -
--                    dedup_rejections.
--
-- Validation targets (Phase 21 SLO):
--   recall hit rate = recall_hits / (recall_hits + recall_misses) >= 70%
--   repeated-error rate = repeated_errors / task_count <= 5%
--   novel-save rate = novel_saves / session_count >= 2 (average new knowledge per session)

ALTER TABLE token_ledger ADD COLUMN recall_hits     INTEGER DEFAULT 0;
ALTER TABLE token_ledger ADD COLUMN recall_misses   INTEGER DEFAULT 0;
ALTER TABLE token_ledger ADD COLUMN repeated_errors INTEGER DEFAULT 0;
ALTER TABLE token_ledger ADD COLUMN novel_saves     INTEGER DEFAULT 0;

INSERT OR IGNORE INTO schema_version (version) VALUES (15);
