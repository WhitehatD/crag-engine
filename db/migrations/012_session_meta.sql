-- Phase 12: canonical claude session → project mapping
--
-- Why this exists (2026-05-20 audit):
-- The dashboard's per-project token breakdown showed ~100% of recent rows
-- as project='unknown' even though projects were clearly known. Root cause:
--   - claude-code does not send the `x-claude-project` header on API calls.
--   - model-router stores `metadata.user_id` (a JSON blob) as token_events.session_id
--     verbatim, so token_events.project ends up NULL.
--   - engine.db `sessions` table has no `session_id` column — its session_ids
--     are date-time keys from engine-cli (`20260409-045851`), entirely
--     unrelated to claude's UUIDs. ZERO overlap between the two systems.
-- There was no canonical place to store {claude_session_uuid → project}.
--
-- This migration adds that canonical mapping. the (former) collector LEFT JOINs
-- `token_events.session_id` (after extracting the inner UUID via json_extract)
-- to `session_meta.session_uuid` to resolve project. Rows that still cannot
-- be resolved are bucketed as 'unattributed' (distinct from the legacy
-- 'unknown' so operators can tell the new path is live).
--
-- Population strategy:
--   - A claude-code UserPromptSubmit hook UPSERTs on every prompt
--     (idempotent; only `last_seen_at` changes on repeated calls).
--   - model-router can also UPSERT on first request per session — read-path
--     resolution falls back to this if the hook missed.

CREATE TABLE IF NOT EXISTS session_meta (
    session_uuid TEXT PRIMARY KEY,   -- canonical claude session UUID (no JSON wrapping)
    project      TEXT NOT NULL,      -- e.g. 'myproject' — from CWD or x-crag-project header
    cwd          TEXT,               -- absolute path where the session started (informational)
    started_at   TEXT NOT NULL,      -- ISO 8601 UTC of first observation
    last_seen_at TEXT NOT NULL,      -- ISO 8601 UTC of most recent observation
    source       TEXT NOT NULL DEFAULT 'unknown'  -- 'hook' | 'router' | 'manual'
);

CREATE INDEX IF NOT EXISTS idx_session_meta_project   ON session_meta(project);
CREATE INDEX IF NOT EXISTS idx_session_meta_last_seen ON session_meta(last_seen_at DESC);

INSERT OR IGNORE INTO schema_version (version) VALUES (12);
