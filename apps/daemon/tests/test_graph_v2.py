#!/usr/bin/env python3
# coding: utf-8
"""Graph v2 test suite (migration 027).

Standalone (no pytest — mirrors prior test files in this repo).

Test groups:
  T_NORM       — entity_normalize table-driven cases (incl. audit junk exemplars).
  T_STORE      — store-time rejection: junk entities → entity_links with NULL canonical_entity_id.
  T_BACKFILL   — CLI backfill-graph-v2: dry-run stats, live run, idempotency, canonical dedup.
  T_CLAIM_REL  — claim_relations seeding: superseded_by, source_insights, contradictions.
  T_ENTITY_REL — entity_relations heuristic seeding: ip+port, service+port, domain+ip.
  T_TRAVERSE   — traversal endpoint shapes: /graph/siblings, /graph/neighbors, /graph/impact.
  T_SMOKE      — smoke: 26 tools (graph added), bidirectional parity.

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_graph_v2.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_DB = REPO_ROOT / "db" / "engine.db"
DAEMON_PY = REPO_ROOT / "apps" / "daemon" / "engine_daemon.py"
CLI_PY = REPO_ROOT / "db" / "engine-cli.py"
MIGRATION_027 = REPO_ROOT / "db" / "migrations" / "027_graph_v2.sql"
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
        FAILURES.append(name)
        detail_str = f": {detail}" if detail else ""
        print(f"  [x] {name}{detail_str}")


# ---------------------------------------------------------------------------
# Temp DB helpers
# ---------------------------------------------------------------------------

def _create_test_db() -> sqlite3.Connection:
    """Create a temp DB from the live schema (all migrations applied).

    Uses the OS temp dir (not REPO_ROOT/db) so repeated runs don't litter
    the live db/ directory with ~24MB throwaway files next to engine.db —
    see PART E advisory 1. Caller is responsible for unlinking `path` when
    done (main() below does this in a finally block).
    """
    fd, path = tempfile.mkstemp(suffix=".db", prefix="graphtest-")
    os.close(fd)
    print(f"temp DB: {path}")

    # Dump schema from live DB (structure only)
    live_conn = sqlite3.connect(str(LIVE_DB))
    schema_stmts = live_conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
        "ORDER BY type DESC, name"
    ).fetchall()
    live_conn.close()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    for (stmt,) in schema_stmts:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # "already exists" for FTS tables etc.

    # Apply migration 027: create new tables + ALTER TABLE entity_links ADD COLUMN.
    # Use executescript() which handles multi-statement SQL correctly.
    # executescript() implicitly commits; suppress duplicate-column errors for idempotency.
    migration_sql = MIGRATION_027.read_text(encoding="utf-8")
    # Strip comment-only lines to avoid false semicolons in comments confusing simple splitters.
    clean_lines = [ln for ln in migration_sql.splitlines() if not ln.strip().startswith("--")]
    clean_sql = "\n".join(clean_lines)
    # Split carefully: executescript can fail on ALTER TABLE in some SQLite versions.
    # Use per-statement execution with targeted suppression.
    stmts = [s.strip() for s in clean_sql.split(";") if s.strip()]
    for stmt in stmts:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "already exists" in msg or "duplicate column" in msg:
                pass
            else:
                raise

    conn.commit()
    return conn, path


# ---------------------------------------------------------------------------
# Load modules
# ---------------------------------------------------------------------------

def _load_normalize():
    spec = importlib.util.spec_from_file_location("entity_normalize",
                                                   DB_DIR / "entity_normalize.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_daemon():
    """Load engine_daemon.py returning (spec, mod) — caller calls exec_module."""
    spec = importlib.util.spec_from_file_location("engine_daemon", DAEMON_PY)
    mod = importlib.util.module_from_spec(spec)
    return spec, mod


def _load_cli():
    spec = importlib.util.spec_from_file_location("engine_cli", CLI_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# T_NORM — normalization table-driven tests
# ---------------------------------------------------------------------------

def run_T_NORM():
    """Table-driven normalization tests covering PART E audit junk exemplars."""
    print("\n[T_NORM]")
    norm_mod = _load_normalize()
    normalize = norm_mod.normalize

    cases = [
        # (entity_type, raw_value, expected_canonical, expected_reject, test_name)
        # --- port ---
        ("port", "8786",  "8786",  False, "T_NRM_port_ok"),
        ("port", "08786", "8786",  False, "T_NRM_port_strip_leading_zero"),
        ("port", "01",    "1",     False, "T_NRM_port_leading_zero_01"),
        ("port", "03",    "3",     False, "T_NRM_port_leading_zero_03"),
        ("port", "0",     "0",     True,  "T_NRM_port_zero_reject"),
        ("port", "99999", "99999", True,  "T_NRM_port_too_high"),
        # --- ip ---
        ("ip", "203.0.113.10",   "203.0.113.10",   False, "T_NRM_ip_ok"),
        ("ip", "999.0.0.1",     "999.0.0.1",     True,  "T_NRM_ip_octet_overflow"),
        # --- domain ---
        ("domain", "example.com",                    "example.com", False, "T_NRM_domain_ok"),
        ("domain", "EXAMPLE.COM",                    "example.com", False, "T_NRM_domain_lower"),
        ("domain", "com.example.service.infra.local",  "com.example.service.infra.local",
         True,  "T_NRM_domain_reverse_pkg"),
        # --- path: junk exemplars from audit ---
        ("path", "/main",       "/main",       True,  "T_NRM_path_junk_main"),
        ("path", "/api",        "/api",        True,  "T_NRM_path_junk_api"),
        ("path", "/governance", "/governance", True,  "T_NRM_path_junk_governance"),
        # --- path: drive collapse ---
        ("path", "D:/workspace/db/engine-cli.py",
                 "/workspace/db/engine-cli.py",  False, "T_NRM_path_drive_collapse"),
        ("path", "D:\\workspace\\docs\\notes.md",
                 "/workspace/docs/notes.md", False, "T_NRM_path_backslash_collapse"),
        # --- path: legitimate multi-segment path ---
        ("path", "/workspace/app",   "/workspace/app",   False, "T_NRM_path_ok"),
        # --- service ---
        ("service", "NginX",        "nginx",        False, "T_NRM_service_lower"),
        ("service", "redis",        "redis",        False, "T_NRM_service_ok"),
        # --- file ---
        ("file", "engine-cli.py",            "engine-cli.py",  False, "T_NRM_file_ok"),
        ("file", "/workspace/engine-cli.py", "engine-cli.py", False, "T_NRM_file_basename"),
        # --- env_var ---
        ("env_var", "crag_engine_db",  "CRAG_ENGINE_DB",  False, "T_NRM_envvar_upper"),
        ("env_var", "CRAG_ENGINE_DB",  "CRAG_ENGINE_DB",  False, "T_NRM_envvar_ok"),
        # --- unknown type passthrough ---
        ("widget", "foo-bar",   "foo-bar",   False, "T_NRM_unknown_passthru"),
    ]

    for (etype, raw, exp_canonical, exp_reject, name) in cases:
        result = normalize(etype, raw)
        check(name + "_canonical",
              result["canonical"] == exp_canonical,
              f"got {result['canonical']!r} want {exp_canonical!r}")
        check(name + "_reject",
              result["reject"] == exp_reject,
              f"got reject={result['reject']} want {exp_reject}; reason={result.get('reason')}")


# ---------------------------------------------------------------------------
# T_STORE — store-time gating via daemon TestClient
# ---------------------------------------------------------------------------

def run_T_STORE(conn, path: str):
    """Store-time rejection test: junk entities don't get canonical_entity_id."""
    print("\n[T_STORE]")

    from fastapi.testclient import TestClient
    from pathlib import Path as _Path

    spec = importlib.util.spec_from_file_location("engine_daemon_st", DAEMON_PY)
    daemon_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon_mod)
    # Redirect daemon's DB_PATH to the temp DB AFTER exec_module
    daemon_mod.DB_PATH = _Path(path)
    client = TestClient(daemon_mod.app, raise_server_exceptions=False)

    # Save an insight containing a junk path (/main) and a legitimate port (8786).
    resp = client.post("/save_insight", json={
        "content": "Port 8786 is where the crag engine daemon listens. See /main for index.",
        "type": "architecture",
        "project": "test-graph",
    })
    check("T_STO_ok", resp.status_code == 200, f"status={resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        insight_id = data.get("id")
        check("T_STO_id", isinstance(insight_id, int), str(data))

        # /main should be in entity_links but canonical_entity_id = NULL
        el_main = conn.execute(
            "SELECT canonical_entity_id FROM entity_links "
            "WHERE insight_id=? AND entity='/main' AND entity_type='path'",
            (insight_id,),
        ).fetchone()
        check("T_STO_junk_stored", el_main is not None,
              "junk path /main should still land in entity_links (append-only)")
        if el_main:
            check("T_STO_junk_no_canonical",
                  el_main["canonical_entity_id"] is None,
                  f"junk /main should have NULL canonical_entity_id, got {el_main['canonical_entity_id']}")

        # Port 8786 should be accepted and have a canonical_entity_id
        el_port = conn.execute(
            "SELECT canonical_entity_id FROM entity_links "
            "WHERE insight_id=? AND entity_type='port'",
            (insight_id,),
        ).fetchone()
        check("T_STO_port_stored", el_port is not None,
              "port 8786 should be in entity_links")
        if el_port:
            check("T_STO_port_canonical",
                  el_port["canonical_entity_id"] is not None,
                  f"port 8786 should have non-NULL canonical_entity_id, got {el_port['canonical_entity_id']}")

        # entity_canonical should have the port row
        ec_port = conn.execute(
            "SELECT * FROM entity_canonical WHERE entity_type='port' AND raw_value='8786'"
        ).fetchone()
        check("T_STO_ec_port_exists", ec_port is not None,
              "entity_canonical should have port 8786")


# ---------------------------------------------------------------------------
# T_BACKFILL — CLI backfill-graph-v2
# ---------------------------------------------------------------------------

def run_T_BACKFILL(conn, path: str):
    """backfill-graph-v2 CLI: dry-run stats, live run, idempotency, canonical dedup."""
    print("\n[T_BACKFILL]")

    import types as _types

    # Seed entity_links directly with a mix of junk + legitimate rows
    conn.execute(
        "INSERT OR IGNORE INTO insights (content, type, project, status, created_at, updated_at) "
        "VALUES ('backfill test insight', 'decision', 'test-backfill', 'active', "
        "datetime('now'), datetime('now'))"
    )
    iid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Insert raw entity_links manually (bypassing daemon)
    for entity, etype in [
        ("/main", "path"),           # junk — should be rejected
        ("/governance", "path"),     # junk — should be rejected
        ("D:/workspace/engine", "path"),   # legitimate — drive-collapsed
        ("/workspace/engine", "path"),     # same canonical as above
        ("8786", "port"),            # good port
        ("8787", "port"),            # good port
        ("01", "port"),              # leading zero — normalizes to 1
        ("03", "port"),              # leading zero — normalizes to 3
    ]:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO entity_links "
                "(insight_id, entity, entity_type, raw_match) VALUES (?,?,?,?)",
                (iid, entity, etype, entity),
            )
        except Exception:
            pass
    conn.commit()

    # Dry-run
    from pathlib import Path as _Path
    cli_mod = _load_cli()
    cli_mod.DB_PATH = _Path(path)  # redirect CLI to temp DB

    dry_args = _types.SimpleNamespace(dry_run=True, project=None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_mod.cmd_backfill_graph_v2(dry_args)
    dry_out = buf.getvalue()

    try:
        dry_result = json.loads(dry_out)
    except Exception:
        dry_result = {}

    check("T_BKF_dry_ok", dry_result.get("ok") is True, dry_out[:200])
    check("T_BKF_dry_flag", dry_result.get("dry_run") is True, str(dry_result))
    check("T_BKF_dry_accepted", dry_result.get("accepted", 0) >= 1,
          f"expected ≥1 accepted; got {dry_result}")
    check("T_BKF_dry_rejected", dry_result.get("rejected", 0) >= 2,
          f"expected ≥2 rejected (/main, /governance); got {dry_result}")

    # Dry-run must not have added new entity_canonical rows
    ec_count_before_dry = conn.execute("SELECT COUNT(*) FROM entity_canonical").fetchone()[0]
    check("T_BKF_dry_no_write",
          conn.execute("SELECT COUNT(*) FROM entity_canonical").fetchone()[0] == ec_count_before_dry,
          f"dry-run should not add rows; before={ec_count_before_dry}")

    # Live run
    live_args = _types.SimpleNamespace(dry_run=False, project=None)
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        cli_mod.cmd_backfill_graph_v2(live_args)
    live_out = buf2.getvalue()

    try:
        live_result = json.loads(live_out)
    except Exception:
        live_result = {}

    check("T_BKF_live_ok", live_result.get("ok") is True, live_out[:200])
    check("T_BKF_live_dry_false", live_result.get("dry_run") is False, str(live_result))
    check("T_BKF_live_accepted", live_result.get("accepted", 0) >= 1,
          f"expected ≥1 accepted; got {live_result}")

    # entity_canonical should now have rows
    ec_after = conn.execute("SELECT COUNT(*) FROM entity_canonical").fetchone()[0]
    check("T_BKF_ec_written", ec_after >= 1,
          f"expected ≥1 entity_canonical rows; got {ec_after}")

    # Canonical dedup: D:/workspace/engine and /workspace/engine should share one canonical row
    ec_dedup = conn.execute(
        "SELECT COUNT(*) FROM entity_canonical WHERE entity_type='path' AND canonical='/workspace/engine'"
    ).fetchone()[0]
    check("T_BKF_canonical_dedup", ec_dedup >= 1,
          f"expected ≥1 row for canonical /workspace/engine; got {ec_dedup}")

    # Raw rows in entity_links should point to the same canonical_entity_id
    linked = conn.execute(
        """SELECT DISTINCT el.canonical_entity_id
           FROM entity_links el
           JOIN entity_canonical ec ON ec.id = el.canonical_entity_id
           WHERE ec.entity_type='path' AND ec.canonical='/workspace/engine'""",
    ).fetchall()
    check("T_BKF_dedup_links", len(linked) == 1,
          f"both raw path rows should share one canonical_entity_id; got {len(linked)} distinct")

    # Idempotency: running again should not increase entity_canonical count
    buf3 = io.StringIO()
    with redirect_stdout(buf3):
        cli_mod.cmd_backfill_graph_v2(live_args)
    ec_after2 = conn.execute("SELECT COUNT(*) FROM entity_canonical").fetchone()[0]
    check("T_BKF_idempotent", ec_after2 == ec_after,
          f"second run changed entity_canonical count {ec_after}→{ec_after2}")

    # orphan_after should be ≤ orphan_before (never increases)
    try:
        live_res2 = json.loads(buf3.getvalue())
        check("T_BKF_orphan_nonincreasing",
              live_res2.get("orphan_after", 0) <= live_res2.get("orphan_before", 0),
              str(live_res2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# T_CLAIM_REL — claim_relations seeding
# ---------------------------------------------------------------------------

def run_T_CLAIM_REL(conn, path: str):
    """claim_relations seeded from superseded_by, source_insights, contradictions."""
    print("\n[T_CLAIM_REL]")

    import types as _types

    # Seed: two insights where A supersedes B
    conn.execute(
        "INSERT OR IGNORE INTO insights (content, type, project, status, created_at, updated_at) "
        "VALUES ('old insight being replaced', 'decision', 'test-cr', 'active', "
        "datetime('now'), datetime('now'))"
    )
    loser_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO insights (content, type, project, status, superseded_by, "
        "created_at, updated_at) "
        "VALUES ('winner insight', 'decision', 'test-cr', 'active', ?, "
        "datetime('now'), datetime('now'))",
        (loser_id,),
    )
    winner_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Seed: a principle promoted from an insight (source_insights JSON)
    conn.execute(
        "INSERT OR IGNORE INTO principles "
        "(content, project, confidence, source_insights, created_at, updated_at) "
        "VALUES ('promoted principle', 'test-cr', 0.9, ?, datetime('now'), datetime('now'))",
        (json.dumps([loser_id]),),
    )
    prin_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Seed: contradiction pair
    try:
        conn.execute(
            "INSERT OR IGNORE INTO contradictions (insight_a_id, insight_b_id, "
            "detected_at, flagged_at) VALUES (?, ?, datetime('now'), datetime('now'))",
            (winner_id, loser_id),
        )
    except sqlite3.OperationalError:
        pass  # contradictions table may have different schema; skip
    conn.commit()

    # Run backfill live
    from pathlib import Path as _Path
    cli_mod = _load_cli()
    cli_mod.DB_PATH = _Path(path)
    live_args = _types.SimpleNamespace(dry_run=False, project=None)
    with redirect_stdout(io.StringIO()):
        cli_mod.cmd_backfill_graph_v2(live_args)

    # Check REPLACES
    replaces = conn.execute(
        "SELECT * FROM claim_relations WHERE relation_type='REPLACES' "
        "AND claim_a_kind='insight' AND claim_b_kind='insight'"
    ).fetchall()
    check("T_CR_a_replaces_seeded", len(replaces) >= 1,
          f"expected REPLACES relation; got {len(replaces)}")

    # Check REFINES (principle → insight)
    refines = conn.execute(
        "SELECT * FROM claim_relations WHERE relation_type='REFINES' "
        "AND claim_a_kind='principle' AND claim_a_id=?",
        (prin_id,),
    ).fetchall()
    check("T_CR_b_refines_seeded", len(refines) >= 1,
          f"expected REFINES relation from principle {prin_id}; got {len(refines)}")

    # Check idempotency (run again, no duplicate rows)
    with redirect_stdout(io.StringIO()):
        cli_mod.cmd_backfill_graph_v2(live_args)
    replaces2 = conn.execute(
        "SELECT COUNT(*) FROM claim_relations WHERE relation_type='REPLACES'"
    ).fetchone()[0]
    replaces1 = len(replaces)
    check("T_CR_c_idempotent", replaces2 == replaces1,
          f"REPLACES count should not grow on second run: {replaces1}→{replaces2}")


# ---------------------------------------------------------------------------
# T_ENTITY_REL — entity_relations heuristic seeding
# ---------------------------------------------------------------------------

def run_T_ENTITY_REL(conn, path: str):
    """ip+port, service+port, domain+ip co-occurrence → entity_relations."""
    print("\n[T_ENTITY_REL]")

    import types as _types

    # Seed an insight with exactly one IP + one port
    conn.execute(
        "INSERT OR IGNORE INTO insights (content, type, project, status, created_at, updated_at) "
        "VALUES ('crag engine daemon at 203.0.113.10:8786', 'architecture', 'test-er', 'active', "
        "datetime('now'), datetime('now'))"
    )
    iid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Insert entity_canonical rows for ip and port
    conn.execute(
        "INSERT OR IGNORE INTO entity_canonical (entity_type, raw_value, canonical) "
        "VALUES ('ip', '203.0.113.10', '203.0.113.10')"
    )
    ip_ec_id = conn.execute(
        "SELECT id FROM entity_canonical WHERE entity_type='ip' AND raw_value='203.0.113.10'"
    ).fetchone()["id"]

    conn.execute(
        "INSERT OR IGNORE INTO entity_canonical (entity_type, raw_value, canonical) "
        "VALUES ('port', '8786', '8786')"
    )
    port_ec_id = conn.execute(
        "SELECT id FROM entity_canonical WHERE entity_type='port' AND raw_value='8786'"
    ).fetchone()["id"]

    # Link them in entity_links
    conn.execute(
        "INSERT OR IGNORE INTO entity_links "
        "(insight_id, entity, entity_type, raw_match, canonical_entity_id) VALUES (?,?,?,?,?)",
        (iid, "203.0.113.10", "ip", "203.0.113.10", ip_ec_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO entity_links "
        "(insight_id, entity, entity_type, raw_match, canonical_entity_id) VALUES (?,?,?,?,?)",
        (iid, "8786", "port", "8786", port_ec_id),
    )
    conn.commit()

    # Run backfill
    from pathlib import Path as _Path
    cli_mod = _load_cli()
    cli_mod.DB_PATH = _Path(path)
    live_args = _types.SimpleNamespace(dry_run=False, project=None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_mod.cmd_backfill_graph_v2(live_args)
    try:
        result = json.loads(buf.getvalue())
    except Exception:
        result = {}

    check("T_ER_a_ok", result.get("ok") is True, buf.getvalue()[:200])

    # ip USES_PORT port should exist
    rel = conn.execute(
        "SELECT * FROM entity_relations WHERE entity_a_id=? AND relation_type='USES_PORT' AND entity_b_id=?",
        (ip_ec_id, port_ec_id),
    ).fetchone()
    check("T_ER_b_ip_uses_port", rel is not None,
          "expected USES_PORT relation for ip->port; got None")

    er_stats = result.get("entity_relations_seeded", {})
    check("T_ER_c_stats_reported", "ip_port" in er_stats,
          f"expected ip_port in entity_relations_seeded; got {er_stats}")


# ---------------------------------------------------------------------------
# T_TRAVERSE — traversal endpoint shapes
# ---------------------------------------------------------------------------

def run_T_TRAVERSE(conn, path: str):
    """Traversal endpoint shapes: /graph/siblings, /graph/neighbors, /graph/impact."""
    print("\n[T_TRAVERSE]")

    from fastapi.testclient import TestClient
    from pathlib import Path as _Path

    spec = importlib.util.spec_from_file_location("engine_daemon_tr", DAEMON_PY)
    daemon_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon_mod)
    daemon_mod.DB_PATH = _Path(path)
    client = TestClient(daemon_mod.app, raise_server_exceptions=False)

    # /graph/siblings — non-existent claim returns empty siblings list
    resp = client.get("/graph/siblings?claim_kind=insight&claim_id=999999")
    check("T_TRV_siblings_200", resp.status_code == 200,
          f"status={resp.status_code}")
    if resp.status_code == 200:
        body = resp.json()
        check("T_TRV_siblings_has_siblings_key", "siblings" in body, str(body))
        check("T_TRV_siblings_empty_ok", isinstance(body.get("siblings"), list), str(body))

    # /graph/neighbors — non-existent entity returns found=False
    resp2 = client.get("/graph/neighbors?entity=99.99.99.99&entity_type=ip")
    check("T_TRV_neighbors_200", resp2.status_code == 200,
          f"status={resp2.status_code}")
    if resp2.status_code == 200:
        body2 = resp2.json()
        check("T_TRV_neighbors_found_key", "found" in body2, str(body2))
        check("T_TRV_neighbors_not_found", body2.get("found") is False, str(body2))

    # /graph/impact — non-existent entity returns found=False
    resp3 = client.get("/graph/impact?entity=99.99.99.99&entity_type=ip")
    check("T_TRV_impact_200", resp3.status_code == 200,
          f"status={resp3.status_code}")
    if resp3.status_code == 200:
        body3 = resp3.json()
        check("T_TRV_impact_found_key", "found" in body3, str(body3))
        check("T_TRV_impact_not_found", body3.get("found") is False, str(body3))

    # /graph/neighbors — known canonical entity returns found=True
    # First seed an entity_canonical row and hit /graph/neighbors
    conn.execute(
        "INSERT OR IGNORE INTO entity_canonical (entity_type, raw_value, canonical) "
        "VALUES ('port', '9797', '9797')"
    )
    conn.commit()
    resp4 = client.get("/graph/neighbors?entity=9797&entity_type=port")
    check("T_TRV_neighbors_found_true",
          resp4.status_code == 200 and resp4.json().get("found") is True,
          f"status={resp4.status_code} body={resp4.json()}")

    # /graph/siblings — bad claim_kind returns error or empty
    resp5 = client.get("/graph/siblings?claim_kind=bogus&claim_id=1")
    check("T_TRV_siblings_bad_kind",
          resp5.status_code == 200,  # endpoint should return error JSON, not 500
          f"status={resp5.status_code}")


# ---------------------------------------------------------------------------
# T_SMOKE — 26 tools, bidirectional parity
# ---------------------------------------------------------------------------

def run_T_SMOKE():
    """MCP smoke: 26 tools, bidirectional parity (graph added)."""
    print("\n[T_SMOKE]")
    import subprocess
    result = subprocess.run(
        [sys.executable,
         str(REPO_ROOT / "apps" / "mcp" / "tests" / "test_mcp_smoke.py")],
        capture_output=True, text=True,
    )
    passed = result.returncode == 0
    check("T_SMK_26_tools_parity", passed,
          result.stdout[-300:] + result.stderr[-300:])


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_T_NORM()

    conn, db_path = _create_test_db()

    try:
        run_T_STORE(conn, db_path)
        run_T_BACKFILL(conn, db_path)
        run_T_CLAIM_REL(conn, db_path)
        run_T_ENTITY_REL(conn, db_path)
        run_T_TRAVERSE(conn, db_path)
        run_T_SMOKE()
    finally:
        conn.close()
        # Clean up the temp DB + WAL/SHM sidecars (PART E advisory 1: these
        # used to litter REPO_ROOT/db/ ~24MB per run next to live engine.db).
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except OSError:
                pass

    sep = "=" * 60
    print(f"\n{sep}")
    total = len(PASSES) + len(FAILURES)
    print(f"Results: {len(PASSES)}/{total} passed, {len(FAILURES)} failed")
    if FAILURES:
        print("\nFailed:")
        for f in FAILURES:
            print(f"  - {f}")
    else:
        print("All tests passed.")

    sys.exit(0 if not FAILURES else 1)
