#!/usr/bin/env python3
# coding: utf-8
"""Session lifecycle test suite — GET /session/start + POST /session/end.

The deterministic P0 loop (design laws 1-2): the harness SessionStart hook GETs
/session/start to inject context; the SessionEnd hook POSTs /session/end to
record an end marker + get the payoff receipt. Both must be fail-soft on an
empty/unmigrated DB (the evaluator's first-run path) and fast (no LLM/embedding).

Standalone (no pytest — mirrors test_aggregates.py). Reuses the SAME temp-DB
harness (clone live schema, apply v3 migrations) so the aggregates builders it
delegates to resolve their tables.

Covers:
  T_EMPTY_*    — both builders run on a FRESH migrated DB (no rows) and return
                 well-formed, non-crashing payloads.
  T_START_*    — start payload composes overview + needs_you_top (ranked, ≤3) +
                 rules_stale_count + last_session from seeded rows.
  T_END_*      — end returns the payoff numbers (reused _today_activity) and
                 records a marker row into `sessions`; a schema-less DB no-ops
                 (recorded=False) rather than raising (fail-open).
  T_FAILSOFT   — builders against a DB missing the diary/staging tables degrade.
  T_HTTP       — route-level via TestClient (catches the cross-thread sqlite
                 misuse the aggregates suite found — conn opened in the executor).

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_session_lifecycle.py
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
    """For the fail-soft test only: base parents but NONE of the diary/staging/
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
    """Clone the live schema (structure only, no rows), apply the v3 migrations.
    Mirrors test_aggregates.py so inter-migration dependencies resolve."""
    if not LIVE_DB.exists():
        print(f"FATAL: {LIVE_DB} missing — CI bootstraps it (see ci.yml); locally run the daemon once.")
        sys.exit(1)
    fd, path = tempfile.mkstemp(suffix=".db", prefix="session-lifecycle-test-")
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
sl = _load("session_lifecycle", DAEMON_DIR / "session_lifecycle.py")


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def db(path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or TEMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


# aggregates binds against the same accessors; session_lifecycle reuses aggregates.
agg.bind(get_db=db, table_exists=_table_exists, claim_layer=claim_layer)
sl.bind(get_db=db, table_exists=_table_exists, aggregates=agg)

NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")
ISO = NOW.isoformat()


# ---------------------------------------------------------------------------
# T_EMPTY — fresh migrated DB, no rows: everything renders, nothing crashes.
# ---------------------------------------------------------------------------
def test_empty():
    conn = db()
    st = sl.build_session_start(conn)
    check("T_EMPTY_start_shape",
          set(st) >= {"overview", "needs_you_top", "needs_you_total",
                      "rules_stale_count", "last_session", "generated_at"},
          str(st.keys()))
    check("T_EMPTY_start_needs_empty", st["needs_you_top"] == [] and st["needs_you_total"] == 0,
          str(st["needs_you_top"]))
    check("T_EMPTY_start_no_last_session", st["last_session"] is None, str(st["last_session"]))
    check("T_EMPTY_start_overview_reused",
          st["overview"]["trust_score"]["value"] is None, str(st["overview"]["trust_score"]))
    en = sl.build_session_end(conn)
    check("T_EMPTY_end_shape",
          set(en) >= {"recorded", "captured_today", "verified_today",
                      "promoted_today", "generated_at"}, str(en.keys()))
    check("T_EMPTY_end_zero_payoff",
          en["captured_today"] == 0 and en["verified_today"] == 0 and en["promoted_today"] == 0,
          str(en))
    conn.close()


# ---------------------------------------------------------------------------
# T_START — seeded: composed payload reflects rows, needs_you_top ranked+capped.
# ---------------------------------------------------------------------------
def test_start_seeded():
    path = _build_temp_db()
    conn = db(path)
    # A last-session diary row.
    conn.execute(
        "INSERT INTO sessions (project, date, accomplished, next_steps, created_at) "
        "VALUES ('infra', ?, 'shipped the loop', 'wire hooks', ?)", (TODAY, ISO))
    # 4 t2 dispositions (so ranking + cap-to-3 is observable) + a proposal.
    for i in range(4):
        conn.execute(
            "INSERT INTO insights_staging (source, project, payload, status, tier, reason, created_at) "
            "VALUES ('gate_failure','infra','{}','pending','t2',?,?)", (f"needs approval {i}", ISO))
    conn.execute(
        "INSERT INTO resolution_proposals (claim_kind, claim_id, proposed_action, reasoning, "
        "stakes, status, created_at) VALUES ('insight',5,'supersede','drifted','high','pending',?)",
        (ISO,))
    conn.commit()

    st = sl.build_session_start(conn, project="infra")
    check("T_START_last_session", st["last_session"] is not None
          and st["last_session"]["accomplished"] == "shipped the loop", str(st["last_session"]))
    check("T_START_needs_capped_3", len(st["needs_you_top"]) == 3, str(len(st["needs_you_top"])))
    check("T_START_needs_total_5", st["needs_you_total"] == 5, str(st["needs_you_total"]))
    # t2 dispositions rank ahead of the proposal — top item must be a disposition.
    check("T_START_ranked_t2_first",
          st["needs_you_top"][0]["kind"] == "t2_disposition", str(st["needs_you_top"][0]))
    shape_ok = all({"id", "kind", "title", "why"} <= set(i) for i in st["needs_you_top"])
    check("T_START_needs_item_shape", shape_ok,
          str(st["needs_you_top"][0]) if st["needs_you_top"] else "empty")
    conn.close()
    os.unlink(path)


def test_start_stale_rules():
    """rules_stale_count counts stale_rule inbox items (drifted compiled rules)."""
    path = _build_temp_db()
    conn = db(path)
    # A live principle with no claims → claim_health 'unverified', not stale.
    conn.execute("INSERT INTO principles (id, content, confidence, created_at) VALUES (1,'r',0.9,?)", (ISO,))
    conn.commit()
    st = sl.build_session_start(conn)
    check("T_START_stale_count_present", "rules_stale_count" in st and isinstance(st["rules_stale_count"], int),
          str(st.get("rules_stale_count")))
    # unverified is NOT stale, so count is 0 here.
    check("T_START_stale_count_zero", st["rules_stale_count"] == 0, str(st["rules_stale_count"]))
    conn.close()
    os.unlink(path)


# ---------------------------------------------------------------------------
# T_END — payoff numbers reuse _today_activity; a marker row is recorded.
# ---------------------------------------------------------------------------
def test_end_seeded():
    path = _build_temp_db()
    conn = db(path)
    # today's captured / promoted (mirror aggregates seeding).
    conn.execute(
        "INSERT INTO insights_staging (source, project, payload, status, tier, created_at) "
        "VALUES ('gate','infra','{}','pending','t0',?)", (ISO,))
    conn.execute("INSERT INTO principles (content, confidence, created_at) VALUES ('p',0.9,?)", (ISO,))
    conn.commit()

    en = sl.build_session_end(conn, project="infra", session_id="uuid-123", summary="did work")
    check("T_END_captured_reused", en["captured_today"] == 1, str(en))
    check("T_END_promoted_reused", en["promoted_today"] == 1, str(en))
    check("T_END_recorded_true", en["recorded"] is True, str(en))
    # The marker row exists in `sessions`.
    row = conn.execute(
        "SELECT accomplished, session_uuid, project FROM sessions WHERE session_uuid='uuid-123'"
    ).fetchone()
    check("T_END_marker_row", row is not None and row["accomplished"] == "did work"
          and row["project"] == "infra", str(dict(row)) if row else "no row")
    conn.close()
    os.unlink(path)


# ---------------------------------------------------------------------------
# T_UPSERT — the anti-fragmentation contract: ONE canonical row per uuid;
# blind markers never persist; enrichment never clobbers.
# ---------------------------------------------------------------------------
def test_end_upsert():
    path = _build_temp_db()
    conn = db(path)

    # 1. Double session_end with the SAME uuid -> exactly ONE row.
    sl.build_session_end(conn, project="infra", session_id="uuid-dup", summary="first")
    sl.build_session_end(conn, project="infra", session_id="uuid-dup", summary="second")
    n = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_uuid='uuid-dup'").fetchone()[0]
    check("T_UPSERT_one_row_per_uuid", n == 1, f"rows={n}")

    # 2. The richer first summary is NOT clobbered by the later one.
    acc = conn.execute(
        "SELECT accomplished FROM sessions WHERE session_uuid='uuid-dup'").fetchone()[0]
    check("T_UPSERT_no_clobber", acc == "first", acc)

    # 3. An EMPTY auto-captured row (the migration-029 shape) gets ENRICHED.
    conn.execute(
        "INSERT INTO sessions (project, date, session_uuid, created_at) "
        "VALUES ('infra','2026-07-18','uuid-empty',?)", (ISO,))
    conn.commit()
    sl.build_session_end(conn, project="infra", session_id="uuid-empty", summary="filled in")
    row = conn.execute(
        "SELECT COUNT(*) n, MAX(accomplished) acc FROM sessions WHERE session_uuid='uuid-empty'"
    ).fetchone()
    check("T_UPSERT_enriches_empty", row["n"] == 1 and row["acc"] == "filled in",
          f"n={row['n']} acc={row['acc']}")

    # 4. No session_id AND no summary -> NOTHING persisted (blind markers are
    #    fragmentation noise), but the call still succeeds with recorded=False.
    before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    en = sl.build_session_end(conn, project="infra", session_id=None, summary=None)
    after = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    check("T_UPSERT_blind_marker_skipped", after == before and en["recorded"] is False,
          f"before={before} after={after} recorded={en['recorded']}")

    conn.close()
    os.unlink(path)


# ---------------------------------------------------------------------------
# T_FAILSOFT — DB missing diary + staging tables: degrade, never raise.
# ---------------------------------------------------------------------------
def test_failsoft():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="sl-failsoft-")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _base_tables(conn)
    conn.commit()
    raised = False
    try:
        st = sl.build_session_start(conn)
        en = sl.build_session_end(conn, project="x")
    except Exception as exc:  # noqa: BLE001
        raised = True
        st = en = str(exc)
    check("T_FAILSOFT_no_raise", not raised, str(st))
    check("T_FAILSOFT_start_no_last", (not raised) and st["last_session"] is None, str(st))
    # `sessions` absent → recorded False, payoff still returned (no crash).
    check("T_FAILSOFT_end_noop", (not raised) and en["recorded"] is False
          and en["captured_today"] == 0, str(en))
    conn.close()
    os.unlink(path)


# ---------------------------------------------------------------------------
# T_HTTP — route-level through the real ASGI stack (TestClient). Catches the
# cross-thread sqlite misuse: the conn MUST be opened inside the executor.
# ---------------------------------------------------------------------------
def test_http_routes():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except ImportError:
        check("T_HTTP_skipped", True, "fastapi not installed — builders-only env")
        return
    test_app = FastAPI()
    test_app.include_router(sl.router)
    with TestClient(test_app, raise_server_exceptions=False) as client:
        r = client.get("/session/start")
        ok = r.status_code == 200 and r.json().get("ok") is True and "overview" in r.json()
        check("T_HTTP_start_200_ok", ok, f"status={r.status_code} body={r.text[:200]}")

        # POST with a body.
        r2 = client.post("/session/end", json={"project": "infra", "session_id": "http-uuid", "summary": "s"})
        ok2 = r2.status_code == 200 and r2.json().get("ok") is True and "captured_today" in r2.json()
        check("T_HTTP_end_200_ok", ok2, f"status={r2.status_code} body={r2.text[:200]}")

        # POST with NO body — must still succeed (SessionEnd hooks may send {}).
        r3 = client.post("/session/end")
        ok3 = r3.status_code == 200 and r3.json().get("ok") is True
        check("T_HTTP_end_no_body_ok", ok3, f"status={r3.status_code} body={r3.text[:200]}")


def main() -> int:
    print("=== session lifecycle suite ===")
    for fn in (test_empty, test_start_seeded, test_start_stale_rules,
               test_end_seeded, test_end_upsert, test_failsoft, test_http_routes):
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
