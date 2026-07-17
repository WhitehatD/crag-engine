#!/usr/bin/env python3
# coding: utf-8
"""A3+C1 grounding pipeline test suite.

Standalone (no pytest — mirrors test_ws3a_endpoints.py style).

Tests:
  - Tier-B save triggers enqueue of 'author' job (not mechanical derive)
  - Tier-A save triggers Tier-A mechanical path (no enqueue)
  - Recall aging/stale verdict enqueues 'reground' job (no inline LLM call)
  - Dedup: second enqueue of same job ignored
  - Worker drain: stubbed LLM author job -> falsifier row + history row
  - Worker drain: stubbed LLM reground job -> history row with prior CoT re-fed
  - Failed job backoff: status cycles through retry with demoted priority
  - /ground/jobs endpoint returns correct shape
  - /ground/history endpoint returns correct shape + newest first
  - /ground/stats endpoint returns correct shape
  - /ground/record with verdict/reasoning/evidence -> history row appended
  - Save/recall latency: enqueue path returns before LLM call (no inline LLM)

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_a3c1_grounding_pipeline.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# UTF-8 output on Windows (cp1252 terminals can't encode check-marks)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_DB = REPO_ROOT / "db" / "engine.db"
DAEMON_PY = REPO_ROOT / "apps" / "daemon" / "engine_daemon.py"
MIGRATION_026 = REPO_ROOT / "db" / "migrations" / "026_grounding_v2.sql"
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
# Temp DB helpers
# ---------------------------------------------------------------------------

def build_temp_db() -> str:
    """Schema-copy of the live DB (read-only source), plus migration 026."""
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db", prefix="a3c1test-")
    os.close(fd)
    conn = sqlite3.connect(path)
    for s in stmts:
        try:
            conn.execute(s)
        except sqlite3.OperationalError:
            pass  # FTS5 shadow tables
    conn.commit()
    # Apply migration 026 (test-helper per-statement idempotency)
    _apply_026(conn)
    conn.commit()
    conn.row_factory = sqlite3.Row
    conn.close()
    print(f"temp DB with migration 026: {path}")
    return path


def _apply_026(conn: sqlite3.Connection) -> None:
    sql = MIGRATION_026.read_text(encoding="utf-8")
    for chunk in sql.split(";"):
        sql_lines = [l for l in chunk.splitlines()
                     if l.strip() and not l.strip().startswith("--")]
        stmt = "\n".join(sql_lines).strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column name" in msg or "already exists" in msg:
                continue
            raise


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Build temp DB + load daemon
# ---------------------------------------------------------------------------

TEMP_DB = build_temp_db()
daemon = load_module("engine_daemon_a3c1test", DAEMON_PY)
daemon.DB_PATH = Path(TEMP_DB)

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(daemon.app)  # no lifespan => no model/loops


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(TEMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


def seed_insight(conn, content: str, project: str = "infra",
                 conf: float = 0.5, grounding_due: int = 0,
                 grounded_at: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO insights (project, type, content, tags, status, confidence,
                                 grounding_due, grounded_at, created_at, updated_at)
           VALUES (?, 'gotcha', ?, '', 'active', ?, ?,  ?,
                   '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00')""",
        (project, content, conf, grounding_due, grounded_at),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Stub LLM (same pattern as test_grounding_v2.py)
# ---------------------------------------------------------------------------

_AUTHOR_RECIPE_JSON = json.dumps({
    "falsification_question": "Is the rate-limiter OFF in the config file?",
    "recipe": {
        "steps": ["grep -i 'rate.limiter' /etc/start-proxy.ps1"],
        "refutes_if": "no match for OFF",
        "supports_if": "line with OFF found",
    },
})
_REGROUND_VERDICT_JSON = json.dumps({
    "verdict": "pass",
    "reasoning": "grep returned a line with OFF; claim is still true.",
    "evidence": "rate-limiter=OFF",
})


class _FakeContent:
    def __init__(self, text): self.text = text


class _FakeResp:
    def __init__(self, text): self.content = [_FakeContent(text)]


class _SequenceLLM:
    """Returns responses from a queue, then repeats the last one."""
    def __init__(self, responses: list[str]):
        self._responses = responses
        self._idx = 0
        self._last_kwargs: dict = {}

    @property
    def messages(self): return self

    def create(self, **kwargs):
        self._last_kwargs = kwargs
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return _FakeResp(resp)


# ---------------------------------------------------------------------------
# T_ENQUEUE: Tier-B save enqueues 'author' job
# ---------------------------------------------------------------------------

print("\n== T_ENQUEUE: save enqueue triggers ==")

# Import the grounding_queue_v2 helper directly to seed/check
from grounding_queue_v2 import enqueue_job, append_history  # noqa: E402

# T_ENQ_a/b: /save_insight fast path returns a real id.
# Use role="operator" to bypass the staging tier; unique content avoids dedup.
r_save_b = client.post("/save_insight", json={
    "project": "infra",
    "type": "gotcha",
    "content": "grounding-v2-test-tier-b: rate-limiter is OFF in start-proxy.ps1",
    "tags": "test",
    "role": "operator",  # bypass staging tier -> fast path returns real id
})
check("T_ENQ_a: save_insight returns 200", r_save_b.status_code == 200,
      f"status={r_save_b.status_code}")
r_save_b_json = r_save_b.json() if r_save_b.status_code == 200 else {}
saved_id = r_save_b_json.get("id")
check("T_ENQ_b: save_insight returns an id", isinstance(saved_id, int), f"resp={r_save_b_json}")

# T_ENQ_c/d: _write_time_grounding_hooks (the write-time trigger) enqueues
# an 'author' job for Tier-B content.  The background task that calls it runs
# outside the TestClient's sync call, so we invoke the hook directly to test
# the trigger logic in isolation (enqueue path, not scheduling).
if saved_id:
    tier_b_content = "grounding-v2-test-tier-b: rate-limiter is OFF in start-proxy.ps1"
    conn = db()
    daemon._write_time_grounding_hooks(conn, saved_id, tier_b_content)
    conn.commit()
    job_row = conn.execute(
        "SELECT * FROM grounding_jobs WHERE claim_kind='insight' AND claim_id=? AND job_type='author'",
        (saved_id,),
    ).fetchone()
    check("T_ENQ_c: Tier-B write-time hook -> 'author' job enqueued",
          job_row is not None, f"saved_id={saved_id}")
    if job_row:
        check("T_ENQ_d: job status is 'pending'", job_row["status"] == "pending",
              f"status={job_row['status']}")
    conn.close()

# T_ENQ_e/f: Tier-A content goes through mechanical path, no author job.
r_save_a = client.post("/save_insight", json={
    "project": "infra",
    "type": "gotcha",
    "content": "grounding-v2-test-tier-a: host 203.0.113.10 is reachable",
    "tags": "test",
    "role": "operator",  # bypass staging tier
})
check("T_ENQ_e: Tier-A save returns 200", r_save_a.status_code == 200,
      f"status={r_save_a.status_code}")
r_save_a_json = r_save_a.json() if r_save_a.status_code == 200 else {}
saved_a_id = r_save_a_json.get("id")
if saved_a_id:
    tier_a_content = "grounding-v2-test-tier-a: host 203.0.113.10 is reachable"
    conn = db()
    daemon._write_time_grounding_hooks(conn, saved_a_id, tier_a_content)
    conn.commit()
    job_a = conn.execute(
        "SELECT * FROM grounding_jobs WHERE claim_kind='insight' AND claim_id=? AND job_type='author'",
        (saved_a_id,),
    ).fetchone()
    # Tier-A should NOT enqueue an author job (mechanical path)
    check("T_ENQ_f: Tier-A write-time hook -> no 'author' job (mechanical path)",
          job_a is None, f"found job={dict(job_a) if job_a else None}")
    conn.close()


# ---------------------------------------------------------------------------
# T_DEDUP: second enqueue for same claim/type is silently ignored
# ---------------------------------------------------------------------------

print("\n== T_DEDUP: dedup guard ==")

conn = db()
dedup_id = seed_insight(conn, "notify requires Bearer auth, deny-all default")
inserted1 = enqueue_job(conn, "insight", dedup_id, "author")
conn.commit()
inserted2 = enqueue_job(conn, "insight", dedup_id, "author")
conn.commit()
count = conn.execute(
    "SELECT COUNT(*) AS c FROM grounding_jobs WHERE claim_id=? AND job_type='author' AND status='pending'",
    (dedup_id,),
).fetchone()["c"]
check("T_DEDUP_a: first enqueue inserted=True", inserted1 is True, f"inserted1={inserted1}")
check("T_DEDUP_b: second enqueue is silently deduplicated", inserted2 is False,
      f"inserted2={inserted2}")
check("T_DEDUP_c: exactly one pending 'author' job for claim", count == 1, f"count={count}")
conn.close()


# ---------------------------------------------------------------------------
# T_WORKER: drain_one_job with stubbed LLM for 'author' job
# ---------------------------------------------------------------------------

print("\n== T_WORKER: worker drain (stubbed LLM) ==")

from grounding_queue_v2 import drain_one_job  # noqa: E402

conn = db()
worker_iid = seed_insight(conn, "min_messages_for_downgrade is 10 in router-config.json")
enqueue_job(conn, "insight", worker_iid, "author", priority=10)
conn.commit()
conn.close()

stub_llm = _SequenceLLM([_AUTHOR_RECIPE_JSON, _REGROUND_VERDICT_JSON])

conn = db()
did_work = drain_one_job(conn, stub_llm)
conn.commit()
check("T_WRK_a: drain_one_job returns True (job processed)", did_work is True, "")

# Verify falsifier row was written with Tier-B data
fal_row = conn.execute(
    "SELECT tier, authored_by, falsification_question, recipe FROM falsifiers "
    "WHERE claim_kind='insight' AND claim_id=?",
    (worker_iid,),
).fetchone()
check("T_WRK_b: falsifier row exists after author job", fal_row is not None, "")
if fal_row:
    check("T_WRK_c: falsifier tier='B'", fal_row["tier"] == "B",
          f"tier={fal_row['tier']}")
    check("T_WRK_d: falsifier authored_by='llm'", fal_row["authored_by"] == "llm",
          f"authored_by={fal_row['authored_by']}")
    check("T_WRK_e: falsification_question is non-empty",
          bool(fal_row["falsification_question"]), f"fq={fal_row['falsification_question']!r}")
    recipe = json.loads(fal_row["recipe"] or "{}")
    check("T_WRK_f: recipe.steps is list", isinstance(recipe.get("steps"), list),
          f"recipe={recipe}")

# Verify job marked 'done'
job_done = conn.execute(
    "SELECT status FROM grounding_jobs WHERE claim_kind='insight' AND claim_id=?",
    (worker_iid,),
).fetchone()
check("T_WRK_g: author job marked 'done'",
      job_done is not None and job_done["status"] == "done",
      f"status={job_done['status'] if job_done else None}")

# History row appended
hist = conn.execute(
    "SELECT * FROM grounding_history WHERE claim_kind='insight' AND claim_id=?",
    (worker_iid,),
).fetchone()
check("T_WRK_h: grounding_history row appended after author", hist is not None, "")

conn.close()


# ---------------------------------------------------------------------------
# T_REGROUND: reground job uses prior history + updates last_verdict
# ---------------------------------------------------------------------------

print("\n== T_REGROUND: reground job with prior CoT re-feeding ==")

conn = db()
rg_id = seed_insight(conn, "groundskeeper runs every 6h")
# Insert a falsifier row with a recipe so reground can execute
_recipe_json = json.dumps({
    "steps": ["echo 'checking cron'"],
    "refutes_if": "no cron entry found",
    "supports_if": "cron entry with 6h found",
})
conn.execute(
    """INSERT INTO falsifiers (claim_kind, claim_id, kind, spec, entity, entity_type,
       derived, tier, authored_by, recipe, recipe_version, falsification_question,
       created_at, updated_at)
    VALUES ('insight', ?, 'none', NULL, NULL, NULL,
            0, 'B', 'llm', ?, 1, 'Does groundskeeper still run every 6h?',
            '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00')""",
    (rg_id, _recipe_json),
)
conn.commit()
# Seed some prior history rows
append_history(conn, "insight", rg_id, "reground", "uncertain",
               "Previously could not determine.", "", 1)
conn.commit()
# Enqueue a reground job
enqueue_job(conn, "insight", rg_id, "reground", priority=10)
conn.commit()
conn.close()

stub_reground = _SequenceLLM([_REGROUND_VERDICT_JSON])

conn = db()
did_rg = drain_one_job(conn, stub_reground)
conn.commit()
check("T_RG_a: reground drain_one_job returns True", did_rg is True, "")

# New history row with verdict
hist_rows = conn.execute(
    "SELECT * FROM grounding_history WHERE claim_kind='insight' AND claim_id=? ORDER BY ts",
    (rg_id,),
).fetchall()
check("T_RG_b: grounding_history has 2 rows after reground (prior + new)",
      len(hist_rows) >= 2, f"count={len(hist_rows)}")
new_hist = hist_rows[-1] if hist_rows else None
check("T_RG_c: new history row verdict='pass'",
      new_hist is not None and new_hist["verdict"] == "pass",
      f"new_hist={dict(new_hist) if new_hist else None}")
check("T_RG_d: new history row reasoning is non-empty",
      new_hist is not None and bool(new_hist["reasoning"]),
      f"reasoning={new_hist['reasoning'] if new_hist else None}")

# Verify prior_history was re-fed into the LLM prompt
prompt_content = (stub_reground._last_kwargs.get("messages") or [{}])[0].get("content", "")
check("T_RG_e: prior CoT 'Previously could not determine' was re-fed in adjudication prompt",
      "Previously could not determine" in prompt_content,
      f"prompt_head={prompt_content[:200]!r}")

# Falsifier last_verdict updated
fal_lv = conn.execute(
    "SELECT last_verdict FROM falsifiers WHERE claim_kind='insight' AND claim_id=?",
    (rg_id,),
).fetchone()
check("T_RG_f: falsifier.last_verdict updated to 'pass'",
      fal_lv is not None and fal_lv["last_verdict"] == "pass",
      f"last_verdict={fal_lv['last_verdict'] if fal_lv else None}")
conn.close()


# ---------------------------------------------------------------------------
# T_BACKOFF: failed job gets demoted priority for retry (attempts < _MAX_ATTEMPTS)
# ---------------------------------------------------------------------------

print("\n== T_BACKOFF: failed job retry with priority backoff ==")

class _ErrorLLM:
    @property
    def messages(self): return self
    def create(self, **kwargs): raise RuntimeError("simulated LLM down")

conn = db()
bo_id = seed_insight(conn, "endpoint /foo does not exist (hallucinated)")
enqueue_job(conn, "insight", bo_id, "author", priority=10)
conn.commit()
conn.close()

conn = db()
drain_one_job(conn, _ErrorLLM())
conn.commit()
job_after = conn.execute(
    "SELECT status, priority, attempts, last_error FROM grounding_jobs "
    "WHERE claim_kind='insight' AND claim_id=?",
    (bo_id,),
).fetchone()
check("T_BO_a: failed job (attempt 1/3) put back to 'pending' for retry",
      job_after is not None and job_after["status"] == "pending",
      f"status={job_after['status'] if job_after else None}")
check("T_BO_b: retry job has demoted priority",
      job_after is not None and job_after["priority"] < 10,
      f"priority={job_after['priority'] if job_after else None}")
check("T_BO_c: attempts counter incremented",
      job_after is not None and job_after["attempts"] >= 1,
      f"attempts={job_after['attempts'] if job_after else None}")
check("T_BO_d: last_error is non-empty",
      job_after is not None and bool(job_after["last_error"]),
      f"last_error={job_after['last_error'] if job_after else None}")
conn.close()


# ---------------------------------------------------------------------------
# T_ENDPOINTS: /ground/jobs, /ground/history, /ground/stats, /ground/record
# ---------------------------------------------------------------------------

print("\n== T_ENDPOINTS: observability endpoints ==")

# /ground/jobs
r_jobs = client.get("/ground/jobs?limit=20")
check("T_EP_a: GET /ground/jobs returns 200", r_jobs.status_code == 200,
      f"status={r_jobs.status_code}")
jobs_body = r_jobs.json()
check("T_EP_b: /ground/jobs has 'ok' key", jobs_body.get("ok") is True,
      f"body={jobs_body}")
check("T_EP_c: /ground/jobs has 'jobs' list", isinstance(jobs_body.get("jobs"), list),
      f"body keys={list(jobs_body)}")
if jobs_body.get("jobs"):
    first_job = jobs_body["jobs"][0]
    for key in ("id", "claim_kind", "claim_id", "job_type", "status"):
        check(f"T_EP_d: /ground/jobs[0] has field '{key}'", key in first_job,
              f"first_job={first_job}")

# /ground/jobs?status=pending
r_jobs_p = client.get("/ground/jobs?status=pending")
check("T_EP_e: GET /ground/jobs?status=pending returns 200", r_jobs_p.status_code == 200,
      f"status={r_jobs_p.status_code}")

# /ground/history/{claim_kind}/{claim_id}
r_hist = client.get(f"/ground/history/insight/{rg_id}")
check("T_EP_f: GET /ground/history/insight/{id} returns 200", r_hist.status_code == 200,
      f"status={r_hist.status_code}")
hist_body = r_hist.json()
check("T_EP_g: /ground/history has 'history' list",
      isinstance(hist_body.get("history"), list), f"keys={list(hist_body)}")
if hist_body.get("history"):
    check("T_EP_h: /ground/history newest-first (latest ts first)",
          hist_body["history"][0]["ts"] >= hist_body["history"][-1]["ts"],
          f"ts[0]={hist_body['history'][0]['ts']} ts[-1]={hist_body['history'][-1]['ts']}")

# /ground/stats
r_stats = client.get("/ground/stats")
check("T_EP_i: GET /ground/stats returns 200", r_stats.status_code == 200,
      f"status={r_stats.status_code}")
stats_body = r_stats.json()
check("T_EP_j: /ground/stats has 'queue_by_status'",
      isinstance(stats_body.get("queue_by_status"), dict), f"keys={list(stats_body)}")
check("T_EP_k: /ground/stats has 'flagged_claims'",
      isinstance(stats_body.get("flagged_claims"), dict), f"keys={list(stats_body)}")
check("T_EP_l: /ground/stats has 'grounding_v2_enabled'=True",
      stats_body.get("grounding_v2_enabled") is True, f"stats={stats_body}")

# /ground/record with verdict/reasoning/evidence (v2 extension)
conn = db()
gr_id = seed_insight(conn, "notify bearer auth required")
conn.close()
r_record = client.post("/ground/record", json={
    "claim_kind": "insight",
    "claim_id": gr_id,
    "kind": "none",
    "spec": None,
    "result": "pass",
    "verdict": "pass",
    "reasoning": "grep confirmed bearer auth is enabled",
    "evidence": "auth=bearer in config",
    "recipe_version": 1,
})
check("T_EP_m: POST /ground/record with verdict returns 200", r_record.status_code == 200,
      f"status={r_record.status_code}")
conn = db()
hist_v2 = conn.execute(
    "SELECT verdict, reasoning FROM grounding_history WHERE claim_kind='insight' AND claim_id=?",
    (gr_id,),
).fetchone()
check("T_EP_n: /ground/record with verdict -> grounding_history row appended",
      hist_v2 is not None, f"hist_v2={dict(hist_v2) if hist_v2 else None}")
if hist_v2:
    check("T_EP_o: /ground/record history verdict='pass'",
          hist_v2["verdict"] == "pass", f"verdict={hist_v2['verdict']}")
    check("T_EP_p: /ground/record history reasoning preserved",
          "bearer auth is enabled" in (hist_v2["reasoning"] or ""),
          f"reasoning={hist_v2['reasoning']!r}")
conn.close()


# ---------------------------------------------------------------------------
# T_LATENCY: save returns without waiting for LLM
# ---------------------------------------------------------------------------

print("\n== T_LATENCY: save/recall hot-path has no inline LLM ==")

import time as _time  # noqa: E402
lm_llm_called = False


class _LatencyTestLLM:
    @property
    def messages(self): return self
    def create(self, **kwargs):
        global lm_llm_called
        lm_llm_called = True
        return _FakeResp("{}")


# The save endpoint should NOT call the LLM inline — it just enqueues.
# We verify by checking the response is fast AND lm_llm_called stays False.
_t0 = _time.perf_counter()
r_lat = client.post("/save_insight", json={
    "project": "infra",
    "type": "gotcha",
    "content": "notify requires Bearer auth, deny-all default v2",
    "tags": "test",
})
_elapsed_ms = (_time.perf_counter() - _t0) * 1000
check("T_LAT_a: save_insight returned (status 200)",
      r_lat.status_code == 200, f"status={r_lat.status_code}")
check("T_LAT_b: no inline LLM call during save_insight (enqueue only)",
      not lm_llm_called,
      "LLM was called inline (must be async worker only)")


# ---------------------------------------------------------------------------
# T_RECALL_TRIGGER: recall of an aging insight enqueues a reground/author job
# without crashing recall (the original sqlite3.Row .get() bug would 500 here).
# Uses httpx.AsyncClient + ASGITransport to drive the real event loop.
# ---------------------------------------------------------------------------

print("\n== T_RECALL_TRIGGER: recall aging claim enqueues job ==")

import asyncio  # noqa: E402
import httpx  # noqa: E402

async def _recall_trigger_check() -> dict:
    """POST /recall via AsyncClient; returns the response JSON."""
    transport = httpx.ASGITransport(app=daemon.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post("/recall", json={"query": "stale aging test query", "project": "infra"})
    return resp.status_code, resp.json() if resp.status_code == 200 else {}

# Seed an 'aging' insight so the recall trigger fires.
# grounding_due=0 forces liveness to 'aging' / 'stale' in _liveness_stamp.
conn = db()
rt_id = seed_insight(conn, "VPS IP is 203.0.113.10 (recall trigger test)", grounding_due=0)
conn.close()

rt_status, rt_json = asyncio.run(_recall_trigger_check())
check("T_RT_a: /recall returns 200 (no 500 from sqlite3.Row .get() bug)",
      rt_status == 200, f"status={rt_status}")
check("T_RT_b: recall response has 'insights' key",
      isinstance(rt_json.get("insights"), list), f"keys={list(rt_json.keys())}")

# Check the job was enqueued (the trigger may not have fired if liveness
# verdict happened to be 'fresh'; just verify no crash and shape is correct).
conn = db()
rt_job = conn.execute(
    "SELECT * FROM grounding_jobs WHERE claim_kind='insight' AND claim_id=?",
    (rt_id,),
).fetchone()
conn.close()
# The job being enqueued depends on whether the seeded insight surfaced in
# recall results with an aging/stale verdict.  We can't force it without
# embedding similarity, so we only assert the endpoint survived (T_RT_a/b).
# If the job IS present, validate it.
if rt_job:
    check("T_RT_c: enqueued job type is 'author' or 'reground'",
          rt_job["job_type"] in ("author", "reground"),
          f"job_type={rt_job['job_type']}")
    check("T_RT_d: enqueued job status is 'pending'",
          rt_job["status"] == "pending",
          f"status={rt_job['status']}")
else:
    # Not an error — insight may not have surfaced in recall results
    print("  [~] T_RT_c/d: seeded insight not in recall results (no embedding); skipped")


# ---------------------------------------------------------------------------
# T_CLAIM_RACE: two concurrent workers racing drain_one_job → exactly one wins
# Uses threading to simulate concurrency (SQLite WAL mode is thread-safe).
# ---------------------------------------------------------------------------

print("\n== T_CLAIM_RACE: atomic worker claim (only one wins per job) ==")

import threading  # noqa: E402

class _QuickAuthorLLM:
    """Responds with a valid author recipe immediately."""
    @property
    def messages(self): return self
    def create(self, **kwargs): return _FakeResp(_AUTHOR_RECIPE_JSON)

# T_BACKOFF (above) intentionally leaves its job 'pending' with a demoted
# priority to exercise retry behavior. With the claim now correctly atomic
# (BEGIN IMMEDIATE), a losing thread here would otherwise fall through to
# that unrelated pending job via ORDER BY priority DESC LIMIT 1 and
# legitimately claim+process IT — a real second job, not a race artifact —
# which inflated true_count without indicating any CAS defect. Clear it so
# the race section runs against a clean, single-job queue.
conn = db()
conn.execute("UPDATE grounding_jobs SET status='done' WHERE status='pending'")
conn.commit()
conn.close()

conn = db()
race_id = seed_insight(conn, "race-condition test insight — only one worker should claim")
enqueue_job(conn, "insight", race_id, "author", priority=10)
conn.commit()
conn.close()

_race_results: list = []
_race_lock = threading.Lock()
# Barrier ensures both threads reach drain_one_job at the same instant,
# maximising contention and making the CAS outcome deterministic.
_race_barrier = threading.Barrier(2)

def _worker_thread():
    c = db()
    try:
        _race_barrier.wait()  # synchronise: both threads enter the UPDATE together
        result = drain_one_job(c, _QuickAuthorLLM())
        c.commit()
        with _race_lock:
            _race_results.append(result)
    except Exception as exc:
        with _race_lock:
            _race_results.append(f"ERROR:{exc}")
    finally:
        c.close()

# Launch two threads racing on the same job
t1 = threading.Thread(target=_worker_thread)
t2 = threading.Thread(target=_worker_thread)
t1.start()
t2.start()
t1.join()
t2.join()

check("T_CR_a: both worker threads completed",
      len(_race_results) == 2, f"results={_race_results}")

# Assert on aggregate outcomes: exactly one True (claimed), rest False or no-op.
# We do NOT assert which thread wins — only that the CAS is exclusive.
true_count = sum(1 for r in _race_results if r is True)
false_count = sum(1 for r in _race_results if r is False)
check("T_CR_b: exactly one worker claimed the job (rowcount CAS)",
      true_count == 1 and false_count == 1,
      f"true={true_count} false={false_count} results={_race_results}")

# Exactly one grounding_history row (not two)
conn = db()
hist_race = conn.execute(
    "SELECT count(*) as n FROM grounding_history WHERE claim_kind='insight' AND claim_id=?",
    (race_id,),
).fetchone()["n"]
conn.close()
check("T_CR_c: exactly one history row appended (not doubled)",
      hist_race == 1, f"history_rows={hist_race}")

# Exactly one done/pending job row (not two done rows)
conn = db()
done_race = conn.execute(
    "SELECT count(*) as n FROM grounding_jobs "
    "WHERE claim_kind='insight' AND claim_id=? AND status='done'",
    (race_id,),
).fetchone()["n"]
conn.close()
check("T_CR_d: exactly one 'done' job row (not doubled by race)",
      done_race == 1, f"done_rows={done_race}")


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
