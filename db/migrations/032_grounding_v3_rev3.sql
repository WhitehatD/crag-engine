-- 032 - Grounding v3 REV 3: correction fast-path schema + write-path staging reason
--
-- WHY: docs/architecture.md REV 3 (2026-07-17) extends the claim layer with two
-- additions beyond migration 031:
--   A. Write-path governance — writes failing a save-time gate route to
--      insights_staging with a MACHINE-READABLE REASON. 031's insights_staging
--      had no column to carry that reason; this migration adds it.
--   B. Correction fast-path (TRACE-style) — capture events with correction
--      signals get an APPLICABILITY JSON field on their claims (conditions
--      under which the rule applies). Schema support only; the extractor
--      itself is a later workstream (per the brief).
--
-- ADDITIVE ONLY. No existing table/row is dropped or rewritten.
--
-- Timestamp convention: ALL TEXT timestamp columns use the canonical Python
-- _utcnow_iso() output. NEVER datetime('now').
--
-- Idempotency: cmd_migrate skips this file if schema_version already has 32.
-- ADD COLUMN has no IF NOT EXISTS; caller tolerates "duplicate column name",
-- mirroring the migration-026/031 pattern.

-- ---------------------------------------------------------------------------
-- claims.applicability: JSON array of condition strings under which the
-- claim's rule applies (e.g. ["only on Windows", "only for background LLM
-- roles"]). NULL = unconditional (the common case today). Populated by the
-- (future) correction-signal extractor; every other claim leaves it NULL.
-- ---------------------------------------------------------------------------
ALTER TABLE claims ADD COLUMN applicability TEXT;

-- ---------------------------------------------------------------------------
-- insights_staging.reason: machine-readable reason a write-path gate routed
-- this capture/save to staging instead of the corpus (e.g.
-- "schema_gate:missing_provenance", "secret_scan:AKIA_pattern",
-- "dedup:merged_into_4821"). NULL for staging rows created before this
-- migration (031's /capture/event path) — those are un-gated captures, not
-- gate rejections, so a NULL reason there is correct, not a gap.
-- ---------------------------------------------------------------------------
ALTER TABLE insights_staging ADD COLUMN reason TEXT;

-- ---------------------------------------------------------------------------
-- insights_staging.lifecycle_action: the TRACE-style resolver verdict when a
-- write WAS attempted against the existing corpus before landing in staging
-- or being accepted (noop|update|supersede|split|new). NULL when the gate
-- rejected the write before the resolver ran (schema/secret failures never
-- reach the resolver).
-- ---------------------------------------------------------------------------
ALTER TABLE insights_staging ADD COLUMN lifecycle_action TEXT;

INSERT OR IGNORE INTO schema_version (version) VALUES (32);
