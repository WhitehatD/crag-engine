#!/usr/bin/env python3
# coding: utf-8
"""E1 principles_export + C3 drain-sweep test suite (closed-loop audit gaps).

Standalone (no pytest — house style, mirrors test_grounding_v3_rev3.py).

  T_EXPORT_SHAPE     — /principles/export returns the crag fetch-principles.js
                       contract: {ok, principles:[{id,text,confidence,
                       claim_health,...}], as_of}. `text` carries the verbatim
                       principle content (engine `content` -> `text` mapping).
  T_EXPORT_HEALTH    — a principle with fresh core claims exports
                       claim_health='fresh'; one with a stale core claim does
                       NOT export 'fresh'; one with no claims exports
                       'unverified'.
  T_EXPORT_FILTER    — compile_eligible=true returns ONLY fresh/passing;
                       false returns all with honest health.
  T_EXPORT_CRAG_GATE — the exported objects pass crag render.js's eligibility
                       semantics: eligible iff id present, text non-empty,
                       claim_health in {fresh, passing}.
  T_DRAIN_CONFIG     — _drain_sweep_config(): default (enabled, 3600s);
                       env disable; interval min-clamp at 60s; bad value
                       falls back to 3600.
  T_DRAIN_WIRED      — _drain_sweep_loop exists as an async coroutine and the
                       lifespan body references it (static wiring check).

Run:
  python apps/daemon/tests/test_principles_export_drain.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[3]
DAEMON_PY = REPO_ROOT / "apps" / "daemon" / "engine_daemon.py"
DB_DIR = REPO_ROOT / "db"
MIGRATIONS = DB_DIR / "migrations"
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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Temp DB from schema files (worktree-safe: never opens the live engine.db).
# ---------------------------------------------------------------------------

def _apply_sql(conn, path: Path):
    """Statement-accumulating splitter (sqlite3.complete_statement) so
    compound statements (CREATE TRIGGER ... BEGIN ...; END) survive —
    same approach as test_closed_loop_hardening.py."""
    raw = path.read_text(encoding="utf-8")
    no_comments = "\n".join(
        ln for ln in raw.splitlines() if not ln.strip().startswith("--"))
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
                continue
            raise


def _build_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e1c3test-")
    os.close(fd)
    conn = sqlite3.connect(path)
    schema = DB_DIR / "schema.sql"
    if schema.exists():
        conn.executescript(schema.read_text(encoding="utf-8"))
        conn.commit()
    for mig in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        if int(mig.name[:3]) <= 3:
            continue  # base schema already covers 001-003
        _apply_sql(conn, mig)
    conn.commit()
    conn.close()
    return path


def _load_daemon():
    spec = importlib.util.spec_from_file_location("engine_daemon_e1c3", str(DAEMON_PY))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["engine_daemon_e1c3"] = mod
    spec.loader.exec_module(mod)
    return mod


TEMP_DB = _build_temp_db()
os.environ["CRAG_ENGINE_ALLOW_SYNC_PATH"] = "1"  # temp dirs can look sync-ish on CI boxes
daemon = _load_daemon()
daemon.DB_PATH = Path(TEMP_DB)

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(daemon.app)


def _seed(conn):
    now = _utcnow()
    # Principle 9101: fresh core claim.  9102: stale core claim.  9103: no claims.
    for pid, content in ((9101, "Fresh-claims principle body."),
                         (9102, "Stale-claims principle body."),
                         (9103, "Claimless principle body.")):
        conn.execute(
            "INSERT INTO principles (id, project, content, confidence, created_at, updated_at)"
            " VALUES (?, 'testproj', ?, 0.9, ?, ?)", (pid, content, now, now))
    conn.execute(
        "INSERT INTO claims (id, canonical_key, text, predicate_class, status,"
        " grounded_at, last_verdict, created_at) VALUES"
        " (9201, 'k-fresh', 'the fresh fact', 'P1', 'active', ?, 'pass', ?)",
        (now, now))
    conn.execute(
        "INSERT INTO claims (id, canonical_key, text, predicate_class, status,"
        " grounded_at, last_verdict, created_at) VALUES"
        " (9202, 'k-stale', 'the drifted fact', 'P1', 'active', ?, 'fail', ?)",
        (now, now))
    conn.execute(
        "INSERT INTO principle_claims (principle_id, claim_id, role, created_at)"
        " VALUES (9101, 9201, 'core', ?)", (now,))
    conn.execute(
        "INSERT INTO principle_claims (principle_id, claim_id, role, created_at)"
        " VALUES (9102, 9202, 'core', ?)", (now,))
    conn.commit()


conn0 = sqlite3.connect(TEMP_DB)
conn0.row_factory = sqlite3.Row
_seed(conn0)
conn0.close()


# ---------------------------------------------------------------------------
# T_EXPORT_SHAPE
# ---------------------------------------------------------------------------

def run_T_EXPORT_SHAPE():
    print("\n[T_EXPORT_SHAPE]")
    r = client.get("/principles/export", params={"project": "testproj"})
    check("T_ES_http_200", r.status_code == 200, f"status={r.status_code}")
    body = r.json()
    check("T_ES_ok", body.get("ok") is True, str(body)[:200])
    check("T_ES_as_of", isinstance(body.get("as_of"), str) and "T" in body["as_of"],
          str(body.get("as_of")))
    ps = body.get("principles")
    check("T_ES_list", isinstance(ps, list) and len(ps) == 3, f"got {type(ps)} len={len(ps or [])}")
    if ps:
        p = next(x for x in ps if x["id"] == 9101)
        check("T_ES_text_mapping", p.get("text") == "Fresh-claims principle body.",
              f"text={p.get('text')!r} — engine content must map to crag text VERBATIM")
        for field in ("id", "text", "confidence", "claim_health"):
            check(f"T_ES_field_{field}", field in p, f"missing {field} in {p}")


# ---------------------------------------------------------------------------
# T_EXPORT_HEALTH
# ---------------------------------------------------------------------------

def run_T_EXPORT_HEALTH():
    print("\n[T_EXPORT_HEALTH]")
    body = client.get("/principles/export", params={"project": "testproj"}).json()
    by_id = {p["id"]: p for p in body["principles"]}
    check("T_EH_fresh", by_id[9101]["claim_health"] == "fresh",
          f"9101 health={by_id[9101]['claim_health']!r}")
    check("T_EH_stale_not_fresh", by_id[9102]["claim_health"] not in ("fresh", "passing"),
          f"9102 health={by_id[9102]['claim_health']!r} — stale core claim must not export eligible")
    check("T_EH_claimless_unverified", by_id[9103]["claim_health"] == "unverified",
          f"9103 health={by_id[9103]['claim_health']!r}")


# ---------------------------------------------------------------------------
# T_EXPORT_FILTER
# ---------------------------------------------------------------------------

def run_T_EXPORT_FILTER():
    print("\n[T_EXPORT_FILTER]")
    body = client.get("/principles/export",
                      params={"project": "testproj", "compile_eligible": "true"}).json()
    ids = [p["id"] for p in body["principles"]]
    check("T_EF_only_fresh", ids == [9101], f"got {ids}")
    body_all = client.get("/principles/export",
                          params={"project": "testproj", "compile_eligible": "false"}).json()
    check("T_EF_all_returned", len(body_all["principles"]) == 3,
          f"got {len(body_all['principles'])}")


# ---------------------------------------------------------------------------
# T_EXPORT_CRAG_GATE — replicate render.js eligiblePrinciples() semantics.
# ---------------------------------------------------------------------------

def run_T_EXPORT_CRAG_GATE():
    print("\n[T_EXPORT_CRAG_GATE]")
    ELIGIBLE = {"fresh", "passing"}

    def crag_eligible(p):
        if not isinstance(p, dict):
            return False
        if p.get("id") is None:
            return False
        if not isinstance(p.get("text"), str) or not p["text"].strip():
            return False
        return p.get("claim_health") in ELIGIBLE

    body = client.get("/principles/export", params={"project": "testproj"}).json()
    eligible = [p["id"] for p in body["principles"] if crag_eligible(p)]
    check("T_CG_exactly_fresh_pass", eligible == [9101], f"crag would render {eligible}")
    # JSON round-trip safety (crag parses the text content of the tool result)
    check("T_CG_json_roundtrip",
          json.loads(json.dumps(body))["principles"][0]["id"] is not None, "roundtrip failed")


# ---------------------------------------------------------------------------
# T_DRAIN_CONFIG
# ---------------------------------------------------------------------------

def run_T_DRAIN_CONFIG():
    print("\n[T_DRAIN_CONFIG]")
    for k in ("CRAG_ENGINE_DISPOSITION_DRAIN_ENABLED", "CRAG_ENGINE_DISPOSITION_DRAIN_INTERVAL_SEC"):
        os.environ.pop(k, None)
    en, iv = daemon._drain_sweep_config()
    check("T_DC_default", en is True and iv == 3600.0, f"({en},{iv})")

    os.environ["CRAG_ENGINE_DISPOSITION_DRAIN_ENABLED"] = "0"
    en, _ = daemon._drain_sweep_config()
    check("T_DC_disable", en is False, f"enabled={en}")
    os.environ.pop("CRAG_ENGINE_DISPOSITION_DRAIN_ENABLED")

    os.environ["CRAG_ENGINE_DISPOSITION_DRAIN_INTERVAL_SEC"] = "5"
    _, iv = daemon._drain_sweep_config()
    check("T_DC_min_clamp", iv == 60.0, f"interval={iv} (must clamp to 60)")

    os.environ["CRAG_ENGINE_DISPOSITION_DRAIN_INTERVAL_SEC"] = "not-a-number"
    _, iv = daemon._drain_sweep_config()
    check("T_DC_bad_value_fallback", iv == 3600.0, f"interval={iv}")
    os.environ.pop("CRAG_ENGINE_DISPOSITION_DRAIN_INTERVAL_SEC")


# ---------------------------------------------------------------------------
# T_DRAIN_WIRED
# ---------------------------------------------------------------------------

def run_T_DRAIN_WIRED():
    print("\n[T_DRAIN_WIRED]")
    import inspect
    check("T_DW_coroutine", inspect.iscoroutinefunction(daemon._drain_sweep_loop),
          "not an async coroutine function")
    src = DAEMON_PY.read_text(encoding="utf-8")
    lifespan_body = src.split("async def lifespan(", 1)[1].split("\napp = FastAPI", 1)[0]
    check("T_DW_lifespan_starts", "_drain_sweep_loop()" in lifespan_body,
          "lifespan does not create the drain sweep task")
    check("T_DW_lifespan_cancels", "drain sweep" in lifespan_body,
          "lifespan shutdown does not reference the drain task")


def main():
    run_T_EXPORT_SHAPE()
    run_T_EXPORT_HEALTH()
    run_T_EXPORT_FILTER()
    run_T_EXPORT_CRAG_GATE()
    run_T_DRAIN_CONFIG()
    run_T_DRAIN_WIRED()

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
    print("All tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
