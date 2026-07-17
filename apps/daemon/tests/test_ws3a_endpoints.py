#!/usr/bin/env python3
"""WS3a daemon endpoint tests — /query/get_batch, /arena_batch, /clear_suspect_batch.

Standalone (no pytest — mirrors test_ws2_loops.py). Runs the daemon FastAPI app
via TestClient against a THROWAWAY temp DB whose schema is dumped read-only from
the live db/engine.db. The live daemon and live DB are never touched (mode=ro only).

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_ws3a_endpoints.py
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_DB = REPO_ROOT / "db" / "engine.db"
DAEMON_PY = REPO_ROOT / "apps" / "daemon" / "engine_daemon.py"

FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASSES.append(name)
        print(f"  [v] {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [X] {name}  -- {detail}")


def build_temp_db() -> str:
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db", prefix="ws3atest-")
    os.close(fd)
    conn = sqlite3.connect(path)
    applied = skipped = 0
    for s in stmts:
        try:
            conn.execute(s)
            applied += 1
        except sqlite3.OperationalError:
            skipped += 1
    conn.commit()
    conn.close()
    print(f"temp DB: {path} ({applied} stmts applied, {skipped} shadow/dup skipped)")
    return path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TEMP_DB = build_temp_db()
daemon = load_module("engine_daemon_ws3atest", DAEMON_PY)
daemon.DB_PATH = Path(TEMP_DB)

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(daemon.app)  # no lifespan => no model/loops


def seed_insight(conn, content: str, project="testproj", suspect_of=None,
                 confidence=0.5, updated_at="2026-01-01T00:00:00+00:00") -> int:
    cur = conn.execute(
        """INSERT INTO insights (project, type, content, tags, source_file, confidence,
                                 status, created_at, updated_at, suspect_of)
           VALUES (?, 'gotcha', ?, '', '', ?, 'active', ?, ?, ?)""",
        (project, content, confidence, updated_at, updated_at, suspect_of),
    )
    return cur.lastrowid


def seed_principle(conn, content: str, project="testproj", source_insights="1") -> int:
    cur = conn.execute(
        "INSERT INTO principles (project, content, confidence, source_insights) "
        "VALUES (?, ?, 0.9, ?)",
        (project, content, source_insights),
    )
    return cur.lastrowid


conn = sqlite3.connect(TEMP_DB)
conn.row_factory = sqlite3.Row

# ---------------------------------------------------------------------------
print("\n== /query/get_batch ==")
i1 = seed_insight(conn, "get_batch insight one")
i2 = seed_insight(conn, "get_batch insight two")
p1 = seed_principle(conn, "get_batch principle one", source_insights=f"{i1}")
conn.commit()

r = client.post("/query/get_batch", json={"kind": "insight", "ids": [i1, i2, 999999]})
d = r.json()
check("get_batch insight: ok envelope", d.get("ok") is True, str(d))
check("get_batch insight: found split", {x["id"] for x in d.get("found", [])} == {i1, i2}, str(d))
check("get_batch insight: not_found split", d.get("not_found") == [999999], str(d))
check("get_batch insight: columns mirror /insight/{id}",
      set(d["found"][0].keys()) == {"id", "project", "type", "content", "tags", "source_file",
                                    "confidence", "verify_count", "verify_streak", "status",
                                    "created_at", "updated_at", "promoted_to"},
      str(sorted(d["found"][0].keys())))

r = client.post("/query/get_batch", json={"kind": "principle", "ids": [p1, 888888]})
d = r.json()
check("get_batch principle: found + source_insights",
      d.get("ok") is True and d["found"][0]["id"] == p1
      and d["found"][0]["source_insights"] == str(i1), str(d))
check("get_batch principle: not_found", d.get("not_found") == [888888], str(d))

r = client.post("/query/get_batch", json={"kind": "bogus", "ids": [1]})
check("get_batch: invalid kind -> 422 + ok:false",
      r.status_code == 422 and r.json().get("ok") is False, f"{r.status_code} {r.text}")

r = client.post("/query/get_batch", json={"kind": "insight", "ids": []})
d = r.json()
check("get_batch: empty ids -> ok empty", d == {"ok": True, "found": [], "not_found": []}, str(d))

# ---------------------------------------------------------------------------
print("\n== /arena_batch ==")
a1 = seed_insight(conn, "arena old claim", updated_at="2026-01-01T00:00:00+00:00")
a2 = seed_insight(conn, "arena new claim", updated_at="2026-06-01T00:00:00+00:00")
b1 = seed_insight(conn, "arena old two", updated_at="2026-02-01T00:00:00+00:00")
b2 = seed_insight(conn, "arena new two", updated_at="2026-06-15T00:00:00+00:00")
conn.commit()

r = client.post("/arena_batch", json={
    "pairs": [[a1, a2], [b1, b2]], "strategy": "recency", "dry_run": True,
    "role": "operator", "session_id": "ws3a-test",
})
d = r.json()
check("arena_batch dry_run: ok + per-pair results",
      d.get("ok") is True and len(d.get("results", [])) == 2, str(d))
check("arena_batch dry_run: recency winners",
      d["results"][0].get("winner") == a2 and d["results"][1].get("winner") == b2, str(d))
check("arena_batch dry_run: flagged dry_run",
      d.get("dry_run") is True and all(x.get("dry_run") for x in d["results"]), str(d))
row = conn.execute("SELECT superseded_by FROM insights WHERE id=?", (a1,)).fetchone()
check("arena_batch dry_run: no DB writes", row["superseded_by"] is None, str(dict(row)))

r = client.post("/arena_batch", json={
    "pairs": [[a1, a2], [b1, b2], [a1]], "strategy": "recency", "dry_run": False,
    "role": "operator", "session_id": "ws3a-test",
})
d = r.json()
check("arena_batch live: 2 results + 1 error (short pair)",
      len(d.get("results", [])) == 2 and len(d.get("errors", [])) == 1, str(d))
check("arena_batch live: total_processed", d.get("total_processed") == 3, str(d))
conn2 = sqlite3.connect(TEMP_DB)
conn2.row_factory = sqlite3.Row
row = conn2.execute("SELECT superseded_by, supersede_reason FROM insights WHERE id=?", (a1,)).fetchone()
check("arena_batch live: loser superseded by winner",
      row["superseded_by"] == a2 and row["supersede_reason"] == "arena:recency", str(dict(row)))
ev = conn2.execute("SELECT COUNT(*) AS n FROM arena_events WHERE session_id='ws3a-test'").fetchone()
check("arena_batch live: one arena_events row per adjudication (provenance kept)",
      ev["n"] == 2, str(dict(ev)))

# ---------------------------------------------------------------------------
print("\n== /clear_suspect_batch ==")
s1 = seed_insight(conn, "suspect one", suspect_of=1)
s2 = seed_insight(conn, "suspect two", suspect_of=1)
s3 = seed_insight(conn, "not suspect", suspect_of=None)
conn.commit()

r = client.post("/clear_suspect_batch", json={
    "pairs": [{"a_id": s1, "b_id": s2}, {"id": s3}, {"id": 777777}, {}],
    "reason": "ws3a-test-fp",
})
d = r.json()
check("clear_suspect_batch: cleared both pair members",
      sorted(d.get("cleared", [])) == sorted([s1, s2]), str(d))
check("clear_suspect_batch: noop for unflagged", d.get("noop") == [s3], str(d))
check("clear_suspect_batch: not_found", d.get("not_found") == [777777], str(d))
check("clear_suspect_batch: errors for empty entry", len(d.get("errors", [])) == 1, str(d))
check("clear_suspect_batch: total_processed", d.get("total_processed") == 4, str(d))
conn3 = sqlite3.connect(TEMP_DB)
conn3.row_factory = sqlite3.Row
row = conn3.execute("SELECT suspect_of FROM insights WHERE id=?", (s1,)).fetchone()
check("clear_suspect_batch: flag actually cleared in DB", row["suspect_of"] is None, str(dict(row)))

# ---------------------------------------------------------------------------
conn.close()
conn2.close()
conn3.close()
print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed")
if FAILURES:
    for f in FAILURES:
        print(f"FAIL: {f}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
