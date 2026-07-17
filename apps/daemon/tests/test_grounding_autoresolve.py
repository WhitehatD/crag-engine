#!/usr/bin/env python3
# coding: utf-8
"""Autonomous Grounding Resolution Engine test suite (migration 028 + A5).

Standalone (no pytest — mirrors test_a3c1_grounding_pipeline.py style).

Covers the architecture the coordinator briefed:
  T_JUDGMENT   junk/mechanical falsifier -> re-authored -> mechanically_unverifiable
               -> flag AUTO-CLEARS + grounding_mode='judgment' + NOT re-swept.
  T_PASS       reground pass verdict -> resolve auto-verifies + clears flag.
  T_CORRECT    reground fail + definitive + low-stakes insight -> auto-correction
               applied + resolution_proposals audit row (auto-applied).
  T_ESCALATE   high-stakes insight / principle / uncertain -> NO mutation,
               resolution_proposals row written (pending), flag non-blocking.
  T_SWEEP      resolution/sweep drains a seeded flagged queue end-to-end
               (flag -> author/reground job enqueued by the sweep).
  T_STALE      falsifier_is_stale heuristic classification.
  T_ENDPOINTS  /ground/proposals, /decide, /ground/resolutions, /revert shapes.

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_grounding_autoresolve.py
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
# Temp DB: schema-copy of live DB (read-only) + migrations 026/027/028
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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="autoresolve-")
    os.close(fd)
    conn = sqlite3.connect(path)
    for s in stmts:
        try:
            conn.execute(s)
        except sqlite3.OperationalError:
            pass  # FTS5 shadow tables
    conn.commit()
    # The live-DB schema copy already carries 026 (grounding v2) + 027 (graph v2)
    # structures — the live DB is at schema v27. We only need to layer 028
    # (autoresolve) on top. If 026 columns are somehow absent (older source),
    # apply 026 too; both are idempotent per-statement.
    for mig in ("026_grounding_v2.sql", "028_grounding_autoresolve.sql",
                "030_grounding_llm_cost.sql"):
        p = MIGRATIONS / mig
        if p.exists():
            _apply_migration(conn, p)
    conn.commit()
    conn.close()
    print(f"temp DB (+028 autoresolve +030 llm-cost): {path}")
    return path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TEMP_DB = build_temp_db()
daemon = load_module("engine_daemon_autoresolve_test", DAEMON_PY)
daemon.DB_PATH = Path(TEMP_DB)

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(daemon.app)  # no lifespan => no model/loops


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(TEMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


def seed_insight(conn, content, project="infra", conf=0.5, grounding_due=0,
                 grounded_at=None) -> int:
    cur = conn.execute(
        """INSERT INTO insights (project, type, content, tags, status, confidence,
                                 grounding_due, grounded_at, created_at, updated_at)
           VALUES (?, 'gotcha', ?, '', 'active', ?, ?, ?,
                   '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00')""",
        (project, content, conf, grounding_due, grounded_at),
    )
    conn.commit()
    return cur.lastrowid


def seed_principle(conn, content, project="infra", conf=0.9, grounding_due=0) -> int:
    cur = conn.execute(
        """INSERT INTO principles (project, content, confidence, grounding_due,
                                   created_at, updated_at)
           VALUES (?, ?, ?, ?, '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00')""",
        (project, content, conf, grounding_due),
    )
    conn.commit()
    return cur.lastrowid


def seed_open_queue(conn, claim_kind, claim_id):
    conn.execute(
        "INSERT INTO grounding_queue (claim_kind, claim_id, reason, trigger_src, detail, status, enqueued_at) "
        "VALUES (?, ?, 'volatile_stale', 'test', '', 'open', '2026-06-01T00:00:00+00:00')",
        (claim_kind, claim_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Stub LLMs
# ---------------------------------------------------------------------------

_UNVERIFIABLE_JSON = json.dumps({
    "unverifiable": True,
    "reason": "This is a meta-principle about agent behaviour with no mechanically checkable entity.",
})
_REGROUND_PASS_JSON = json.dumps({
    "verdict": "pass", "reasoning": "output confirms the claim.", "evidence": "match found",
})
_REGROUND_FAIL_JSON = json.dumps({
    "verdict": "fail", "reasoning": "config now shows 20, not 10.", "evidence": "min=20",
})
_CORRECTION_TEXT = "min_messages_for_downgrade is now 20 in router-config.json"
_STAKES_LOW = "low"
_STAKES_HIGH = "high"
_CREDENTIAL_LEAK_CORRECTION = (
    "the anthropic key is sk-ant-api03-abcdefghijklm"
    "nopqrstuvwxyz0123456789ABCDEFGH "
    "and it is stored in .env"
)


class _FakeContent:
    def __init__(self, text): self.text = text


class _FakeResp:
    def __init__(self, text): self.content = [_FakeContent(text)]


class _SequenceLLM:
    def __init__(self, responses):
        self._r = responses
        self._i = 0
        self._last_kwargs = {}

    @property
    def messages(self): return self

    def create(self, **kwargs):
        self._last_kwargs = kwargs
        r = self._r[min(self._i, len(self._r) - 1)]
        self._i += 1
        return _FakeResp(r)


import grounding_resolve as gr  # noqa: E402
from grounding_queue_v2 import enqueue_job, drain_one_job, append_history  # noqa: E402


# ---------------------------------------------------------------------------
# T_STALE: falsifier_is_stale heuristic
# ---------------------------------------------------------------------------

print("\n== T_STALE: stale/junk falsifier detection ==")

check("T_STALE_a: None falsifier -> stale", gr.falsifier_is_stale("anything", None) is True)


class _Row(dict):
    """dict with .keys() semantics used by falsifier_is_stale (sqlite3.Row-like)."""
    def keys(self): return super().keys()
    def __getitem__(self, k): return super().__getitem__(k)


check("T_STALE_b: kind='none' -> stale",
      gr.falsifier_is_stale("x", _Row(kind="none", tier="A", authored_by="mechanical", recipe=None)) is True)
check("T_STALE_c: tier='judgment' -> NOT stale (settled)",
      gr.falsifier_is_stale("x", _Row(kind="none", tier="judgment", authored_by="llm", recipe=None)) is False)
check("T_STALE_d: mechanical + predicate content -> stale",
      gr.falsifier_is_stale("the rate limiter is OFF by default in config",
                            _Row(kind="path_exists", tier="A", authored_by="mechanical", recipe=None)) is True)
check("T_STALE_e: llm-authored Tier-B -> NOT stale",
      gr.falsifier_is_stale("the rate limiter is OFF",
                            _Row(kind="none", tier="B", authored_by="llm", recipe="{}")) is False)


# ---------------------------------------------------------------------------
# T_JUDGMENT: junk falsifier -> author job -> mechanically_unverifiable
#             -> flag auto-clears + grounding_mode='judgment' + not re-swept
# ---------------------------------------------------------------------------

print("\n== T_JUDGMENT: mechanically_unverifiable auto-clears the flag ==")

conn = db()
j_id = seed_insight(conn, "never confabulate a phantom external actor when reasoning",
                    grounding_due=1)
seed_open_queue(conn, "insight", j_id)
# Junk v1 falsifier: mechanical existence probe force-fit onto a meta claim
conn.execute(
    "INSERT INTO falsifiers (claim_kind, claim_id, kind, spec, derived, tier, authored_by, created_at, updated_at) "
    "VALUES ('insight', ?, 'path_exists', \"test -e 'config.yaml'\", 1, 'A', 'mechanical', "
    "'2026-06-01T00:00:00+00:00','2026-06-01T00:00:00+00:00')",
    (j_id,),
)
enqueue_job(conn, "insight", j_id, "author", priority=10)
conn.commit()
conn.close()

conn = db()
did = drain_one_job(conn, _SequenceLLM([_UNVERIFIABLE_JSON]))
conn.commit()
check("T_JDG_a: author job processed", did is True)
row = conn.execute("SELECT grounding_due, grounding_mode FROM insights WHERE id=?", (j_id,)).fetchone()
check("T_JDG_b: grounding_due auto-cleared to 0", row["grounding_due"] == 0, f"due={row['grounding_due']}")
check("T_JDG_c: grounding_mode='judgment'", row["grounding_mode"] == "judgment", f"mode={row['grounding_mode']}")
fal = conn.execute("SELECT tier FROM falsifiers WHERE claim_kind='insight' AND claim_id=?", (j_id,)).fetchone()
check("T_JDG_d: falsifier tier='judgment'", fal["tier"] == "judgment", f"tier={fal['tier']}")
gqrow = conn.execute(
    "SELECT status, resolution FROM grounding_queue WHERE claim_kind='insight' AND claim_id=?", (j_id,)
).fetchone()
check("T_JDG_e: grounding_queue row resolved='unverifiable-judgment'",
      gqrow["status"] == "resolved" and gqrow["resolution"] == "unverifiable-judgment",
      f"row={dict(gqrow)}")
conn.close()

# Re-sweep must NOT re-enqueue (judgment excluded). Force grounding_due back to
# simulate a stray re-flag, then confirm the sweep still skips a judgment claim.
conn = db()
conn.execute("UPDATE insights SET grounding_due=1 WHERE id=?", (j_id,))
conn.commit()
n_before = conn.execute("SELECT COUNT(*) AS c FROM grounding_jobs WHERE claim_id=? AND status='pending'", (j_id,)).fetchone()["c"]
gr.sweep_flagged_claims(conn, limit=50)
n_after = conn.execute("SELECT COUNT(*) AS c FROM grounding_jobs WHERE claim_id=? AND status='pending'", (j_id,)).fetchone()["c"]
check("T_JDG_f: sweep does NOT enqueue for a judgment claim",
      n_after == n_before, f"before={n_before} after={n_after}")
conn.close()


# ---------------------------------------------------------------------------
# T_PASS: reground pass -> resolve auto-verifies + clears flag
# ---------------------------------------------------------------------------

print("\n== T_PASS: pass verdict -> auto-verify ==")

conn = db()
p_id = seed_insight(conn, "the daemon listens on port 8786", conf=0.5, grounding_due=1)
seed_open_queue(conn, "insight", p_id)
recipe = json.dumps({"steps": ["echo ok"], "refutes_if": "no", "supports_if": "ok"})
conn.execute(
    "INSERT INTO falsifiers (claim_kind, claim_id, kind, tier, authored_by, recipe, recipe_version, "
    "falsification_question, created_at, updated_at) VALUES ('insight', ?, 'none', 'B', 'llm', ?, 1, "
    "'is it bound?', '2026-06-01T00:00:00+00:00','2026-06-01T00:00:00+00:00')",
    (p_id, recipe),
)
enqueue_job(conn, "insight", p_id, "reground", priority=10)
conn.commit()
conn.close()

# reground (pass) then the chained resolve job — drain twice. The resolve
# job's pass branch now ALSO runs the LLM stakes gate for a mechanically-low
# insight (FIX2b two-gate-on-pass) before auto-verifying, so the sequence
# needs a second response for that call.
llm = _SequenceLLM([_REGROUND_PASS_JSON, _STAKES_LOW])
conn = db()
drain_one_job(conn, llm)   # reground -> enqueues resolve
conn.commit()
conn.close()
conn = db()
drain_one_job(conn, llm)   # resolve
conn.commit()
row = conn.execute("SELECT grounding_due, confidence, verified_at FROM insights WHERE id=?", (p_id,)).fetchone()
check("T_PASS_a: grounding_due cleared", row["grounding_due"] == 0, f"due={row['grounding_due']}")
check("T_PASS_b: confidence bumped (>0.5)", row["confidence"] > 0.5, f"conf={row['confidence']}")
check("T_PASS_c: verified_at stamped", bool(row["verified_at"]), f"verified_at={row['verified_at']}")
resolve_hist = conn.execute(
    "SELECT verdict FROM grounding_history WHERE claim_kind='insight' AND claim_id=? AND job_type='resolve'",
    (p_id,),
).fetchone()
check("T_PASS_d: resolve grounding_history row appended", resolve_hist is not None and resolve_hist["verdict"] == "pass",
      f"hist={dict(resolve_hist) if resolve_hist else None}")
prop = conn.execute(
    "SELECT proposed_action, status, auto_applied FROM resolution_proposals WHERE claim_kind='insight' AND claim_id=?",
    (p_id,),
).fetchone()
check("T_PASS_e: auto-applied verify proposal recorded (audit)",
      prop is not None and prop["proposed_action"] == "verify" and prop["auto_applied"] == 1
      and prop["status"] == "auto-applied", f"prop={dict(prop) if prop else None}")
conn.close()


# ---------------------------------------------------------------------------
# T_CORRECT: fail + low-stakes insight + confident correction -> auto-update
# ---------------------------------------------------------------------------

print("\n== T_CORRECT: fail + low-stakes insight -> auto-correction ==")

conn = db()
c_id = seed_insight(conn, "min_messages_for_downgrade is 10 in router-config.json", grounding_due=1)
seed_open_queue(conn, "insight", c_id)
conn.execute(
    "INSERT INTO falsifiers (claim_kind, claim_id, kind, tier, authored_by, recipe, recipe_version, "
    "falsification_question, created_at, updated_at) VALUES ('insight', ?, 'none', 'B', 'llm', ?, 1, "
    "'is it 10?', '2026-06-01T00:00:00+00:00','2026-06-01T00:00:00+00:00')",
    (c_id, recipe),
)
enqueue_job(conn, "insight", c_id, "reground", priority=10)
conn.commit()
conn.close()

# reground returns fail; resolve's FIX2 two-gate first asks the LLM stakes
# question (2nd response), THEN -- only if that says 'low' -- calls
# draft_correction (3rd response).
llm = _SequenceLLM([_REGROUND_FAIL_JSON, _STAKES_LOW, _CORRECTION_TEXT])
conn = db()
drain_one_job(conn, llm)   # reground fail -> enqueue resolve
conn.commit()
conn.close()
conn = db()
drain_one_job(conn, llm)   # resolve -> auto-correct
conn.commit()
row = conn.execute("SELECT content, grounding_due FROM insights WHERE id=?", (c_id,)).fetchone()
check("T_COR_a: content auto-corrected in place", row["content"] == _CORRECTION_TEXT, f"content={row['content']!r}")
check("T_COR_b: grounding_due cleared after correction", row["grounding_due"] == 0, f"due={row['grounding_due']}")
prop = conn.execute(
    "SELECT proposed_action, proposed_content, prior_content, status, auto_applied "
    "FROM resolution_proposals WHERE claim_kind='insight' AND claim_id=?", (c_id,),
).fetchone()
check("T_COR_c: auto-applied update proposal (audit + revert handle)",
      prop is not None and prop["proposed_action"] == "update" and prop["auto_applied"] == 1
      and prop["status"] == "auto-applied", f"prop={dict(prop) if prop else None}")
check("T_COR_d: prior_content preserved for revert",
      prop is not None and "is 10" in (prop["prior_content"] or ""), f"prior={prop['prior_content'] if prop else None!r}")
gqrow = conn.execute(
    "SELECT resolution FROM grounding_queue WHERE claim_kind='insight' AND claim_id=?", (c_id,)
).fetchone()
check("T_COR_e: queue row resolution='auto-corrected'", gqrow["resolution"] == "auto-corrected",
      f"resolution={gqrow['resolution']}")
conn.close()


# ---------------------------------------------------------------------------
# T_ESCALATE: high-stakes insight / principle / uncertain -> proposal, NO mutation
# ---------------------------------------------------------------------------

print("\n== T_ESCALATE: high-stakes / principle / uncertain -> proposal only ==")

# (a) high-stakes insight, fail verdict -> NO mutation, pending proposal
conn = db()
hs_id = seed_insight(conn, "NEVER kill the breathing cord process on the VPS", grounding_due=1)
seed_open_queue(conn, "insight", hs_id)
append_history(conn, "insight", hs_id, "reground", "fail", "cord seems changed", "evidence-x", 1)
enqueue_job(conn, "insight", hs_id, "resolve", priority=10)
conn.commit()
conn.close()

orig = "NEVER kill the breathing cord process on the VPS"
conn = db()
drain_one_job(conn, _SequenceLLM([_CORRECTION_TEXT]))  # correction offered but must be IGNORED
conn.commit()
row = conn.execute("SELECT content, grounding_due FROM insights WHERE id=?", (hs_id,)).fetchone()
check("T_ESC_a: high-stakes insight content NOT mutated", row["content"] == orig, f"content={row['content']!r}")
prop = conn.execute(
    "SELECT status, stakes, proposed_action, auto_applied FROM resolution_proposals "
    "WHERE claim_kind='insight' AND claim_id=?", (hs_id,),
).fetchone()
check("T_ESC_b: pending proposal written for high-stakes fail",
      prop is not None and prop["status"] == "pending" and prop["stakes"] == "high"
      and prop["auto_applied"] == 0, f"prop={dict(prop) if prop else None}")
check("T_ESC_c: high-stakes flag left OPEN (non-blocking note)", row["grounding_due"] == 1,
      f"due={row['grounding_due']}")
conn.close()

# (b) principle, fail verdict -> NEVER auto-mutated even if low-stakes wording
conn = db()
pr_id = seed_principle(conn, "the model router downgrades after 8 messages", grounding_due=1)
seed_open_queue(conn, "principle", pr_id)
append_history(conn, "principle", pr_id, "reground", "fail", "now 12 messages", "evidence-y", 1)
enqueue_job(conn, "principle", pr_id, "resolve", priority=10)
conn.commit()
conn.close()

conn = db()
drain_one_job(conn, _SequenceLLM([_CORRECTION_TEXT]))
conn.commit()
row = conn.execute("SELECT content FROM principles WHERE id=?", (pr_id,)).fetchone()
check("T_ESC_d: principle content NOT auto-mutated", "8 messages" in row["content"], f"content={row['content']!r}")
prop = conn.execute(
    "SELECT status, stakes FROM resolution_proposals WHERE claim_kind='principle' AND claim_id=?", (pr_id,),
).fetchone()
check("T_ESC_e: principle -> pending proposal, stakes=high", prop is not None and prop["status"] == "pending"
      and prop["stakes"] == "high", f"prop={dict(prop) if prop else None}")
conn.close()

# (c) uncertain verdict -> proposal proposed_action='dismiss', no mutation
conn = db()
un_id = seed_insight(conn, "the CI cache hit rate is around forty percent", grounding_due=1)
append_history(conn, "insight", un_id, "reground", "uncertain", "steps errored", "", 1)
enqueue_job(conn, "insight", un_id, "resolve", priority=10)
conn.commit()
conn.close()

conn = db()
drain_one_job(conn, _SequenceLLM([_CORRECTION_TEXT]))
conn.commit()
prop = conn.execute(
    "SELECT status, proposed_action FROM resolution_proposals WHERE claim_kind='insight' AND claim_id=?", (un_id,),
).fetchone()
check("T_ESC_f: uncertain -> pending proposal proposed_action='dismiss'",
      prop is not None and prop["status"] == "pending" and prop["proposed_action"] == "dismiss",
      f"prop={dict(prop) if prop else None}")
conn.close()


# ---------------------------------------------------------------------------
# T_SWEEP: sweep drains a seeded flagged queue end-to-end
# ---------------------------------------------------------------------------

print("\n== T_SWEEP: backlog sweep enqueues jobs for flagged claims ==")

conn = db()
# stale-falsifier claim -> should get an 'author' job
s1 = seed_insight(conn, "the rate limiter is OFF by default in the config", grounding_due=1)
conn.execute(
    "INSERT INTO falsifiers (claim_kind, claim_id, kind, tier, authored_by, created_at, updated_at) "
    "VALUES ('insight', ?, 'path_exists', 'A', 'mechanical', '2026-06-01T00:00:00+00:00','2026-06-01T00:00:00+00:00')",
    (s1,),
)
# valid-recipe claim -> should get a 'reground' job
s2 = seed_insight(conn, "some benign topology claim about host 203.0.113.10", grounding_due=1)
conn.execute(
    "INSERT INTO falsifiers (claim_kind, claim_id, kind, tier, authored_by, recipe, recipe_version, "
    "created_at, updated_at) VALUES ('insight', ?, 'none', 'B', 'llm', ?, 1, "
    "'2026-06-01T00:00:00+00:00','2026-06-01T00:00:00+00:00')",
    (s2, recipe),
)
conn.commit()
n = gr.sweep_flagged_claims(conn, limit=100)
check("T_SWP_a: sweep enqueued >=2 jobs", n >= 2, f"n={n}")
j1 = conn.execute("SELECT job_type FROM grounding_jobs WHERE claim_id=? AND status='pending'", (s1,)).fetchone()
check("T_SWP_b: stale-falsifier claim -> 'author' job", j1 is not None and j1["job_type"] == "author",
      f"job={dict(j1) if j1 else None}")
j2 = conn.execute("SELECT job_type FROM grounding_jobs WHERE claim_id=? AND status='pending'", (s2,)).fetchone()
check("T_SWP_c: valid-recipe claim -> 'reground' job", j2 is not None and j2["job_type"] == "reground",
      f"job={dict(j2) if j2 else None}")
# idempotency: second sweep enqueues nothing new (dedup)
n2 = gr.sweep_flagged_claims(conn, limit=100)
check("T_SWP_d: second sweep is idempotent (0 new)", n2 == 0, f"n2={n2}")
conn.close()


# ---------------------------------------------------------------------------
# T_ENDPOINTS: proposals / decide / resolutions / revert HTTP surface
# ---------------------------------------------------------------------------

print("\n== T_ENDPOINTS: resolution HTTP surface ==")

r = client.get("/ground/proposals?status=pending")
check("T_EP_a: GET /ground/proposals 200", r.status_code == 200, f"status={r.status_code}")
body = r.json()
check("T_EP_b: proposals list present", isinstance(body.get("proposals"), list), f"keys={list(body)}")

# decide (approve) the high-stakes pending proposal from T_ESCALATE(a)
conn = db()
pend = conn.execute(
    "SELECT id, claim_id FROM resolution_proposals WHERE claim_kind='insight' AND status='pending' "
    "ORDER BY id LIMIT 1"
).fetchone()
conn.close()
check("T_EP_c: a pending proposal exists to decide", pend is not None, "no pending proposal")
if pend:
    rd = client.post(f"/ground/proposals/{pend['id']}/decide", json={"decision": "approve", "decided_by": "tester"})
    check("T_EP_d: POST decide approve 200", rd.status_code == 200, f"status={rd.status_code} body={rd.text[:200]}")
    check("T_EP_e: decide returns approved", rd.json().get("status") == "approved", f"body={rd.json()}")
    # re-deciding a decided proposal -> 409
    rd2 = client.post(f"/ground/proposals/{pend['id']}/decide", json={"decision": "reject"})
    check("T_EP_f: re-decide decided proposal -> 409", rd2.status_code == 409, f"status={rd2.status_code}")

# invalid decision -> 422
rbad = client.post("/ground/proposals/999999/decide", json={"decision": "maybe"})
check("T_EP_g: invalid decision -> 422", rbad.status_code == 422, f"status={rbad.status_code}")

# resolutions list (auto_applied rows from T_PASS / T_CORRECT)
rr = client.get("/ground/resolutions")
check("T_EP_h: GET /ground/resolutions 200", rr.status_code == 200, f"status={rr.status_code}")
rr_body = rr.json()
check("T_EP_i: resolutions list has >=2 auto-applied rows",
      isinstance(rr_body.get("resolutions"), list) and rr_body.get("count", 0) >= 2,
      f"count={rr_body.get('count')}")

# revert an auto-applied update resolution (the T_CORRECT one)
conn = db()
auto_upd = conn.execute(
    "SELECT id, claim_id, prior_content FROM resolution_proposals "
    "WHERE proposed_action='update' AND auto_applied=1 AND status='auto-applied' ORDER BY id LIMIT 1"
).fetchone()
conn.close()
check("T_EP_j: an auto-applied update resolution exists", auto_upd is not None, "none found")
if auto_upd:
    rv = client.post(f"/ground/resolutions/{auto_upd['id']}/revert")
    check("T_EP_k: POST revert 200", rv.status_code == 200, f"status={rv.status_code} body={rv.text[:200]}")
    conn = db()
    reverted = conn.execute("SELECT content, grounding_due FROM insights WHERE id=?", (auto_upd["claim_id"],)).fetchone()
    prop_after = conn.execute("SELECT status FROM resolution_proposals WHERE id=?", (auto_upd["id"],)).fetchone()
    conn.close()
    check("T_EP_l: revert restored prior_content", reverted["content"] == auto_upd["prior_content"],
          f"content={reverted['content']!r}")
    check("T_EP_m: revert re-flagged the claim (grounding_due=1)", reverted["grounding_due"] == 1,
          f"due={reverted['grounding_due']}")
    check("T_EP_n: proposal status='reverted'", prop_after["status"] == "reverted", f"status={prop_after['status']}")
    # double revert -> 409
    rv2 = client.post(f"/ground/resolutions/{auto_upd['id']}/revert")
    check("T_EP_o: double revert -> 409", rv2.status_code == 409, f"status={rv2.status_code}")


# ---------------------------------------------------------------------------
# T_ECON: /ground/economics — Phase 1b (migration 030) config + budget +
# 7-day spend reflection consumed by the dashboard Economics panel.
# ---------------------------------------------------------------------------

print("\n== T_ECON: /ground/economics HTTP surface ==")

conn = db()
conn.execute(
    "INSERT INTO llm_cost_ledger (ts, provider, model, stage, tokens_in, tokens_out, est_cost_usd, quota_type) "
    "VALUES (?, 'anthropic-oauth', 'claude-haiku-4-5-20251001', 'author', 500, 200, 0.0012, 'weekly_subscription')",
    (datetime.now(timezone.utc).isoformat(),),
)
conn.execute(
    "INSERT INTO llm_cost_ledger (ts, provider, model, stage, tokens_in, tokens_out, est_cost_usd, quota_type) "
    "VALUES (?, 'anthropic-oauth', 'claude-haiku-4-5-20251001', 'adjudicate', 300, 100, 0.0006, 'weekly_subscription')",
    (datetime.now(timezone.utc).isoformat(),),
)
conn.commit()
conn.close()

recon = client.get("/ground/economics")
check("T_ECON_a: GET /ground/economics -> 200", recon.status_code == 200, f"status={recon.status_code}")
econ = recon.json()
check("T_ECON_b: ok=True", econ.get("ok") is True, f"body={econ}")
check("T_ECON_c: config block has provider/model", "provider" in econ.get("config", {}) and "model" in econ.get("config", {}),
      f"config={econ.get('config')}")
check("T_ECON_d: budget block has calls_today/exceeded", "calls_today" in econ.get("budget", {}) and "exceeded" in econ.get("budget", {}),
      f"budget={econ.get('budget')}")
check("T_ECON_e: spend_7d reflects the 2 seeded ledger rows", econ.get("spend_7d", {}).get("calls", 0) >= 2,
      f"spend_7d={econ.get('spend_7d')}")
check("T_ECON_f: per_model_7d non-empty and carries est_cost_usd", len(econ.get("per_model_7d", [])) >= 1
      and "est_cost_usd" in econ["per_model_7d"][0], f"per_model_7d={econ.get('per_model_7d')}")
check("T_ECON_g: per_stage_7d includes both author and adjudicate stages",
      {r["stage"] for r in econ.get("per_stage_7d", [])} >= {"author", "adjudicate"},
      f"per_stage_7d={econ.get('per_stage_7d')}")


# ---------------------------------------------------------------------------
# T_FIX1: pass verdict must NOT auto-mutate confidence on a principle or a
# high-stakes insight (the original bug: the pass branch called _apply_verify
# unconditionally). Flag still clears (claim confirmed true); a PENDING verify
# proposal is written so a human can confirm the trust bump.
# ---------------------------------------------------------------------------

print("\n== T_FIX1: pass verdict on principle / high-stakes insight -> NO confidence mutation ==")

# (a) principle + pass verdict
conn = db()
fix1_pr = seed_principle(conn, "the model router downgrades after 8 messages", conf=0.9, grounding_due=1)
seed_open_queue(conn, "principle", fix1_pr)
append_history(conn, "principle", fix1_pr, "reground", "pass", "still 8 messages", "config confirms 8", 1)
enqueue_job(conn, "principle", fix1_pr, "resolve", priority=10)
conn.commit()
conn.close()

conn = db()
drain_one_job(conn, _SequenceLLM([_STAKES_LOW]))   # principle pass path makes NO llm call; response unused
conn.commit()
prow = conn.execute("SELECT confidence, content, grounding_due FROM principles WHERE id=?", (fix1_pr,)).fetchone()
check("T_FIX1_a: principle PASS -> confidence NOT bumped (stays 0.9)",
      abs(prow["confidence"] - 0.9) < 1e-9, f"conf={prow['confidence']}")
check("T_FIX1_b: principle PASS -> content untouched",
      "8 messages" in prow["content"], f"content={prow['content']!r}")
check("T_FIX1_c: principle PASS -> grounding flag CLEARED (claim confirmed true)",
      prow["grounding_due"] == 0, f"due={prow['grounding_due']}")
prop = conn.execute(
    "SELECT status, proposed_action, auto_applied, stakes FROM resolution_proposals "
    "WHERE claim_kind='principle' AND claim_id=?", (fix1_pr,),
).fetchone()
check("T_FIX1_d: principle PASS -> PENDING verify proposal (human confirms bump)",
      prop is not None and prop["status"] == "pending" and prop["proposed_action"] == "verify"
      and prop["auto_applied"] == 0, f"prop={dict(prop) if prop else None}")
gq = conn.execute(
    "SELECT status FROM grounding_queue WHERE claim_kind='principle' AND claim_id=?", (fix1_pr,)
).fetchone()
check("T_FIX1_e: principle PASS -> grounding_queue row resolved",
      gq is not None and gq["status"] == "resolved", f"gq={dict(gq) if gq else None}")
conn.close()

# (b) high-stakes insight + pass verdict
conn = db()
fix1_hs = seed_insight(conn, "NEVER kill the breathing cord process on the VPS", conf=0.6, grounding_due=1)
seed_open_queue(conn, "insight", fix1_hs)
append_history(conn, "insight", fix1_hs, "reground", "pass", "cord still running", "pid present", 1)
enqueue_job(conn, "insight", fix1_hs, "resolve", priority=10)
conn.commit()
conn.close()

conn = db()
drain_one_job(conn, _SequenceLLM([_STAKES_LOW]))   # high-stakes pass path makes NO llm call; response unused
conn.commit()
hrow = conn.execute("SELECT confidence, content, grounding_due FROM insights WHERE id=?", (fix1_hs,)).fetchone()
check("T_FIX1_f: high-stakes insight PASS -> confidence NOT bumped (stays 0.6)",
      abs(hrow["confidence"] - 0.6) < 1e-9, f"conf={hrow['confidence']}")
check("T_FIX1_g: high-stakes insight PASS -> content untouched",
      hrow["content"] == "NEVER kill the breathing cord process on the VPS", f"content={hrow['content']!r}")
check("T_FIX1_h: high-stakes insight PASS -> flag cleared", hrow["grounding_due"] == 0, f"due={hrow['grounding_due']}")
prop = conn.execute(
    "SELECT status, proposed_action, stakes FROM resolution_proposals WHERE claim_kind='insight' AND claim_id=?",
    (fix1_hs,),
).fetchone()
check("T_FIX1_i: high-stakes insight PASS -> PENDING verify proposal, stakes=high",
      prop is not None and prop["status"] == "pending" and prop["proposed_action"] == "verify"
      and prop["stakes"] == "high", f"prop={dict(prop) if prop else None}")
# Prove _apply_verify was NEVER reached: no auto-applied verify row exists for this claim.
auto_verify = conn.execute(
    "SELECT COUNT(*) c FROM resolution_proposals WHERE claim_kind='insight' AND claim_id=? "
    "AND proposed_action='verify' AND auto_applied=1", (fix1_hs,),
).fetchone()
check("T_FIX1_j: _apply_verify NEVER reached for high-stakes insight (0 auto-applied verify rows)",
      auto_verify["c"] == 0, f"count={auto_verify['c']}")
conn.close()


# ---------------------------------------------------------------------------
# T_STAKES: mechanical regex backstop -- the 10 previously-slipping dangerous
# prose claims now classify high-stakes, and the plural/gerund regex bugs are
# fixed. Direct unit tests on gr._is_high_stakes (the mechanical gate).
# ---------------------------------------------------------------------------

print("\n== T_STAKES: mechanical regex -- adversarial prose slips + plural/gerund fixes ==")

_ADVERSARIAL_SLIPS = [
    "wiping the boot disk before reimaging the host",
    "the deploy script leaves the upload bucket world-writable",
    "purging every row from the customer accounts table",
    "powering off the primary database server for maintenance",
    "turning off the perimeter firewall during the migration window",
    "clearing the audit trail table after the incident review",
    "truncating the access log every night at midnight",
    "the config sets chmod 0777 on the shared upload directory",
    "storing the customer export unencrypted on the shared drive",
    "copying the private key to the shared drive as a backup",
]
for i, phrase in enumerate(_ADVERSARIAL_SLIPS):
    check(f"T_STAKES_slip_{i}: {phrase[:38]!r}.. -> high-stakes",
          gr._is_high_stakes(phrase) is True, f"phrase={phrase!r}")

# plural/gerund isolation cases -- each carries NO other high-stakes keyword,
# so a pass here proves the plural/gerund fix specifically (not a coincidental
# match on 'never'/'production'/etc.).
_PLURAL_GERUND = [
    ("store plaintext passwords in the migrations table", "passwords (plural)"),
    ("rotate the api tokens weekly for hygiene", "tokens (plural)"),
    ("the vault holds multiple secrets for the pipeline", "secrets (plural)"),
    ("audit all api keys before the review", "api keys (plural)"),
    ("force-pushing to a shared branch rewrites history", "force-pushing (gerund)"),
]
for i, (phrase, label) in enumerate(_PLURAL_GERUND):
    check(f"T_STAKES_plural_{i}: {label} -> high-stakes",
          gr._is_high_stakes(phrase) is True, f"phrase={phrase!r}")

# Negative control: a genuinely benign claim stays low-stakes (the fixes did
# NOT turn _is_high_stakes into a constant-true function).
check("T_STAKES_neg_a: benign 'daemon listens on port 8786' -> low-stakes",
      gr._is_high_stakes("the daemon listens on port 8786") is False)
check("T_STAKES_neg_b: benign 'CI cache hit rate is ~40 percent' -> low-stakes",
      gr._is_high_stakes("the CI cache hit rate is around forty percent") is False)


# ---------------------------------------------------------------------------
# T_GATE: LLM stakes gate (second, more-important layer). Fail-safe behaviour +
# the override case (mechanical-low but LLM-high -> escalate, no auto-correct).
# ---------------------------------------------------------------------------

print("\n== T_GATE: LLM stakes gate (two-gate policy) ==")

# unit: fail-safe outcomes all resolve to 'high'
check("T_GATE_a: llm=None -> high (fail-safe)", gr._classify_stakes_llm("anything", None) == "high")


class _ExplodingLLM:
    @property
    def messages(self): return self

    def create(self, **kwargs): raise RuntimeError("proxy unreachable")


check("T_GATE_b: LLM error -> high (fail-safe)",
      gr._classify_stakes_llm("anything", _ExplodingLLM()) == "high")
check("T_GATE_c: ambiguous LLM reply -> high (fail-safe)",
      gr._classify_stakes_llm("anything", _SequenceLLM(["maybe, not sure"])) == "high")
check("T_GATE_d: confident 'low' reply -> low", gr._classify_stakes_llm("anything", _SequenceLLM(["low"])) == "low")
check("T_GATE_e: 'HIGH' reply -> high", gr._classify_stakes_llm("anything", _SequenceLLM(["HIGH"])) == "high")

# integration: mechanical-low claim + LLM says HIGH -> escalate, NO auto-correction
conn = db()
gate_id = seed_insight(conn, "the smoke test webhook only pings a public status page", grounding_due=1)
seed_open_queue(conn, "insight", gate_id)
append_history(conn, "insight", gate_id, "reground", "fail", "webhook url changed", "curl 404", 1)
enqueue_job(conn, "insight", gate_id, "resolve", priority=10)
conn.commit()
conn.close()

gate_orig = "the smoke test webhook only pings a public status page"
conn = db()
# 1st resolve LLM call = stakes gate (HIGH); the 2nd (correction) must NEVER be reached.
drain_one_job(conn, _SequenceLLM([_STAKES_HIGH, _CORRECTION_TEXT]))
conn.commit()
grow = conn.execute("SELECT content, grounding_due FROM insights WHERE id=?", (gate_id,)).fetchone()
check("T_GATE_f: mechanical-low but LLM-high -> content NOT auto-corrected",
      grow["content"] == gate_orig, f"content={grow['content']!r}")
prop = conn.execute(
    "SELECT status, stakes, proposed_action, auto_applied FROM resolution_proposals "
    "WHERE claim_kind='insight' AND claim_id=?", (gate_id,),
).fetchone()
check("T_GATE_g: LLM-high override -> PENDING proposal recorded with stakes=high",
      prop is not None and prop["status"] == "pending" and prop["stakes"] == "high"
      and prop["auto_applied"] == 0, f"prop={dict(prop) if prop else None}")
conn.close()

# integration: the SAME two-gate policy must also apply on the PASS branch,
# not just fail (adversarial re-check 2026-07-05 found "deleting the customer
# records permanently" and "rotate the AWS access key" both slip the
# mechanical regex -- a `pass` verdict on either would previously auto-bump
# confidence with no LLM check at all, since the pass branch only consulted
# `_is_high_stakes`). This claim mechanically reads as low-stakes (no listed
# keyword matches) but an LLM gate saying HIGH must block the auto-verify and
# route to a pending proposal instead -- mirroring T_GATE_f/g on the pass path.
conn = db()
gatep_id = seed_insight(conn, "deleting the customer records permanently", conf=0.5, grounding_due=1)
seed_open_queue(conn, "insight", gatep_id)
append_history(conn, "insight", gatep_id, "reground", "pass", "confirmed by source review", "n/a", 1)
enqueue_job(conn, "insight", gatep_id, "resolve", priority=10)
conn.commit()
conn.close()

conn = db()
drain_one_job(conn, _SequenceLLM([_STAKES_HIGH]))   # pass-path stakes gate call
conn.commit()
gatep_row = conn.execute("SELECT confidence FROM insights WHERE id=?", (gatep_id,)).fetchone()
check("T_GATE_PASS_a: mechanical-low but LLM-high on PASS -> confidence NOT bumped",
      abs(gatep_row["confidence"] - 0.5) < 1e-9, f"conf={gatep_row['confidence']}")
gatep_prop = conn.execute(
    "SELECT status, stakes, proposed_action, auto_applied FROM resolution_proposals "
    "WHERE claim_kind='insight' AND claim_id=?", (gatep_id,),
).fetchone()
check("T_GATE_PASS_b: PASS + LLM-high -> PENDING verify proposal, stakes=high, not auto-applied",
      gatep_prop is not None and gatep_prop["status"] == "pending" and gatep_prop["stakes"] == "high"
      and gatep_prop["proposed_action"] == "verify" and gatep_prop["auto_applied"] == 0,
      f"prop={dict(gatep_prop) if gatep_prop else None}")
conn.close()


# ---------------------------------------------------------------------------
# T_REDACT: a credential-shaped string in the LLM correction output is redacted
# BEFORE any write -- raw shape absent from insights.content AND from
# resolution_proposals.proposed_content (closes the insight #2048 leak class).
# ---------------------------------------------------------------------------

print("\n== T_REDACT: credential redaction on drafted corrections ==")

_RAW_KEY = "sk-ant-api03-abcdefghijklm" "nopqrstuvwxyz0123456789ABCDEFGH"

conn = db()
red_id = seed_insight(conn, "the env file stores an old anthropic key for testing", grounding_due=1)
seed_open_queue(conn, "insight", red_id)
append_history(conn, "insight", red_id, "reground", "fail", "key rotated, value changed", "evidence-z", 1)
enqueue_job(conn, "insight", red_id, "resolve", priority=10)
conn.commit()
conn.close()

conn = db()
# resolve: stakes gate 'low' -> draft_correction returns text carrying a raw key.
drain_one_job(conn, _SequenceLLM([_STAKES_LOW, _CREDENTIAL_LEAK_CORRECTION]))
conn.commit()
rrow = conn.execute("SELECT content FROM insights WHERE id=?", (red_id,)).fetchone()
rprop = conn.execute(
    "SELECT proposed_content FROM resolution_proposals WHERE claim_kind='insight' AND claim_id=? "
    "ORDER BY id DESC LIMIT 1", (red_id,),
).fetchone()
conn.close()
check("T_REDACT_a: insights.content has NO raw credential shape",
      _RAW_KEY not in (rrow["content"] or ""), f"content={rrow['content']!r}")
check("T_REDACT_b: insights.content carries [REDACTED-CREDENTIAL] marker",
      "[REDACTED-CREDENTIAL]" in (rrow["content"] or ""), f"content={rrow['content']!r}")
check("T_REDACT_c: resolution_proposals.proposed_content has NO raw credential shape",
      rprop is not None and _RAW_KEY not in (rprop["proposed_content"] or ""),
      f"prop={dict(rprop) if rprop else None}")
check("T_REDACT_d: resolution_proposals.proposed_content carries redaction marker",
      rprop is not None and "[REDACTED-CREDENTIAL]" in (rprop["proposed_content"] or ""),
      f"prop={dict(rprop) if rprop else None}")


# ---------------------------------------------------------------------------
# T_REVERT_CONF: auto-verify a low-stakes insight, then revert -> the prior
# confidence is RESTORED (FIX4, doctrine 5th clause: reversible).
# ---------------------------------------------------------------------------

print("\n== T_REVERT_CONF: auto-verify then revert restores prior confidence ==")

conn = db()
rvc_id = seed_insight(conn, "the daemon health endpoint returns 200 on localhost 8786", conf=0.5, grounding_due=1)
seed_open_queue(conn, "insight", rvc_id)
append_history(conn, "insight", rvc_id, "reground", "pass", "curl returned 200", "HTTP 200", 1)
enqueue_job(conn, "insight", rvc_id, "resolve", priority=10)
conn.commit()
conn.close()

conn = db()
drain_one_job(conn, _SequenceLLM([_STAKES_LOW]))   # low-stakes insight pass -> auto-verify
conn.commit()
after = conn.execute("SELECT confidence FROM insights WHERE id=?", (rvc_id,)).fetchone()
rprop = conn.execute(
    "SELECT id, proposed_action, auto_applied, prior_confidence FROM resolution_proposals "
    "WHERE claim_kind='insight' AND claim_id=?", (rvc_id,),
).fetchone()
conn.close()
check("T_REVERT_CONF_a: low-stakes insight pass -> confidence bumped 0.5 -> 0.6",
      abs(after["confidence"] - 0.6) < 1e-9, f"conf={after['confidence']}")
check("T_REVERT_CONF_b: auto-applied verify proposal stored prior_confidence=0.5",
      rprop is not None and rprop["proposed_action"] == "verify" and rprop["auto_applied"] == 1
      and rprop["prior_confidence"] is not None and abs(rprop["prior_confidence"] - 0.5) < 1e-9,
      f"prop={dict(rprop) if rprop else None}")

rv = client.post(f"/ground/resolutions/{rprop['id']}/revert")
check("T_REVERT_CONF_c: revert endpoint returns 200", rv.status_code == 200, f"status={rv.status_code} body={rv.text}")

conn = db()
restored = conn.execute("SELECT confidence, grounding_due FROM insights WHERE id=?", (rvc_id,)).fetchone()
conn.close()
check("T_REVERT_CONF_d: revert RESTORED prior confidence (0.6 -> 0.5)",
      abs(restored["confidence"] - 0.5) < 1e-9, f"conf={restored['confidence']}")
check("T_REVERT_CONF_e: revert re-flagged the claim (grounding_due=1)",
      restored["grounding_due"] == 1, f"due={restored['grounding_due']}")


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
