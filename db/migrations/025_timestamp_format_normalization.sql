-- 025 — TEXT timestamp format normalization (schema v25)
--
-- WHY: two writer conventions coexisted for every TEXT timestamp column:
--   legacy SQLite  datetime('now')  ->  'YYYY-MM-DD HH:MM:SS'   (space, naive)
--   canonical Python isoformat()    ->  'YYYY-MM-DDTHH:MM:SS.ffffff+00:00'
-- Space (0x20) sorts BEFORE 'T' (0x54), so any lexical comparison or ORDER BY
-- that mixes the two formats is wrong for same-day values. This corrupted the
-- dashboard supersede-burst watermark counting (fixed 2026-07-02, -- e0f0acc) and silently mis-ordered mixed columns. As of this migration ALL
-- Python writers emit the canonical ISO-T offset-aware format (_utcnow_iso()
-- helpers in engine_daemon.py / engine-cli.py / lifecycle.py; enforced by
-- scripts/test-timestamp-convention.py in CI). This migration converts the
-- historical rows so every column is single-format.
--
-- WHAT: for each timestamp column, two idempotent passes:
--   pass 1: space-format legacy  ->  'T' separator + '+00:00' offset
--           (datetime('now') is UTC, so the +00:00 annotation is exact)
--   pass 2: 'T'-separated but offset-naive  ->  append '+00:00'
--           (defensive: strptime-era writers occasionally produced these)
-- Guards are GLOB patterns anchored on the exact legacy shape, so re-running
-- is a no-op and canonical values are never touched. Sub-second precision is
-- not invented for legacy rows; second-boundary ordering ties between
-- '...:45+00:00' and '...:45.123456+00:00' are acceptable (documented in the
-- anomaly-fix verification, requires whole-second collision).
--
-- sessions.date is a DATE-ONLY field ('YYYY-MM-DD') and is deliberately NOT
-- touched.

-- ── insights ─────────────────────────────────────────────────────────────────
UPDATE insights SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE insights SET updated_at = replace(updated_at, ' ', 'T') || '+00:00'
 WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE insights SET verified_at = replace(verified_at, ' ', 'T') || '+00:00'
 WHERE verified_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE insights SET last_recalled_at = replace(last_recalled_at, ' ', 'T') || '+00:00'
 WHERE last_recalled_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE insights SET suspect_detected_at = replace(suspect_detected_at, ' ', 'T') || '+00:00'
 WHERE suspect_detected_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE insights SET superseded_at = replace(superseded_at, ' ', 'T') || '+00:00'
 WHERE superseded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE insights SET grounded_at = replace(grounded_at, ' ', 'T') || '+00:00'
 WHERE grounded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';

UPDATE insights SET created_at = created_at || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND created_at NOT GLOB '*+*' AND created_at NOT GLOB '*Z';
UPDATE insights SET updated_at = updated_at || '+00:00'
 WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND updated_at NOT GLOB '*+*' AND updated_at NOT GLOB '*Z';
UPDATE insights SET verified_at = verified_at || '+00:00'
 WHERE verified_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND verified_at NOT GLOB '*+*' AND verified_at NOT GLOB '*Z';
UPDATE insights SET last_recalled_at = last_recalled_at || '+00:00'
 WHERE last_recalled_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND last_recalled_at NOT GLOB '*+*' AND last_recalled_at NOT GLOB '*Z';
UPDATE insights SET suspect_detected_at = suspect_detected_at || '+00:00'
 WHERE suspect_detected_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND suspect_detected_at NOT GLOB '*+*' AND suspect_detected_at NOT GLOB '*Z';
UPDATE insights SET superseded_at = superseded_at || '+00:00'
 WHERE superseded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND superseded_at NOT GLOB '*+*' AND superseded_at NOT GLOB '*Z';
UPDATE insights SET grounded_at = grounded_at || '+00:00'
 WHERE grounded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND grounded_at NOT GLOB '*+*' AND grounded_at NOT GLOB '*Z';

-- ── principles ───────────────────────────────────────────────────────────────
UPDATE principles SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE principles SET updated_at = replace(updated_at, ' ', 'T') || '+00:00'
 WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE principles SET suspect_detected_at = replace(suspect_detected_at, ' ', 'T') || '+00:00'
 WHERE suspect_detected_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE principles SET superseded_at = replace(superseded_at, ' ', 'T') || '+00:00'
 WHERE superseded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE principles SET grounded_at = replace(grounded_at, ' ', 'T') || '+00:00'
 WHERE grounded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';

UPDATE principles SET created_at = created_at || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND created_at NOT GLOB '*+*' AND created_at NOT GLOB '*Z';
UPDATE principles SET updated_at = updated_at || '+00:00'
 WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND updated_at NOT GLOB '*+*' AND updated_at NOT GLOB '*Z';
UPDATE principles SET suspect_detected_at = suspect_detected_at || '+00:00'
 WHERE suspect_detected_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND suspect_detected_at NOT GLOB '*+*' AND suspect_detected_at NOT GLOB '*Z';
UPDATE principles SET superseded_at = superseded_at || '+00:00'
 WHERE superseded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND superseded_at NOT GLOB '*+*' AND superseded_at NOT GLOB '*Z';
UPDATE principles SET grounded_at = grounded_at || '+00:00'
 WHERE grounded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'
   AND grounded_at NOT GLOB '*+*' AND grounded_at NOT GLOB '*Z';

-- ── event / queue / audit tables ─────────────────────────────────────────────
UPDATE arena_events SET ts = replace(ts, ' ', 'T') || '+00:00'
 WHERE ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE broadcast_events SET emitted_at = replace(emitted_at, ' ', 'T') || '+00:00'
 WHERE emitted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE contradiction_events SET detected_at = replace(detected_at, ' ', 'T') || '+00:00'
 WHERE detected_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE entity_links SET extracted_at = replace(extracted_at, ' ', 'T') || '+00:00'
 WHERE extracted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE falsifiers SET last_run_at = replace(last_run_at, ' ', 'T') || '+00:00'
 WHERE last_run_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE falsifiers SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE falsifiers SET updated_at = replace(updated_at, ' ', 'T') || '+00:00'
 WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE grounding_queue SET enqueued_at = replace(enqueued_at, ' ', 'T') || '+00:00'
 WHERE enqueued_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE grounding_queue SET resolved_at = replace(resolved_at, ' ', 'T') || '+00:00'
 WHERE resolved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE insights_staged SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE insights_staged SET reviewed_at = replace(reviewed_at, ' ', 'T') || '+00:00'
 WHERE reviewed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE operator_audit_log SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE pending_events SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE pending_events SET claimed_at = replace(claimed_at, ' ', 'T') || '+00:00'
 WHERE claimed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE pending_events SET completed_at = replace(completed_at, ' ', 'T') || '+00:00'
 WHERE completed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE pending_events SET expires_at = replace(expires_at, ' ', 'T') || '+00:00'
 WHERE expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE plans SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE plans SET updated_at = replace(updated_at, ' ', 'T') || '+00:00'
 WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE project_context SET updated_at = replace(updated_at, ' ', 'T') || '+00:00'
 WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE recall_events SET recalled_at = replace(recalled_at, ' ', 'T') || '+00:00'
 WHERE recalled_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE recall_feedback SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE recall_filter_events SET ts = replace(ts, ' ', 'T') || '+00:00'
 WHERE ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE recall_timings SET ts = replace(ts, ' ', 'T') || '+00:00'
 WHERE ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE session_meta SET started_at = replace(started_at, ' ', 'T') || '+00:00'
 WHERE started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE session_meta SET last_seen_at = replace(last_seen_at, ' ', 'T') || '+00:00'
 WHERE last_seen_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE sessions SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE task_clusters SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
UPDATE token_ledger SET created_at = replace(created_at, ' ', 'T') || '+00:00'
 WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';

-- Record migration FIRST, then normalize schema_version.applied_at — the
-- INSERT uses the column's legacy space-format DEFAULT, so normalizing before
-- recording would leave this migration's own row as the one space-format
-- exception (caught by verification 2026-07-02).
INSERT OR IGNORE INTO schema_version (version) VALUES (25);

-- schema_version.applied_at is bookkeeping for this very mechanism; normalize
-- it too (including the row just inserted) so the table is not its own exception.
UPDATE schema_version SET applied_at = replace(applied_at, ' ', 'T') || '+00:00'
 WHERE applied_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]*';
