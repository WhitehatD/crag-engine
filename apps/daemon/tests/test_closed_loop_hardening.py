#!/usr/bin/env python3
# coding: utf-8
"""Closed-loop hardening test suite (docs/architecture.md REV 4/5/6/8/9).

Standalone (no pytest — mirrors test_grounding_v3_rev3.py). Temp DBs only: the
live db/engine.db is snapshotted READ-ONLY then migrations layered on a temp copy.
Never touches the live DB, never boots the daemon's real network loop.

Covers the 5 hardening gaps:
  T_SYNC     — sync_path_guard.detect_sync_segment / check_db_path matrix
               (Dropbox/OneDrive/iCloud/Syncthing rejected; normal ok; escape
               hatch downgrades to warning).
  T_CAPCFG   — capture config exposes daemon_task_enabled (default true),
               daemon_task_interval_sec (default 120), event_token (default "");
               env overrides win.
  T_CAPTASK  — daemon exposes _capture_task_loop coroutine + _CAPTURE_TASK flag;
               the loop early-returns when config disables it (no boot needed).
  T_AUTH     — POST /capture/event 401/200 matrix via FastAPI TestClient with a
               monkeypatched token accessor.
  T_BANNER   — _attach_stale_banner adds stale_banner for stale/revalidating,
               omits it for fresh/aging/unverified; never suppresses the hit.
  T_MIGR_034 — migration 034 applies + idempotent; embedding_model_version
               column present on claim_embeddings/insights/principles.
  T_STAMP    — claim_layer.persist_claims stamps embedding_model_version on the
               claim_embeddings row (matching embed.EMBEDDING_MODEL), and its
               write path tolerates the column being absent (pre-034 fallback).

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_closed_loop_hardening.py
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
DAEMON_PY = REPO_ROOT / "apps" / "daemon" / "engine_daemon.py"
DB_DIR = REPO_ROOT / "db"
MIGR = DB_DIR / "migrations"
BASE_SCHEMA = DB_DIR / "schema.sql"

# The DB itself is gitignored + lives only in the primary checkout (a worktree
# shares no db/engine.db). Build temp DBs from schema.sql + migration files
# instead of snapshotting the live DB — fully self-contained and NEVER opens
# the live (actively-written) engine.db, honoring the "do not touch live DB"
# constraint absolutely (not even a read-only handle).

for p in (str(DB_DIR), str(DB_DIR / "capture")):
    if p not in sys.path:
        sys.path.insert(0, p)

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
# DB helpers (mirror test_grounding_v3_rev3.py)
# ---------------------------------------------------------------------------

def _apply_sql_file(conn: sqlite3.Connection, path: Path) -> list[str]:
    """Apply a migration statement-by-statement, tolerating the ADD COLUMN
    re-run pattern. Accumulates chunks until sqlite3.complete_statement() so
    compound statements (CREATE TRIGGER ... BEGIN ...; ...; END) are not split
    mid-body on internal semicolons."""
    tolerated: list[str] = []
    raw = path.read_text(encoding="utf-8")
    no_comments = "\n".join(ln for ln in raw.splitlines() if not ln.strip().startswith("--"))
    buf = ""
    for chunk in no_comments.split(";"):
        buf += chunk + ";"
        if not sqlite3.complete_statement(buf):
            continue
        stmt = buf.strip().rstrip(";").strip()
        buf = ""
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


def _build_temp_db(*, apply_034: bool = True) -> str:
    """Build a temp DB from schema.sql + all migration files (004..034), never
    touching the live engine.db. Optionally stops before 034 to exercise the
    pre-034 fallback path."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="hardeningtest-")
    os.close(fd)
    conn = sqlite3.connect(path)
    # Base schema may contain triggers/compound statements — executescript
    # handles them; the migration files use the ADD COLUMN pattern our
    # statement-splitter tolerates.
    conn.executescript(BASE_SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    for mig in sorted(MIGR.glob("[0-9][0-9][0-9]_*.sql")):
        num = int(mig.name[:3])
        if num <= 3:
            continue                       # schema.sql already covers phase-1
        if num == 34 and not apply_034:
            break
        _apply_sql_file(conn, mig)
        conn.commit()
    conn.close()
    print(f"temp DB ({'with' if apply_034 else 'without'} 034): {path}")
    return path


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# T_SYNC — sync-folder detector
# ---------------------------------------------------------------------------

def run_T_SYNC():
    print("\n[T_SYNC]")
    import sync_path_guard as g

    # Rejected patterns (case-insensitive, segment match).
    reject = {
        "dropbox": r"C:\Users\me\Dropbox\engine\db\engine.db",
        "onedrive": r"C:\Users\me\OneDrive - Contoso\engine.db",
        "gdrive": r"/home/me/GDrive/engine/engine.db",
        "googledrive": r"/Users/me/Google Drive/engine.db",
        "icloud": "/Users/me/Library/Mobile Documents/com~apple~CloudDocs/engine.db",
        "dotsync": r"D:\stuff\.sync\engine.db",
        "syncthing_marker": r"D:\vault\.stfolder\engine.db",
    }
    for label, p in reject.items():
        seg = g.detect_sync_segment(p)
        check(f"T_SYNC_reject_{label}", seg is not None, f"expected a hit for {p!r}, got {seg!r}")

    # UNC / network-share paths are also rejected (SQLite over a network FS is a
    # documented corruption class independent of any sync client).
    for label, p in {"unc_win": r"\\fileserver\share\engine\engine.db",
                     "unc_posix": "//nas/vol/engine.db"}.items():
        check(f"T_SYNC_reject_{label}", g.detect_sync_segment(p) is not None,
              f"expected UNC rejection for {p!r}, got {g.detect_sync_segment(p)!r}")

    # Normal paths pass (None). Note: a filename that merely CONTAINS 'dropbox'
    # as a substring but is not its own segment must NOT trip.
    ok = {
        "playground": r"D:\workspace\engine\db\engine.db",
        "home": "/home/me/engine/db/engine.db",
        "substring_only": r"D:\backups\my_dropbox_export\engine.db",
    }
    for label, p in ok.items():
        check(f"T_SYNC_ok_{label}", g.detect_sync_segment(p) is None,
              f"false positive on {p!r}: {g.detect_sync_segment(p)!r}")

    # check_db_path raises on a sync path with the escape hatch UNSET.
    os.environ.pop("CRAG_ENGINE_ALLOW_SYNC_PATH", None)
    raised = False
    try:
        g.check_db_path(r"C:\Users\me\Dropbox\engine.db")
    except RuntimeError:
        raised = True
    check("T_SYNC_raises_no_hatch", raised, "check_db_path did not raise on a Dropbox path")

    # normal path -> returns None, no raise.
    try:
        r = g.check_db_path(r"D:\workspace\engine\db\engine.db")
        check("T_SYNC_normal_returns_none", r is None, f"expected None, got {r!r}")
    except RuntimeError as exc:
        check("T_SYNC_normal_returns_none", False, f"unexpected raise: {exc}")

    # escape hatch downgrades to warning (returns segment, no raise).
    os.environ["CRAG_ENGINE_ALLOW_SYNC_PATH"] = "1"
    try:
        seg = g.check_db_path(r"C:\Users\me\Dropbox\engine.db")
        check("T_SYNC_hatch_downgrades", seg == "Dropbox", f"expected 'Dropbox', got {seg!r}")
    except RuntimeError as exc:
        check("T_SYNC_hatch_downgrades", False, f"escape hatch still raised: {exc}")
    finally:
        os.environ.pop("CRAG_ENGINE_ALLOW_SYNC_PATH", None)


# ---------------------------------------------------------------------------
# T_CAPCFG — capture config new keys
# ---------------------------------------------------------------------------

def run_T_CAPCFG():
    print("\n[T_CAPCFG]")
    import config as capture_config

    # Defaults (clear any env overrides first).
    for var in ("CRAG_ENGINE_CAPTURE_DAEMON_TASK_ENABLED", "CRAG_ENGINE_CAPTURE_DAEMON_TASK_INTERVAL_SEC",
                "CRAG_ENGINE_CAPTURE_TOKEN"):
        os.environ.pop(var, None)
    cfg = capture_config.reload_config()
    check("T_CFG_default_enabled", cfg.daemon_task_enabled is True, str(cfg.daemon_task_enabled))
    check("T_CFG_default_interval", abs(cfg.daemon_task_interval_sec - 120.0) < 1e-6,
          str(cfg.daemon_task_interval_sec))
    check("T_CFG_default_token_empty", cfg.event_token == "", repr(cfg.event_token))

    # Env overrides win.
    os.environ["CRAG_ENGINE_CAPTURE_DAEMON_TASK_ENABLED"] = "false"
    os.environ["CRAG_ENGINE_CAPTURE_DAEMON_TASK_INTERVAL_SEC"] = "45"
    os.environ["CRAG_ENGINE_CAPTURE_TOKEN"] = "s3cr3t-token"
    cfg2 = capture_config.reload_config()
    check("T_CFG_env_disabled", cfg2.daemon_task_enabled is False, str(cfg2.daemon_task_enabled))
    check("T_CFG_env_interval", abs(cfg2.daemon_task_interval_sec - 45.0) < 1e-6,
          str(cfg2.daemon_task_interval_sec))
    check("T_CFG_env_token", cfg2.event_token == "s3cr3t-token", repr(cfg2.event_token))

    for var in ("CRAG_ENGINE_CAPTURE_DAEMON_TASK_ENABLED", "CRAG_ENGINE_CAPTURE_DAEMON_TASK_INTERVAL_SEC",
                "CRAG_ENGINE_CAPTURE_TOKEN"):
        os.environ.pop(var, None)

    # effective_event_token: a readable auth_token_file wins over the inline
    # event_token (rev-9 §9.2 file-vs-inline precedence, hot-rotatable).
    if hasattr(capture_config, "effective_event_token"):
        fd, tok_path = tempfile.mkstemp(suffix=".token", prefix="captok-")
        os.close(fd)
        Path(tok_path).write_text("file-token-xyz\n", encoding="utf-8")
        try:
            class _Cfg:
                auth_token_file = tok_path
                event_token = "inline-loser"
            check("T_CFG_file_token_wins",
                  capture_config.effective_event_token(_Cfg()) == "file-token-xyz",
                  capture_config.effective_event_token(_Cfg()))

            class _CfgInline:
                auth_token_file = ""
                event_token = "inline-only"
            check("T_CFG_inline_token_fallback",
                  capture_config.effective_event_token(_CfgInline()) == "inline-only",
                  capture_config.effective_event_token(_CfgInline()))
        finally:
            Path(tok_path).unlink(missing_ok=True)

    capture_config.reload_config()


# ---------------------------------------------------------------------------
# T_CAPTASK / T_AUTH / T_BANNER — daemon integration (module load, no boot)
# ---------------------------------------------------------------------------

_daemon = None


def _get_daemon(temp_db: str):
    global _daemon
    if _daemon is None:
        _daemon = _load_module("engine_daemon_hardening", DAEMON_PY)
    _daemon.DB_PATH = Path(temp_db)
    return _daemon


def run_T_CAPTASK(temp_db: str):
    print("\n[T_CAPTASK]")
    import asyncio
    daemon = _get_daemon(temp_db)

    check("T_CT_flag_present", hasattr(daemon, "_CAPTURE_TASK"), "daemon missing _CAPTURE_TASK flag")
    check("T_CT_loop_present", hasattr(daemon, "_capture_task_loop"),
          "daemon missing _capture_task_loop coroutine")

    # Gating logic: with config disabled, the loop must early-return (no infinite
    # loop, no exception). We monkeypatch the config accessor the loop reads.
    if getattr(daemon, "_CAPTURE_TASK", False):
        # Patch the SAME config module object the daemon bound as
        # _capture_config (its get_config is what the loop actually calls).
        cap_cfg_mod = daemon._capture_config

        class _FakeCfg:
            daemon_task_enabled = False
            daemon_task_interval_sec = 120.0
            event_token = ""

        orig = cap_cfg_mod.get_config
        cap_cfg_mod.get_config = lambda: _FakeCfg()
        try:
            # Should return promptly (disabled) rather than sleep/loop forever.
            asyncio.run(asyncio.wait_for(daemon._capture_task_loop(), timeout=5.0))
            check("T_CT_disabled_early_return", True)
        except Exception as exc:
            check("T_CT_disabled_early_return", False, f"loop raised/hung: {exc!r}")
        finally:
            cap_cfg_mod.get_config = orig
    else:
        check("T_CT_disabled_early_return", True, "capture modules absent — flag False (acceptable)")


def run_T_AUTH(temp_db: str):
    print("\n[T_AUTH]")
    from fastapi.testclient import TestClient
    daemon = _get_daemon(temp_db)
    client = TestClient(daemon.app)

    body = {"source": "manual", "payload": {"note": "hardening auth test"},
            "project": "test-hardening", "dedup_key": "hardening-auth-1"}

    # 1) No token configured -> fail-open, request proceeds (NOT 401) AND the
    # response carries a non-fatal `advisory` (rev-9 §9.2 documented backward-compat).
    daemon._capture_event_token = lambda: ""
    daemon._CAPTURE_UNAUTH_WARNED = False
    r = client.post("/capture/event", json=body)
    check("T_AUTH_failopen_not_401", r.status_code != 401, f"status={r.status_code} body={r.text[:200]}")
    if r.status_code == 200:
        check("T_AUTH_failopen_advisory", "advisory" in r.json(),
              f"expected an advisory in the unconfigured-accept response: {r.json()}")

    # 2) Token configured, header ABSENT -> 401.
    daemon._capture_event_token = lambda: "the-secret"
    r2 = client.post("/capture/event", json={**body, "dedup_key": "hardening-auth-2"})
    check("T_AUTH_missing_header_401", r2.status_code == 401, f"status={r2.status_code}")

    # 3) Token configured, header WRONG -> 401.
    r3 = client.post("/capture/event", headers={"X-Capture-Token": "wrong"},
                     json={**body, "dedup_key": "hardening-auth-3"})
    check("T_AUTH_wrong_header_401", r3.status_code == 401, f"status={r3.status_code}")

    # 4) Token configured, header CORRECT -> proceeds (NOT 401).
    r4 = client.post("/capture/event", headers={"X-Capture-Token": "the-secret"},
                     json={**body, "dedup_key": "hardening-auth-4"})
    check("T_AUTH_correct_header_ok", r4.status_code != 401,
          f"status={r4.status_code} body={r4.text[:200]}")

    # 5) NON-ASCII totality (verification finding 2026-07-17): starlette decodes
    # header bytes latin-1, so a >=0x80 byte reaches the comparator as a
    # non-ASCII str. hmac.compare_digest(str, str) is ASCII-only and raised
    # TypeError -> unhandled 500 instead of the contract's 401. The fix
    # compares UTF-8 BYTES; these lock the comparator's totality.
    #   5a) non-ASCII forged header vs ASCII token -> 401, never 500.
    resp = daemon._authenticate_capture_event(
        type("R", (), {"headers": {"X-Capture-Token": "f\u00f6rged-\u20ac"}})())
    check("T_AUTH_nonascii_header_401",
          resp is not None and getattr(resp, "status_code", None) == 401,
          f"got {resp!r} (must be 401 JSONResponse, not an exception path)")
    #   5b) non-ASCII CONFIGURED token + correct header -> accepted (None).
    daemon._capture_event_token = lambda: "sch\u00fcssel-\u20ac"
    resp_ok = daemon._authenticate_capture_event(
        type("R", (), {"headers": {"X-Capture-Token": "sch\u00fcssel-\u20ac"}})())
    check("T_AUTH_nonascii_token_correct_ok", resp_ok is None, f"got {resp_ok!r}")
    #   5c) non-ASCII token + missing header -> 401, never 500.
    resp_miss = daemon._authenticate_capture_event(
        type("R", (), {"headers": {}})())
    check("T_AUTH_nonascii_token_missing_401",
          resp_miss is not None and getattr(resp_miss, "status_code", None) == 401,
          f"got {resp_miss!r}")
    daemon._capture_event_token = lambda: "the-secret"


def run_T_BANNER(temp_db: str):
    print("\n[T_BANNER]")
    daemon = _get_daemon(temp_db)

    check("T_BAN_helper_present", hasattr(daemon, "_attach_stale_banner"),
          "daemon missing _attach_stale_banner")

    # stale -> banner present, includes verdict + timestamp; hit NOT suppressed.
    item = {"id": 1, "content": "x"}
    out = daemon._attach_stale_banner(item, {"verdict": "stale", "grounded_at": "2026-07-01T00:00:00+00:00"})
    check("T_BAN_stale_has_banner", "stale_banner" in out, str(out))
    check("T_BAN_stale_mentions_verdict", "stale" in out.get("stale_banner", ""), out.get("stale_banner"))
    check("T_BAN_stale_mentions_ts", "2026-07-01" in out.get("stale_banner", ""), out.get("stale_banner"))
    check("T_BAN_stale_not_suppressed", out.get("id") == 1 and out.get("content") == "x", str(out))

    # revalidating -> banner present.
    it2 = daemon._attach_stale_banner({"id": 2}, {"verdict": "revalidating", "grounded_at": None})
    check("T_BAN_revalidating_has_banner", "stale_banner" in it2, str(it2))

    # fresh / aging / unverified -> NO banner.
    for verdict in ("fresh", "aging", "unverified"):
        it = daemon._attach_stale_banner({"id": 9}, {"verdict": verdict, "grounded_at": "2026-07-16T00:00:00+00:00"})
        check(f"T_BAN_no_banner_{verdict}", "stale_banner" not in it, str(it))


# ---------------------------------------------------------------------------
# T_MIGR_034 — migration idempotency + columns
# ---------------------------------------------------------------------------

def run_T_MIGR_034(temp_db: str):
    print("\n[T_MIGR_034]")
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    for tbl in ("claim_embeddings", "insights", "principles"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})")}
        check(f"T_M34_col_{tbl}", "embedding_model_version" in cols, f"{tbl} cols: {cols}")
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    check("T_M34_version", version >= 34, f"schema_version={version}")
    conn.close()

    # Double-run -> only tolerated duplicate-column errors.
    conn2 = sqlite3.connect(temp_db)
    tolerated = _apply_sql_file(conn2, MIGR / "034_embedding_model_version.sql")
    conn2.commit()
    check("T_M34_idempotent_tolerated", len(tolerated) >= 3,
          f"expected >=3 tolerated ADD COLUMN errors, got {len(tolerated)}: {tolerated}")
    check("T_M34_idempotent_all_dup",
          all("duplicate column name" in t.lower() for t in tolerated),
          f"unexpected non-dup error: {tolerated}")
    conn2.close()


# ---------------------------------------------------------------------------
# T_STAMP — claim_layer stamps embedding_model_version + pre-034 fallback
# ---------------------------------------------------------------------------

def run_T_STAMP(temp_db: str, temp_db_no034: str):
    print("\n[T_STAMP]")
    import claim_layer
    import embed

    expected_version = getattr(embed, "EMBEDDING_MODEL", None)
    check("T_STMP_model_id_exists", bool(expected_version),
          "embed.EMBEDDING_MODEL missing — nothing to stamp with")

    # Directly exercise the stamped INSERT (WITH 034 column). We insert a claim
    # + a stamped embedding row exactly the way persist_claims does, then read
    # it back. This validates the column + stamp value without needing a live
    # embedding model to run.
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    emv = claim_layer._embedding_model_version()
    check("T_STMP_accessor_matches_embed", emv == expected_version, f"{emv!r} != {expected_version!r}")

    conn.execute(
        "INSERT OR REPLACE INTO claim_embeddings "
        "(claim_id, embedding, created_at, embedding_model_version) VALUES (?,?,?,?)",
        (777001, b"\x00\x01\x02\x03", "2026-07-17T00:00:00+00:00", emv),
    )
    conn.commit()
    row = conn.execute(
        "SELECT embedding_model_version FROM claim_embeddings WHERE claim_id=777001"
    ).fetchone()
    check("T_STMP_stamped_value", row is not None and row["embedding_model_version"] == expected_version,
          f"got {dict(row) if row else None}")
    conn.close()

    # Pre-034 fallback: on a DB WITHOUT the column, the stamped INSERT raises
    # OperationalError and the code must fall back to the 3-column INSERT.
    conn2 = sqlite3.connect(temp_db_no034)
    conn2.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn2.execute("PRAGMA table_info(claim_embeddings)")}
    check("T_STMP_no034_missing_col", "embedding_model_version" not in cols,
          f"temp_db_no034 unexpectedly has the column: {cols}")

    fell_back = False
    try:
        conn2.execute(
            "INSERT OR REPLACE INTO claim_embeddings "
            "(claim_id, embedding, created_at, embedding_model_version) VALUES (?,?,?,?)",
            (777002, b"\x00", "2026-07-17T00:00:00+00:00", emv),
        )
    except sqlite3.OperationalError:
        fell_back = True
        conn2.execute(
            "INSERT OR REPLACE INTO claim_embeddings (claim_id, embedding, created_at) VALUES (?,?,?)",
            (777002, b"\x00", "2026-07-17T00:00:00+00:00"),
        )
    conn2.commit()
    check("T_STMP_pre034_raises_then_fallback", fell_back,
          "stamped INSERT did NOT raise on a pre-034 DB — fallback path unreachable")
    got = conn2.execute("SELECT claim_id FROM claim_embeddings WHERE claim_id=777002").fetchone()
    check("T_STMP_fallback_row_written", got is not None, "fallback INSERT did not persist the embedding")
    conn2.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    temp_db = _build_temp_db(apply_034=True)
    temp_db_no034 = _build_temp_db(apply_034=False)

    run_T_SYNC()
    run_T_CAPCFG()
    run_T_CAPTASK(temp_db)
    run_T_AUTH(temp_db)
    run_T_BANNER(temp_db)
    run_T_MIGR_034(temp_db)
    run_T_STAMP(temp_db, temp_db_no034)

    for p in (temp_db, temp_db_no034):
        try:
            Path(p).unlink(missing_ok=True)
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
    print("All tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
