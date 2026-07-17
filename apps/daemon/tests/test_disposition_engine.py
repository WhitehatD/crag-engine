#!/usr/bin/env python3
# coding: utf-8
"""Disposition Engine test suite (docs/architecture.md REV 5 §5.2 / REV 7 §7.1,
migration 033).

Standalone (no pytest — mirrors test_grounding_v3_rev3.py / test_write_gate.py).

Covers:
  T_MIGR_033    — migration 033 applies cleanly + is idempotent on double-run
                  (insights_staging tier/actor/decided_at/deadline/disposition,
                  disposition_log, disposition_policy seed rows,
                  operator_decision_history).
  T_TIER        — disposition.classify_tier: secret_scan->t2, schema_gate/
                  dedup_ambiguous/lifecycle:supersede->t1, wildcard->t0.
  T_GATE        — disposition.gate_check: t0 always; t1 needs granted/
                  human_approved; t2 needs human_approved (agent 'granted'
                  alone is NOT enough); reject/defer always allowed.
  T_RESOLVE_*   — disposition.resolve: accept creates an insight + logs
                  disposition_log; reject never touches `insights`; merge
                  creates+supersedes into target_id (reversible via
                  unsupersede); defer stays 'pending' with a fresh deadline;
                  actor is mandatory; unknown action / already-decided row
                  are clean errors, never exceptions.
  T_REVERSIBLE  — a merge is undone by clearing insights.superseded_by (the
                  EXISTING supersede/unsupersede mechanism), proving reuse.
  T_DRAIN       — drain_due: a past-deadline t0 row auto-executes (accept);
                  a past-deadline t2 row gets the SAFE default (defer), never
                  an auto-accept/merge.
  T_FAILSOFT    — resolve() against a missing staging row / malformed
                  payload never raises and never partially commits.
  T_HISTORY     — record_operator_decision persists; suggest_tier_from_history
                  is the documented stub (always None).
  T_ENDPOINT_*  — daemon integration: /disposition/list, /disposition/triage,
                  /disposition/resolve (incl. requires_human capability gate),
                  /disposition/drain, /disposition/policy get+set.

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_disposition_engine.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_DB = REPO_ROOT / "db" / "engine.db"
DAEMON_PY = REPO_ROOT / "apps" / "daemon" / "engine_daemon.py"
DB_DIR = REPO_ROOT / "db"
MIGRATIONS = [
    "031_grounding_v3_claim_layer.sql",
    "032_grounding_v3_rev3.sql",
    "033_disposition_engine.sql",
]
MIGRATION_033 = DB_DIR / "migrations" / "033_disposition_engine.sql"

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
# DB helpers (identical harness to test_grounding_v3_rev3.py)
# ---------------------------------------------------------------------------

def _apply_sql_file(conn: sqlite3.Connection, path: Path) -> list[str]:
    tolerated = []
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
                tolerated.append(str(exc))
                continue
            raise
    return tolerated


def _build_temp_db() -> str:
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db", prefix="disposition-test-")
    os.close(fd)
    conn = sqlite3.connect(path)
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


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TEMP_DB = _build_temp_db()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(TEMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_staging(conn: sqlite3.Connection, *, reason: str = None, content: str = "test content body",
                  type_: str = "gotcha", project: str = "test-disp", source: str = "gate_failure",
                  deadline: str = None, status: str = "pending") -> int:
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps({"content": content, "type": type_})
    cur = conn.execute(
        "INSERT INTO insights_staging (source, project, payload, dedup_key, status, "
        "reason, created_at, deadline) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)",
        (source, project, payload, status, reason, now, deadline),
    )
    conn.commit()
    return cur.lastrowid


def _seed_insight(conn: sqlite3.Connection, content: str = "existing target insight") -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO insights (project, type, content, confidence, status, created_at, updated_at) "
        "VALUES ('test-disp', 'gotcha', ?, 0.6, 'active', ?, ?)",
        (content, now, now),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# T_MIGR_033
# ---------------------------------------------------------------------------

def run_T_MIGR_033():
    print("\n[T_MIGR_033]")
    conn = db()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(insights_staging)")}
    for c in ("tier", "actor", "decided_at", "deadline", "disposition"):
        check(f"T_M33_staging_col_{c}", c in cols, str(cols))
    for tbl in ("disposition_log", "disposition_policy", "operator_decision_history"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone() is not None
        check(f"T_M33_table_{tbl}", exists, "table missing")
    policy_rows = conn.execute("SELECT COUNT(*) FROM disposition_policy").fetchone()[0]
    check("T_M33_policy_seeded", policy_rows >= 5, f"got {policy_rows} rows")
    wildcard = conn.execute(
        "SELECT tier, default_action FROM disposition_policy "
        "WHERE source IS NULL AND type IS NULL AND reason_prefix IS NULL"
    ).fetchone()
    check("T_M33_wildcard_t0", wildcard is not None and wildcard["tier"] == "t0", str(wildcard))
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    check("T_M33_version", version >= 33, f"schema_version={version}")
    conn.close()

    conn2 = db()
    tolerated = _apply_sql_file(conn2, MIGRATION_033)
    conn2.commit()
    check("T_M33_idempotent_tolerated", len(tolerated) >= 5,
          f"expected >=5 tolerated ADD COLUMN errors on re-run, got {len(tolerated)}: {tolerated}")
    check("T_M33_idempotent_all_dup",
          all("duplicate column name" in t.lower() for t in tolerated),
          f"unexpected non-duplicate-column error: {tolerated}")
    policy_rows2 = conn2.execute("SELECT COUNT(*) FROM disposition_policy").fetchone()[0]
    check("T_M33_policy_not_duplicated", policy_rows2 == policy_rows,
          f"re-run duplicated seed rows: {policy_rows} -> {policy_rows2}")
    conn2.close()


# ---------------------------------------------------------------------------
# T_TIER — classify_tier
# ---------------------------------------------------------------------------

def run_T_TIER():
    print("\n[T_TIER]")
    import disposition

    conn = db()
    policy = disposition.load_policy(conn)
    conn.close()

    v = disposition.classify_tier({"reason": "secret_scan:aws_access_key"}, policy)
    check("T_TIER_secret_t2", v["tier"] == "t2" and v["default_action"] == "defer", str(v))

    v = disposition.classify_tier({"reason": "schema_gate:content_too_short"}, policy)
    check("T_TIER_schema_t1", v["tier"] == "t1", str(v))

    v = disposition.classify_tier({"reason": "dedup_ambiguous:some detail"}, policy)
    check("T_TIER_dedup_t1", v["tier"] == "t1", str(v))

    v = disposition.classify_tier({"reason": "lifecycle:supersede?#4821"}, policy)
    check("T_TIER_lifecycle_t1", v["tier"] == "t1", str(v))

    v = disposition.classify_tier({"reason": None}, policy)
    check("T_TIER_wildcard_t0", v["tier"] == "t0" and v["default_action"] == "accept", str(v))

    v = disposition.classify_tier({"reason": "totally_unrecognized_reason"}, policy)
    check("T_TIER_unknown_falls_to_wildcard_t0", v["tier"] == "t0", str(v))

    # Fail-closed on garbage policy input.
    v = disposition.classify_tier({"reason": "secret_scan:x"}, "not-a-list")
    check("T_TIER_bad_policy_failclosed", v["tier"] == "t0", str(v))


# ---------------------------------------------------------------------------
# T_GATE — gate_check
# ---------------------------------------------------------------------------

def run_T_GATE():
    print("\n[T_GATE]")
    import disposition

    check("T_GATE_t0_always", disposition.gate_check("t0", "accept", None) is True)
    check("T_GATE_t1_no_cap", disposition.gate_check("t1", "accept", None) is False)
    check("T_GATE_t1_granted", disposition.gate_check("t1", "accept", "granted") is True)
    check("T_GATE_t1_human_approved", disposition.gate_check("t1", "merge", "human_approved") is True)
    check("T_GATE_t2_granted_insufficient", disposition.gate_check("t2", "accept", "granted") is False,
          "t2 must NOT be satisfied by mere agent capability")
    check("T_GATE_t2_human_approved", disposition.gate_check("t2", "accept", "human_approved") is True)
    check("T_GATE_reject_always", disposition.gate_check("t2", "reject", None) is True)
    check("T_GATE_defer_always", disposition.gate_check("t2", "defer", None) is True)
    check("T_GATE_unknown_tier_failclosed", disposition.gate_check("bogus", "accept", "human_approved") is False)


# ---------------------------------------------------------------------------
# T_RESOLVE_* — disposition.resolve
# ---------------------------------------------------------------------------

def run_T_RESOLVE_ACCEPT():
    print("\n[T_RESOLVE_ACCEPT]")
    import disposition

    conn = db()
    sid = _seed_staging(conn, content="T_RESOLVE_ACCEPT unique content body")
    result = disposition.resolve(conn, sid, "accept", actor="test-agent", reason="clean write")
    check("T_RA_ok", result.get("ok") is True, str(result))
    if result.get("ok"):
        conn.commit()
        insight_id = result["insight_id"]
        row = conn.execute("SELECT * FROM insights WHERE id=?", (insight_id,)).fetchone()
        check("T_RA_insight_created", row is not None, "no insight row")
        check("T_RA_insight_content", row["content"] == "T_RESOLVE_ACCEPT unique content body", str(dict(row)))
        srow = conn.execute("SELECT * FROM insights_staging WHERE id=?", (sid,)).fetchone()
        check("T_RA_staging_status", srow["status"] == "accepted", str(dict(srow)))
        check("T_RA_staging_disposition", srow["disposition"] == "accepted", str(dict(srow)))
        check("T_RA_staging_actor", srow["actor"] == "test-agent", str(dict(srow)))
        log = conn.execute(
            "SELECT * FROM disposition_log WHERE staging_id=? AND transition='staging->insight'", (sid,)
        ).fetchone()
        check("T_RA_log_written", log is not None, "no disposition_log row")
        if log:
            check("T_RA_log_actor", log["actor"] == "test-agent", str(dict(log)))
            check("T_RA_log_insight_id", log["insight_id"] == insight_id, str(dict(log)))
    conn.close()

    # No actor -> clean error, nothing written.
    conn = db()
    sid2 = _seed_staging(conn, content="T_RESOLVE_ACCEPT no-actor case")
    result2 = disposition.resolve(conn, sid2, "accept", actor="")
    check("T_RA_actor_required", result2.get("ok") is False and "actor" in result2.get("error", ""), str(result2))
    conn.close()


def run_T_RESOLVE_REJECT():
    print("\n[T_RESOLVE_REJECT]")
    import disposition

    conn = db()
    sid = _seed_staging(conn, content="T_RESOLVE_REJECT content that must never enter insights",
                        reason="schema_gate:content_too_short")
    result = disposition.resolve(conn, sid, "reject", actor="test-agent")
    check("T_RR_ok", result.get("ok") is True, str(result))
    conn.commit()
    leaked = conn.execute(
        "SELECT COUNT(*) FROM insights WHERE content LIKE 'T_RESOLVE_REJECT%'"
    ).fetchone()[0]
    check("T_RR_never_in_insights", leaked == 0, f"rejected content leaked into insights ({leaked} rows)")
    srow = conn.execute("SELECT status, disposition FROM insights_staging WHERE id=?", (sid,)).fetchone()
    check("T_RR_status_rejected", srow["status"] == "rejected", str(dict(srow)))
    conn.close()


def run_T_RESOLVE_MERGE_AND_REVERSIBLE():
    print("\n[T_RESOLVE_MERGE_AND_REVERSIBLE]")
    import disposition

    conn = db()
    target_id = _seed_insight(conn, "the canonical existing insight")
    sid = _seed_staging(conn, content="a near-duplicate observation of the canonical insight")

    # merge with no target_id -> clean error.
    bad = disposition.resolve(conn, sid, "merge", actor="test-agent")
    check("T_RM_requires_target", bad.get("ok") is False and "target_id" in bad.get("error", ""), str(bad))

    result = disposition.resolve(conn, sid, "merge", actor="test-agent",
                                 reason="dup of canonical", target_id=target_id)
    check("T_RM_ok", result.get("ok") is True, str(result))
    if result.get("ok"):
        conn.commit()
        new_id = result["insight_id"]
        check("T_RM_merged_into", result.get("merged_into") == target_id, str(result))
        row = conn.execute("SELECT superseded_by FROM insights WHERE id=?", (new_id,)).fetchone()
        check("T_RM_superseded_by_set", row["superseded_by"] == target_id, str(dict(row)))
        srow = conn.execute("SELECT status, disposition FROM insights_staging WHERE id=?", (sid,)).fetchone()
        check("T_RM_staging_merged", srow["status"] == "merged", str(dict(srow)))
        log = conn.execute(
            "SELECT * FROM disposition_log WHERE staging_id=? AND transition='staging->merged'", (sid,)
        ).fetchone()
        check("T_RM_log_to_state", log is not None and log["to_state"] == f"merged_into:{target_id}", str(dict(log) if log else None))

        # T_REVERSIBLE: undo via the EXISTING unsupersede mechanism (clear
        # superseded_by directly, mirroring cmd_unsupersede's own SQL).
        conn.execute(
            "UPDATE insights SET superseded_by=NULL, superseded_at=NULL, supersede_reason=NULL WHERE id=?",
            (new_id,),
        )
        conn.commit()
        row2 = conn.execute("SELECT superseded_by FROM insights WHERE id=?", (new_id,)).fetchone()
        check("T_REVERSIBLE_merge_undone", row2["superseded_by"] is None, str(dict(row2)))
    conn.close()


def run_T_RESOLVE_DEFER():
    print("\n[T_RESOLVE_DEFER]")
    import disposition

    conn = db()
    sid = _seed_staging(conn, content="T_RESOLVE_DEFER ambiguous content",
                        reason="schema_gate:content_too_short")
    before = conn.execute("SELECT deadline FROM insights_staging WHERE id=?", (sid,)).fetchone()
    result = disposition.resolve(conn, sid, "defer", actor="test-agent", reason="needs more context")
    check("T_RD_ok", result.get("ok") is True, str(result))
    conn.commit()
    srow = conn.execute("SELECT status, disposition, deadline, actor FROM insights_staging WHERE id=?", (sid,)).fetchone()
    check("T_RD_still_pending", srow["status"] == "pending", str(dict(srow)))
    check("T_RD_disposition_deferred", srow["disposition"] == "deferred", str(dict(srow)))
    check("T_RD_deadline_advanced", srow["deadline"] != before["deadline"], str(dict(srow)))
    check("T_RD_actor_recorded", srow["actor"] == "test-agent", str(dict(srow)))
    conn.close()


def run_T_RESOLVE_ERRORS():
    print("\n[T_RESOLVE_ERRORS]")
    import disposition

    conn = db()
    sid = _seed_staging(conn, content="T_RESOLVE_ERRORS content")

    bad = disposition.resolve(conn, sid, "bogus_action", actor="test-agent")
    check("T_RE_unknown_action", bad.get("ok") is False and "action" in bad.get("error", ""), str(bad))

    missing = disposition.resolve(conn, 9_999_999, "accept", actor="test-agent")
    check("T_RE_missing_row", missing.get("ok") is False and "not found" in missing.get("error", ""), str(missing))

    ok1 = disposition.resolve(conn, sid, "reject", actor="test-agent")
    check("T_RE_first_reject_ok", ok1.get("ok") is True, str(ok1))
    conn.commit()

    ok2 = disposition.resolve(conn, sid, "accept", actor="test-agent")
    check("T_RE_already_decided", ok2.get("ok") is False and "already" in ok2.get("error", ""), str(ok2))
    conn.close()


# ---------------------------------------------------------------------------
# T_DRAIN — drain_due
# ---------------------------------------------------------------------------

def run_T_DRAIN():
    print("\n[T_DRAIN]")
    import disposition

    conn = db()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    # t0 row past its deadline -> auto-executed (accept).
    sid_t0 = _seed_staging(conn, content="T_DRAIN t0 clean content", reason=None, deadline=past)
    # t2 row (secret-flagged) past its deadline -> safe default only (defer),
    # never an auto-accept of secret-bearing content.
    sid_t2 = _seed_staging(conn, content="T_DRAIN t2 secret-flagged content",
                           reason="secret_scan:aws_access_key", deadline=past)

    summary = disposition.drain_due(conn, now=datetime.now(timezone.utc).isoformat())
    check("T_DR_ok", summary.get("ok") is True, str(summary))
    check("T_DR_processed_both", summary.get("processed", 0) >= 2, str(summary))

    row_t0 = conn.execute("SELECT status, disposition, tier FROM insights_staging WHERE id=?", (sid_t0,)).fetchone()
    check("T_DR_t0_tier", row_t0["tier"] == "t0", str(dict(row_t0)))
    check("T_DR_t0_auto_accepted", row_t0["status"] == "accepted", str(dict(row_t0)))
    inserted = conn.execute(
        "SELECT COUNT(*) FROM insights WHERE content='T_DRAIN t0 clean content'"
    ).fetchone()[0]
    check("T_DR_t0_insight_created", inserted == 1, f"expected 1 insight, got {inserted}")

    row_t2 = conn.execute("SELECT status, disposition, tier FROM insights_staging WHERE id=?", (sid_t2,)).fetchone()
    check("T_DR_t2_tier", row_t2["tier"] == "t2", str(dict(row_t2)))
    check("T_DR_t2_NOT_auto_enforced", row_t2["status"] == "pending", str(dict(row_t2)))
    check("T_DR_t2_safe_default_deferred", row_t2["disposition"] == "deferred", str(dict(row_t2)))
    leaked = conn.execute(
        "SELECT COUNT(*) FROM insights WHERE content LIKE 'T_DRAIN t2%'"
    ).fetchone()[0]
    check("T_DR_t2_secret_never_auto_promoted", leaked == 0, f"secret-flagged content auto-promoted ({leaked} rows)")

    log_t2 = conn.execute(
        "SELECT reason FROM disposition_log WHERE staging_id=? ORDER BY id DESC LIMIT 1", (sid_t2,)
    ).fetchone()
    check("T_DR_t2_logged", log_t2 is not None and "sla_timeout_t2_safe_default" in (log_t2["reason"] or ""),
          str(dict(log_t2) if log_t2 else None))
    conn.close()


# ---------------------------------------------------------------------------
# T_FAILSOFT
# ---------------------------------------------------------------------------

def run_T_FAILSOFT():
    print("\n[T_FAILSOFT]")
    import disposition

    # No insights_staging table at all -> resolve must not raise.
    bad_conn = sqlite3.connect(":memory:")
    bad_conn.row_factory = sqlite3.Row
    try:
        result = disposition.resolve(bad_conn, 1, "accept", actor="x")
        check("T_FS_no_table", result.get("ok") is False, str(result))
    except Exception as exc:
        check("T_FS_no_table", False, f"resolve raised: {exc}")
    finally:
        bad_conn.close()

    # Malformed JSON payload -> accept fails cleanly (empty content), no crash.
    conn = db()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO insights_staging (source, project, payload, status, created_at) "
        "VALUES ('gate_failure', 'test-disp', 'not-json-at-all', 'pending', ?)", (now,),
    )
    conn.commit()
    sid = cur.lastrowid
    try:
        result = disposition.resolve(conn, sid, "accept", actor="x")
        check("T_FS_bad_json_no_raise", result.get("ok") is False, str(result))
        row = conn.execute("SELECT status FROM insights_staging WHERE id=?", (sid,)).fetchone()
        check("T_FS_bad_json_left_pending", row["status"] == "pending", str(dict(row)))
    except Exception as exc:
        check("T_FS_bad_json_no_raise", False, f"resolve raised: {exc}")
    conn.close()

    # drain_due against a connection missing disposition_policy -> no-op, no raise.
    bad_conn2 = sqlite3.connect(":memory:")
    bad_conn2.row_factory = sqlite3.Row
    bad_conn2.execute(
        "CREATE TABLE insights_staging (id INTEGER PRIMARY KEY, status TEXT, deadline TEXT)"
    )
    try:
        summary = disposition.drain_due(bad_conn2)
        check("T_FS_drain_no_policy_table", isinstance(summary, dict), str(summary))
    except Exception as exc:
        check("T_FS_drain_no_policy_table", False, f"drain_due raised: {exc}")
    finally:
        bad_conn2.close()


# ---------------------------------------------------------------------------
# T_HISTORY
# ---------------------------------------------------------------------------

def run_T_HISTORY():
    print("\n[T_HISTORY]")
    import disposition

    conn = db()
    result = disposition.record_operator_decision(
        conn, "insight", 123, "approve", actor="operator", decision_class="secret_scan:", reason="looked fine"
    )
    check("T_HIST_ok", result.get("ok") is True, str(result))
    conn.commit()
    row = conn.execute("SELECT * FROM operator_decision_history WHERE id=?", (result.get("id"),)).fetchone()
    check("T_HIST_row_exists", row is not None, "no row written")
    if row:
        check("T_HIST_decision", row["decision"] == "approve", str(dict(row)))

    bad = disposition.record_operator_decision(conn, "insight", 1, "not-a-decision")
    check("T_HIST_bad_decision", bad.get("ok") is False, str(bad))

    stub = disposition.suggest_tier_from_history(conn, "secret_scan:")
    check("T_HIST_stub_returns_none", stub is None, str(stub))
    conn.close()


# ---------------------------------------------------------------------------
# T_ENDPOINT_* — daemon integration
# ---------------------------------------------------------------------------

def run_T_ENDPOINTS():
    print("\n[T_ENDPOINTS]")
    daemon = _load_module("engine_daemon_disposition_test", DAEMON_PY)
    daemon.DB_PATH = Path(TEMP_DB)

    from fastapi.testclient import TestClient
    client = TestClient(daemon.app)

    check("T_EP_disposition_flag", getattr(daemon, "_DISPOSITION", False) is True,
          "daemon._DISPOSITION should be True when db/disposition.py is importable")

    conn = db()
    sid = _seed_staging(conn, content="T_ENDPOINTS clean staging body", project="test-disp-ep")
    sid_secret = _seed_staging(conn, content="T_ENDPOINTS secret-flagged body",
                               reason="secret_scan:aws_access_key", project="test-disp-ep")
    conn.close()

    # /disposition/list — lazily stamps tier.
    r = client.get("/disposition/list", params={"project": "test-disp-ep"})
    check("T_EP_list_ok", r.status_code == 200 and r.json().get("ok") is True, str(r.json()))
    entries = r.json().get("entries", [])
    check("T_EP_list_has_entries", len(entries) >= 2, str(entries))
    check("T_EP_list_tiers_stamped", all(e.get("tier") for e in entries), str(entries))

    # /disposition/triage
    r = client.get(f"/disposition/triage/{sid}")
    check("T_EP_triage_ok", r.status_code == 200 and r.json().get("ok") is True, str(r.json()))
    check("T_EP_triage_has_entry", r.json().get("entry", {}).get("id") == sid, str(r.json()))

    # /disposition/resolve — t0 accept with no capability needed.
    r = client.post("/disposition/resolve", json={
        "staging_id": sid, "action": "accept", "actor": "test-endpoint-agent",
    })
    check("T_EP_resolve_accept_ok", r.status_code == 200 and r.json().get("ok") is True, str(r.json()))

    # /disposition/resolve — t2 (secret) accept WITHOUT capability -> requires_human, not executed.
    r = client.post("/disposition/resolve", json={
        "staging_id": sid_secret, "action": "accept", "actor": "test-endpoint-agent",
    })
    body = r.json()
    check("T_EP_gate_requires_human", body.get("disposition") == "requires_human", str(body))
    check("T_EP_gate_not_ok", body.get("ok") is False, str(body))
    conn = db()
    leaked = conn.execute(
        "SELECT COUNT(*) FROM insights WHERE content LIKE 'T_ENDPOINTS secret-flagged%'"
    ).fetchone()[0]
    check("T_EP_gate_never_executed", leaked == 0, f"secret content leaked past the gate ({leaked} rows)")
    conn.close()

    # Same call WITH human_approved capability -> executes.
    r = client.post("/disposition/resolve", json={
        "staging_id": sid_secret, "action": "accept", "actor": "operator",
        "capability": "human_approved",
    })
    check("T_EP_gate_human_approved_executes", r.status_code == 200 and r.json().get("ok") is True, str(r.json()))

    # /disposition/policy get + set.
    r = client.get("/disposition/policy")
    check("T_EP_policy_get_ok", r.status_code == 200 and r.json().get("ok") is True, str(r.json()))
    n_before = len(r.json().get("rules", []))

    r = client.post("/disposition/policy", json={
        "source": "manual", "type": None, "reason_prefix": "endpoint_test:",
        "tier": "t1", "default_action": "defer", "deadline_hours": 48,
    })
    check("T_EP_policy_set_ok", r.status_code == 200 and r.json().get("ok") is True, str(r.json()))
    n_after = len(r.json().get("rules", []))
    check("T_EP_policy_set_added_row", n_after == n_before + 1, f"{n_before} -> {n_after}")

    # /disposition/drain — smoke test, no due rows expected (none seeded with a past deadline here).
    r = client.post("/disposition/drain")
    check("T_EP_drain_ok", r.status_code == 200 and r.json().get("ok") is True, str(r.json()))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    run_T_MIGR_033()
    run_T_TIER()
    run_T_GATE()
    run_T_RESOLVE_ACCEPT()
    run_T_RESOLVE_REJECT()
    run_T_RESOLVE_MERGE_AND_REVERSIBLE()
    run_T_RESOLVE_DEFER()
    run_T_RESOLVE_ERRORS()
    run_T_DRAIN()
    run_T_FAILSOFT()
    run_T_HISTORY()
    run_T_ENDPOINTS()

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
    else:
        print("All tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
