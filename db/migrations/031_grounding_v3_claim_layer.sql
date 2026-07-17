-- 031 - Grounding v3: the Claim Layer (schema v31)
--
-- WHY: v2 typed/verified at INSIGHT granularity. But an insight is a NARRATIVE
-- holding many assertions of different verifiability classes at once, so 72% of
-- grounding jobs ended "mechanically unverifiable". v3 decomposes each insight
-- into ATOMIC CLAIMS, canonicalizes+dedups them (one node shared by many
-- parents), types each claim P1-P5 (closed+total taxonomy = 100% coverage by
-- construction) and verifies at claim granularity. One canonical claim drifting
-- flips liveness for every parent at once (blast radius).
--
-- ADDITIVE ONLY. No existing table/row is dropped or rewritten. v2 insight-level
-- falsifier columns are kept read-only one release (dashboard seam). Existing
-- grounding_jobs.claim_kind / grounding_history.claim_kind already accept an
-- arbitrary TEXT kind — v3 reuses them with claim_kind='claim'; the two new
-- columns below (lane) are the only additions to those tables.
--
-- Timestamp convention: ALL TEXT timestamp columns use the canonical Python
-- _utcnow_iso() output (YYYY-MM-DDTHH:MM:SS.ffffff+00:00). NEVER datetime('now').
--
-- Idempotency: cmd_migrate skips this file if schema_version already has 31.
-- Every CREATE uses IF NOT EXISTS; the ALTERs are wrapped by the test harness /
-- caller tolerating "duplicate column name" (SQLite has no ADD COLUMN IF NOT
-- EXISTS), mirroring the migration-026 pattern.

-- ---------------------------------------------------------------------------
-- claims: one row per canonical atomic assertion.
--   canonical_key   : normalized-text hash (sha1 of _normalize_claim_text()).
--   predicate_class : P1|P2|P3|P4|P5 (mechanical|documentary|temporal|semantic|axiomatic).
--   predicate_spec  : JSON, class-specific (see db/claim_layer.py for shapes).
--   status          : active|superseded.
--   review_after    : P5 only — ISO date to re-surface a preference/decision.
--   primary_entity / primary_entity_type: strongest linked entity (dedup key + blast anchor).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key       TEXT    NOT NULL,
    text                TEXT    NOT NULL,
    predicate_class     TEXT,
    predicate_spec      TEXT,
    predicate_version   INTEGER DEFAULT 1,
    status              TEXT    NOT NULL DEFAULT 'active',
    primary_entity      TEXT,
    primary_entity_type TEXT,
    review_after        TEXT,
    grounded_at         TEXT,
    grounding_due       INTEGER NOT NULL DEFAULT 0,
    last_verdict        TEXT,
    superseded_by       INTEGER,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT
);

-- Canonical dedup lookup: at most one ACTIVE claim per (canonical_key, primary_entity).
-- Embedding-similarity linking (>=0.92) is done in Python before insert; this
-- unique index is the exact-hash backstop that makes canonicalization idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_canonical_active
    ON claims(canonical_key, COALESCE(primary_entity, ''))
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_claims_class      ON claims(predicate_class);
CREATE INDEX IF NOT EXISTS idx_claims_due        ON claims(grounding_due) WHERE grounding_due = 1;
CREATE INDEX IF NOT EXISTS idx_claims_review     ON claims(review_after)  WHERE review_after IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_claims_prim_ent   ON claims(primary_entity, primary_entity_type);

-- ---------------------------------------------------------------------------
-- insight_claims / principle_claims: many-to-many parent links.
--   role   : core|supporting|context (rollup = worst-of core).
--   weight : reserved for weighted rollup (default 1.0).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS insight_claims (
    insight_id INTEGER NOT NULL,
    claim_id   INTEGER NOT NULL,
    role       TEXT    NOT NULL DEFAULT 'supporting',
    weight     REAL    NOT NULL DEFAULT 1.0,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (insight_id, claim_id)
);
CREATE INDEX IF NOT EXISTS idx_insight_claims_claim ON insight_claims(claim_id);

CREATE TABLE IF NOT EXISTS principle_claims (
    principle_id INTEGER NOT NULL,
    claim_id     INTEGER NOT NULL,
    role         TEXT    NOT NULL DEFAULT 'supporting',
    weight       REAL    NOT NULL DEFAULT 1.0,
    created_at   TEXT    NOT NULL,
    PRIMARY KEY (principle_id, claim_id)
);
CREATE INDEX IF NOT EXISTS idx_principle_claims_claim ON principle_claims(claim_id);

-- ---------------------------------------------------------------------------
-- claim_entities: joins a claim to the canonical entities it references
-- (graph v2 entity_canonical). Powers blast-radius: entity -> claims -> insights.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claim_entities (
    claim_id            INTEGER NOT NULL,
    entity              TEXT    NOT NULL,
    entity_type         TEXT    NOT NULL,
    canonical_entity_id INTEGER,
    PRIMARY KEY (claim_id, entity, entity_type)
);
CREATE INDEX IF NOT EXISTS idx_claim_entities_entity ON claim_entities(entity, entity_type);
CREATE INDEX IF NOT EXISTS idx_claim_entities_canon  ON claim_entities(canonical_entity_id)
    WHERE canonical_entity_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- claim_embeddings: 1:1 with claims. Kept in its own table (not a claims column)
-- so the canonical dedup near-dup scan can SELECT id,embedding without dragging
-- the text/spec blobs, mirroring how insights stores embedding inline but the
-- claim pool is queried in tight loops during backfill.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claim_embeddings (
    claim_id   INTEGER PRIMARY KEY,
    embedding  BLOB,
    created_at TEXT
);

-- ---------------------------------------------------------------------------
-- claim_contradictions: v3 assertion-vs-assertion pairs (replaces insight-level
-- suspect_of). Same-primary-entity + negation/value-mismatch + embedding
-- antipodality. Old insight-level detector stays live behind a config flag one
-- release; this table is where the new detector writes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claim_contradictions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_a_id   INTEGER NOT NULL,
    claim_b_id   INTEGER NOT NULL,
    reason       TEXT,
    score        REAL,
    status       TEXT NOT NULL DEFAULT 'open',
    detected_at  TEXT NOT NULL,
    resolved_at  TEXT,
    UNIQUE(claim_a_id, claim_b_id)
);
CREATE INDEX IF NOT EXISTS idx_claim_contra_status ON claim_contradictions(status);

-- ---------------------------------------------------------------------------
-- insights_staging: capture-receiving primitive (E2/E11). The daemon endpoint
-- POST /capture/event writes here; accepted rows flow through the normal save
-- path (and thus the claim pipeline). Emitters are a separate workstream.
--   source  : gate_failure|hook_block|ci_red|transcript_extract|manual
--   status  : pending|accepted|rejected|merged
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS insights_staging (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    project     TEXT,
    payload     TEXT    NOT NULL,
    dedup_key   TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending',
    created_at  TEXT    NOT NULL,
    triaged_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_staging_dedup
    ON insights_staging(dedup_key)
    WHERE dedup_key IS NOT NULL AND status IN ('pending', 'accepted');
CREATE INDEX IF NOT EXISTS idx_staging_status ON insights_staging(status);

-- ---------------------------------------------------------------------------
-- grounding_jobs / grounding_history: add `lane` so the worker can route
-- P2/P3 (free/local) separately from P1 (sandboxed shell) and P4 (LLM lane).
-- claim_kind='claim' + claim_id=<claims.id> reuse the existing keying.
-- ADD COLUMN has no IF NOT EXISTS; caller tolerates "duplicate column name".
-- ---------------------------------------------------------------------------
ALTER TABLE grounding_jobs    ADD COLUMN lane TEXT;
ALTER TABLE grounding_history ADD COLUMN lane TEXT;

INSERT OR IGNORE INTO schema_version (version) VALUES (31);
