-- Migration 022 - token_ledger: add role column, UNIQUE(session_id), dedup
--
-- Problem: the old INSERT OR IGNORE + per-turn writes produced up to 210 rows
-- per session_id (4755 rows, 322 distinct non-null session_ids).  The role_q
-- JOIN fan-out inflated SUM(tokens) to 119B.  Backfill via
-- POST /admin/backfill_token_ledger will overwrite token columns with
-- authoritative transcript values after this migration runs.
--
-- Strategy: TABLE REBUILD (SQLite does not support ALTER TABLE ADD CONSTRAINT
-- after-the-fact on an existing table with data).
--
-- Dedup rule per session_id:
--   tokens_in, tokens_out, etc -> SUM  (so any non-zero partial data accumulates)
--   recall_hits, recall_misses, repeated_errors, novel_saves -> SUM
--   cache_hits, cache_misses -> SUM
--   rtk_savings_pct, headroom_savings_pct, wall_time_sec -> MAX (last known)
--   model, project, task_summary -> MAX (any non-null, MAX picks latest string)
--   created_at -> MIN (first time this session appeared)
--
-- NULL/empty session_id rows: assigned a synthetic unique key via
-- 'null-row-'||id so they don't collapse together in the GROUP BY.  After
-- the rebuild, their session_id is NORMALIZED to NULL (empty strings '' also
-- become NULL).  We do this because:
--   1. SQLite UNIQUE allows multiple NULLs (NULL != NULL) but NOT multiple ''.
--   2. A full column-level UNIQUE(session_id) is required for the
--      ON CONFLICT(session_id) DO UPDATE upsert path (partial indexes cannot
--      back an upsert conflict target).
-- Normalizing '' -> NULL lets every legacy orphan survive individually while
-- still permitting upsert on real session_ids.  We never fabricate UUIDs.

-- Step 1: Create new table with role column + full UNIQUE(session_id).
CREATE TABLE IF NOT EXISTS token_ledger_new (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            TEXT    UNIQUE,            -- one row per real session; multiple NULLs allowed
    project               TEXT,
    task_summary          TEXT,
    tokens_in             INTEGER DEFAULT 0,
    tokens_out            INTEGER DEFAULT 0,
    cache_hits            INTEGER DEFAULT 0,
    cache_misses          INTEGER DEFAULT 0,
    rtk_savings_pct       REAL    DEFAULT 0,
    headroom_savings_pct  REAL    DEFAULT 0,
    wall_time_sec         REAL    DEFAULT 0,
    model                 TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    cache_read_tokens     INTEGER DEFAULT 0,
    cache_write_tokens    INTEGER DEFAULT 0,
    fresh_input_tokens    INTEGER DEFAULT 0,
    recall_hits           INTEGER DEFAULT 0,
    recall_misses         INTEGER DEFAULT 0,
    repeated_errors       INTEGER DEFAULT 0,
    novel_saves           INTEGER DEFAULT 0,
    role                  TEXT                      -- NEW: 'coordinator'|'subagent'|'operator'|NULL
);

-- Step 2: Collapse duplicates into one row per session_id.
--
-- Non-null session_ids: GROUP BY session_id, SUM token columns.
-- NULL/empty session_ids: each row gets a unique dedup key ('null-row-'||id)
-- so no two NULL rows are grouped together, but we INSERT them with their
-- original session_id (NULL or ''), not the synthetic key.
--
-- We use a CTE so we can derive the synthetic key for GROUP BY without
-- affecting the inserted session_id value.
INSERT INTO token_ledger_new
    (session_id, project, task_summary,
     tokens_in, tokens_out,
     cache_hits, cache_misses,
     rtk_savings_pct, headroom_savings_pct, wall_time_sec,
     model, created_at,
     cache_read_tokens, cache_write_tokens, fresh_input_tokens,
     recall_hits, recall_misses, repeated_errors, novel_saves,
     role)
SELECT
    -- Restore session_id: NULL/empty rows normalize to NULL (multiple NULLs OK
    -- under SQLite UNIQUE); real session_ids restored verbatim.
    CASE WHEN dedup_key LIKE 'null-row-%' THEN NULL ELSE dedup_key END
        AS session_id,
    MAX(project)               AS project,
    MAX(task_summary)          AS task_summary,
    COALESCE(SUM(tokens_in),0)             AS tokens_in,
    COALESCE(SUM(tokens_out),0)            AS tokens_out,
    COALESCE(SUM(cache_hits),0)            AS cache_hits,
    COALESCE(SUM(cache_misses),0)          AS cache_misses,
    MAX(rtk_savings_pct)       AS rtk_savings_pct,
    MAX(headroom_savings_pct)  AS headroom_savings_pct,
    MAX(wall_time_sec)         AS wall_time_sec,
    MAX(model)                 AS model,
    MIN(created_at)            AS created_at,
    COALESCE(SUM(cache_read_tokens),0)     AS cache_read_tokens,
    COALESCE(SUM(cache_write_tokens),0)    AS cache_write_tokens,
    COALESCE(SUM(fresh_input_tokens),0)    AS fresh_input_tokens,
    COALESCE(SUM(recall_hits),0)           AS recall_hits,
    COALESCE(SUM(recall_misses),0)         AS recall_misses,
    COALESCE(SUM(repeated_errors),0)       AS repeated_errors,
    COALESCE(SUM(novel_saves),0)           AS novel_saves,
    NULL                       AS role     -- backfilled by /ingest/session_tokens
FROM (
    SELECT
        id,
        session_id                         AS orig_session_id,
        CASE
            WHEN session_id IS NULL OR session_id = ''
                THEN 'null-row-' || id     -- unique per NULL/empty row
            ELSE session_id
        END                                AS dedup_key,
        project, task_summary,
        tokens_in, tokens_out,
        cache_hits, cache_misses,
        rtk_savings_pct, headroom_savings_pct, wall_time_sec,
        model, created_at,
        cache_read_tokens, cache_write_tokens, fresh_input_tokens,
        recall_hits, recall_misses, repeated_errors, novel_saves
    FROM token_ledger
) src
GROUP BY dedup_key;

-- Step 3: Swap tables
DROP TABLE token_ledger;
ALTER TABLE token_ledger_new RENAME TO token_ledger;

-- Step 4: Recreate indexes (matching originals + new role index)
CREATE INDEX IF NOT EXISTS idx_ledger_date    ON token_ledger(created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_project ON token_ledger(project);
CREATE INDEX IF NOT EXISTS idx_ledger_role    ON token_ledger(role);

-- Step 5: Record migration
INSERT OR IGNORE INTO schema_version (version) VALUES (22);
