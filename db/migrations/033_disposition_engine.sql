-- 033 - The Disposition Engine (docs/architecture.md REV 5 §5.2, REV 7 §7.1)
--
-- WHY: every promotion in the loop (staging→insight, insight→principle,
-- principle→rule-eligible, .gen regen) is the SAME shape — a proposed state
-- transition needing a decision — carrying four properties: MCP tool, policy
-- tier, attribution, reversibility, plus a drain-SLA so nothing rots forever.
-- This migration adds the control-plane schema on top of the v3 staging
-- ledger (migrations 031/032) WITHOUT replacing it: insights_staging remains
-- the proposal ledger; this migration adds the columns + tables the
-- Disposition Engine (db/disposition.py) needs to govern it.
--
-- ADDITIVE ONLY. No existing table/row is dropped or rewritten.
--
-- Timestamp convention: ALL TEXT timestamp columns use the canonical Python
-- _utcnow_iso() output. NEVER datetime('now').
--
-- Idempotency: cmd_migrate skips this file if schema_version already has 33.
-- ADD COLUMN has no IF NOT EXISTS; caller tolerates "duplicate column name",
-- mirroring the migration-026/031/032 pattern (see apps/daemon/tests/
-- test_grounding_v3_rev3.py::_apply_sql_file for the reference harness).

-- ---------------------------------------------------------------------------
-- insights_staging: disposition-engine columns.
--   tier        : t0 (auto) | t1 (agent-delegable) | t2 (human). Lazily
--                 stamped by disposition.stamp_tier() the first time a row is
--                 listed/resolved/drained — write_gate.route_to_staging()
--                 itself is UNCHANGED (least-invasive: the engine wraps the
--                 existing ledger, it does not require every writer to learn
--                 about tiers).
--   actor       : who/what decided (agent id / "operator" / "system:drain-sla").
--                 Mandatory on every resolve() call (attribution invariant).
--   decided_at  : when a disposition was recorded (accept|reject|merge|defer).
--   deadline    : drain-SLA — ISO timestamp past which drain_due() forces a
--                 terminal-or-safe-default action. NULL = not yet stamped.
--   disposition : accepted|rejected|merged|deferred. Distinct from the
--                 existing `status` column (pending|accepted|rejected|merged,
--                 migration 031) — `status` is the ledger state machine,
--                 `disposition` is the governed OUTCOME recorded by the
--                 engine (kept separate so a future outcome taxonomy can
--                 diverge from the ledger's terminal states without a schema
--                 change to either).
-- ---------------------------------------------------------------------------
ALTER TABLE insights_staging ADD COLUMN tier TEXT;
ALTER TABLE insights_staging ADD COLUMN actor TEXT;
ALTER TABLE insights_staging ADD COLUMN decided_at TEXT;
ALTER TABLE insights_staging ADD COLUMN deadline TEXT;
ALTER TABLE insights_staging ADD COLUMN disposition TEXT;

CREATE INDEX IF NOT EXISTS idx_staging_tier ON insights_staging(tier);
CREATE INDEX IF NOT EXISTS idx_staging_deadline ON insights_staging(deadline)
    WHERE deadline IS NOT NULL;

-- ---------------------------------------------------------------------------
-- disposition_log: the attribution/audit spine for EVERY governed promotion.
-- One row per transition, regardless of which entity kind moved (staging row,
-- insight, principle) — mirrors the insight_claims/principle_claims pattern
-- of separate nullable FK columns per referenced kind rather than a single
-- polymorphic (entity_kind, entity_id) pair, so each column stays a real FK
-- an operator can join straight through.
--   transition : e.g. "staging→insight", "staging→rejected",
--                "staging→merged", "staging→deferred", "insight→principle".
--   from_state / to_state : human-readable state labels (e.g. "pending" ->
--                "accepted", or "pending" -> "merged_into:4821").
--   actor      : REQUIRED (attribution invariant — never NULL in practice;
--                left nullable at the schema level only because SQLite has no
--                cheap conditional NOT NULL and the write path enforces it).
--   tier       : the policy tier in effect at decision time (t0|t1|t2).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS disposition_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    staging_id   INTEGER,
    insight_id   INTEGER,
    claim_id     INTEGER,
    principle_id INTEGER,
    transition   TEXT    NOT NULL,
    from_state   TEXT,
    to_state     TEXT,
    actor        TEXT,
    reason       TEXT,
    tier         TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disposition_log_staging   ON disposition_log(staging_id);
CREATE INDEX IF NOT EXISTS idx_disposition_log_insight   ON disposition_log(insight_id);
CREATE INDEX IF NOT EXISTS idx_disposition_log_principle ON disposition_log(principle_id);
CREATE INDEX IF NOT EXISTS idx_disposition_log_created   ON disposition_log(created_at DESC);

-- ---------------------------------------------------------------------------
-- disposition_policy: maps (source, type, reason_prefix) -> default tier +
-- default action + drain-SLA deadline. Rows are matched most-specific-first
-- (non-NULL reason_prefix before the wildcard row); disposition.classify_tier
-- owns the matching logic, this table is pure data. A single wildcard row
-- (source=NULL, type=NULL, reason_prefix=NULL) is the catch-all default and
-- MUST always exist — seeded below, never deleted by the /disposition/policy
-- endpoint (it only upserts additional, more specific rules).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS disposition_policy (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT,
    type           TEXT,
    reason_prefix  TEXT,
    tier           TEXT    NOT NULL,
    default_action TEXT    NOT NULL,
    deadline_hours INTEGER NOT NULL,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_disposition_policy_key
    ON disposition_policy(COALESCE(source, ''), COALESCE(type, ''), COALESCE(reason_prefix, ''));

INSERT OR IGNORE INTO disposition_policy
    (source, type, reason_prefix, tier, default_action, deadline_hours, created_at)
VALUES
    (NULL, NULL, 'secret_scan:',          't2', 'defer',  168, '2026-07-17T00:00:00+00:00'),
    (NULL, NULL, 'schema_gate:',          't1', 'defer',   72, '2026-07-17T00:00:00+00:00'),
    (NULL, NULL, 'dedup_ambiguous',       't1', 'defer',   72, '2026-07-17T00:00:00+00:00'),
    (NULL, NULL, 'lifecycle:supersede',   't1', 'defer',   72, '2026-07-17T00:00:00+00:00'),
    (NULL, NULL, NULL,                    't0', 'accept',  24, '2026-07-17T00:00:00+00:00');

-- ---------------------------------------------------------------------------
-- operator_decision_history: the rev-7 (§7.1 Autonomic Disposition) learning
-- substrate. Every human approve/reject on a governed transition is recorded
-- here so a future auto-tuner can raise/lower a class's tier from the
-- operator's own track record ("consistently approve class X -> X's tier
-- auto-raises"). Storage only in this migration; disposition.
-- suggest_tier_from_history() is a documented STUB (TODO(rev7)) that always
-- returns None — the learning loop itself is a later workstream.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operator_decision_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_kind   TEXT    NOT NULL,   -- 'staging'|'insight'|'principle'
    entity_id     INTEGER NOT NULL,
    decision_class TEXT,              -- e.g. reason_prefix / policy bucket
    decision      TEXT    NOT NULL,   -- 'approve'|'reject'
    actor         TEXT,
    reason        TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operator_decision_class ON operator_decision_history(decision_class);
CREATE INDEX IF NOT EXISTS idx_operator_decision_created ON operator_decision_history(created_at DESC);

INSERT OR IGNORE INTO schema_version (version) VALUES (33);
