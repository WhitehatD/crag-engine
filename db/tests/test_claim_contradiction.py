#!/usr/bin/env python3
# coding: utf-8
"""claim_contradiction.py unit test suite — REV 3 claim-level contradiction
detection (v3, structural precision over the v2 insight-topicality detector).

Standalone (no pytest — mirrors db/tests/test_write_gate.py).

Tests:
  T_DISABLED       — flag off (grounding_config default) -> [] always, even
                     with an obvious negation flip on the same entity.
  T_NO_ENTITY      — claim with no primary_entity -> [] (nothing to compare).
  T_DIFF_ENTITY     — peers on a DIFFERENT primary_entity never flag.
  T_NEGATION_FLIP  — "X is up" vs "X is not up" on the same entity, no
                     embeddings present (antipodality check degrades to
                     "no evidence against" -> still flags per fail-open design).
  T_ANTONYM_FLIP    — "enabled" vs "disabled" on the same entity flags.
  T_VALUE_MISMATCH — same entity, disjoint port numbers, shared wording ->
                     flags with a value: reason, no negation/antonym needed.
  T_NO_FLAG_UNRELATED — same entity, unrelated non-contradicting claims -> [].
  T_PERSISTS       — a flagged pair is written to claim_contradictions with
                     status='open' and (lo,hi) id ordering.
  T_IDEMPOTENT     — re-running detect_for_claim on the same pair does not
                     create a second row (UNIQUE + INSERT OR IGNORE).
  T_SELF_EXCLUDED  — a claim never flags against itself.

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python db/tests/test_claim_contradiction.py
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

import claim_contradiction  # noqa: E402
import grounding_config  # noqa: E402

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
# DB helper — minimal subset of migration 031's claim-layer schema.
# ---------------------------------------------------------------------------

def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE claims (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             canonical_key TEXT, text TEXT NOT NULL,
             predicate_class TEXT, status TEXT NOT NULL DEFAULT 'active',
             primary_entity TEXT, primary_entity_type TEXT,
             created_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE claim_embeddings (
             claim_id INTEGER PRIMARY KEY, embedding BLOB, created_at TEXT
           )"""
    )
    conn.execute(
        """CREATE TABLE claim_contradictions (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             claim_a_id INTEGER NOT NULL, claim_b_id INTEGER NOT NULL,
             reason TEXT, score REAL, status TEXT NOT NULL DEFAULT 'open',
             detected_at TEXT NOT NULL, resolved_at TEXT,
             UNIQUE(claim_a_id, claim_b_id)
           )"""
    )
    conn.commit()
    return conn


def _add_claim(conn: sqlite3.Connection, text: str, entity: str | None,
               entity_type: str = "service") -> int:
    cur = conn.execute(
        "INSERT INTO claims (canonical_key, text, status, primary_entity, "
        "primary_entity_type, created_at) VALUES (?, ?, 'active', ?, ?, datetime('now'))",
        (text.lower()[:40], text, entity, entity_type if entity else None),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Config toggling — patch the module-level function claim_contradiction.py
# imports fresh each call (`import grounding_config` is local to detect_for_claim).
# ---------------------------------------------------------------------------

_ORIG_GET_CLAIMS_CONFIG = grounding_config.get_claims_config


def _enable_detector(cosine: float = 0.80):
    grounding_config.get_claims_config = lambda: {
        "claim_contradiction_enabled": True,
        "contradiction_cosine": cosine,
    }


def _disable_detector():
    grounding_config.get_claims_config = lambda: {
        "claim_contradiction_enabled": False,
        "contradiction_cosine": 0.80,
    }


def _restore_detector():
    grounding_config.get_claims_config = _ORIG_GET_CLAIMS_CONFIG


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def run_T_DISABLED():
    print("\n[T_DISABLED]")
    conn = _make_db()
    _disable_detector()
    _a = _add_claim(conn, "the tunnel is up", "vps-tunnel")
    b = _add_claim(conn, "the tunnel is not up", "vps-tunnel")
    flagged = claim_contradiction.detect_for_claim(conn, b)
    check("T_DISABLED_empty", flagged == [], str(flagged))
    row = conn.execute("SELECT COUNT(*) FROM claim_contradictions").fetchone()[0]
    check("T_DISABLED_no_rows", row == 0, f"expected 0 rows, got {row}")
    conn.close()


def run_T_NO_ENTITY():
    print("\n[T_NO_ENTITY]")
    conn = _make_db()
    _enable_detector()
    a = _add_claim(conn, "some free-floating assertion", None)
    _b = _add_claim(conn, "some other free-floating assertion", None)
    flagged = claim_contradiction.detect_for_claim(conn, a)
    check("T_NO_ENTITY_empty", flagged == [], str(flagged))
    conn.close()


def run_T_DIFF_ENTITY():
    print("\n[T_DIFF_ENTITY]")
    conn = _make_db()
    _enable_detector()
    _a = _add_claim(conn, "service X is enabled", "service-x")
    b = _add_claim(conn, "service X is disabled", "service-y")  # different entity
    flagged = claim_contradiction.detect_for_claim(conn, b)
    check("T_DIFF_ENTITY_empty", flagged == [], str(flagged))
    conn.close()


def run_T_NEGATION_FLIP():
    print("\n[T_NEGATION_FLIP]")
    conn = _make_db()
    _enable_detector()
    _a = _add_claim(conn, "the vps tunnel is up", "vps-tunnel")
    b = _add_claim(conn, "the vps tunnel is not up", "vps-tunnel")
    flagged = claim_contradiction.detect_for_claim(conn, b)
    check("T_NEGATION_FLIP_found", len(flagged) == 1, str(flagged))
    if flagged:
        check("T_NEGATION_FLIP_reason", flagged[0]["reason"].startswith("polarity-flip"),
              str(flagged[0]))
    conn.close()


def run_T_ANTONYM_FLIP():
    print("\n[T_ANTONYM_FLIP]")
    conn = _make_db()
    _enable_detector()
    _a = _add_claim(conn, "the retry feature is enabled by default", "retry-feature")
    b = _add_claim(conn, "the retry feature is disabled by default", "retry-feature")
    flagged = claim_contradiction.detect_for_claim(conn, b)
    check("T_ANTONYM_FLIP_found", len(flagged) == 1, str(flagged))
    if flagged:
        check("T_ANTONYM_FLIP_reason", "enabled/disabled" in flagged[0]["reason"]
              or "polarity-flip" in flagged[0]["reason"], str(flagged[0]))
    conn.close()


def run_T_VALUE_MISMATCH():
    print("\n[T_VALUE_MISMATCH]")
    conn = _make_db()
    _enable_detector()
    _a = _add_claim(conn, "the router listens on port 8788", "router")
    b = _add_claim(conn, "the router listens on port 8790", "router")
    flagged = claim_contradiction.detect_for_claim(conn, b)
    check("T_VALUE_MISMATCH_found", len(flagged) == 1, str(flagged))
    if flagged:
        check("T_VALUE_MISMATCH_reason", flagged[0]["reason"].startswith("value:"),
              str(flagged[0]))
    conn.close()


def run_T_NO_FLAG_UNRELATED():
    print("\n[T_NO_FLAG_UNRELATED]")
    conn = _make_db()
    _enable_detector()
    _a = _add_claim(conn, "the router logs requests to stdout", "router")
    b = _add_claim(conn, "the router supports sonnet and haiku models", "router")
    flagged = claim_contradiction.detect_for_claim(conn, b)
    check("T_NO_FLAG_UNRELATED_empty", flagged == [], str(flagged))
    conn.close()


def run_T_PERSISTS():
    print("\n[T_PERSISTS]")
    conn = _make_db()
    _enable_detector()
    a = _add_claim(conn, "the daemon health check passes", "crag-anchor")
    b = _add_claim(conn, "the daemon health check fails", "crag-anchor")
    flagged = claim_contradiction.detect_for_claim(conn, b)
    check("T_PERSISTS_found", len(flagged) == 1, str(flagged))
    lo, hi = sorted((a, b))
    row = conn.execute(
        "SELECT * FROM claim_contradictions WHERE claim_a_id=? AND claim_b_id=?",
        (lo, hi),
    ).fetchone()
    check("T_PERSISTS_row_exists", row is not None, "no row persisted")
    if row is not None:
        check("T_PERSISTS_status_open", row["status"] == "open", str(dict(row)))
        check("T_PERSISTS_ordering", row["claim_a_id"] == lo and row["claim_b_id"] == hi,
              str(dict(row)))
    conn.close()


def run_T_IDEMPOTENT():
    print("\n[T_IDEMPOTENT]")
    conn = _make_db()
    _enable_detector()
    _a = _add_claim(conn, "the watchdog restarts on failure", "watchdog")
    b = _add_claim(conn, "the watchdog never restarts on failure", "watchdog")
    flagged1 = claim_contradiction.detect_for_claim(conn, b)
    flagged2 = claim_contradiction.detect_for_claim(conn, b)  # re-run, same claim
    check("T_IDEMPOTENT_found_both", len(flagged1) == 1 and len(flagged2) == 1,
          f"{flagged1} / {flagged2}")
    count = conn.execute("SELECT COUNT(*) FROM claim_contradictions").fetchone()[0]
    check("T_IDEMPOTENT_single_row", count == 1, f"expected 1 row, got {count}")
    conn.close()


def run_T_SELF_EXCLUDED():
    print("\n[T_SELF_EXCLUDED]")
    conn = _make_db()
    _enable_detector()
    a = _add_claim(conn, "the notify queue is not empty", "notify")
    flagged = claim_contradiction.detect_for_claim(conn, a)
    check("T_SELF_EXCLUDED_empty", flagged == [], str(flagged))
    conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        run_T_DISABLED()
        run_T_NO_ENTITY()
        run_T_DIFF_ENTITY()
        run_T_NEGATION_FLIP()
        run_T_ANTONYM_FLIP()
        run_T_VALUE_MISMATCH()
        run_T_NO_FLAG_UNRELATED()
        run_T_PERSISTS()
        run_T_IDEMPOTENT()
        run_T_SELF_EXCLUDED()
    finally:
        _restore_detector()

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
