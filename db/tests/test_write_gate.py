#!/usr/bin/env python3
# coding: utf-8
"""write_gate.py unit test suite — REV 3 write-path governance.

Standalone (no pytest — mirrors apps/daemon/tests/test_staging_removal.py).

Tests:
  T_SCHEMA_OK       — valid content/type/tags passes the schema gate.
  T_SCHEMA_SHORT    — content below MIN_CONTENT_LEN is a HARD reject.
  T_SCHEMA_LONG     — content above MAX_CONTENT_LEN is a HARD reject.
  T_SCHEMA_TYPE     — invalid `type` value is a HARD reject.
  T_SCHEMA_PROV_ADV — missing session_id/source_file/evidence-ref is ADVISORY
                      only (ok=True, provenance_present=False) — T_DIRECT.
  T_SECRET_*        — each credential-shaped pattern is caught; benign text
                      with superficially similar tokens is NOT flagged.
  T_HARD_GATE_ORDER — evaluate_hard_gates returns schema reason before
                      running the secret scan (short-circuit on first HARD
                      failure), and returns None when both gates pass.
  T_LIFECYCLE_*     — resolve_lifecycle: no-candidates -> new; explicit
                      "supersedes #N" -> supersede; near-dup (>=0.97) -> noop;
                      lower-similarity dup -> update.
  T_STAGING_WRITE   — route_to_staging inserts a row into insights_staging
                      with the given reason and returns its id.
  T_STAGING_FAILSOFT— route_to_staging never raises even against a connection
                      missing the target table.

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python db/tests/test_write_gate.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_DIR = Path(__file__).resolve().parents[1]
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

import write_gate  # noqa: E402

FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSES.append(name)
        print(f"  [v] {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [x] {name}: {detail}")


# ---------------------------------------------------------------------------
# Schema gate
# ---------------------------------------------------------------------------

def run_T_SCHEMA():
    print("\n[T_SCHEMA]")

    v = write_gate.check_schema(
        "A perfectly reasonable insight about the dedup guard.",
        "gotcha", "tag1,tag2", "some/file.py", "sess-123", "operator",
    )
    check("T_SCHEMA_OK", v.ok and v.provenance_present and not v.advisories, str(v))

    v = write_gate.check_schema("ab", "gotcha", None, None, None, None)
    check("T_SCHEMA_SHORT", not v.ok and v.reason == "schema_gate:content_too_short", str(v))

    huge = "x" * (write_gate.MAX_CONTENT_LEN + 1)
    v = write_gate.check_schema(huge, "gotcha", None, None, None, None)
    check("T_SCHEMA_LONG", not v.ok and v.reason.startswith("schema_gate:content_too_long"), str(v))

    v = write_gate.check_schema(
        "Valid length content but bogus type.", "not-a-real-type", None, None, None, None,
    )
    check("T_SCHEMA_TYPE", not v.ok and v.reason.startswith("schema_gate:invalid_type"), str(v))

    # Provenance is ADVISORY, not blocking (T_DIRECT precedent).
    v = write_gate.check_schema(
        "No session, no source_file, no evidence tag — still must be accepted.",
        "decision", None, None, None, None,
    )
    check("T_SCHEMA_PROV_ADV_ok", v.ok, str(v))
    check("T_SCHEMA_PROV_ADV_flag", v.provenance_present is False, str(v))
    check("T_SCHEMA_PROV_ADV_note", len(v.advisories) == 1 and "T_DIRECT" in v.advisories[0], str(v))

    # evidence: tag in `tags` counts as provenance even with no session/file.
    v = write_gate.check_schema(
        "Has an evidence tag instead of session/file.",
        "decision", "evidence:incident-3547", None, None, None,
    )
    check("T_SCHEMA_PROV_EVIDENCE", v.ok and v.provenance_present and not v.advisories, str(v))


# ---------------------------------------------------------------------------
# Secret scan
# ---------------------------------------------------------------------------

def run_T_SECRET():
    print("\n[T_SECRET]")

    cases = [
        ("aws_access_key", "export AWS_KEY=AKIAABCD" "EFGHIJKLMNOP please rotate"),
        ("stripe_live_key", "found sk_live_4eC39Hq" "LyjWDarjtT1zdp7dc in a log line"),
        ("github_pat", "token was ghp_abcdefghij" "0123456789ABCD in the diff"),
        ("github_fine_grained_pat", "leaked github_pat_11ABCDEFG0abc" "defghijklmnop_1234567890abcdefghij"),
        ("private_key_block", "-----BEGIN RSA PRIVATE" " KEY-----\nMIIEow...\n"),
        ("generic_password_assign", 'password="hunter2plus" in the config dump'),
        ("slack_token", "webhook token xoxb-1234567" "890-abcdefghijklmno posted"),
        ("openai_key", "sk-abcdefghijklmnopqr" "stuvwx12345678 was in the prompt"),
        ("anthropic_key", "sk-ant-api03-abcdefghijklm" "nopqrstuvwxyz01234567 in the env"),
    ]
    for expected_name, text in cases:
        hit = write_gate.scan_content_secrets(text)
        check(f"T_SECRET_{expected_name}", hit == expected_name, f"got {hit!r} for {text!r}")

    # Benign text that merely mentions the word "password" without a live
    # value assignment must NOT be flagged.
    benign = "Remember to rotate the database password every quarter per policy."
    check("T_SECRET_benign_password_mention", write_gate.scan_content_secrets(benign) is None,
          f"false positive: {benign!r}")

    # Empty/None content is never flagged.
    check("T_SECRET_empty", write_gate.scan_content_secrets("") is None)
    check("T_SECRET_none", write_gate.scan_content_secrets(None) is None)


# ---------------------------------------------------------------------------
# evaluate_hard_gates — order + fail-open
# ---------------------------------------------------------------------------

def run_T_HARD_GATE_ORDER():
    print("\n[T_HARD_GATE_ORDER]")

    # Schema failure (too short) must be reported even though the string
    # also happens to be innocuous re: secrets.
    reason = write_gate.evaluate_hard_gates("ab", "gotcha", None, None, None, None)
    check("T_HARD_GATE_schema_first", reason == "schema_gate:content_too_short", str(reason))

    # Passes schema, fails secret scan.
    reason = write_gate.evaluate_hard_gates(
        "This message embeds AKIAABCD" "EFGHIJKLMNOP as a live key.",
        "gotcha", None, None, None, None,
    )
    check("T_HARD_GATE_secret", reason == "secret_scan:aws_access_key", str(reason))

    # Both pass -> None.
    reason = write_gate.evaluate_hard_gates(
        "A clean insight with no secrets and a valid type.",
        "pattern", None, "src/file.py", "sess-1", "operator",
    )
    check("T_HARD_GATE_clean", reason is None, str(reason))


# ---------------------------------------------------------------------------
# Lifecycle resolver
# ---------------------------------------------------------------------------

def run_T_LIFECYCLE():
    print("\n[T_LIFECYCLE]")

    r = write_gate.resolve_lifecycle("Some new observation.", [])
    check("T_LIFECYCLE_new", r == {"action": "new", "target_id": None}, str(r))

    r = write_gate.resolve_lifecycle("This supersedes #4821 with the corrected value.", [])
    check("T_LIFECYCLE_supersede", r == {"action": "supersede", "target_id": 4821}, str(r))

    r = write_gate.resolve_lifecycle("This replaces #17 entirely.", [])
    check("T_LIFECYCLE_replaces", r == {"action": "supersede", "target_id": 17}, str(r))

    candidates_noop = [
        {"id": 10, "content": "a", "similarity": 0.90},
        {"id": 11, "content": "b", "similarity": 0.985},
    ]
    r = write_gate.resolve_lifecycle("Near-identical restatement.", candidates_noop)
    check("T_LIFECYCLE_noop", r == {"action": "noop", "target_id": 11}, str(r))

    candidates_update = [
        {"id": 20, "content": "a", "similarity": 0.55},
        {"id": 21, "content": "b", "similarity": 0.80},
    ]
    r = write_gate.resolve_lifecycle("Related but not identical.", candidates_update)
    check("T_LIFECYCLE_update", r == {"action": "update", "target_id": 21}, str(r))

    # Invalidation verb takes precedence over dup candidates.
    r = write_gate.resolve_lifecycle("supersedes #99 per the incident review.", candidates_noop)
    check("T_LIFECYCLE_supersede_precedence", r == {"action": "supersede", "target_id": 99}, str(r))


# ---------------------------------------------------------------------------
# Staging writer
# ---------------------------------------------------------------------------

def _make_staging_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE insights_staging (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             source TEXT, project TEXT, payload TEXT, dedup_key TEXT,
             status TEXT, reason TEXT, created_at TEXT
           )"""
    )
    conn.commit()
    return conn


def run_T_STAGING():
    print("\n[T_STAGING]")

    conn = _make_staging_db()
    staged_id = write_gate.route_to_staging(
        conn, "Rejected content example.", "gotcha", "test-project",
        "schema_gate:content_too_short",
    )
    check("T_STAGING_returns_id", isinstance(staged_id, int), f"got {staged_id!r}")

    row = conn.execute("SELECT * FROM insights_staging WHERE id=?", (staged_id,)).fetchone()
    check("T_STAGING_row_exists", row is not None, "no row inserted")
    if row is not None:
        check("T_STAGING_reason", row["reason"] == "schema_gate:content_too_short", str(dict(row)))
        check("T_STAGING_status_pending", row["status"] == "pending", str(dict(row)))
        check("T_STAGING_project", row["project"] == "test-project", str(dict(row)))
    conn.close()

    # Fail-soft: connection with NO insights_staging table must not raise,
    # and must return None.
    bad_conn = sqlite3.connect(":memory:")
    try:
        result = write_gate.route_to_staging(bad_conn, "content", "gotcha", "p", "some_reason")
        check("T_STAGING_FAILSOFT", result is None, f"expected None, got {result!r}")
    except Exception as exc:
        check("T_STAGING_FAILSOFT", False, f"route_to_staging raised: {exc}")
    finally:
        bad_conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    run_T_SCHEMA()
    run_T_SECRET()
    run_T_HARD_GATE_ORDER()
    run_T_LIFECYCLE()
    run_T_STAGING()

    total = len(PASSES) + len(FAILURES)
    print(f"\n{'=' * 60}")
    print(f"Results: {len(PASSES)}/{total} passed, {len(FAILURES)} failed")
    if FAILURES:
        print("\nFailed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("All tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
