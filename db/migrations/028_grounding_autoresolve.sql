-- 028 - Grounding autoresolve: resolution_proposals table + grounding_mode marker
--       (schema v28)
--
-- WHY: v26 (grounding v2) built the durable job queue + LLM authoring/adjudication,
-- but RESOLUTION was still 100% human: every flagged claim (449 live on 2026-07-05,
-- 434 insights + 15 principles) sits in grounding_queue waiting for a person to click
-- Verified / Noted / Dismissed on the dashboard.  Worse, many carry USELESS v1
-- mechanical falsifiers (a meta-principle "never confabulate a phantom actor" with
-- falsifier `find config.yaml`, an SEO insight with `curl example.com/`, some kind=none).
-- Running those proves nothing.  This migration adds the persistence needed to move
-- from "detect autonomously -> dump junk on the human" to
-- "detect -> GOVERNED autonomous resolution with reversibility + escalation":
--
--   1. `grounding_mode` on insights/principles — a durable 'judgment' marker for
--      claims the LLM has declared mechanically_unverifiable (kind=none junk
--      falsifiers, meta-principles, operator-preference statements).  Once set, the
--      claim is auto-cleared (grounding_due=0) and PERMANENTLY excluded from the
--      mechanical/reground sweeps — it STOPS being re-flagged.  It can still be
--      verified / updated manually: nothing is destroyed, only its grounding
--      CLASSIFICATION changes (doctrine: supersede/flag, never destroy).
--
--   2. `resolution_proposals` — the review surface for verdicts that must NOT be
--      auto-applied (uncertain verdicts, principles, high-stakes insights, or a
--      fail verdict with no confident LLM correction).  Mirrors the
--      audit_contradictions -> arena/clear_suspect pattern: the worker writes
--      proposals, a human (or a future arena-style adjudicator) approves/rejects via
--      POST /ground/proposals/{id}/decide.  auto_applied=1 rows are the audit trail
--      for mutations the worker DID make autonomously (verify-on-pass,
--      low-stakes-insight auto-correction) and back the /ground/resolutions revert
--      surface — every auto-action stays reversible (doctrine: nothing destroyed).
--
-- ADDITIVE ONLY. No existing column/table is altered destructively.
-- Timestamp convention: ALL TEXT timestamps use the canonical Python _utcnow_iso()
-- output (YYYY-MM-DDTHH:MM:SS.ffffff+00:00). NEVER SQLite datetime('now').
--
-- Idempotency: engine-cli.py cmd_migrate auto-discovers db/migrations/*.sql and skips
-- any version already present in schema_version. The per-statement try/except in the
-- test helpers is a convenience, NOT the production path.

-- ------------------------------------------------------------------
-- grounding_mode marker on insights + principles
-- ------------------------------------------------------------------
ALTER TABLE insights   ADD COLUMN grounding_mode TEXT;
ALTER TABLE principles ADD COLUMN grounding_mode TEXT;

-- Partial indexes: the sweep + recall trigger only need to EXCLUDE judgment claims,
-- so index exactly that set (kept tiny — most claims are NULL).
CREATE INDEX IF NOT EXISTS idx_insights_grounding_mode
    ON insights(grounding_mode) WHERE grounding_mode = 'judgment';
CREATE INDEX IF NOT EXISTS idx_principles_grounding_mode
    ON principles(grounding_mode) WHERE grounding_mode = 'judgment';

-- ------------------------------------------------------------------
-- resolution_proposals: escalation + auto-action audit surface
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resolution_proposals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_kind       TEXT    NOT NULL,                 -- insight | principle
    claim_id         INTEGER NOT NULL,
    verdict          TEXT,                              -- pass | fail | uncertain (from grounding_history)
    proposed_action  TEXT    NOT NULL,                  -- verify | update | supersede | dismiss
    proposed_content TEXT,                               -- new content on update action, NULL otherwise
    prior_content    TEXT,                               -- content at proposal time (revert target)
    reasoning        TEXT,
    evidence         TEXT,
    stakes           TEXT,                               -- low | high
    auto_applied     INTEGER NOT NULL DEFAULT 0,          -- 1 = worker applied without waiting for a human
    status           TEXT    NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|auto-applied|reverted|auto-expired
    created_at       TEXT    NOT NULL,
    decided_at       TEXT,
    decided_by       TEXT,
    /* confidence BEFORE an auto-verify bump (proposed_action='verify' rows only).
       Lets /ground/resolutions/{id}/revert restore the exact pre-bump trust score
       instead of only re-flagging the claim (safety-verifier FIX4, added pre-ship —
       column lives in 028 directly since this migration had not yet been applied to
       any live DB, schema_version max=27, when the fix landed). */
    prior_confidence REAL
);

CREATE INDEX IF NOT EXISTS idx_resolution_proposals_status
    ON resolution_proposals(status);
CREATE INDEX IF NOT EXISTS idx_resolution_proposals_claim
    ON resolution_proposals(claim_kind, claim_id);
-- At most one meaningful OPEN (pending) proposal per claim matters for the sweep's
-- "skip if awaiting a human" gate, so index that hot lookup.
CREATE INDEX IF NOT EXISTS idx_resolution_proposals_pending
    ON resolution_proposals(claim_kind, claim_id) WHERE status = 'pending';

-- Record migration
INSERT OR IGNORE INTO schema_version (version) VALUES (28);
