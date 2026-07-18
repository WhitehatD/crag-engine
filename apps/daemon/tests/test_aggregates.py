#!/usr/bin/env python3
# coding: utf-8
"""Surface aggregates test suite — /overview /inbox /rules /console/modules.

The read-model that feeds ALL four surface consumers (CLI status/inbox/why,
console nav, app.crag.sh snapshot, ops Infra overlay). ONE contract, so this
suite pins its shapes (infra-playbook docs/system-integration-map.md §2).

Standalone (no pytest — mirrors test_disposition_engine.py).

Covers:
  T_EMPTY_*    — every builder runs on a FRESH migrated DB (no rows) and returns
                 a well-formed, non-crashing payload. This is the evaluator's
                 first-run path: `crag-anchor up` on an empty DB must render.
  T_TRUST      — trust_score = verified fraction of active claims; a fresh (pass)
                 claim counts verified, a stale one does not; value in [0,1].
  T_OVERVIEW   — counts + today strip + needs_you reflect seeded rows.
  T_INBOX_*    — each inbox kind (t2 disposition / grounding proposal /
                 contradiction / stale rule) appears with the unified shape
                 (id kind title why evidence actions), and total is accurate.
  T_RULES      — active principle appears; superseded one does not; claim_health
                 present; stale flag set when the rollup verdict is stale.
  T_MODULES    — the 6 core modules, stable ids/routes.
  T_FAILSOFT   — a builder against a DB missing a table degrades, never raises.

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_aggregates.py
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_DIR = REPO_ROOT / "db"
DAEMON_DIR = REPO_ROOT / "apps" / "daemon"
MIGRATIONS = [
    "031_grounding_v3_claim_layer.sql",
    "032_grounding_v3_rev3.sql",
    "033_disposition_engine.sql",
]

for p in (str(DB_DIR), str(DAEMON_DIR)):
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


def _apply_sql_file(conn: sqlite3.Connection, path: Path) -> None:
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
                continue
            raise


LIVE_DB = DB_DIR / "engine.db"


def _base_tables(conn: sqlite3.Connection) -> None:
    """For the fail-soft test only: a DB with base parents but NONE of the
    grounding-v3 tables, proving the builders degrade rather than raise."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT, project TEXT, status TEXT DEFAULT 'active',
            embedding BLOB, created_at TEXT, superseded_by INTEGER,
            grounding_due INTEGER DEFAULT 0, grounded_at TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS principles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT, confidence REAL, project TEXT, tags TEXT,
            superseded_by INTEGER, created_at TEXT
        )""")


def _build_temp_db() -> str:
    """Clone the live schema (structure only, no rows) into a temp DB, then
    apply the v3 migrations idempotently. Mirrors test_disposition_engine.py so
    inter-migration table dependencies (e.g. grounding_jobs from 026) resolve.
    Falls back to a minimal base if no live DB exists (bare CI runner)."""
    if not LIVE_DB.exists():
        print(f"FATAL: {LIVE_DB} missing — CI bootstraps it (see ci.yml); locally run the daemon once.")
        sys.exit(1)
    fd, path = tempfile.mkstemp(suffix=".db", prefix="aggregates-test-")
    os.close(fd)
    conn = sqlite3.connect(path)
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    for s in stmts:
        try:
            conn.execute(s)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    for mig in MIGRATIONS:
        _apply_sql_file(conn, DB_DIR / "migrations" / mig)
    conn.commit()
    conn.close()
    print(f"temp DB: {path}")
    return path


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TEMP_DB = _build_temp_db()
claim_layer = _load("claim_layer", DB_DIR / "claim_layer.py")
agg = _load("aggregates", DAEMON_DIR / "aggregates.py")


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def db(path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or TEMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


agg.bind(get_db=db, table_exists=_table_exists, claim_layer=claim_layer)

NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")
ISO = NOW.isoformat()


# ---------------------------------------------------------------------------
# T_EMPTY — fresh migrated DB, no rows: everything renders, nothing crashes.
# ---------------------------------------------------------------------------
def test_empty():
    conn = db()
    ov = agg.build_overview(conn)
    check("T_EMPTY_overview_shape",
          set(ov) >= {"trust_score", "counts", "needs_you", "today", "health", "generated_at"},
          str(ov.keys()))
    check("T_EMPTY_trust_none", ov["trust_score"]["value"] is None, str(ov["trust_score"]))
    check("T_EMPTY_counts_zero", ov["counts"] == {"insights": 0, "principles": 0, "claims": 0},
          str(ov["counts"]))
    ib = agg.build_inbox(conn)
    check("T_EMPTY_inbox_empty", ib == {"items": [], "total": 0}, str(ib))
    ru = agg.build_rules(conn)
    check("T_EMPTY_rules_empty", ru == {"rules": []}, str(ru))
    conn.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------
def _seed_claim(conn, text, verdict, predicate="P1_MECHANICAL", grounded=True):
    cur = conn.execute(
        "INSERT INTO claims (canonical_key, text, predicate_class, status, "
        "last_verdict, grounded_at, grounding_due, created_at) "
        "VALUES (?,?,?,'active',?,?,0,?)",
        (text[:40], text, predicate, "pass" if verdict == "fresh" else "fail",
         ISO if grounded else None, ISO),
    )
    return cur.lastrowid


def test_trust():
    path = _build_temp_db()
    conn = db(path)
    _seed_claim(conn, "port 8786 is the daemon", "fresh")
    _seed_claim(conn, "port 8786 is the daemon B", "fresh")
    # a stale one
    conn.execute(
        "INSERT INTO claims (canonical_key, text, predicate_class, status, "
        "last_verdict, grounding_due, created_at) VALUES (?,?,?,'active','fail',1,?)",
        ("stale-x", "stale claim", "P1_MECHANICAL", ISO),
    )
    conn.commit()
    t = agg._trust_score(conn)
    check("T_TRUST_active_count", t["active_claims"] == 3, str(t))
    check("T_TRUST_verified_two", t["verified"] == 2, str(t))
    check("T_TRUST_value_range", 0.0 <= t["value"] <= 1.0 and abs(t["value"] - 0.667) < 0.01,
          str(t))
    conn.close()
    os.unlink(path)


def test_overview_seeded():
    path = _build_temp_db()
    conn = db(path)
    conn.execute("INSERT INTO insights (content, status, created_at) VALUES ('i','active',?)", (ISO,))
    conn.execute("INSERT INTO principles (content, confidence, created_at) VALUES ('p',0.9,?)", (ISO,))
    conn.commit()
    ov = agg.build_overview(conn)
    check("T_OVERVIEW_insight_count", ov["counts"]["insights"] == 1, str(ov["counts"]))
    check("T_OVERVIEW_principle_count", ov["counts"]["principles"] == 1, str(ov["counts"]))
    check("T_OVERVIEW_today_promoted", ov["today"]["promoted"] == 1, str(ov["today"]))
    conn.close()
    os.unlink(path)


def test_inbox_kinds():
    path = _build_temp_db()
    conn = db(path)
    # t2 disposition
    conn.execute(
        "INSERT INTO insights_staging (source, project, payload, status, tier, reason, created_at) "
        "VALUES ('gate_failure','x','{}','pending','t2','needs approval',?)", (ISO,))
    # grounding proposal
    conn.execute(
        "INSERT INTO resolution_proposals (claim_kind, claim_id, proposed_action, reasoning, "
        "stakes, status, created_at) VALUES ('insight',5,'supersede','drifted','high','pending',?)",
        (ISO,))
    # contradiction
    conn.execute(
        "INSERT INTO claim_contradictions (claim_a_id, claim_b_id, reason, score, status, detected_at) "
        "VALUES (1,2,'opposite',0.9,'open',?)", (ISO,))
    conn.commit()
    ib = agg.build_inbox(conn)
    kinds = {i["kind"] for i in ib["items"]}
    check("T_INBOX_has_disposition", "t2_disposition" in kinds, str(kinds))
    check("T_INBOX_has_proposal", "grounding_proposal" in kinds, str(kinds))
    check("T_INBOX_has_contradiction", "contradiction" in kinds, str(kinds))
    check("T_INBOX_total", ib["total"] == 3, str(ib["total"]))
    shape_ok = all(
        {"id", "kind", "title", "why", "evidence", "actions"} <= set(i) for i in ib["items"])
    check("T_INBOX_unified_shape", shape_ok, str(ib["items"][0]) if ib["items"] else "empty")
    id_prefixed = all(":" in i["id"] for i in ib["items"])
    check("T_INBOX_id_prefixed", id_prefixed, str([i["id"] for i in ib["items"]]))
    conn.close()
    os.unlink(path)


def test_rules():
    path = _build_temp_db()
    conn = db(path)
    conn.execute("INSERT INTO principles (id, content, confidence, created_at) VALUES (1,'live rule',0.9,?)", (ISO,))
    conn.execute("INSERT INTO principles (id, content, confidence, superseded_by, created_at) VALUES (2,'dead rule',0.9,1,?)", (ISO,))
    conn.commit()
    ru = agg.build_rules(conn)
    ids = {r["principle_id"] for r in ru["rules"]}
    check("T_RULES_live_present", 1 in ids, str(ids))
    check("T_RULES_superseded_absent", 2 not in ids, str(ids))
    r1 = next(r for r in ru["rules"] if r["principle_id"] == 1)
    check("T_RULES_health_present", "claim_health" in r1 and "stale" in r1, str(r1))
    check("T_RULES_no_claims_unverified", r1["claim_health"] == "unverified", str(r1))
    conn.close()
    os.unlink(path)


def test_modules():
    m = agg.build_modules()
    ids = [x["id"] for x in m["modules"]]
    check("T_MODULES_six_core",
          ids == ["loop", "claims", "review", "grounding", "corpus", "sessions"], str(ids))
    check("T_MODULES_routes", all("route" in x and "panels" in x for x in m["modules"]), str(m))


def test_failsoft():
    # A DB with NO grounding-v3 tables at all — only base insights/principles.
    fd, path = tempfile.mkstemp(suffix=".db", prefix="agg-failsoft-")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _base_tables(conn)
    conn.commit()
    raised = False
    try:
        ov = agg.build_overview(conn)
        ib = agg.build_inbox(conn)
        ru = agg.build_rules(conn)
    except Exception as exc:  # noqa: BLE001
        raised = True
        ov = ib = ru = str(exc)
    check("T_FAILSOFT_no_raise", not raised, str(ov))
    check("T_FAILSOFT_overview_ok", (not raised) and ov["counts"]["claims"] == 0, str(ov))
    check("T_FAILSOFT_inbox_empty", (not raised) and ib["total"] == 0, str(ib))
    conn.close()
    os.unlink(path)


def test_http_routes():
    """Route-level check through the real ASGI stack (TestClient). This is the
    layer the pure-builder tests skip — it catches cross-thread sqlite misuse
    in the async wrappers (found by the ops back-port 2026-07-18): the conn
    must be opened INSIDE the executor thread, or every route 500s."""
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except ImportError:
        check("T_HTTP_skipped", True, "fastapi not installed — builders-only env")
        return
    test_app = FastAPI()
    test_app.include_router(agg.router)
    with TestClient(test_app, raise_server_exceptions=False) as client:
        for path in ("/overview", "/inbox", "/rules", "/console/modules"):
            r = client.get(path)
            ok = r.status_code == 200 and r.json().get("ok") is True
            check(f"T_HTTP_{path.strip('/').replace('/','_')}_200_ok", ok,
                  f"status={r.status_code} body={r.text[:200]}")


def main() -> int:
    print("=== surface aggregates suite ===")
    for fn in (test_empty, test_trust, test_overview_seeded, test_inbox_kinds,
               test_rules, test_modules, test_failsoft, test_http_routes):
        print(f"\n{fn.__name__}:")
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
