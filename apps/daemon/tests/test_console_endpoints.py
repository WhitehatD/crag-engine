#!/usr/bin/env python3
"""Console claim-layer endpoint tests — /claims, /claims/{id},
/claims/contradictions, and the /console static-mount fail-soft.

Standalone (no pytest — mirrors test_ws3a_endpoints.py / test_disposition_engine.py).
Builds a THROWAWAY temp DB from schema.sql + every migration (no dependency on
any live DB / live daemon), seeds a small claim graph, swaps daemon.DB_PATH, and
drives the FastAPI app via TestClient (no lifespan => no model/loops).

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_console_endpoints.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_DIR = REPO_ROOT / "db"
SCHEMA = DB_DIR / "schema.sql"
MIGRATIONS_DIR = DB_DIR / "migrations"
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


_TOLERATE = ("duplicate column name", "already exists")


def _apply_migration(conn: sqlite3.Connection, path: Path) -> None:
    """Apply an additive migration file, tolerating the re-run errors the
    daemon's own migrator tolerates (duplicate column / already exists).

    Split on ';' but re-join any chunk that opened a trigger BEGIN..END block so
    trigger bodies (migrations 003/005) execute as one statement. All tables
    they touch already exist from schema.sql, so those are effectively no-ops."""
    raw = path.read_text(encoding="utf-8")
    # Strip inline "-- ..." comments to end-of-line (migrations never carry "--"
    # inside a string literal) so a ';' hidden in a trailing column comment does
    # not truncate a CREATE statement mid-body.
    no_comments = "\n".join(
        re.sub(r"--.*$", "", ln) for ln in raw.splitlines()
    )
    chunks = no_comments.split(";")
    stmts: list[str] = []
    buf = ""
    depth = 0
    for chunk in chunks:
        buf = buf + chunk if buf else chunk
        # Track BEGIN/END nesting so a trigger body isn't cut at its inner ';'.
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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="console-test-")
    os.close(fd)
    conn = sqlite3.connect(path)
    # schema.sql carries trigger BEGIN..END blocks — executescript parses those
    # correctly (a naive ';' split would truncate them).
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
        _apply_migration(conn, mig)
        conn.commit()
    conn.close()
    print(f"temp DB: {path}")
    return path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TEMP_DB = build_temp_db()
daemon = load_module("engine_daemon_consoletest", DAEMON_PY)
daemon.DB_PATH = Path(TEMP_DB)

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(daemon.app)  # no lifespan => no model/loops

NOW = "2026-07-17T12:00:00.000000+00:00"


def seed_insight(conn, content, project="consoleproj") -> int:
    cur = conn.execute(
        "INSERT INTO insights (project, type, content, status, created_at, updated_at) "
        "VALUES (?, 'gotcha', ?, 'active', ?, ?)",
        (project, content, NOW, NOW),
    )
    return cur.lastrowid


def seed_principle(conn, content, project="consoleproj") -> int:
    cur = conn.execute(
        "INSERT INTO principles (project, content, confidence, created_at, updated_at) "
        "VALUES (?, ?, 0.9, ?, ?)",
        (project, content, NOW, NOW),
    )
    return cur.lastrowid


def seed_claim(conn, text, predicate_class="P1", primary_entity=None,
               primary_entity_type=None, last_verdict=None, grounded_at=None,
               grounding_due=0, spec=None) -> int:
    import hashlib
    key = hashlib.sha1(text.lower().encode()).hexdigest()
    cur = conn.execute(
        """INSERT INTO claims
               (canonical_key, text, predicate_class, predicate_spec, status,
                primary_entity, primary_entity_type, last_verdict, grounded_at,
                grounding_due, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
        (key, text, predicate_class, spec, primary_entity, primary_entity_type,
         last_verdict, grounded_at, grounding_due, NOW, NOW),
    )
    return cur.lastrowid


conn = sqlite3.connect(TEMP_DB)
conn.row_factory = sqlite3.Row

# ---------------------------------------------------------------------------
print("\n== seed claim graph ==")
ins_a = seed_insight(conn, "the daemon binds a loopback port", project="alpha")
ins_b = seed_insight(conn, "background roles route direct", project="beta")
prin = seed_principle(conn, "isolation is routing", project="alpha")

# Fresh P1 claim, entity 'daemon', with a parent insight + principle.
c_fresh = seed_claim(conn, "the daemon listens on a loopback socket",
                     predicate_class="P1", primary_entity="daemon",
                     primary_entity_type="service", last_verdict="pass",
                     grounded_at=NOW, spec='{"cmd": "ss -tlnp", "expect": "daemon"}')
# Stale P1 claim, same entity 'daemon' (contradiction candidate).
c_stale = seed_claim(conn, "the daemon does not bind any socket",
                     predicate_class="P1", primary_entity="daemon",
                     primary_entity_type="service", last_verdict="fail")
# Axiomatic P5 claim, entity 'router'.
c_axiom = seed_claim(conn, "we prefer direct routing for background roles",
                     predicate_class="P5", primary_entity="router",
                     primary_entity_type="service")
# Unverified P4 claim, no entity.
c_unver = seed_claim(conn, "the evidence bundle covers the recall path",
                     predicate_class="P4")

conn.execute("INSERT INTO insight_claims (insight_id, claim_id, role, weight, created_at) "
             "VALUES (?, ?, 'core', 1.0, ?)", (ins_a, c_fresh, NOW))
conn.execute("INSERT INTO insight_claims (insight_id, claim_id, role, weight, created_at) "
             "VALUES (?, ?, 'core', 1.0, ?)", (ins_b, c_stale, NOW))
conn.execute("INSERT INTO principle_claims (principle_id, claim_id, role, weight, created_at) "
             "VALUES (?, ?, 'core', 1.0, ?)", (prin, c_fresh, NOW))
conn.execute("INSERT INTO claim_entities (claim_id, entity, entity_type) VALUES (?, 'daemon', 'service')",
             (c_fresh,))
conn.execute("INSERT INTO claim_entities (claim_id, entity, entity_type) VALUES (?, 'loopback', 'service')",
             (c_fresh,))
conn.execute("INSERT INTO grounding_history (claim_kind, claim_id, ts, job_type, verdict, reasoning, evidence, lane, recipe_version) "
             "VALUES ('claim', ?, ?, 'p1_probe', 'pass', 'socket present', 'LISTEN 127.0.0.1', 'p1', 1)",
             (c_fresh, NOW))
# Open contradiction pair on entity 'daemon'.
conn.execute("INSERT INTO claim_contradictions (claim_a_id, claim_b_id, reason, score, status, detected_at) "
             "VALUES (?, ?, 'polarity-flip (neg)', 1.0, 'open', ?)",
             (min(c_fresh, c_stale), max(c_fresh, c_stale), NOW))
conn.commit()
print(f"  claims: fresh={c_fresh} stale={c_stale} axiom={c_axiom} unver={c_unver}")

# ---------------------------------------------------------------------------
print("\n== GET /claims (paged + filters) ==")
r = client.get("/claims")
d = r.json()
check("/claims: ok envelope", d.get("ok") is True and "total" in d, str(d)[:200])
check("/claims: total == 4", d.get("total") == 4, str(d.get("total")))
check("/claims: rows carry derived verdict",
      all("verdict" in c for c in d["claims"]), str(d["claims"][:1]))
vmap = {c["id"]: c["verdict"] for c in d["claims"]}
check("/claims: fresh verdict derived", vmap.get(c_fresh) == "fresh", str(vmap))
check("/claims: stale verdict derived", vmap.get(c_stale) == "stale", str(vmap))
check("/claims: axiomatic verdict derived", vmap.get(c_axiom) == "axiomatic", str(vmap))
check("/claims: unverified verdict derived", vmap.get(c_unver) == "unverified", str(vmap))
fresh_row = next(c for c in d["claims"] if c["id"] == c_fresh)
check("/claims: parent counts present",
      fresh_row["insight_parents"] == 1 and fresh_row["principle_parents"] == 1,
      str(fresh_row))

r = client.get("/claims", params={"predicate_class": "P5"})
d = r.json()
check("/claims?predicate_class=P5 filter", d["total"] == 1 and d["claims"][0]["id"] == c_axiom, str(d))

r = client.get("/claims", params={"class": "P4"})
d = r.json()
check("/claims?class= alias works", d["total"] == 1 and d["claims"][0]["id"] == c_unver, str(d))

r = client.get("/claims", params={"entity": "daemon"})
d = r.json()
check("/claims?entity= (primary or linked)",
      {c["id"] for c in d["claims"]} == {c_fresh, c_stale}, str(d))

r = client.get("/claims", params={"entity": "loopback"})
d = r.json()
check("/claims?entity= matches claim_entities link",
      {c["id"] for c in d["claims"]} == {c_fresh}, str(d))

r = client.get("/claims", params={"project": "alpha"})
d = r.json()
check("/claims?project= via parent insight",
      {c["id"] for c in d["claims"]} == {c_fresh}, str(d))

r = client.get("/claims", params={"q": "does not bind"})
d = r.json()
check("/claims?q= substring", {c["id"] for c in d["claims"]} == {c_stale}, str(d))

r = client.get("/claims", params={"verdict": "fresh"})
d = r.json()
check("/claims?verdict= post-derivation filter",
      {c["id"] for c in d["claims"]} == {c_fresh}, str(d))

r = client.get("/claims", params={"limit": 2, "offset": 0})
d = r.json()
check("/claims pagination: limit honored", len(d["claims"]) == 2 and d["total"] == 4, str(d))

# ---------------------------------------------------------------------------
print("\n== GET /claims/{id} (detail) ==")
r = client.get(f"/claims/{c_fresh}")
d = r.json()
check("/claims/{id}: ok", d.get("ok") is True, str(d)[:200])
check("/claims/{id}: predicate_spec parsed to dict",
      isinstance(d.get("predicate_spec"), dict) and d["predicate_spec"].get("cmd") == "ss -tlnp",
      str(d.get("predicate_spec")))
check("/claims/{id}: entities listed",
      {e["entity"] for e in d["entities"]} == {"daemon", "loopback"}, str(d["entities"]))
check("/claims/{id}: parent insight preview",
      len(d["parents"]["insights"]) == 1 and d["parents"]["insights"][0]["id"] == ins_a
      and "loopback" in d["parents"]["insights"][0]["preview"], str(d["parents"]))
check("/claims/{id}: parent principle preview",
      len(d["parents"]["principles"]) == 1 and d["parents"]["principles"][0]["id"] == prin,
      str(d["parents"]))
check("/claims/{id}: grounding_history last-10",
      len(d["grounding_history"]) == 1 and d["grounding_history"][0]["verdict"] == "pass",
      str(d["grounding_history"]))
check("/claims/{id}: derived verdict on claim", d["claim"]["verdict"] == "fresh", str(d["claim"].get("verdict")))

r = client.get("/claims/99999999")
check("/claims/{id}: 404-shape for missing claim",
      r.json().get("ok") is False, r.text[:120])

# ---------------------------------------------------------------------------
print("\n== GET /claims/contradictions (pairs) ==")
r = client.get("/claims/contradictions")
d = r.json()
check("/claims/contradictions: ok", d.get("ok") is True, str(d)[:200])
check("/claims/contradictions: total == 1", d.get("total") == 1, str(d))
pair = d["pairs"][0]
check("/claims/contradictions: both claims embedded",
      pair["claim_a"] and pair["claim_b"]
      and {pair["claim_a"]["id"], pair["claim_b"]["id"]} == {c_fresh, c_stale},
      str(pair))
check("/claims/contradictions: shared entity computed",
      pair["shared_entity"] == "daemon", str(pair))
check("/claims/contradictions: embedded claims carry verdict",
      pair["claim_a"].get("verdict") in ("fresh", "stale")
      and pair["claim_b"].get("verdict") in ("fresh", "stale"), str(pair))

r = client.get("/claims/contradictions", params={"status": "resolved"})
d = r.json()
check("/claims/contradictions?status=resolved -> empty", d["total"] == 0, str(d))

# ---------------------------------------------------------------------------
print("\n== /console static-mount fail-soft ==")
# In this test the console has NOT been built (no apps/console/dist), so the
# mount registers the JSON build-hint route rather than a StaticFiles handler.
dist = REPO_ROOT / "apps" / "console" / "dist" / "index.html"
if dist.is_file():
    r = client.get("/console/")
    check("/console served (dist present)", r.status_code == 200, str(r.status_code))
else:
    r = client.get("/console")
    d = r.json()
    check("/console fail-soft: 503 + build hint",
          r.status_code == 503 and d.get("error") == "console not built"
          and "npm run build" in d.get("hint", ""), f"{r.status_code} {r.text[:160]}")

# ---------------------------------------------------------------------------
conn.close()
print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed")
if FAILURES:
    for f in FAILURES:
        print(f"FAIL: {f}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
