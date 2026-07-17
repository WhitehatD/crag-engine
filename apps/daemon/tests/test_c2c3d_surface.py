#!/usr/bin/env python3
# coding: utf-8
"""C2+C3+D surface test suite.

Standalone (no pytest — mirrors prior test files in this repo).

Tests:
  T_MANIFEST  — capabilities.py self-consistency: tool count matches TOOLS_MANIFEST;
                all ENDPOINTS_MANIFEST entries have method+path+summary;
                render_llms_txt() mentions every category; render_guide() has all categories.
  T_SESSION   — POST /lifecycle/session/add + GET /lifecycle/session/get: add, retrieve, limit.
  T_CONTEXT   — GET /lifecycle/context/get (no context) + POST /lifecycle/context/set + re-get.
  T_EVENTS    — POST /lifecycle/events/add + GET list + POST claim + POST complete + bulk_expire.
  T_COST      — GET /lifecycle/cost_report: token_ledger aggregation for a test project.
  T_BRIEF     — GET /brief: returns all 7 keys; principles list; events critical+high only;
                grounding_flags int; last_session str-or-None; token_nudge int; grounding_jobs int.
  T_LLMS      — GET /llms.txt: plain text, contains "## MCP Tools" and "## HTTP Endpoints".
  T_GUIDE     — GET /guide: JSON, version field, tools_by_category, endpoints list, key_workflows.
  T_COMPLETE  — No-CLI lifecycle: session add + context set + event add/claim/complete
                round-trips fully through daemon endpoints (no engine-cli.py subprocess).
  T_GROUND_EXT — grounding action='jobs' / 'history' / 'stats' endpoints exist and respond.
  T_JTYPE     — POST /ground/jobs/enqueue rejects job_type not in (reground|author).
  T_COLDAGENT — Cold-agent simulation: GET /brief provides enough context to orient a new agent
                (has 'project', 'principles', 'pending_events', 'last_session').

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_c2c3d_surface.py
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_DB = REPO_ROOT / "db" / "engine.db"
DAEMON_PY = REPO_ROOT / "apps" / "daemon" / "engine_daemon.py"
MIGRATION_026 = REPO_ROOT / "db" / "migrations" / "026_grounding_v2.sql"
CAPABILITIES_PY = REPO_ROOT / "db" / "capabilities.py"
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
# Schema helpers — mirrors test_a3c1_grounding_pipeline.py pattern
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
    """Schema-copy of live DB + migration 026. Returns file path."""
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db", prefix="c2c3dtest-")
    os.close(fd)
    conn = sqlite3.connect(path)
    for s in stmts:
        try:
            conn.execute(s)
        except sqlite3.OperationalError:
            pass  # FTS5 shadow tables
    conn.commit()
    _apply_026(conn)
    conn.commit()
    conn.close()
    print(f"temp DB: {path}")
    return path


# ---------------------------------------------------------------------------
# Daemon loader — mirrors prior test files
# ---------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Capabilities module loader
# ---------------------------------------------------------------------------

def _load_capabilities():
    spec = importlib.util.spec_from_file_location("capabilities", CAPABILITIES_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Build temp DB + load daemon (module-level, once)
# ---------------------------------------------------------------------------

TEMP_DB = _build_temp_db()
daemon = _load_module("engine_daemon_c2c3dtest", DAEMON_PY)
daemon.DB_PATH = Path(TEMP_DB)

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(daemon.app)  # no lifespan => no background loops


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(TEMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Test groups
# ---------------------------------------------------------------------------


def run_T_MANIFEST():
    """T_MANIFEST — capabilities.py self-consistency checks."""
    print("\n[T_MANIFEST]")
    cap = _load_capabilities()

    # All tools have required fields
    ok_shape = True
    for t in cap.TOOLS_MANIFEST:
        if not all(k in t for k in ("name", "description", "category", "required", "params")):
            ok_shape = False
            break
    check("T_MAN_tool_shape", ok_shape, "every tool must have name/description/category/required/params")

    # TOOLS_BY_NAME is a dict keyed by name
    check("T_MAN_by_name", isinstance(cap.TOOLS_BY_NAME, dict),
          "TOOLS_BY_NAME must be a dict")
    check("T_MAN_by_name_count",
          len(cap.TOOLS_BY_NAME) == len(cap.TOOLS_MANIFEST),
          "TOOLS_BY_NAME must have same count as TOOLS_MANIFEST")

    # ENDPOINTS_MANIFEST entries all have method+path+summary
    ep_ok = all("method" in e and "path" in e and "summary" in e
                for e in cap.ENDPOINTS_MANIFEST)
    check("T_MAN_endpoint_shape", ep_ok, "every endpoint must have method/path/summary")

    # Lifecycle endpoints present
    lifecycle_paths = {e["path"] for e in cap.ENDPOINTS_MANIFEST}
    for lp in (
        "/lifecycle/session/add", "/lifecycle/session/get",
        "/lifecycle/context/get", "/lifecycle/context/set",
        "/lifecycle/events/add", "/lifecycle/events/list",
        "/lifecycle/events/claim", "/lifecycle/events/complete",
        "/lifecycle/events/bulk_expire", "/lifecycle/cost_report",
        "/brief", "/llms.txt", "/guide",
    ):
        check(f"T_MAN_endpoint_{lp}", lp in lifecycle_paths,
              f"{lp} missing from ENDPOINTS_MANIFEST")

    # render_llms_txt mentions all categories
    txt = cap.render_llms_txt()
    for cat in cap.TOOL_CATEGORIES:
        check(f"T_MAN_llms_cat_{cat}", cat.title() in txt,
              f"render_llms_txt missing category '{cat}'")

    # render_llms_txt contains both sections
    check("T_MAN_llms_mcp_section", "## MCP Tools" in txt)
    check("T_MAN_llms_http_section", "## HTTP Endpoints" in txt)

    # render_guide returns all categories
    guide = cap.render_guide()
    check("T_MAN_guide_version", "version" in guide and guide["version"] == cap.MANIFEST_VERSION)
    check("T_MAN_guide_tools", "tools_by_category" in guide)
    check("T_MAN_guide_endpoints", "endpoints" in guide and len(guide["endpoints"]) > 0)
    check("T_MAN_guide_workflows", "key_workflows" in guide and "pre_session" in guide["key_workflows"])

    for cat in cap.TOOL_CATEGORIES:
        check(f"T_MAN_guide_cat_{cat}", cat in guide["tools_by_category"],
              f"render_guide missing category '{cat}'")

    # Tool descriptions all ≤ 280 chars (tweet-length)
    long_descs = [t["name"] for t in cap.TOOLS_MANIFEST if len(t["description"]) > 280]
    check("T_MAN_desc_length", len(long_descs) == 0,
          f"description > 280 chars: {long_descs}")


def run_T_DAEMON():
    """T_SESSION, T_CONTEXT, T_EVENTS, T_COST, T_BRIEF, T_LLMS, T_GUIDE,
    T_COMPLETE, T_GROUND_EXT, T_JTYPE — all via TestClient against temp DB."""

    # ── T_SESSION ───────────────────────────────────────────────────────────
    print("\n[T_SESSION]")

    # Add a session
    r = client.post("/lifecycle/session/add", json={
        "project": "test-c2c3d",
        "date": "2026-07-04",
        "accomplished": "Implemented C2+C3+D surface",
        "files_changed": "engine_daemon.py, mcp-server.py",
        "commits": "abc1234",
        "decisions": "Use /brief as one-call warm-up",
        "problems": "none",
        "next_steps": "write tests",
    })
    check("T_SES_add_ok", r.status_code == 200, f"status={r.status_code}")
    body = r.json()
    check("T_SES_add_id", isinstance(body.get("id"), int), str(body))
    session_id = body.get("id", 0)

    # Get sessions
    r = client.get("/lifecycle/session/get?project=test-c2c3d&limit=5")
    check("T_SES_get_ok", r.status_code == 200, f"status={r.status_code}")
    body = r.json()
    check("T_SES_get_sessions_key", "sessions" in body and "count" in body, str(body))
    check("T_SES_get_finds_it", any(s["id"] == session_id for s in body.get("sessions", [])),
          f"session {session_id} not found in {body}")
    check("T_SES_get_accomplished",
          any(s.get("accomplished") == "Implemented C2+C3+D surface" for s in body.get("sessions", [])),
          str(body))

    # Limit enforcement
    for _ in range(3):
        client.post("/lifecycle/session/add", json={
            "project": "test-c2c3d", "date": "2026-07-04", "accomplished": "extra"
        })
    r = client.get("/lifecycle/session/get?project=test-c2c3d&limit=2")
    check("T_SES_limit", len(r.json().get("sessions", [])) <= 2, str(r.json()))

    # ── T_CONTEXT ───────────────────────────────────────────────────────────
    print("\n[T_CONTEXT]")

    # Get non-existent context
    r = client.get("/lifecycle/context/get?project=test-c2c3d-ctx")
    check("T_CTX_missing_ok", r.status_code == 200, f"status={r.status_code}")
    check("T_CTX_missing_status", r.json().get("status") == "no context saved", str(r.json()))

    # Set context
    r = client.post("/lifecycle/context/set", json={
        "project": "test-c2c3d-ctx",
        "status": "active development",
        "current_branch": "grounding-v2-c2c3d",
        "known_issues": "none yet",
        "backlog": "write guide, test everything",
    })
    check("T_CTX_set_ok", r.status_code == 200 and r.json().get("ok") is True, str(r.json()))

    # Get it back
    r = client.get("/lifecycle/context/get?project=test-c2c3d-ctx")
    check("T_CTX_get_ok", r.status_code == 200, f"status={r.status_code}")
    ctx = r.json()
    check("T_CTX_get_project", ctx.get("project") == "test-c2c3d-ctx", str(ctx))
    check("T_CTX_get_branch", ctx.get("current_branch") == "grounding-v2-c2c3d", str(ctx))
    check("T_CTX_get_status", ctx.get("status") == "active development", str(ctx))

    # Null fields unchanged (partial upsert)
    r = client.post("/lifecycle/context/set", json={
        "project": "test-c2c3d-ctx",
        "known_issues": "found a bug",
    })
    r2 = client.get("/lifecycle/context/get?project=test-c2c3d-ctx")
    ctx2 = r2.json()
    check("T_CTX_partial_branch", ctx2.get("current_branch") == "grounding-v2-c2c3d",
          "branch should be unchanged after partial update")
    check("T_CTX_partial_issues", ctx2.get("known_issues") == "found a bug", str(ctx2))

    # ── T_EVENTS ────────────────────────────────────────────────────────────
    print("\n[T_EVENTS]")

    # Add critical event
    r = client.post("/lifecycle/events/add", json={
        "project": "test-c2c3d",
        "source": "ci",
        "event_type": "build_failure",
        "summary": "Build failed on main",
        "priority": "critical",
    })
    check("T_EVT_add_critical_ok", r.status_code == 200 and r.json().get("ok"), str(r.json()))
    evt_id_critical = r.json().get("id")

    # Add normal event
    r = client.post("/lifecycle/events/add", json={
        "project": "test-c2c3d",
        "source": "monitor",
        "event_type": "memory_warn",
        "summary": "Memory above 80%",
        "priority": "normal",
    })
    check("T_EVT_add_normal_ok", r.status_code == 200 and r.json().get("ok"), str(r.json()))
    evt_id_normal = r.json().get("id")

    # Invalid priority rejected
    r = client.post("/lifecycle/events/add", json={
        "project": "test-c2c3d",
        "source": "x",
        "event_type": "y",
        "summary": "z",
        "priority": "supercritical",
    })
    check("T_EVT_bad_priority", r.status_code == 422, f"status={r.status_code}")

    # List events — critical first
    r = client.get("/lifecycle/events/list?project=test-c2c3d")
    check("T_EVT_list_ok", r.status_code == 200, f"status={r.status_code}")
    evts = r.json().get("events", [])
    check("T_EVT_list_has_both", len(evts) >= 2, f"got {len(evts)}")
    if len(evts) >= 2:
        check("T_EVT_list_order", evts[0]["priority"] == "critical",
              f"first event priority={evts[0]['priority']}")

    # Claim event
    r = client.post("/lifecycle/events/claim", json={
        "id": evt_id_critical,
        "claimed_by": "test-agent",
    })
    check("T_EVT_claim_ok", r.status_code == 200 and r.json().get("ok"), str(r.json()))

    # Claiming already-claimed event returns ok:False
    r = client.post("/lifecycle/events/claim", json={"id": evt_id_critical, "claimed_by": "x"})
    check("T_EVT_claim_dup", r.json().get("ok") is False,
          "double-claim should fail with ok:False")

    # Complete event
    r = client.post("/lifecycle/events/complete", json={
        "id": evt_id_critical,
        "status": "completed",
        "result": "CI fixed",
    })
    check("T_EVT_complete_ok", r.status_code == 200 and r.json().get("ok"), str(r.json()))

    # Invalid status rejected
    r = client.post("/lifecycle/events/complete", json={"id": evt_id_normal, "status": "done"})
    check("T_EVT_bad_status", r.status_code == 422, f"status={r.status_code}")

    # Bulk expire
    r = client.post("/lifecycle/events/bulk_expire", json={
        "project": "test-c2c3d",
        "priority": "normal",
    })
    check("T_EVT_bulk_ok", r.status_code == 200 and r.json().get("ok"), str(r.json()))
    check("T_EVT_bulk_count", r.json().get("expired", 0) >= 1, str(r.json()))

    # Verify normal event is now expired
    r = client.get("/lifecycle/events/list?project=test-c2c3d")
    normal_pending = [e for e in r.json().get("events", []) if e["id"] == evt_id_normal]
    check("T_EVT_bulk_verify", len(normal_pending) == 0,
          f"normal event should be expired, found {normal_pending}")

    # ── T_COST ──────────────────────────────────────────────────────────────
    print("\n[T_COST]")

    # Seed a token_ledger row via the existing endpoint
    conn = db()
    conn.execute(
        """INSERT INTO token_ledger (project, session_id, tokens_in, tokens_out, cache_hits,
           wall_time_sec, rtk_savings_pct, headroom_savings_pct, model, task_summary, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("test-c2c3d", "sess-test-1", 1000, 200, 50, 120.0, 45.0, 30.0, "claude-test", "test session"),
    )
    conn.commit()
    conn.close()

    r = client.get("/lifecycle/cost_report?project=test-c2c3d&days=7")
    check("T_COST_ok", r.status_code == 200, f"status={r.status_code}")
    report = r.json()
    check("T_COST_totals", "totals" in report and "by_project" in report and "trend" in report,
          str(list(report.keys())))
    totals = report["totals"]
    check("T_COST_sessions_count", totals.get("sessions", 0) >= 1, str(totals))
    check("T_COST_token_in", totals.get("total_in", 0) >= 1000, str(totals))

    # Cross-project with no filter
    r2 = client.get("/lifecycle/cost_report?days=7")
    check("T_COST_no_project_ok", r2.status_code == 200, f"status={r2.status_code}")
    check("T_COST_no_project_totals", "totals" in r2.json(), str(r2.json()))

    # ── T_BRIEF ─────────────────────────────────────────────────────────────
    print("\n[T_BRIEF]")

    # Seed a principle so brief returns it
    conn = db()
    conn.execute(
        """INSERT INTO principles (project, content, confidence, created_at, updated_at)
           VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
        ("test-c2c3d", "Always write tests before merging. This is rule #1.", 0.95),
    )
    conn.commit()
    conn.close()

    r = client.get("/brief?project=test-c2c3d")
    check("T_BRF_ok", r.status_code == 200, f"status={r.status_code}")
    brief = r.json()

    # All 7 required keys
    for key in ("project", "principles", "pending_events", "grounding_flags",
                "last_session", "token_nudge", "grounding_jobs"):
        check(f"T_BRF_key_{key}", key in brief, f"missing key '{key}' in {list(brief.keys())}")

    check("T_BRF_project", brief.get("project") == "test-c2c3d", str(brief.get("project")))
    check("T_BRF_principles_list", isinstance(brief.get("principles"), list), str(type(brief.get("principles"))))
    check("T_BRF_principles_content",
          any("tests" in p.lower() for p in brief.get("principles", [])),
          f"seeded principle not found: {brief.get('principles')}")
    check("T_BRF_events_list", isinstance(brief.get("pending_events"), list))
    check("T_BRF_flags_int", isinstance(brief.get("grounding_flags"), int))
    check("T_BRF_token_nudge_int", isinstance(brief.get("token_nudge"), int))
    check("T_BRF_jobs_int", isinstance(brief.get("grounding_jobs"), int))
    # last_session should contain today's session
    check("T_BRF_last_session", brief.get("last_session") is not None,
          "no session was found — expected 'last_session' to have a value")

    # Only critical+high events in brief
    for ev in brief.get("pending_events", []):
        check(f"T_BRF_events_priority_{ev['id']}",
              ev["priority"] in ("critical", "high"),
              f"event {ev['id']} has priority {ev['priority']}")

    # Missing project returns empty-but-valid structure
    r2 = client.get("/brief?project=test-nonexistent-xyz")
    check("T_BRF_missing_ok", r2.status_code == 200)
    brief2 = r2.json()
    check("T_BRF_missing_keys", all(k in brief2 for k in ("principles", "pending_events")), str(brief2))

    # ── T_LLMS ──────────────────────────────────────────────────────────────
    print("\n[T_LLMS]")

    r = client.get("/llms.txt")
    check("T_LLMS_ok", r.status_code == 200, f"status={r.status_code}")
    check("T_LLMS_content_type",
          "text/plain" in r.headers.get("content-type", ""),
          r.headers.get("content-type"))
    txt = r.text
    check("T_LLMS_mcp_section", "## MCP Tools" in txt)
    check("T_LLMS_http_section", "## HTTP Endpoints" in txt)
    check("T_LLMS_workflows", "## Key Workflows" in txt)
    check("T_LLMS_brief_endpoint", "/brief" in txt)
    check("T_LLMS_recall_tool", "recall" in txt)

    # ── T_GUIDE ─────────────────────────────────────────────────────────────
    print("\n[T_GUIDE]")

    r = client.get("/guide")
    check("T_GDE_ok", r.status_code == 200, f"status={r.status_code}")
    guide = r.json()
    check("T_GDE_version", "version" in guide, str(list(guide.keys())))
    check("T_GDE_tools_by_cat", "tools_by_category" in guide)
    check("T_GDE_endpoints", "endpoints" in guide and len(guide["endpoints"]) > 0)
    check("T_GDE_workflows", "key_workflows" in guide and "pre_session" in guide["key_workflows"])
    check("T_GDE_lifecycle_cat",
          "lifecycle" in guide.get("tools_by_category", {}),
          f"categories: {list(guide.get('tools_by_category', {}).keys())}")
    check("T_GDE_meta_cat",
          "meta" in guide.get("tools_by_category", {}),
          f"categories: {list(guide.get('tools_by_category', {}).keys())}")

    # ── T_GROUND_EXT ────────────────────────────────────────────────────────
    print("\n[T_GROUND_EXT]")

    r = client.get("/ground/jobs?limit=5")
    check("T_GRD_jobs_ok", r.status_code == 200, f"status={r.status_code}")
    check("T_GRD_jobs_shape", "jobs" in r.json() or "ok" in r.json(), str(r.json()))

    r = client.get("/ground/stats")
    check("T_GRD_stats_ok", r.status_code == 200, f"status={r.status_code}")
    stats = r.json()
    check("T_GRD_stats_keys",
          any(k in stats for k in ("pending", "running", "done", "total", "ok")),
          str(list(stats.keys())))

    # History for non-existent claim returns 404 or ok:False gracefully
    r = client.get("/ground/history/insight/99999999")
    check("T_GRD_hist_missing_graceful", r.status_code in (200, 404), f"status={r.status_code}")

    # ── T_JTYPE ─────────────────────────────────────────────────────────────
    print("\n[T_JTYPE]")

    # Seed a real insight first
    conn = db()
    conn.execute(
        """INSERT INTO insights (project, type, content, status, confidence, created_at, updated_at)
           VALUES ('test-c2c3d', 'gotcha', 'Test insight for jtype check', 'active', 0.5,
           datetime('now'), datetime('now'))"""
    )
    conn.commit()
    ins_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    # Valid job_type accepted (may fail because no falsifier, but 422 must not be from jtype)
    r = client.post("/ground/jobs/enqueue", json={
        "claim_kind": "insight",
        "claim_id": ins_id,
        "job_type": "author",
    })
    check("T_JT_valid_not_422", r.status_code != 422 or "job_type" not in r.json().get("error", ""),
          f"valid job_type rejected: {r.json()}")

    # Invalid job_type rejected with 422
    r = client.post("/ground/jobs/enqueue", json={
        "claim_kind": "insight",
        "claim_id": ins_id,
        "job_type": "magic",
    })
    check("T_JT_invalid_422", r.status_code == 422, f"status={r.status_code}, body={r.json()}")
    check("T_JT_invalid_msg", "job_type" in r.json().get("error", ""), str(r.json()))

    # ── T_COMPLETE (no-CLI lifecycle round-trip) ─────────────────────────────
    print("\n[T_COMPLETE]")

    # Add session → context → event → claim → complete: all via daemon, no CLI
    proj = "test-complete-nocli"

    r1 = client.post("/lifecycle/session/add", json={
        "project": proj, "accomplished": "nocli round-trip", "date": "2026-07-04"
    })
    check("T_CMP_session", r1.status_code == 200 and r1.json().get("ok"), str(r1.json()))

    r2 = client.post("/lifecycle/context/set", json={
        "project": proj, "status": "testing", "current_branch": "grounding-v2-c2c3d"
    })
    check("T_CMP_context", r2.status_code == 200 and r2.json().get("ok"), str(r2.json()))

    r3 = client.post("/lifecycle/events/add", json={
        "project": proj, "source": "test", "event_type": "unit_test",
        "summary": "nocli test event", "priority": "high"
    })
    check("T_CMP_event_add", r3.status_code == 200 and r3.json().get("ok"), str(r3.json()))
    eid = r3.json().get("id")

    r4 = client.post("/lifecycle/events/claim", json={"id": eid, "claimed_by": "nocli-test"})
    check("T_CMP_claim", r4.status_code == 200 and r4.json().get("ok"), str(r4.json()))

    r5 = client.post("/lifecycle/events/complete", json={
        "id": eid, "status": "completed", "result": "done"
    })
    check("T_CMP_complete", r5.status_code == 200 and r5.json().get("ok"), str(r5.json()))

    # Verify the event no longer appears in pending list
    r6 = client.get(f"/lifecycle/events/list?project={proj}")
    remaining = [e for e in r6.json().get("events", []) if e["id"] == eid]
    check("T_CMP_gone", len(remaining) == 0, f"completed event still pending: {remaining}")

    # Brief round-trip — seeded session appears
    r7 = client.get(f"/brief?project={proj}")
    check("T_CMP_brief_ok", r7.status_code == 200, f"status={r7.status_code}")
    check("T_CMP_brief_session",
          r7.json().get("last_session") is not None,
          "session should appear in brief")

    # ── T_COLDAGENT ─────────────────────────────────────────────────────────
    print("\n[T_COLDAGENT]")

    # Simulate: a brand new agent with only /brief — does it get enough context?
    brief_r = client.get("/brief?project=test-c2c3d")
    b = brief_r.json()
    # Must have: project identity, principles (know the rules), events (what to act on),
    # last session (where we left off).
    check("T_CA_project_key", b.get("project") == "test-c2c3d")
    check("T_CA_principles_present", isinstance(b.get("principles"), list) and len(b.get("principles", [])) > 0,
          "cold agent needs principles to orient itself")
    check("T_CA_pending_events_key", isinstance(b.get("pending_events"), list),
          "cold agent needs pending events list")
    check("T_CA_last_session_key", "last_session" in b,
          "cold agent needs last_session key to know where we left off")

    # Guide round-trip — agent can discover the full tool surface
    guide_r = client.get("/guide")
    g = guide_r.json()
    # Count MCP tool names exposed
    all_tool_names = [
        t["name"]
        for cat_tools in g.get("tools_by_category", {}).values()
        for t in cat_tools
    ]
    check("T_CA_guide_tool_count", len(all_tool_names) >= 20,
          f"guide exposes only {len(all_tool_names)} tools — expected >= 20")
    check("T_CA_brief_in_guide", "brief" in all_tool_names,
          "brief tool must be discoverable via /guide")
    check("T_CA_engine_guide_in_guide", "engine_guide" in all_tool_names,
          "engine_guide tool must be discoverable via /guide")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    run_T_MANIFEST()
    run_T_DAEMON()

    # cleanup temp DB
    try:
        Path(TEMP_DB).unlink(missing_ok=True)
    except OSError:
        pass  # Windows lock — tolerable

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
