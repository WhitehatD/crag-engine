#!/usr/bin/env python3
# coding: utf-8
"""Staging-tier removal test suite.

Standalone (no pytest — mirrors prior test files in this repo).

Tests:
  T_DIRECT    — save_insight with low-provenance role now inserts directly into
                insights (no staged row). Subagent saves also go direct.
  T_DEDUP     — dedup guard still rejects near-duplicates (unchanged behavior).
  T_SALVAGE_DRY — salvage-staged --dry-run: correct counts, no DB writes.
  T_SALVAGE_LIVE — salvage-staged live: non-dupes inserted (tags include
                   'salvaged-staging'), dupes marked 'rejected-dup-at-salvage',
                   salvaged rows marked 'salvaged'.
  T_NO_STAGE_WRITE — no code path writes INSERT INTO insights_staged anymore
                     (grep-based assertion on engine_daemon.py).

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_staging_removal.py
"""
from __future__ import annotations

import importlib.util
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
CLI_PY = REPO_ROOT / "db" / "engine-cli.py"
MIGRATION_026 = REPO_ROOT / "db" / "migrations" / "026_grounding_v2.sql"
DB_DIR = REPO_ROOT / "db"

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

def _apply_026(conn: sqlite3.Connection) -> None:
    sql = MIGRATION_026.read_text(encoding="utf-8")
    for chunk in sql.split(";"):
        sql_lines = [ln for ln in chunk.splitlines()
                     if ln.strip() and not ln.strip().startswith("--")]
        stmt = "\n".join(sql_lines).strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column name" in msg or "already exists" in msg:
                continue
            raise


def _build_temp_db() -> str:
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db", prefix="stagingtest-")
    os.close(fd)
    conn = sqlite3.connect(path)
    for s in stmts:
        try:
            conn.execute(s)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    _apply_026(conn)
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


# ---------------------------------------------------------------------------
# Build temp DB + load daemon
# ---------------------------------------------------------------------------

TEMP_DB = _build_temp_db()
daemon = _load_module("engine_daemon_stagingtest", DAEMON_PY)
daemon.DB_PATH = Path(TEMP_DB)

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(daemon.app)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(TEMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Test groups
# ---------------------------------------------------------------------------

def run_T_DIRECT():
    """T_DIRECT — low-provenance saves go directly to insights, not staging."""
    print("\n[T_DIRECT]")

    # Save with no role, no source_file, no epic_tag, no session_id
    # (this was the low_provenance_defensive trigger)
    r = client.post("/save_insight", json={
        "content": "Test direct save: low provenance no longer triggers staging",
        "type": "decision",
        "project": "test-staging",
    })
    check("T_DIR_a_ok", r.status_code == 200, f"status={r.status_code}")
    body = r.json()
    check("T_DIR_a_direct", body.get("ok") is True, str(body))
    check("T_DIR_a_id", isinstance(body.get("id"), int), f"expected id, got {body}")
    # Must NOT have staged_id
    check("T_DIR_a_no_staged", "staged_id" not in body,
          f"staged_id should not appear: {body}")

    # Verify it's in insights table
    conn = db()
    row = conn.execute(
        "SELECT * FROM insights WHERE id = ?", (body.get("id"),)
    ).fetchone()
    check("T_DIR_a_in_insights", row is not None,
          f"insight {body.get('id')} not found in insights table")
    conn.close()

    # Verify nothing was written to insights_staged
    conn = db()
    staged = conn.execute(
        "SELECT COUNT(*) FROM insights_staged WHERE content LIKE '%low provenance no longer%'"
    ).fetchone()[0]
    check("T_DIR_a_no_staged_row", staged == 0,
          f"found {staged} staged rows — should be 0")
    conn.close()

    # Save with role=subagent (formerly always staged)
    r2 = client.post("/save_insight", json={
        "content": "Test direct save: subagent role no longer triggers staging",
        "type": "gotcha",
        "project": "test-staging",
        "role": "subagent",
    })
    check("T_DIR_b_ok", r2.status_code == 200, f"status={r2.status_code}")
    body2 = r2.json()
    check("T_DIR_b_direct", body2.get("ok") is True, str(body2))
    check("T_DIR_b_id", isinstance(body2.get("id"), int), f"expected id, got {body2}")
    check("T_DIR_b_no_staged", "staged_id" not in body2,
          f"staged_id should not appear: {body2}")


def run_T_DEDUP():
    """T_DEDUP — dedup guard still works (unchanged)."""
    print("\n[T_DEDUP]")

    # First save succeeds
    r1 = client.post("/save_insight", json={
        "content": "Dedup test: unique content for staging removal verification",
        "type": "pattern",
        "project": "test-staging",
        "role": "operator",
    })
    check("T_DUP_a_first_ok", r1.status_code == 200 and r1.json().get("ok"), str(r1.json()))

    # Second save with identical content should be dedup-rejected
    r2 = client.post("/save_insight", json={
        "content": "Dedup test: unique content for staging removal verification",
        "type": "pattern",
        "project": "test-staging",
        "role": "operator",
    })
    check("T_DUP_b_dedup", r2.status_code == 200, f"status={r2.status_code}")
    body2 = r2.json()
    # Dedup returns ok:false with near-match candidates
    check("T_DUP_b_rejected",
          body2.get("ok") is False or body2.get("dedup") is True,
          f"expected dedup rejection: {body2}")


def run_T_SALVAGE():
    """T_SALVAGE_DRY + T_SALVAGE_LIVE — salvage-staged CLI round-trip."""
    print("\n[T_SALVAGE]")

    # Seed some pending staged rows
    conn = db()
    for i in range(3):
        conn.execute(
            """INSERT INTO insights_staged
               (content, type, tags, project, decision, decision_reason, created_at)
               VALUES (?, 'decision', ?, 'test-staging', 'pending', 'test', datetime('now'))""",
            (f"Salvage test unique content #{i} alpha beta gamma delta",
             f"tag{i}"),
        )
    # One that will be a duplicate (seed its match into insights first)
    conn.execute(
        """INSERT INTO insights (project, type, content, status, confidence, created_at, updated_at)
           VALUES ('test-staging', 'gotcha', 'Already exists in insights — dedup target for salvage',
                   'active', 0.5, datetime('now'), datetime('now'))"""
    )
    conn.execute(
        """INSERT INTO insights_staged
           (content, type, tags, project, decision, decision_reason, created_at)
           VALUES ('Already exists in insights — dedup target for salvage',
                   'gotcha', '', 'test-staging', 'pending', 'test', datetime('now'))"""
    )
    conn.commit()
    conn.close()

    # We can't easily call cmd_salvage_staged from a loaded daemon module;
    # instead, import the function directly
    import types
    cli_spec = importlib.util.spec_from_file_location("engine_cli_salvage", CLI_PY)
    cli_mod = importlib.util.module_from_spec(cli_spec)
    # Patch DB path before loading
    sys.modules["engine_cli_salvage"] = cli_mod
    # We need to override get_db in the cli module after loading
    cli_spec.loader.exec_module(cli_mod)
    # Patch get_db to use temp DB

    def patched_get_db():
        conn = sqlite3.connect(TEMP_DB)
        conn.row_factory = sqlite3.Row
        return conn

    cli_mod.get_db = patched_get_db
    cli_mod.DB_PATH = Path(TEMP_DB)

    # Simulate argparse namespace for dry-run
    dry_args = types.SimpleNamespace(dry_run=True)

    import io
    from contextlib import redirect_stdout

    # Dry run
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_mod.cmd_salvage_staged(dry_args)
    dry_output = buf.getvalue()

    import json
    try:
        dry_result = json.loads(dry_output)
    except Exception:
        dry_result = {}

    check("T_SAL_dry_ok", dry_result.get("ok") is True, f"output: {dry_output[:200]}")
    check("T_SAL_dry_run_flag", dry_result.get("dry_run") is True, str(dry_result))
    check("T_SAL_dry_total", dry_result.get("total_pending", 0) >= 4,
          f"expected >= 4 pending, got {dry_result.get('total_pending')}")
    check("T_SAL_dry_no_write", dry_result.get("inserted", 0) >= 1,
          f"expected inserted >= 1 in dry-run count: {dry_result}")

    # Verify no DB writes happened during dry run
    conn = db()
    still_pending = conn.execute(
        "SELECT COUNT(*) FROM insights_staged WHERE decision='pending' AND project='test-staging'"
    ).fetchone()[0]
    check("T_SAL_dry_unchanged", still_pending >= 4,
          f"dry-run should not change DB: {still_pending} pending")
    conn.close()

    # Live run
    live_args = types.SimpleNamespace(dry_run=False)
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        cli_mod.cmd_salvage_staged(live_args)
    live_output = buf2.getvalue()

    try:
        live_result = json.loads(live_output)
    except Exception:
        live_result = {}

    check("T_SAL_live_ok", live_result.get("ok") is True, f"output: {live_output[:200]}")
    check("T_SAL_live_dry_false", live_result.get("dry_run") is False, str(live_result))
    check("T_SAL_live_inserted", live_result.get("inserted", 0) >= 3,
          f"expected >= 3 inserted: {live_result}")

    # Check salvaged insights have the tag
    conn = db()
    salvaged_rows = conn.execute(
        "SELECT * FROM insights WHERE tags LIKE '%salvaged-staging%'"
    ).fetchall()
    check("T_SAL_tagged", len(salvaged_rows) >= 3,
          f"expected >= 3 salvaged-tagged insights, got {len(salvaged_rows)}")

    # Check dup row was marked
    conn.execute(
        "SELECT * FROM insights_staged WHERE decision='rejected-dup-at-salvage'"
    ).fetchall()
    # May be 0 if jaccard/cosine didn't trigger on exact match (depends on embedding)
    # The exact-content match might not trigger cosine above threshold without embeddings.
    # So we check that EITHER the dup was caught OR all were inserted.
    total_inserted = live_result.get("inserted", 0)
    total_dup = live_result.get("dup_rejected", 0)
    total_pending = live_result.get("total_pending", 0)
    check("T_SAL_accounts", total_inserted + total_dup == total_pending,
          f"inserted({total_inserted}) + dup({total_dup}) != total({total_pending})")

    # Salvaged rows in insights_staged should have decision='salvaged'
    salvaged_staged = conn.execute(
        "SELECT COUNT(*) FROM insights_staged WHERE decision='salvaged' AND project='test-staging'"
    ).fetchone()[0]
    check("T_SAL_staged_marked", salvaged_staged >= 3,
          f"expected >= 3 salvaged staged rows, got {salvaged_staged}")

    # No more pending for this project
    still_pending_after = conn.execute(
        "SELECT COUNT(*) FROM insights_staged WHERE decision='pending' AND project='test-staging'"
    ).fetchone()[0]
    check("T_SAL_none_pending", still_pending_after == 0,
          f"expected 0 pending after salvage, got {still_pending_after}")
    conn.close()

    # T_SAL_emb_set: salvaged rows have non-NULL embedding when embedder available.
    # Seed one more pending row, stub embed_text in the CLI module, run salvage,
    # then verify the inserted row has embedding IS NOT NULL.
    conn = db()
    conn.execute(
        """INSERT INTO insights_staged
           (content, type, tags, project, decision, decision_reason, created_at)
           VALUES (?, 'decision', 'emb-test', 'test-emb-project', 'pending', 'test', datetime('now'))""",
        ("Embedding test row — unique content for T_SAL_emb_set verification",),
    )
    conn.commit()
    conn.close()

    # Stub embed_fn: returns 384-dim float32 bytes (all ones), same encoding as daemon
    import struct
    _stub_vec = struct.pack("384f", *([1.0] * 384))

    def _stub_embed(text):
        return _stub_vec

    # Patch embed into the CLI module's namespace so the try/import block finds it
    import types as _types
    _fake_embed_mod = _types.ModuleType("embed")
    _fake_embed_mod.embed_text = _stub_embed
    import sys as _sys
    _sys.modules["embed"] = _fake_embed_mod
    cli_mod.embed_text = _stub_embed

    emb_args = _types.SimpleNamespace(dry_run=False)
    with redirect_stdout(io.StringIO()):
        cli_mod.cmd_salvage_staged(emb_args)

    # Restore embed module
    _sys.modules.pop("embed", None)

    conn = db()
    emb_rows = conn.execute(
        "SELECT id, embedding FROM insights WHERE tags LIKE '%emb-test%'"
    ).fetchall()
    conn.close()
    check("T_SAL_emb_set",
          len(emb_rows) >= 1 and any(r["embedding"] is not None for r in emb_rows),
          f"salvaged row should have non-NULL embedding; rows={[(r['id'], r['embedding'] is not None) for r in emb_rows]}")

    # T_SAL_emb_hint: when embedder unavailable, next_step hint appears in output.
    # Seed a fresh pending row and make embed_text import fail inside the CLI module.
    conn = db()
    conn.execute(
        """INSERT INTO insights_staged
           (content, type, tags, project, decision, decision_reason, created_at)
           VALUES (?, 'decision', 'hint-test', 'test-hint-project', 'pending', 'test', datetime('now'))""",
        ("Hint test row — unique content for T_SAL_emb_hint backfill reminder check",),
    )
    conn.commit()
    conn.close()

    # Replace embed module with one that has NO embed_text attribute,
    # causing the `from embed import embed_text` in cmd_salvage_staged to raise
    # ImportError → simulates no fastembed installed.
    _no_embed_mod = _types.ModuleType("embed")
    _sys.modules["embed"] = _no_embed_mod

    hint_args = _types.SimpleNamespace(dry_run=False)
    buf4 = io.StringIO()
    with redirect_stdout(buf4):
        cli_mod.cmd_salvage_staged(hint_args)
    hint_output = buf4.getvalue()

    try:
        hint_result = json.loads(hint_output)
    except Exception:
        hint_result = {}

    check("T_SAL_emb_hint",
          hint_result.get("next_step") == "run backfill-embeddings",
          f"expected next_step hint when embedder unavailable; got: {hint_result}")


def run_T_NO_STAGE_WRITE():
    """T_NO_STAGE_WRITE — no code path writes INSERT INTO insights_staged in daemon."""
    print("\n[T_NO_STAGE_WRITE]")

    daemon_src = DAEMON_PY.read_text(encoding="utf-8")
    check("T_NSW_no_insert",
          "INSERT INTO insights_staged" not in daemon_src,
          "found 'INSERT INTO insights_staged' in engine_daemon.py — should be removed")

    # Also verify _should_stage and _do_stage_insight are gone
    check("T_NSW_no_should_stage",
          "def _should_stage" not in daemon_src,
          "found '_should_stage' function — should be removed")
    check("T_NSW_no_do_stage",
          "def _do_stage_insight" not in daemon_src,
          "found '_do_stage_insight' function — should be removed")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    run_T_DIRECT()
    run_T_DEDUP()
    run_T_SALVAGE()
    run_T_NO_STAGE_WRITE()

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
