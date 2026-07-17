-- Phase 1 (unified roadmap) — provenance fields on recall_events.
-- recall_events already has session_id (Phase 6). Add role + epic_tag so
-- Phase 17 ROI reporting can rollup by role (subagent vs coordinator
-- recall patterns) and by epic_tag (per-sprint effectiveness).
--
-- Columns:
--   role     - who issued the recall: 'coordinator'|'subagent'|'operator'|NULL
--   epic_tag - sprint/epic label of the agent at recall time
--
-- Backward compat: legacy recall_events rows leave both NULL.

ALTER TABLE recall_events ADD COLUMN role     TEXT;
ALTER TABLE recall_events ADD COLUMN epic_tag TEXT;

INSERT OR IGNORE INTO schema_version (version) VALUES (18);
