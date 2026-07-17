-- 029 - Session-state recording: link auto-captured facts to manual narrative
--       (schema v29)
--
-- WHY (insight #3341, 2026-07-05 audit): engine.db has had TWO session-capture
-- paths with OPPOSITE architectures since Phase 12 (migration 012):
--
--   TOKEN state = captured AUTOMATICALLY. Stop hook (token-ledger-log.sh) POSTs
--   {session_id, transcript_path} -> daemon /ingest/session_tokens -> daemon
--   parses the transcript and UPSERTs ONE row per session_id into token_ledger.
--   Never missing, never fragmented.
--
--   WORK state (accomplished/commits/files/decisions/problems/next_steps) =
--   captured MANUALLY, only if the agent remembers to call session_diary(add)
--   before running out of context. `sessions` has no session_uuid column at
--   all -- every call is a bare INSERT keyed by an autoincrement id + a
--   YYYY-MM-DD date string. One real Claude session (one session_uuid in
--   session_meta, migration 012) has produced 0..N fragmented `sessions` rows
--   with ZERO way to join them back to session_meta or token_ledger (observed:
--   5 diary rows for one session_uuid on 2026-07-05).
--
-- FIX: extend the PROVEN token_ledger UPSERT-by-session_uuid pattern to
-- `sessions`, instead of building a parallel table. `sessions` becomes the
-- single canonical work-state record, addressable by session_uuid, populated
-- from TWO independent writers that never blow each other's data away:
--
--   1. Auto-capture (POST /ingest/session_state, fired by a new Stop-hook
--      step, mirrors /ingest/session_tokens): writes git_branch,
--      commits_count, files_changed_count, wall_time_sec, auto_captured=1.
--      UPSERTs by session_uuid. NEVER touches the narrative columns
--      (accomplished/decisions/problems/next_steps/raw_markdown) -- those are
--      excluded from this writer's SET clause entirely, so a Stop-hook fire
--      can never blank out narrative the agent already wrote.
--
--   2. Manual enrichment (session_diary(add) MCP tool, existing
--      POST /lifecycle/session/add, extended to accept an optional
--      session_uuid): when session_uuid is supplied, UPSERTs the SAME row
--      (enriching narrative fields via COALESCE-on-non-empty, same idiom as
--      token_ledger's role/model columns) instead of inserting a new row.
--      When session_uuid is omitted (back-compat: manual/non-Claude-Code
--      callers), the legacy bare-INSERT path is preserved unchanged.
--
-- Existing 212 rows keep session_uuid = NULL (pre-fix history, unrecoverable
-- without the timestamp/project backfill in a separate admin endpoint --
-- see /admin/backfill_session_uuid, doctrine: best-effort with an honest
-- 'unlinked' bucket, never guess a fabricated 1:1 match. Ref insight #2191:
-- the sibling token_events backfill was explicitly DEFERRED for the same
-- reason -- do not silently correlate ambiguous historical rows).
--
-- ADDITIVE ONLY. No existing column/table altered destructively. Partial
-- UNIQUE index (WHERE session_uuid IS NOT NULL) needs no table rebuild
-- because the 212 existing rows all land on the NULL side, which SQLite's
-- UNIQUE treats as pairwise-distinct (ref insight #2552 -- that technique is
-- for retrofitting UNIQUE onto a column that ALREADY has real duplicate
-- values -- not needed here since session_uuid is a brand-new column).
--
-- Timestamp convention: ALL TEXT timestamps use the canonical Python
-- _utcnow_iso() output (YYYY-MM-DDTHH:MM:SS.ffffff+00:00). NEVER SQLite
-- datetime('now'). NOTE: the pre-existing `sessions.created_at` column
-- already defaults to datetime('now') (schema.sql:26) -- that is PRE-EXISTING
-- drift, out of scope for this migration (additive-only, not touching it).
-- New columns added here follow the convention for any value the daemon sets.
--
-- Idempotency: engine-cli.py cmd_migrate auto-discovers db/migrations/*.sql and
-- skips any version already present in schema_version.

-- ------------------------------------------------------------------
-- sessions: link column + auto-captured fact columns
-- ------------------------------------------------------------------
ALTER TABLE sessions ADD COLUMN session_uuid         TEXT;
ALTER TABLE sessions ADD COLUMN git_branch           TEXT;
ALTER TABLE sessions ADD COLUMN commits_count         INTEGER;
ALTER TABLE sessions ADD COLUMN files_changed_count   INTEGER;
ALTER TABLE sessions ADD COLUMN wall_time_sec          INTEGER;
ALTER TABLE sessions ADD COLUMN auto_captured_at      TEXT;      -- _utcnow_iso() of the last /ingest/session_state write, NULL if never auto-captured
ALTER TABLE sessions ADD COLUMN narrative_updated_at  TEXT;      -- _utcnow_iso() of the last session_diary(add) write, NULL if never enriched

-- One canonical row per real Claude session. Partial index (see note above):
-- only rows that HAVE a session_uuid participate in the uniqueness constraint,
-- so this is what `ON CONFLICT(session_uuid) DO UPDATE` targets.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_uuid
    ON sessions(session_uuid) WHERE session_uuid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sessions_auto_captured
    ON sessions(auto_captured_at);

-- Record migration
INSERT OR IGNORE INTO schema_version (version) VALUES (29);
