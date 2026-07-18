#!/usr/bin/env python3
"""Overlay module-discovery tests — the `crag_anchor.modules` seam.

Standalone (no pytest — mirrors test_console_endpoints.py / test_aggregates.py).
Builds a THROWAWAY temp DB from schema.sql + every migration, then imports the
FULL daemon module (which runs overlay discovery at import time) with the
CRAG_ANCHOR_MODULES env var pointed at fixture overlay modules we write to a temp
dir. Each case re-imports the daemon under a unique module name after clearing
the shared `aggregates` module from sys.modules, so a register() append in one
case never bleeds into another.

Covers:
  T_NONE     — no overlays: daemon boots, /console/modules is the 6 core modules
               only, and no `_loaded_overlay_modules` entries.
  T_GOOD     — a fixture overlay with router + register() + bind(): its route
               serves (200), it appears in /console/modules, and its name is in
               _loaded_overlay_modules.
  T_BROKEN   — a fixture overlay that raises at import: daemon STILL boots, the
               broken module is absent from _loaded_overlay_modules, a sibling
               GOOD overlay in the same list loads fine, and the manifest is
               otherwise unaffected.

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_module_discovery.py
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_DIR = REPO_ROOT / "db"
SCHEMA = DB_DIR / "schema.sql"
MIGRATIONS_DIR = DB_DIR / "migrations"
DAEMON_PY = REPO_ROOT / "apps" / "daemon" / "engine_daemon.py"
DAEMON_DIR = REPO_ROOT / "apps" / "daemon"

# The daemon and its sibling modules (aggregates, session_lifecycle) are imported
# by bare name from apps/daemon; ensure that dir + db/ are importable.
for p in (str(DAEMON_DIR), str(DB_DIR)):
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
        print(f"  [X] {name}  -- {detail}")


_TOLERATE = ("duplicate column name", "already exists")


def _apply_migration(conn: sqlite3.Connection, path: Path) -> None:
    """Apply an additive migration, tolerating re-run errors and keeping trigger
    BEGIN..END bodies whole (mirrors test_console_endpoints.py)."""
    raw = path.read_text(encoding="utf-8")
    no_comments = "\n".join(re.sub(r"--.*$", "", ln) for ln in raw.splitlines())
    chunks = no_comments.split(";")
    stmts: list[str] = []
    buf = ""
    depth = 0
    for chunk in chunks:
        buf = buf + chunk if buf else chunk
        depth += len(re.findall(r"\bBEGIN\b", chunk, re.I))
        depth -= len(re.findall(r"\bEND\b", chunk, re.I))
        if depth > 0:
            buf += ";"
            continue
        stmts.append(buf)
        buf = ""
    if buf.strip():
        stmts.append(buf)
    for stmt in stmts:
        s = stmt.strip()
        if not s:
            continue
        try:
            conn.execute(s)
        except sqlite3.OperationalError as exc:
            if any(t in str(exc).lower() for t in _TOLERATE):
                continue
            raise


def build_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="moddisc-test-")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
        _apply_migration(conn, mig)
        conn.commit()
    conn.close()
    return path


TEMP_DB = build_temp_db()
print(f"temp DB: {TEMP_DB}")

# A temp dir holding fixture overlay modules; put it on sys.path so the daemon's
# CRAG_ANCHOR_MODULES importlib.import_module() resolves them by bare name.
FIXTURE_DIR = tempfile.mkdtemp(prefix="moddisc-overlays-")
if FIXTURE_DIR not in sys.path:
    sys.path.insert(0, FIXTURE_DIR)

# A GOOD overlay: bind() (takes **kwargs), register() (idempotent CORE_MODULES
# append), and a router with one route. This is exactly ops_infra's shape.
(Path(FIXTURE_DIR) / "fixture_good_overlay.py").write_text(
    '''
from fastapi import APIRouter

_bound = {}

def bind(**kwargs):
    _bound.update(kwargs)

router = APIRouter()

@router.get("/fixture/ping")
async def ping():
    return {"ok": True, "pong": True, "bound_get_db": _bound.get("get_db") is not None}

FIXTURE_MODULE = {"id": "fixture", "title": "Fixture", "icon": "beaker",
                  "route": "/fixture", "panels": ["ping"]}

def register(aggregates_module):
    core = aggregates_module.CORE_MODULES
    if not any(m.get("id") == "fixture" for m in core):
        core.append(dict(FIXTURE_MODULE))
''',
    encoding="utf-8",
)

# A BROKEN overlay: raises at import time.
(Path(FIXTURE_DIR) / "fixture_broken_overlay.py").write_text(
    'raise RuntimeError("deliberately broken overlay")\n',
    encoding="utf-8",
)


def _load_daemon(unique: str, modules_env: str | None):
    """Import a FRESH daemon instance under a unique module name, with the given
    CRAG_ANCHOR_MODULES env. Clears the shared `aggregates`/`session_lifecycle`
    modules first so each case gets a pristine CORE_MODULES list (register()
    appends are per-import, not cumulative across cases)."""
    for shared in ("aggregates", "session_lifecycle"):
        sys.modules.pop(shared, None)
    if modules_env is None:
        os.environ.pop("CRAG_ANCHOR_MODULES", None)
    else:
        os.environ["CRAG_ANCHOR_MODULES"] = modules_env

    import importlib.util

    spec = importlib.util.spec_from_file_location(unique, str(DAEMON_PY))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique] = mod
    spec.loader.exec_module(mod)
    mod.DB_PATH = Path(TEMP_DB)
    return mod


def _client(daemon):
    from fastapi.testclient import TestClient
    return TestClient(daemon.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
def test_none():
    print("\ntest_none:")
    daemon = _load_daemon("engine_daemon_moddisc_none", None)
    check("T_NONE_no_overlays_loaded",
          daemon._loaded_overlay_modules == [],
          str(daemon._loaded_overlay_modules))
    with _client(daemon) as client:
        r = client.get("/console/modules")
        ids = [m["id"] for m in r.json().get("modules", [])]
        check("T_NONE_manifest_core_only",
              ids == ["loop", "claims", "review", "grounding", "corpus", "sessions"],
              str(ids))


def test_good():
    print("\ntest_good:")
    daemon = _load_daemon("engine_daemon_moddisc_good", "fixture_good_overlay")
    check("T_GOOD_overlay_loaded",
          "env:fixture_good_overlay" in daemon._loaded_overlay_modules,
          str(daemon._loaded_overlay_modules))
    with _client(daemon) as client:
        r = client.get("/fixture/ping")
        body = r.json()
        check("T_GOOD_route_serves",
              r.status_code == 200 and body.get("pong") is True,
              f"status={r.status_code} body={r.text[:160]}")
        check("T_GOOD_bind_injected_get_db",
              body.get("bound_get_db") is True, str(body))
        r = client.get("/console/modules")
        ids = [m["id"] for m in r.json().get("modules", [])]
        check("T_GOOD_manifest_has_fixture", "fixture" in ids, str(ids))
        check("T_GOOD_manifest_keeps_core",
              ids[:6] == ["loop", "claims", "review", "grounding", "corpus", "sessions"],
              str(ids))


def test_broken():
    print("\ntest_broken:")
    # broken listed FIRST, good listed SECOND — proves per-module isolation:
    # the good sibling must still load after the broken one is skipped.
    daemon = _load_daemon(
        "engine_daemon_moddisc_broken",
        "fixture_broken_overlay,fixture_good_overlay",
    )
    loaded = daemon._loaded_overlay_modules
    check("T_BROKEN_broken_absent",
          "env:fixture_broken_overlay" not in loaded, str(loaded))
    check("T_BROKEN_sibling_still_loaded",
          "env:fixture_good_overlay" in loaded, str(loaded))
    with _client(daemon) as client:
        # daemon booted at all — a core route answers.
        r = client.get("/console/modules")
        check("T_BROKEN_daemon_still_boots",
              r.status_code == 200 and r.json().get("ok") is True,
              f"status={r.status_code}")
        ids = [m["id"] for m in r.json().get("modules", [])]
        check("T_BROKEN_good_sibling_route",
              "fixture" in ids, str(ids))
        # the good sibling's route works despite the broken neighbor.
        r = client.get("/fixture/ping")
        check("T_BROKEN_sibling_route_serves",
              r.status_code == 200 and r.json().get("pong") is True,
              f"status={r.status_code}")


def main() -> int:
    print("=== overlay module-discovery suite ===")
    try:
        from fastapi.testclient import TestClient  # noqa: F401
    except ImportError:
        check("T_SKIPPED", True, "fastapi not installed — cannot exercise ASGI")
        print(f"\nPASS {len(PASSES)}  FAIL {len(FAILURES)}")
        return 0
    for fn in (test_none, test_good, test_broken):
        fn()
    print(f"\n{'='*50}\nPASS {len(PASSES)}  FAIL {len(FAILURES)}")
    for f in FAILURES:
        print(f"  FAIL: {f}")
    try:
        os.unlink(TEMP_DB)
    except OSError:
        pass
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
