#!/usr/bin/env python3
# coding: utf-8
"""Deterministic fixture-DB seeder for the console Playwright gate.

Builds a temp engine.db the SAME way the daemon's own fresh-DB bootstrap does
(schema.sql + every db/migrations/NNN_*.sql in version order — see
engine_daemon._bootstrap_empty_db and test_aggregates._build_temp_db), then
inserts a small deterministic dataset so the POPULATED states of every console
surface render:

  - a few insights + principles
  - claims across verdicts (pass / fail / unverified) incl. a P5 axiomatic
  - one OPEN claim_contradiction pair
  - one PENDING t2 insights_staging row
  - one PENDING resolution_proposal

Usage:
    python seed.py <db_path>          # bootstrap schema + seed rows
    python seed.py <db_path> --empty  # bootstrap schema ONLY (empty-DB spec)

stdlib only (sqlite3). No daemon, no network, no embedding model. The daemon
started against this file sees schema_version populated and SKIPS its own
bootstrap, so the seeded rows survive.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# apps/console/e2e/fixtures/seed.py -> parents = [fixtures, e2e, console, apps, root]
REPO_ROOT = Path(__file__).resolve().parents[4]
DB_DIR = REPO_ROOT / "db"
SCHEMA = DB_DIR / "schema.sql"
MIGRATIONS = DB_DIR / "migrations"

NOW = datetime.now(timezone.utc)
ISO = NOW.isoformat()


def _bootstrap(conn: sqlite3.Connection) -> None:
    """Apply base schema then every migration in version order — the daemon's
    own fresh-DB path, reproduced with stdlib so CI needs no daemon to seed."""
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    applied = {
        r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()
    }
    for mf in sorted(MIGRATIONS.glob("*.sql")):
        try:
            version = int(mf.stem.split("_")[0])
        except ValueError:
            continue
        if version in applied:
            continue
        try:
            conn.executescript(mf.read_text(encoding="utf-8"))
            conn.commit()
        except sqlite3.OperationalError as exc:
            # Idempotent-tolerant, mirroring the daemon/ci bootstrap.
            msg = str(exc).lower()
            if "duplicate column name" in msg or "already exists" in msg:
                continue
            raise


def _seed_claim(conn, key, text, cls, verdict, grounded=True, primary_entity=None):
    """verdict: 'pass' | 'fail' | None (unverified). grounded stamps grounded_at."""
    cur = conn.execute(
        "INSERT INTO claims (canonical_key, text, predicate_class, status, "
        "primary_entity, primary_entity_type, last_verdict, grounded_at, "
        "grounding_due, created_at, updated_at) "
        "VALUES (?,?,?,'active',?,?,?,?,?,?,?)",
        (
            key, text, cls,
            primary_entity, "port" if primary_entity else None,
            verdict,
            ISO if grounded else None,
            1 if verdict == "fail" else 0,
            ISO, ISO,
        ),
    )
    return cur.lastrowid


def _seed(conn: sqlite3.Connection) -> None:
    # --- insights -----------------------------------------------------------
    conn.execute(
        "INSERT INTO insights (id, project, type, content, tags, status, confidence, "
        "verify_count, verify_streak, created_at, updated_at) VALUES "
        "(1,'infra','decision','The daemon binds 127.0.0.1:8786 by default.',"
        "'infra,daemon','active',0.7,2,2,?,?)",
        (ISO, ISO),
    )
    conn.execute(
        "INSERT INTO insights (id, project, type, content, tags, status, confidence, "
        "created_at, updated_at) VALUES "
        "(2,'infra','pattern','Console nav is data-driven from GET /console/modules.',"
        "'console,nav','active',0.6,?,?)",
        (ISO, ISO),
    )
    conn.execute(
        "INSERT INTO insights (id, project, type, content, tags, status, confidence, "
        "created_at, updated_at) VALUES "
        "(3,'infra','gotcha','Health returns 503 until the embedding model loads.',"
        "'daemon,health','active',0.55,?,?)",
        (ISO, ISO),
    )

    # --- principles ---------------------------------------------------------
    conn.execute(
        "INSERT INTO principles (id, project, content, confidence, tags, created_at, updated_at) "
        "VALUES (1,'infra','Never kill the breathing cord mid-session.',0.9,'safety',?,?)",
        (ISO, ISO),
    )
    conn.execute(
        "INSERT INTO principles (id, project, content, confidence, tags, created_at, updated_at) "
        "VALUES (2,'infra','Evidence backs every claimed state change.',0.85,'verification',?,?)",
        (ISO, ISO),
    )

    # --- claims across verdicts + classes -----------------------------------
    c_pass = _seed_claim(conn, "claim-pass", "port 8786 is the daemon", "P1_MECHANICAL", "pass",
                         primary_entity="8786")
    c_fail = _seed_claim(conn, "claim-fail", "port 8788 is the daemon", "P1_MECHANICAL", "fail",
                         primary_entity="8788")
    _seed_claim(conn, "claim-unverified", "the console renders on mobile", "P3_SEMANTIC",
                None, grounded=False)
    # P5 axiomatic — grounded-by-definition
    _seed_claim(conn, "claim-axiom", "trust is re-grounding, not a rising number", "P5_AXIOMATIC",
                "pass")

    # link a claim to insight 1 (core) so rollup health has something to chew on
    conn.execute(
        "INSERT INTO insight_claims (insight_id, claim_id, role, weight, created_at) "
        "VALUES (1, ?, 'core', 1.0, ?)", (c_pass, ISO),
    )
    conn.execute(
        "INSERT INTO principle_claims (principle_id, claim_id, role, weight, created_at) "
        "VALUES (1, ?, 'core', 1.0, ?)", (c_pass, ISO),
    )

    # --- one OPEN contradiction pair ----------------------------------------
    conn.execute(
        "INSERT INTO claim_contradictions (claim_a_id, claim_b_id, reason, score, status, detected_at) "
        "VALUES (?,?,'same entity, opposite value',0.91,'open',?)",
        (c_pass, c_fail, ISO),
    )

    # --- one PENDING t2 insights_staging row --------------------------------
    conn.execute(
        "INSERT INTO insights_staging (source, project, payload, status, tier, reason, created_at) "
        "VALUES ('gate_failure','infra','{\"content\":\"a staged lesson\"}','pending','t2',"
        "'needs human approval',?)",
        (ISO,),
    )

    # --- one PENDING resolution_proposal ------------------------------------
    conn.execute(
        "INSERT INTO resolution_proposals (claim_kind, claim_id, verdict, proposed_action, "
        "reasoning, stakes, status, created_at) "
        "VALUES ('insight',1,'fail','supersede','claim drifted from reality','high','pending',?)",
        (ISO,),
    )

    conn.commit()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python seed.py <db_path> [--empty]", file=sys.stderr)
        return 2
    db_path = Path(sys.argv[1])
    empty = "--empty" in sys.argv[2:]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        _bootstrap(conn)
        if not empty:
            _seed(conn)
    finally:
        conn.close()
    mode = "empty" if empty else "seeded"
    print(f"fixture DB ready ({mode}): {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
