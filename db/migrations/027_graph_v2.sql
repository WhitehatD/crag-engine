-- Migration 027: Graph v2 — canonical entities + typed relations + claim relations
-- Additive only: no existing tables/rows are dropped or changed (except ALTER ADD COLUMN).
-- entity_links is kept intact (append-only doctrine). New columns are nullable FKs.

-- 1. Canonical entity registry: one row per (entity_type, raw_value) pair.
--    canonical: normalized form (lowercase, path-collapsed, port stripped of leading zeros).
--    aliases: JSON array of alternate raw values that resolved to this canonical.
--    volatility_class: low|medium|high|topology (mirrors falsifiers.volatility_class).
CREATE TABLE IF NOT EXISTS entity_canonical (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type      TEXT NOT NULL,
    raw_value        TEXT NOT NULL,
    canonical        TEXT NOT NULL,
    aliases          TEXT DEFAULT '[]',      -- JSON array
    volatility_class TEXT DEFAULT 'medium',
    extracted_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(entity_type, raw_value)
);

CREATE INDEX IF NOT EXISTS idx_entity_canonical_type_canonical
    ON entity_canonical(entity_type, canonical);
CREATE INDEX IF NOT EXISTS idx_entity_canonical_canonical
    ON entity_canonical(canonical);

-- 2. Entity-to-entity typed relations (e.g. ip USES_PORT port, domain RESOLVES_TO ip).
--    Seeded mechanically from co-occurrence within a bounded cross-product per
--    insight (<= _ER_MAX_PER_TYPE candidates per side in engine-cli.py's
--    cmd_backfill_graph_v2 — see PART E advisory 2). An earlier version
--    required EXACTLY 1 candidate per side, which silently produced zero
--    relations for the busiest cross-referenced entities (multi-port/service
--    insights are the common case, not the exception) — root-caused via
--    GET /graph/impact returning hop1_neighbors=0 for port 8787 despite 19
--    co-occurring insights. Tier-B LLM authoring (confidence-scored,
--    predicate-aware relations) is still future work; this table only holds
--    mechanically-derived edges today.
CREATE TABLE IF NOT EXISTS entity_relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_a_id INTEGER NOT NULL REFERENCES entity_canonical(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL
        CHECK(relation_type IN ('USES_PORT','RESOLVES_TO','HOSTS','DEPENDS_ON','DEPLOYED_TO')),
    entity_b_id INTEGER NOT NULL REFERENCES entity_canonical(id) ON DELETE CASCADE,
    metadata    TEXT DEFAULT '{}',           -- JSON: source insight ids, confidence, etc.
    grounded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(entity_a_id, relation_type, entity_b_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_relations_a ON entity_relations(entity_a_id);
CREATE INDEX IF NOT EXISTS idx_entity_relations_b ON entity_relations(entity_b_id);

-- 3. Claim-to-claim typed relations (first-class semantics).
--    Seeds: contradiction pairs (CONTRADICTS), superseded_by (REPLACES),
--           promoted_to provenance (REFINES), arena losers (CONTRADICTS resolved).
CREATE TABLE IF NOT EXISTS claim_relations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_a_kind TEXT NOT NULL CHECK(claim_a_kind IN ('insight','principle')),
    claim_a_id   INTEGER NOT NULL,
    relation_type TEXT NOT NULL
        CHECK(relation_type IN ('SUPPORTS','CONTRADICTS','REFINES','REPLACES','DEPENDS_ON')),
    claim_b_kind TEXT NOT NULL CHECK(claim_b_kind IN ('insight','principle')),
    claim_b_id   INTEGER NOT NULL,
    confidence   REAL DEFAULT 1.0,
    created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(claim_a_kind, claim_a_id, relation_type, claim_b_kind, claim_b_id)
);

CREATE INDEX IF NOT EXISTS idx_claim_relations_a
    ON claim_relations(claim_a_kind, claim_a_id);
CREATE INDEX IF NOT EXISTS idx_claim_relations_b
    ON claim_relations(claim_b_kind, claim_b_id);

-- 4. Add nullable FK from entity_links to entity_canonical.
--    Populated by the backfill CLI and by store-time gating going forward.
--    NULL = not yet normalized (pre-027 rows or junk-rejected rows).
ALTER TABLE entity_links ADD COLUMN canonical_entity_id INTEGER
    REFERENCES entity_canonical(id);

CREATE INDEX IF NOT EXISTS idx_entity_links_canonical
    ON entity_links(canonical_entity_id)
    WHERE canonical_entity_id IS NOT NULL;

INSERT OR IGNORE INTO schema_version (version) VALUES (27);
