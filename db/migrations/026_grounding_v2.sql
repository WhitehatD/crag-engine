-- 026 - Grounding v2: Tier-A/B falsifier columns + durable job queue + history
--       (schema v26)
--
-- WHY: The v23 falsifier derivation tests that a NOUN exists (F1-F5 structural
-- flaws described in docs/architecture.md, section A0). v26 replaces that
-- with a two-tier system:
--   Tier A (mechanical): pure-existence claims. Existing kind/spec keep
--           working unchanged.  authored_by='mechanical', tier='A'.
--   Tier B (agentic): predicate-bearing claims. LLM authors a
--           falsification_question + structured recipe. authored_by='llm',
--           tier='B'.
--
-- The grounding_jobs table is the durable work queue (survives daemon restart).
-- Triggers ENQUEUE (never block callers): save_insight -> enqueue 'author';
-- recall aging/stale -> enqueue 'reground'.
--
-- grounding_history is append-only. Chain-of-thought is never discarded.
-- Prior reasoning+evidence is re-fed on every re-ground cycle.
--
-- ADDITIVE ONLY. Existing falsifier rows continue to work as Tier-A until
-- re-authored by the worker pool. No data is removed or rewritten here.
--
-- Timestamp convention: ALL TEXT timestamp columns use the canonical Python
-- _utcnow_iso() output (YYYY-MM-DDTHH:MM:SS.ffffff+00:00). NEVER datetime('now').
-- Enforced by scripts/test-timestamp-convention.py.
--
-- Idempotency: engine-cli.py cmd_migrate skips this migration entirely if
-- schema_version already contains version 26 (see engine-cli.py ~line 2224).
-- The per-statement try/except in test_grounding_v2.py is a test-helper
-- convenience, NOT the production path.

-- falsifiers: add Tier-B columns
ALTER TABLE falsifiers ADD COLUMN tier                  TEXT;
ALTER TABLE falsifiers ADD COLUMN falsification_question TEXT;
ALTER TABLE falsifiers ADD COLUMN recipe                TEXT;
ALTER TABLE falsifiers ADD COLUMN authored_by           TEXT;
ALTER TABLE falsifiers ADD COLUMN recipe_version        INTEGER DEFAULT 1;
ALTER TABLE falsifiers ADD COLUMN last_verdict          TEXT;

-- Backfill: existing rows are Tier-A mechanical (existence probes).
UPDATE falsifiers SET tier = 'A', authored_by = 'mechanical'
 WHERE tier IS NULL;

-- grounding_jobs: durable async work queue
CREATE TABLE IF NOT EXISTS grounding_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_kind   TEXT    NOT NULL,
    claim_id     INTEGER NOT NULL,
    job_type     TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    priority     INTEGER NOT NULL DEFAULT 0,
    enqueued_at  TEXT    NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    last_error   TEXT
);

-- Drain index: worker picks pending jobs by priority DESC, enqueued_at ASC.
CREATE INDEX IF NOT EXISTS idx_gj_status_priority
    ON grounding_jobs(status, priority DESC, enqueued_at);

-- Dedup guard: at most ONE pending job per (claim_kind, claim_id, job_type).
-- Partial index on status='pending' is the idiomatic SQLite approach.
CREATE UNIQUE INDEX IF NOT EXISTS idx_gj_pending_dedup
    ON grounding_jobs(claim_kind, claim_id, job_type)
    WHERE status = 'pending';

-- grounding_history: append-only reasoning trail
CREATE TABLE IF NOT EXISTS grounding_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_kind     TEXT    NOT NULL,
    claim_id       INTEGER NOT NULL,
    ts             TEXT    NOT NULL,
    job_type       TEXT    NOT NULL,
    verdict        TEXT,
    reasoning      TEXT,
    evidence       TEXT,
    recipe_version INTEGER
);

CREATE INDEX IF NOT EXISTS idx_gh_claim
    ON grounding_history(claim_kind, claim_id, ts);

-- Record migration
INSERT OR IGNORE INTO schema_version (version) VALUES (26);
