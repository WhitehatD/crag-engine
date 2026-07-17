#!/usr/bin/env python3
# coding: utf-8
"""Grounding v3 REV 3 test suite (docs/architecture.md REV 3 section).

Standalone (no pytest — mirrors prior test files in this repo, e.g.
test_staging_removal.py).

Covers:
  T_MIGR_032   — migration 032 applies cleanly + is idempotent on double-run
                 (claims.applicability, insights_staging.reason/lifecycle_action).
  T_SCHEMA     — write_gate.check_schema: size caps, type constraint (HARD),
                 provenance is ADVISORY only (T_DIRECT preserved, see module
                 docstring on write_gate.check_schema).
  T_SECRET     — write_gate.scan_content_secrets: catches AWS/GitHub/Stripe/
                 private-key patterns; plain content passes.
  T_LIFECYCLE  — write_gate.resolve_lifecycle: supersede via "supersedes #N",
                 noop above the similarity floor, update below it, new with
                 no candidates.
  T_STAGING    — write_gate.route_to_staging persists a machine-readable
                 `reason` into insights_staging (migration 032 column).
  T_SAVE_GATE  — daemon integration: POST /save_insight with an embedded
                 credential is staged (never enters `insights`, disposition
                 == "staged"); a clean save is disposition == "accepted";
                 a near-duplicate save is disposition == "merged_into:<id>".
  T_CONTRA     — claim_contradiction.detect_for_claim flags a negation-flip
                 pair on the same primary_entity and does NOT flag an
                 unrelated-entity pair; behind claim_contradiction_enabled.
  T_ROUTING    — claim_layer.assert_no_interactive_proxy raises on any
                 :8788/:8787 base_url and is a no-op on a direct/empty one;
                 the CURRENT get_role_base_url() config never resolves to
                 either interactive-proxy port (routing-isolation doctrine).

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_grounding_v3_rev3.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_DB = REPO_ROOT / "db" / "engine.db"
DAEMON_PY = REPO_ROOT / "apps" / "daemon" / "engine_daemon.py"
DB_DIR = REPO_ROOT / "db"
MIGRATION_031 = DB_DIR / "migrations" / "031_grounding_v3_claim_layer.sql"
MIGRATION_032 = DB_DIR / "migrations" / "032_grounding_v3_rev3.sql"

if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

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
# DB helpers
# ---------------------------------------------------------------------------

def _apply_sql_file(conn: sqlite3.Connection, path: Path) -> list[str]:
    """Apply a migration file statement-by-statement, tolerating the
    documented ADD COLUMN re-run pattern. Returns the list of tolerated
    ("duplicate column name"/"already exists") errors, for idempotency
    assertions.

    Comment lines are stripped BEFORE splitting on ';' — several migration
    header comments contain a semicolon mid-sentence (e.g. migration 031
    line 14: "claim_kind='claim'; the two new ..."), and splitting the raw
    text on ';' first would break such a comment line into a fragment that
    no longer starts with '--' and gets misread as a statement continuation.
    """
    tolerated = []
    raw = path.read_text(encoding="utf-8")
    no_comments = "\n".join(
        ln for ln in raw.splitlines() if not ln.strip().startswith("--")
    )
    for chunk in no_comments.split(";"):
        stmt = chunk.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column name" in msg or "already exists" in msg:
                tolerated.append(str(exc))
                continue
            raise
    return tolerated


def _build_temp_db() -> str:
    """Snapshot the LIVE schema (currently at version 30 — 031/032 are not
    yet applied to db/engine.db, an operator-run step) then layer 031+032 on
    top, exactly like test_staging_removal.py layers 026."""
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db", prefix="v3rev3test-")
    os.close(fd)
    conn = sqlite3.connect(path)
    for s in stmts:
        try:
            conn.execute(s)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    _apply_sql_file(conn, MIGRATION_031)
    conn.commit()
    _apply_sql_file(conn, MIGRATION_032)
    conn.commit()
    conn.close()
    print(f"temp DB: {path}")
    return path


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TEMP_DB = _build_temp_db()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(TEMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# T_MIGR_032 — migration idempotency
# ---------------------------------------------------------------------------

def run_T_MIGR_032():
    print("\n[T_MIGR_032]")
    conn = db()
    cols_claims = {r["name"] for r in conn.execute("PRAGMA table_info(claims)")}
    cols_staging = {r["name"] for r in conn.execute("PRAGMA table_info(insights_staging)")}
    check("T_M32_applicability", "applicability" in cols_claims, str(cols_claims))
    check("T_M32_reason", "reason" in cols_staging, str(cols_staging))
    check("T_M32_lifecycle_action", "lifecycle_action" in cols_staging, str(cols_staging))
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    check("T_M32_version", version >= 32, f"schema_version={version}")
    conn.close()

    # Double-run: re-applying 032 to the SAME (already-migrated) connection
    # must raise ONLY the documented tolerated errors (duplicate column name /
    # already exists), never anything else, and must not corrupt the DB.
    conn2 = db()
    tolerated = _apply_sql_file(conn2, MIGRATION_032)
    conn2.commit()
    check("T_M32_idempotent_tolerated", len(tolerated) >= 2,
          f"expected >=2 tolerated ADD COLUMN errors on re-run, got {len(tolerated)}: {tolerated}")
    check("T_M32_idempotent_all_dup",
          all("duplicate column name" in t.lower() for t in tolerated),
          f"unexpected non-duplicate-column error in re-run: {tolerated}")
    # Table still queryable post double-run.
    row = conn2.execute("SELECT COUNT(*) FROM insights_staging").fetchone()
    check("T_M32_post_rerun_queryable", row is not None, "insights_staging unqueryable after re-run")
    conn2.close()


# ---------------------------------------------------------------------------
# T_SCHEMA — write_gate.check_schema
# ---------------------------------------------------------------------------

def run_T_SCHEMA():
    print("\n[T_SCHEMA]")
    import write_gate

    # HARD: too short
    v = write_gate.check_schema("ab", "gotcha", "", "", "", "")
    check("T_SCH_short_fails", v.ok is False and v.reason == "schema_gate:content_too_short", str(v))

    # HARD: too long
    v = write_gate.check_schema("x" * (write_gate.MAX_CONTENT_LEN + 1), "gotcha", "", "", "", "")
    check("T_SCH_long_fails", v.ok is False and "content_too_long" in (v.reason or ""), str(v))

    # HARD: invalid type
    v = write_gate.check_schema("A perfectly normal insight body.", "not_a_real_type", "", "", "", "")
    check("T_SCH_bad_type_fails", v.ok is False and "invalid_type" in (v.reason or ""), str(v))

    # HARD: valid type + valid length passes
    v = write_gate.check_schema("A perfectly normal insight body.", "gotcha", "", "", "", "")
    check("T_SCH_valid_passes", v.ok is True, str(v))

    # ADVISORY (T_DIRECT preservation): no session_id/source_file/evidence tag
    # -> ok STILL True, but provenance_present False + an advisory recorded.
    v = write_gate.check_schema("A perfectly normal insight body.", "gotcha", "", "", "", "")
    check("T_SCH_no_provenance_still_ok", v.ok is True, str(v))
    check("T_SCH_no_provenance_flagged", v.provenance_present is False, str(v))
    check("T_SCH_no_provenance_advisory", len(v.advisories) == 1, str(v.advisories))

    # ADVISORY: session_id present -> provenance_present True, no advisory.
    v = write_gate.check_schema("A perfectly normal insight body.", "gotcha", "", "", "sess-123", "")
    check("T_SCH_session_id_provenance", v.provenance_present is True and not v.advisories, str(v))

    # ADVISORY: source_file present -> provenance_present True.
    v = write_gate.check_schema("A perfectly normal insight body.", "gotcha", "", "some/file.py", "", "")
    check("T_SCH_source_file_provenance", v.provenance_present is True, str(v))

    # ADVISORY: evidence: tag present -> provenance_present True.
    v = write_gate.check_schema("A perfectly normal insight body.", "gotcha", "evidence:#1234", "", "", "")
    check("T_SCH_evidence_tag_provenance", v.provenance_present is True, str(v))


# ---------------------------------------------------------------------------
# T_SECRET — write_gate.scan_content_secrets
# ---------------------------------------------------------------------------

def run_T_SECRET():
    print("\n[T_SECRET]")
    import write_gate

    cases = {
        "aws_access_key": "Found stray creds: AKIAABCD" "EFGHIJKLMNOP in the log",
        "github_pat": "leaked token ghp_" + "a" * 36 + " in the diff",
        "stripe_live_key": "found sk_live_" + "a1b2c3d4e5f6g7h8i9j0" + " in a config file",
        "private_key_block": "-----BEGIN RSA PRIVATE" " KEY-----\nMIIB...",
        "anthropic_key": "sk-ant-" + "a" * 30 + " printed to stdout",
    }
    for name, content in cases.items():
        hit = write_gate.scan_content_secrets(content)
        check(f"T_SEC_{name}", hit == name, f"expected {name}, got {hit!r}")

    # Clean content -> None
    clean = write_gate.scan_content_secrets(
        "The daemon restarts cleanly and /health returns 200 within 60s."
    )
    check("T_SEC_clean_passes", clean is None, f"expected None, got {clean!r}")

    # Empty content -> None (fail-open on empty input, not a crash)
    check("T_SEC_empty_passes", write_gate.scan_content_secrets("") is None, "empty content should return None")


# ---------------------------------------------------------------------------
# T_LIFECYCLE — write_gate.resolve_lifecycle
# ---------------------------------------------------------------------------

def run_T_LIFECYCLE():
    print("\n[T_LIFECYCLE]")
    import write_gate

    # supersede: explicit "supersedes #N" wins regardless of candidates.
    r = write_gate.resolve_lifecycle("This supersedes #4821 with corrected data.", [])
    check("T_LC_supersede", r == {"action": "supersede", "target_id": 4821}, str(r))

    r = write_gate.resolve_lifecycle("This replaces #99 entirely.", [])
    check("T_LC_replaces_alias", r == {"action": "supersede", "target_id": 99}, str(r))

    # new: no candidates, no invalidation marker.
    r = write_gate.resolve_lifecycle("A brand new observation about the router.", [])
    check("T_LC_new", r == {"action": "new", "target_id": None}, str(r))

    # noop: top candidate similarity >= NOOP_SIMILARITY_FLOOR (0.97).
    cands = [{"id": 11, "content": "x", "similarity": 0.98}, {"id": 12, "content": "y", "similarity": 0.80}]
    r = write_gate.resolve_lifecycle("Some content", cands)
    check("T_LC_noop", r == {"action": "noop", "target_id": 11}, str(r))

    # update: top candidate similarity below the floor but a dedup-guard hit.
    cands2 = [{"id": 21, "content": "x", "similarity": 0.85}]
    r = write_gate.resolve_lifecycle("Some other content", cands2)
    check("T_LC_update", r == {"action": "update", "target_id": 21}, str(r))


# ---------------------------------------------------------------------------
# T_STAGING — write_gate.route_to_staging
# ---------------------------------------------------------------------------

def run_T_STAGING():
    print("\n[T_STAGING]")
    import write_gate

    conn = db()
    staging_id = write_gate.route_to_staging(
        conn, "Some rejected content with a secret-like AKIA pattern",
        "gotcha", "test-v3rev3", "secret_scan:aws_access_key",
    )
    check("T_STG_returns_id", isinstance(staging_id, int), f"got {staging_id!r}")

    row = conn.execute(
        "SELECT * FROM insights_staging WHERE id = ?", (staging_id,)
    ).fetchone()
    check("T_STG_row_exists", row is not None, "no row written")
    if row is not None:
        check("T_STG_reason_persisted", row["reason"] == "secret_scan:aws_access_key", dict(row).__repr__())
        check("T_STG_status_pending", row["status"] == "pending", dict(row).__repr__())
        check("T_STG_source", row["source"] == "gate_failure", dict(row).__repr__())
    conn.close()


# ---------------------------------------------------------------------------
# T_SAVE_GATE — daemon integration (/save_insight)
# ---------------------------------------------------------------------------

def run_T_SAVE_GATE():
    print("\n[T_SAVE_GATE]")
    daemon = _load_module("engine_daemon_v3rev3test", DAEMON_PY)
    daemon.DB_PATH = Path(TEMP_DB)

    from fastapi.testclient import TestClient
    client = TestClient(daemon.app)

    check("T_SG_write_gate_flag", getattr(daemon, "_WRITE_GATE", False) is True,
          "daemon._WRITE_GATE should be True when db/write_gate.py is importable")

    # Secret content -> staged, disposition == "staged", never enters `insights`.
    secret_content = "Rotated the key but forgot to redact AKIAABCD" "EFGHIJKLMNOP from the notes"
    r = client.post("/save_insight", json={
        "content": secret_content, "type": "gotcha", "project": "test-v3rev3",
    })
    check("T_SG_secret_http_ok", r.status_code == 200, f"status={r.status_code}")
    body = r.json()
    check("T_SG_secret_not_ok", body.get("ok") is False, str(body))
    check("T_SG_secret_disposition", body.get("disposition") == "staged", str(body))
    check("T_SG_secret_reason", (body.get("reason") or "").startswith("secret_scan:"), str(body))
    conn = db()
    leaked = conn.execute(
        "SELECT COUNT(*) FROM insights WHERE content = ?", (secret_content,)
    ).fetchone()[0]
    check("T_SG_secret_never_in_insights", leaked == 0, f"credential-bearing content leaked into insights ({leaked} rows)")
    staged_row = conn.execute(
        "SELECT * FROM insights_staging WHERE reason LIKE 'secret_scan:%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    check("T_SG_secret_staged_row", staged_row is not None, "no staging row for the secret-bearing save")
    conn.close()

    # Clean, unique content -> accepted, disposition == "accepted".
    r2 = client.post("/save_insight", json={
        "content": "T_SAVE_GATE clean unique insight body for accepted-disposition check",
        "type": "pattern", "project": "test-v3rev3", "session_id": "sess-v3rev3",
    })
    check("T_SG_clean_ok", r2.status_code == 200 and r2.json().get("ok") is True, str(r2.json()))
    check("T_SG_clean_disposition", r2.json().get("disposition") == "accepted", str(r2.json()))
    check("T_SG_clean_has_id", isinstance(r2.json().get("id"), int), str(r2.json()))

    # Near-duplicate (identical) content -> merged_into disposition, ok False.
    r3 = client.post("/save_insight", json={
        "content": "T_SAVE_GATE clean unique insight body for accepted-disposition check",
        "type": "pattern", "project": "test-v3rev3", "session_id": "sess-v3rev3",
    })
    check("T_SG_dup_ok_field", r3.status_code == 200, f"status={r3.status_code}")
    body3 = r3.json()
    check("T_SG_dup_rejected", body3.get("ok") is False and body3.get("duplicate") is True, str(body3))
    disp = body3.get("disposition", "")
    check("T_SG_dup_disposition", isinstance(disp, str) and disp.startswith("merged_into:"), str(body3))


# ---------------------------------------------------------------------------
# T_CONTRA — claim_contradiction.detect_for_claim
# ---------------------------------------------------------------------------

def run_T_CONTRA():
    print("\n[T_CONTRA]")
    import claim_contradiction
    import grounding_config
    from lifecycle import _utcnow_iso

    conn = db()
    now = _utcnow_iso()

    # Force the flag on for this test regardless of the default (config is
    # env/file driven; we monkeypatch get_claims_config for isolation).
    orig_get_claims_config = grounding_config.get_claims_config
    grounding_config.get_claims_config = lambda: {
        "claim_contradiction_enabled": True, "contradiction_cosine": 0.80,
    }
    try:
        # Two claims about the SAME primary_entity that disagree via negation.
        conn.execute(
            "INSERT INTO claims (id, canonical_key, text, predicate_class, status, "
            "primary_entity, primary_entity_type, created_at) VALUES "
            "(9001, 'k1', 'the daemon is running on port 8786', 'P1', 'active', "
            "'8786', 'port', ?)", (now,),
        )
        conn.execute(
            "INSERT INTO claims (id, canonical_key, text, predicate_class, status, "
            "primary_entity, primary_entity_type, created_at) VALUES "
            "(9002, 'k2', 'the daemon is not running on port 8786', 'P1', 'active', "
            "'8786', 'port', ?)", (now,),
        )
        # An unrelated-entity claim — must NOT be flagged against either above.
        conn.execute(
            "INSERT INTO claims (id, canonical_key, text, predicate_class, status, "
            "primary_entity, primary_entity_type, created_at) VALUES "
            "(9003, 'k3', 'the router listens on port 8788', 'P1', 'active', "
            "'8788', 'port', ?)", (now,),
        )
        conn.commit()

        flagged = claim_contradiction.detect_for_claim(conn, 9002)
        check("T_CTR_flags_negation_pair",
              any(f["a"] == 9001 and f["b"] == 9002 for f in flagged), str(flagged))

        row = conn.execute(
            "SELECT * FROM claim_contradictions WHERE claim_a_id=9001 AND claim_b_id=9002"
        ).fetchone()
        check("T_CTR_persisted", row is not None, "no claim_contradictions row written")
        if row is not None:
            check("T_CTR_status_open", row["status"] == "open", dict(row).__repr__())

        # Unrelated entity: scanning 9003 must not flag anything against 9001/9002.
        flagged3 = claim_contradiction.detect_for_claim(conn, 9003)
        check("T_CTR_unrelated_not_flagged", flagged3 == [], str(flagged3))

        # Flag off (default) -> no-op even with the same disagreeing pair.
        grounding_config.get_claims_config = lambda: {"claim_contradiction_enabled": False}
        conn.execute("DELETE FROM claim_contradictions")
        conn.commit()
        flagged_off = claim_contradiction.detect_for_claim(conn, 9002)
        check("T_CTR_disabled_noop", flagged_off == [], str(flagged_off))
    finally:
        grounding_config.get_claims_config = orig_get_claims_config
        conn.close()


# ---------------------------------------------------------------------------
# T_ROUTING — claim_layer routing-isolation guard
# ---------------------------------------------------------------------------

def run_T_ROUTING():
    print("\n[T_ROUTING]")
    import claim_layer

    for port in (":8788", ":8787"):
        try:
            claim_layer.assert_no_interactive_proxy(f"http://localhost{port}", role="decompose")
            check(f"T_ROUTE_raises_{port.strip(':')}", False, "expected RuntimeError, none raised")
        except RuntimeError as exc:
            check(f"T_ROUTE_raises_{port.strip(':')}", True, str(exc))

    # Direct api.anthropic.com base_url and empty-string sentinel are both fine.
    try:
        claim_layer.assert_no_interactive_proxy("https://api.anthropic.com", role="decompose")
        claim_layer.assert_no_interactive_proxy("", role="decompose")
        check("T_ROUTE_direct_ok", True)
    except RuntimeError as exc:
        check("T_ROUTE_direct_ok", False, str(exc))

    # Live config resolution never lands on an interactive-proxy port.
    resolved = claim_layer.get_role_base_url()
    check("T_ROUTE_live_config_isolated",
          not any(p in (resolved or "") for p in (":8788", ":8787")),
          f"get_role_base_url() resolved to {resolved!r} — routes through interactive proxy")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    run_T_MIGR_032()
    run_T_SCHEMA()
    run_T_SECRET()
    run_T_LIFECYCLE()
    run_T_STAGING()
    run_T_SAVE_GATE()
    run_T_CONTRA()
    run_T_ROUTING()

    try:
        Path(TEMP_DB).unlink(missing_ok=True)
    except OSError:
        pass

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
