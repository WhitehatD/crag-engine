-- Phase 13: Memory arena — adjudication + supersede edges
--
-- Why this exists (2026-05-20 audit, principle #124):
-- The contradiction detector (Phase 9) FLAGS conflicting insights but does
-- not RESOLVE them. Recall happily serves both contradicting insights at
-- the top of the ranked list. The agent has no way to declare "this one
-- is correct, that one is superseded." Today's session uncovered multiple
-- live examples: insight #1887 (Phase 12 dashboard architecture, snapshots
-- table) is ranked #1 on "engine dashboard architecture" queries even
-- though the snapshots table was DROPPED in the doctrine-pure refactor
-- (insight #2187). The `_conflict` field on #1889 → #2177 is decoration
-- only — recall still returns #1889 at high rank.
--
-- This migration adds the resolution layer:
--   - supersede edges on insights + principles (winner-by-id)
--   - arena_events audit log: every adjudication leaves a record
--   - recall paths gain an implicit `WHERE superseded_by IS NULL` filter
--     (implemented in code; the column is the source of truth)
--
-- Resolution strategies (implemented in engine-cli + engine_daemon):
--   recency:    newest with >= confidence wins
--   evidence:   insight with a valid source_file (file exists) wins
--   confidence: highest (confidence × log(1 + verify_count)) wins
--   auto:       runs all 3; if >=2 agree → unambiguous winner
--   merge:      distill into a NEW insight; all inputs marked superseded
--               by the new one
--
-- Convention for supersede_reason values:
--   'arena:<strategy>'   — adjudication chose this winner via engine-cli/MCP
--   'manual:<note>'      — operator-set via supersede subcommand
--   'merged-by-distill'  — collapsed into a higher-trust principle/insight

-- Schema additions to insights table
ALTER TABLE insights ADD COLUMN superseded_by INTEGER;
ALTER TABLE insights ADD COLUMN superseded_at TEXT;
ALTER TABLE insights ADD COLUMN supersede_reason TEXT;

-- Partial index: most queries filter for "non-superseded" insights, which is
-- the vast majority. A WHERE-indexed scan keeps recall fast.
CREATE INDEX IF NOT EXISTS idx_insights_not_superseded
    ON insights(id) WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_insights_superseded_by
    ON insights(superseded_by) WHERE superseded_by IS NOT NULL;

-- Same idea for principles (a principle CAN be superseded by a newer
-- principle that the operator/agent explicitly promotes).
ALTER TABLE principles ADD COLUMN superseded_by INTEGER;
ALTER TABLE principles ADD COLUMN superseded_at TEXT;
ALTER TABLE principles ADD COLUMN supersede_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_principles_not_superseded
    ON principles(id) WHERE superseded_by IS NULL;

-- Arena audit log: every adjudication is recorded.  Operators can audit
-- the chain of decisions later (and reverse them if the arena got it wrong).
CREATE TABLE IF NOT EXISTS arena_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT    NOT NULL,
    project             TEXT,
    input_insight_ids   TEXT    NOT NULL,    -- JSON array
    winner_insight_id   INTEGER,             -- NULL if AMBIGUOUS
    strategy            TEXT    NOT NULL,    -- recency|evidence|confidence|auto|merge
    rationale           TEXT,
    merged_insight_id   INTEGER,             -- only set for strategy=merge
    verdict             TEXT    NOT NULL     -- WINNER | AMBIGUOUS | MERGED | NO_OP
);

CREATE INDEX IF NOT EXISTS idx_arena_events_ts        ON arena_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_arena_events_project   ON arena_events(project, ts DESC);
CREATE INDEX IF NOT EXISTS idx_arena_events_strategy  ON arena_events(strategy, ts DESC);

INSERT OR IGNORE INTO schema_version (version) VALUES (13);
