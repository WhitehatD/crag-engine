#!/usr/bin/env python3
# coding: utf-8
"""Session-state recording test suite (migration 029).

Standalone (no pytest — mirrors test_grounding_autoresolve.py style).

Covers the architecture fix:
  T_INGEST     POST /ingest/session_state creates a row keyed by session_uuid,
               then a second call UPSERTs (overwrites auto columns, never
               touches narrative).
  T_ENRICH     POST /lifecycle/session/add with an explicit session_uuid
               enriches the SAME row created by /ingest/session_state
               (one canonical row per session_uuid, not a new fragment).
  T_PARTIAL    A later /lifecycle/session/add call with some fields blank
               does NOT blank out narrative a prior call already wrote
               (CASE-WHEN-non-empty, not COALESCE).
  T_AUTOLINK   /lifecycle/session/add with NO session_uuid but a live,
               recent session_meta row for the project auto-resolves and
               links (root-cause fix: no calling-convention change needed).
  T_STALE      /lifecycle/session/add with NO session_uuid and only a STALE
               session_meta row (older than CRAG_ENGINE_SESSION_LINK_MAX_AGE_MIN)
               falls back to the legacy bare-INSERT (never guesses).
  T_LEGACY     Two calls with different/no session_uuid create separate rows
               (no false merging).
  T_BACKFILL   /admin/backfill_session_uuid links unambiguous 1:1:1 cases and
               leaves ambiguous (multiple sessions rows same day, multiple
               session_meta candidates) and no-candidate cases unlinked with
               honest counts.

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_session_state.py
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_DB = REPO_ROOT / "db" / "engine.db"
DAEMON_PY = REPO_ROOT / "apps" / "daemon" / "engine_daemon.py"
MIGRATIONS = REPO_ROOT / "db" / "migrations"
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
        print(f"  [X] {name}  -- {detail}")


# ---------------------------------------------------------------------------
# Temp DB: schema-copy of live DB (read-only) + migration 029
# ---------------------------------------------------------------------------

def _apply_migration(conn: sqlite3.Connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    for chunk in sql.split(";"):
        lines = [l for l in chunk.splitlines()
                 if l.strip() and not l.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            m = str(e).lower()
            if "duplicate column name" in m or "already exists" in m:
                continue
            raise


def build_temp_db() -> str:
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db", prefix="session-state-")
    os.close(fd)
    conn = sqlite3.connect(path)
    for s in stmts:
        try:
            conn.execute(s)
        except sqlite3.OperationalError:
            pass  # FTS5 shadow tables
    conn.commit()
    # session_meta (012) is already baked into the live-schema copy above
    # (live DB is well past schema v12) -- only layer the migration under test.
    for mig in ("029_session_state.sql",):
        p = MIGRATIONS / mig
        if p.exists():
            _apply_migration(conn, p)
    conn.commit()
    conn.close()
    print(f"temp DB (+029 session_state): {path}")
    return path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TEMP_DB = build_temp_db()
daemon = load_module("engine_daemon_session_state_test", DAEMON_PY)
daemon.DB_PATH = Path(TEMP_DB)

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(daemon.app)  # no lifespan => no model/loops


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(TEMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


def seed_session_meta(conn, session_uuid, project="infra", last_seen_at=None):
    now = last_seen_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO session_meta (session_uuid, project, cwd, started_at, last_seen_at, source)
           VALUES (?, ?, '/tmp', ?, ?, 'hook')""",
        (session_uuid, project, now, now),
    )
    conn.commit()


def iso_ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


# ---------------------------------------------------------------------------
# T_INGEST — /ingest/session_state UPSERT, never touches narrative
# ---------------------------------------------------------------------------
print("\n== T_INGEST ==")

r1 = client.post("/ingest/session_state", json={
    "session_id": "uuid-ingest-1", "project": "infra",
    "git_branch": "main", "commits_count": 1, "files_changed_count": 2,
    "wall_time_sec": 100,
})
check("T_INGEST_a: 200", r1.status_code == 200, f"status={r1.status_code} body={r1.text[:200]}")
check("T_INGEST_b: upserted=True", r1.json().get("upserted") is True, str(r1.json()))

conn = db()
row = conn.execute("SELECT * FROM sessions WHERE session_uuid='uuid-ingest-1'").fetchone()
conn.close()
check("T_INGEST_c: row created with auto columns", row is not None and row["commits_count"] == 1,
      f"row={dict(row) if row else None}")
check("T_INGEST_d: narrative columns empty/NULL on auto-capture-only row",
      not (row["accomplished"] or ""), f"accomplished={row['accomplished']!r}")

# Manually write narrative directly (simulating a prior diary write), then
# re-ingest auto-capture with DIFFERENT numbers — narrative must survive.
conn = db()
conn.execute("UPDATE sessions SET accomplished='did the thing' WHERE session_uuid='uuid-ingest-1'")
conn.commit()
conn.close()

r2 = client.post("/ingest/session_state", json={
    "session_id": "uuid-ingest-1", "project": "infra",
    "git_branch": "feature-x", "commits_count": 5, "files_changed_count": 9,
    "wall_time_sec": 500,
})
check("T_INGEST_e: second ingest 200", r2.status_code == 200, f"status={r2.status_code}")
conn = db()
row2 = conn.execute("SELECT * FROM sessions WHERE session_uuid='uuid-ingest-1'").fetchone()
conn.close()
check("T_INGEST_f: auto columns overwritten (commits_count=5)", row2["commits_count"] == 5,
      f"commits_count={row2['commits_count']}")
check("T_INGEST_g: narrative untouched by second auto-capture",
      row2["accomplished"] == "did the thing", f"accomplished={row2['accomplished']!r}")
check("T_INGEST_h: still exactly one row for this session_uuid",
      conn is not None, "n/a")
conn = db()
cnt = conn.execute("SELECT COUNT(*) c FROM sessions WHERE session_uuid='uuid-ingest-1'").fetchone()["c"]
conn.close()
check("T_INGEST_i: exactly one row (no fragmentation)", cnt == 1, f"count={cnt}")


# ---------------------------------------------------------------------------
# T_ENRICH — /lifecycle/session/add with explicit session_uuid enriches the
# SAME row /ingest/session_state created.
# ---------------------------------------------------------------------------
print("\n== T_ENRICH ==")

r3 = client.post("/lifecycle/session/add", json={
    "project": "infra", "session_uuid": "uuid-ingest-1",
    "accomplished": "overwritten narrative", "decisions": "picked approach A",
})
check("T_ENRICH_a: 200", r3.status_code == 200, f"status={r3.status_code} body={r3.text[:200]}")

conn = db()
cnt = conn.execute("SELECT COUNT(*) c FROM sessions WHERE session_uuid='uuid-ingest-1'").fetchone()["c"]
row3 = conn.execute("SELECT * FROM sessions WHERE session_uuid='uuid-ingest-1'").fetchone()
conn.close()
check("T_ENRICH_b: still exactly one row (enriched, not fragmented)", cnt == 1, f"count={cnt}")
check("T_ENRICH_c: narrative updated", row3["accomplished"] == "overwritten narrative",
      f"accomplished={row3['accomplished']!r}")
check("T_ENRICH_d: auto-captured columns from /ingest untouched by diary write",
      row3["commits_count"] == 5, f"commits_count={row3['commits_count']}")
check("T_ENRICH_e: decisions written", row3["decisions"] == "picked approach A",
      f"decisions={row3['decisions']!r}")


# ---------------------------------------------------------------------------
# T_PARTIAL — a later add with some fields blank must not blank prior values
# ---------------------------------------------------------------------------
print("\n== T_PARTIAL ==")

r4 = client.post("/lifecycle/session/add", json={
    "project": "infra", "session_uuid": "uuid-ingest-1",
    "next_steps": "ship it",
    # accomplished/decisions intentionally omitted (default "" per SessionAddBody)
})
check("T_PARTIAL_a: 200", r4.status_code == 200, f"status={r4.status_code}")
conn = db()
row4 = conn.execute("SELECT * FROM sessions WHERE session_uuid='uuid-ingest-1'").fetchone()
conn.close()
check("T_PARTIAL_b: prior accomplished preserved", row4["accomplished"] == "overwritten narrative",
      f"accomplished={row4['accomplished']!r}")
check("T_PARTIAL_c: prior decisions preserved", row4["decisions"] == "picked approach A",
      f"decisions={row4['decisions']!r}")
check("T_PARTIAL_d: new next_steps applied", row4["next_steps"] == "ship it",
      f"next_steps={row4['next_steps']!r}")


# ---------------------------------------------------------------------------
# T_AUTOLINK — no session_uuid passed, but a recent session_meta row exists
# for the project -> server-side auto-resolve links it.
# ---------------------------------------------------------------------------
print("\n== T_AUTOLINK ==")

conn = db()
seed_session_meta(conn, "uuid-autolink-1", project="project-a", last_seen_at=iso_ago(5))
conn.close()

r5 = client.post("/lifecycle/session/add", json={
    "project": "project-a", "accomplished": "auto-linked via session_meta",
})
check("T_AUTOLINK_a: 200", r5.status_code == 200, f"status={r5.status_code} body={r5.text[:200]}")
conn = db()
row5 = conn.execute("SELECT * FROM sessions WHERE session_uuid='uuid-autolink-1'").fetchone()
conn.close()
check("T_AUTOLINK_b: row linked to session_meta uuid without an explicit arg",
      row5 is not None and row5["accomplished"] == "auto-linked via session_meta",
      f"row={dict(row5) if row5 else None}")


# ---------------------------------------------------------------------------
# T_STALE — no session_uuid, only a STALE session_meta row -> legacy fallback
# (never guesses a link to a long-idle session).
# ---------------------------------------------------------------------------
print("\n== T_STALE ==")

conn = db()
seed_session_meta(conn, "uuid-stale-1", project="project-c", last_seen_at=iso_ago(600))  # 10h old
conn.close()

r6 = client.post("/lifecycle/session/add", json={
    "project": "project-c", "accomplished": "should not link to stale session",
})
check("T_STALE_a: 200", r6.status_code == 200, f"status={r6.status_code}")
conn = db()
linked = conn.execute("SELECT * FROM sessions WHERE session_uuid='uuid-stale-1'").fetchone()
legacy_rows = conn.execute(
    "SELECT * FROM sessions WHERE project='project-c' AND session_uuid IS NULL"
).fetchall()
conn.close()
check("T_STALE_b: stale session_meta row NOT linked", linked is None, f"linked={dict(linked) if linked else None}")
check("T_STALE_c: legacy bare-INSERT fallback used instead",
      any(r["accomplished"] == "should not link to stale session" for r in legacy_rows),
      f"legacy_rows={[dict(r) for r in legacy_rows]}")


# ---------------------------------------------------------------------------
# T_LEGACY — different/absent session_uuid never merges unrelated rows
# ---------------------------------------------------------------------------
print("\n== T_LEGACY ==")

client.post("/lifecycle/session/add", json={"project": "crag", "session_uuid": "uuid-crag-1", "accomplished": "a"})
client.post("/lifecycle/session/add", json={"project": "crag", "session_uuid": "uuid-crag-2", "accomplished": "b"})
conn = db()
crag_rows = conn.execute("SELECT session_uuid, accomplished FROM sessions WHERE project='crag'").fetchall()
conn.close()
check("T_LEGACY_a: two distinct session_uuids -> two distinct rows",
      len(crag_rows) == 2, f"rows={[dict(r) for r in crag_rows]}")


# ---------------------------------------------------------------------------
# T_BACKFILL — /admin/backfill_session_uuid: unambiguous links + honest
# ambiguous/no-candidate buckets
# ---------------------------------------------------------------------------
print("\n== T_BACKFILL ==")

conn = db()
# Case 1: unambiguous 1 sessions-row : 1 session_meta-row -> LINK
conn.execute("INSERT INTO sessions (project, date, accomplished) VALUES ('project-d', '2026-06-10', 'solo day')")
seed_session_meta(conn, "uuid-bf-solo", project="project-d", last_seen_at="2026-06-10T12:00:00+00:00")

# Case 2: ambiguous — TWO sessions rows same (project, date) -> leave unlinked
conn.execute("INSERT INTO sessions (project, date, accomplished) VALUES ('project-e', '2026-06-11', 'first entry')")
conn.execute("INSERT INTO sessions (project, date, accomplished) VALUES ('project-e', '2026-06-11', 'second entry')")
seed_session_meta(conn, "uuid-bf-e", project="project-e", last_seen_at="2026-06-11T12:00:00+00:00")

# Case 3: ambiguous — TWO session_meta candidates for one sessions row -> leave unlinked
conn.execute("INSERT INTO sessions (project, date, accomplished) VALUES ('frontend', '2026-06-12', 'only entry')")
seed_session_meta(conn, "uuid-bf-fe-1", project="frontend", last_seen_at="2026-06-12T09:00:00+00:00")
seed_session_meta(conn, "uuid-bf-fe-2", project="frontend", last_seen_at="2026-06-12T18:00:00+00:00")

# Case 4: no candidate -> leave unlinked, counted
conn.execute("INSERT INTO sessions (project, date, accomplished) VALUES ('project-b', '2026-06-13', 'orphan')")

conn.commit()
conn.close()

rb = client.post("/admin/backfill_session_uuid")
check("T_BACKFILL_a: 200", rb.status_code == 200, f"status={rb.status_code} body={rb.text[:300]}")
bf = rb.json()
check("T_BACKFILL_b: linked >= 1 (the solo case)", bf.get("linked", 0) >= 1, str(bf))
check("T_BACKFILL_c: ambiguous_multiple_sessions_rows counted >= 2 (both project-e rows)",
      bf.get("ambiguous_multiple_sessions_rows", 0) >= 2, str(bf))
check("T_BACKFILL_d: ambiguous_multiple_meta_rows counted >= 1 (frontend case)",
      bf.get("ambiguous_multiple_meta_rows", 0) >= 1, str(bf))
check("T_BACKFILL_e: no_candidate counted >= 1 (project-b orphan)",
      bf.get("no_candidate", 0) >= 1, str(bf))

conn = db()
solo = conn.execute("SELECT session_uuid FROM sessions WHERE project='project-d' AND date='2026-06-10'").fetchone()
proj_e_rows = conn.execute("SELECT session_uuid FROM sessions WHERE project='project-e'").fetchall()
frontend_row = conn.execute("SELECT session_uuid FROM sessions WHERE project='frontend'").fetchone()
project_b_row = conn.execute("SELECT session_uuid FROM sessions WHERE project='project-b'").fetchone()
conn.close()
check("T_BACKFILL_f: solo case actually linked", solo["session_uuid"] == "uuid-bf-solo",
      f"session_uuid={solo['session_uuid']!r}")
check("T_BACKFILL_g: ambiguous project-e rows left NULL (not guessed)",
      all(r["session_uuid"] is None for r in proj_e_rows), f"rows={[dict(r) for r in proj_e_rows]}")
check("T_BACKFILL_h: ambiguous frontend row left NULL (not guessed)",
      frontend_row["session_uuid"] is None, f"session_uuid={frontend_row['session_uuid']!r}")
check("T_BACKFILL_i: no-candidate project-b row left NULL",
      project_b_row["session_uuid"] is None, f"session_uuid={project_b_row['session_uuid']!r}")

# Re-running the backfill must be idempotent (already-linked rows untouched,
# no double-linking, no crash on the now-narrower unlinked set).
rb2 = client.post("/admin/backfill_session_uuid")
check("T_BACKFILL_j: re-run 200 (idempotent)", rb2.status_code == 200, f"status={rb2.status_code}")
check("T_BACKFILL_k: re-run links 0 new (solo already linked)", rb2.json().get("linked", -1) == 0,
      str(rb2.json()))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n{'='*60}")
print(f"Results: {len(PASSES)} passed, {len(FAILURES)} failed")
if FAILURES:
    print("\nFAILURES:")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
else:
    print("All tests passed.")
    sys.exit(0)
