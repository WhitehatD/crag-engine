-- Phase 25 — Grounded Memory (schema v23)
--
-- Insights/principles are saved as point-in-time truths but the world changes,
-- so they drift silently (e.g. principle #70 pointed at a dead IP for ~3 weeks at
-- confidence 1.0). This phase makes trust a measured property of *how recently a
-- claim was re-grounded against reality* instead of a number that only rises.
--
-- Design (see docs/architecture.md):
--   * Each claim can carry a FALSIFIER — a cheap, re-runnable check derived from
--     its strongest entity_link (Phase 7). entity_type -> falsifier kind:
--       ip/domain/service -> endpoint probe; port -> grep config;
--       path/file -> existence + content grep; classname/env_var -> grep symbol;
--       version/SHA/line-number content -> 'observation' (store the QUERY, not value).
--   * Recall (Tier 1) annotates each hit with a liveness stamp from these columns
--     (pure O(1) reads, NO I/O on the hot path) and (Tier 2) enqueues stale hits.
--   * The groundskeeper cron drains the queue, runs cheap local + read-only VPS
--     probes, and FLAGS (grounding_due + grounding_queue row). It NEVER mutates
--     confidence/supersede — resolution is an agent MCP workflow (detection !=
--     resolution, mirrors audit_contradictions -> arena/clear_suspect).
--
-- volatility_class semantics:
--   invariant   - near-permanent (breathing cord, safety rules) — slow re-ground
--   topology    - IPs/ports/paths/services — re-ground on migration / on probe
--   observation - doomed-by-design literals (version/SHA/line/count) — never promote

-- ------------------------------------------------------------------
-- falsifiers: one (strongest) falsifier per claim
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS falsifiers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_kind   TEXT NOT NULL,                       -- 'insight' | 'principle'
    claim_id     INTEGER NOT NULL,
    kind         TEXT NOT NULL,                        -- endpoint | grep_config | path_exists
                                                       --  | grep_symbol | query | none
    spec         TEXT,                                 -- the probe/command (read-only)
    entity       TEXT,                                 -- the entity it was derived from
    entity_type  TEXT,                                 -- port|ip|domain|path|service|...
    derived      INTEGER NOT NULL DEFAULT 1,           -- 1 = auto-derived, 0 = explicit
    last_run_at  TEXT,
    last_result  TEXT,                                 -- pass | fail | error | skip
    last_detail  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_falsifiers_claim
    ON falsifiers(claim_kind, claim_id);
CREATE INDEX IF NOT EXISTS idx_falsifiers_result
    ON falsifiers(last_result);

-- ------------------------------------------------------------------
-- grounding_queue: review surface (like the contradiction queue).
-- The cron writes rows; agents drain via audit_grounding / clear_grounding.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grounding_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_kind   TEXT NOT NULL,                        -- 'insight' | 'principle'
    claim_id     INTEGER NOT NULL,
    reason       TEXT NOT NULL,                        -- falsifier_fail | source_changed
                                                       --  | volatile_stale | trigger:<class>
    trigger_src  TEXT,                                 -- recall|git|file-watch|periodic|cron|write
    detail       TEXT,
    status       TEXT NOT NULL DEFAULT 'open',         -- open | resolved | dismissed
    enqueued_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at  TEXT,
    resolved_by  TEXT,
    resolution   TEXT
);

-- At most one OPEN row per claim (dedup re-enqueues); resolved/dismissed unbounded for audit.
CREATE UNIQUE INDEX IF NOT EXISTS idx_gq_open_claim
    ON grounding_queue(claim_kind, claim_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_gq_status     ON grounding_queue(status);
CREATE INDEX IF NOT EXISTS idx_gq_enqueued   ON grounding_queue(enqueued_at);

-- ------------------------------------------------------------------
-- Grounding columns on insights + principles (version-guarded one-time ALTERs).
-- ------------------------------------------------------------------
ALTER TABLE insights   ADD COLUMN volatility_class TEXT;
ALTER TABLE insights   ADD COLUMN grounded_at      TEXT;
ALTER TABLE insights   ADD COLUMN grounded_against TEXT;
ALTER TABLE insights   ADD COLUMN grounding_due    INTEGER DEFAULT 0;
ALTER TABLE insights   ADD COLUMN falsifier_id     INTEGER;

ALTER TABLE principles ADD COLUMN volatility_class TEXT;
ALTER TABLE principles ADD COLUMN grounded_at      TEXT;
ALTER TABLE principles ADD COLUMN grounded_against TEXT;
ALTER TABLE principles ADD COLUMN grounding_due    INTEGER DEFAULT 0;
ALTER TABLE principles ADD COLUMN falsifier_id     INTEGER;

-- Partial indexes: the groundskeeper + Tier-1 recall query only the flagged set.
CREATE INDEX IF NOT EXISTS idx_insights_grounding_due
    ON insights(grounding_due) WHERE grounding_due = 1;
CREATE INDEX IF NOT EXISTS idx_principles_grounding_due
    ON principles(grounding_due) WHERE grounding_due = 1;
CREATE INDEX IF NOT EXISTS idx_insights_grounded_at   ON insights(grounded_at);
CREATE INDEX IF NOT EXISTS idx_insights_volatility    ON insights(volatility_class);

INSERT OR IGNORE INTO schema_version (version) VALUES (23);
