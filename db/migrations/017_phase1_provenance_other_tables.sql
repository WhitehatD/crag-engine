-- Phase 1 (unified roadmap) — provenance fields on principles, arena_events, broadcast_events.
-- Mirrors Phase 16-E (migration 016 added role/epic_tag/session_id to insights).
-- Required for the Object Spine: every record class shares a consistent envelope.
--
-- Columns:
--   role        - who created/triggered: 'coordinator'|'subagent'|'operator'|NULL (legacy)
--   epic_tag    - optional sprint/epic label, e.g. 'phase-1-spine', 'vps-migration'
--   session_id  - CLAUDE_SESSION_ID at time of creation
--
-- All new columns are nullable for backward compat — legacy rows stay NULL.
-- Indexes on session_id where the object class is session-scoped (principles
-- are cross-session, so no session_id index there; arena+broadcast are
-- session-triggered events).

ALTER TABLE principles       ADD COLUMN role       TEXT;
ALTER TABLE principles       ADD COLUMN epic_tag   TEXT;
ALTER TABLE principles       ADD COLUMN session_id TEXT;

ALTER TABLE arena_events     ADD COLUMN role       TEXT;
ALTER TABLE arena_events     ADD COLUMN epic_tag   TEXT;
ALTER TABLE arena_events     ADD COLUMN session_id TEXT;

ALTER TABLE broadcast_events ADD COLUMN role       TEXT;
ALTER TABLE broadcast_events ADD COLUMN epic_tag   TEXT;
ALTER TABLE broadcast_events ADD COLUMN session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_principles_session_id    ON principles(session_id)        WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_arena_events_session_id  ON arena_events(session_id)      WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_broadcast_session_id     ON broadcast_events(session_id)  WHERE session_id IS NOT NULL;

INSERT OR IGNORE INTO schema_version (version) VALUES (17);
