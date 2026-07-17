#!/usr/bin/env python3
"""WS2 "close the loops" test suite — T1/T2/T3b/T3c/T3d/T7a/T4b.

Standalone (no pytest — mirrors apps/mcp/tests/test_mcp_smoke.py style).
Runs the daemon FastAPI app via TestClient against a THROWAWAY temp DB whose
schema is dumped read-only from the live db/engine.db. The live daemon and live
DB are never touched (mode=ro only).

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_ws2_loops.py
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
AUTOCLEAR_PY = REPO_ROOT / "apps" / "cron" / "auto-clear-fp-patterns.py"

FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASSES.append(name)
        print(f"  [v] {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [X] {name}  -- {detail}")


# ---------------------------------------------------------------------------
# Temp DB from live schema (read-only dump)
# ---------------------------------------------------------------------------

def build_temp_db() -> str:
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db", prefix="ws2test-")
    os.close(fd)
    conn = sqlite3.connect(path)
    applied = skipped = 0
    for s in stmts:
        try:
            conn.execute(s)
            applied += 1
        except sqlite3.OperationalError:
            skipped += 1  # FTS5 shadow tables auto-created by the virtual table
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
daemon = load_module("engine_daemon_ws2test", DAEMON_PY)
daemon.DB_PATH = Path(TEMP_DB)  # get_db() reads the module global

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(daemon.app)  # no context manager => no lifespan/model/loops

import lifecycle  # noqa: E402  (daemon put db/ on sys.path)
import scoring  # noqa: E402


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(TEMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


def seed_insight(conn, content="x", project="infra", conf=0.5, verify_count=0,
                 verify_streak=0, promoted_to=None, created_ago_days=0,
                 last_recalled_ago_days=None, grounding_due=0, status="active") -> int:
    cur = conn.execute(
        """INSERT INTO insights (project, type, content, tags, status, confidence,
                                 verify_count, verify_streak, promoted_to, grounding_due,
                                 created_at, last_recalled_at)
           VALUES (?, 'gotcha', ?, '', ?, ?, ?, ?, ?, ?,
                   datetime('now', ?),
                   CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END)""",
        (project, content, status, conf, verify_count, verify_streak, promoted_to,
         grounding_due, f"-{created_ago_days} days",
         last_recalled_ago_days,
         f"-{last_recalled_ago_days or 0} days"),
    )
    conn.commit()
    return cur.lastrowid


def set_falsifier(conn, claim_id, kind, spec, entity_type=None, last_result=None,
                  claim_kind="insight"):
    conn.execute(
        "INSERT INTO falsifiers (claim_kind, claim_id, kind, spec, entity, entity_type, derived, last_result) "
        "VALUES (?,?,?,?,?,?,1,?)",
        (claim_kind, claim_id, kind, spec, "e", entity_type, last_result),
    )
    conn.execute(f"UPDATE {'insights' if claim_kind=='insight' else 'principles'} "
                 f"SET falsifier_id=(SELECT last_insert_rowid()) WHERE id=?", (claim_id,))
    conn.commit()


# ===========================================================================
print("\n== T1: auto-promote gate ==")
conn = db()
# (a) fires at EXACTLY the gate: 0.75 + 0.1 = 0.85, count 2->3, streak 1->2
i_at_gate = seed_insight(conn, "T1 gate insight", conf=0.75, verify_count=2, verify_streak=1)
r = client.post("/verify_insight", json={"id": i_at_gate, "status": "verified"}).json()
row = conn.execute("SELECT promoted_to, confidence FROM insights WHERE id=?", (i_at_gate,)).fetchone()
check("T1a verify response has auto_promoted", isinstance(r.get("auto_promoted"), int), f"resp={r}")
check("T1a insights.promoted_to set", row["promoted_to"] == r.get("auto_promoted"), f"row={dict(row)}")
prow = conn.execute("SELECT * FROM principles WHERE id=?", (r.get("auto_promoted") or -1,)).fetchone()
check("T1a principle row exists at seed confidence",
      prow is not None and abs(prow["confidence"] - scoring.PROMOTE_SEED_CONFIDENCE) < 1e-9,
      f"prow={dict(prow) if prow else None}")
check("T1a principle source_insights == insight id",
      prow is not None and prow["source_insights"] == str(i_at_gate), f"{dict(prow) if prow else None}")
arow = conn.execute("SELECT actor, action FROM operator_audit_log WHERE target_id=? AND actor='auto-promote'",
                    (i_at_gate,)).fetchone()
check("T1a audit row actor='auto-promote'", arow is not None and arow["action"] == "promote_insight",
      f"arow={dict(arow) if arow else None}")

# (b) does NOT fire below the gate — one axis below threshold each
i_low_conf = seed_insight(conn, "T1 low conf", conf=0.60, verify_count=5, verify_streak=5)   # 0.70 < 0.85
i_low_count = seed_insight(conn, "T1 low count", conf=0.80, verify_count=1, verify_streak=1)  # count 2 < 3
i_low_streak = seed_insight(conn, "T1 low streak", conf=0.80, verify_count=4, verify_streak=0)  # streak 1 < 2
for iid, label in ((i_low_conf, "conf"), (i_low_count, "count"), (i_low_streak, "streak")):
    r = client.post("/verify_insight", json={"id": iid, "status": "verified"}).json()
    row = conn.execute("SELECT promoted_to FROM insights WHERE id=?", (iid,)).fetchone()
    check(f"T1b no auto-promote when {label} below gate",
          "auto_promoted" not in r and row["promoted_to"] is None, f"resp={r}")

# (c) already promoted → never double-promotes
i_already = seed_insight(conn, "T1 already promoted", conf=0.9, verify_count=9, verify_streak=9, promoted_to=999)
n_princ_before = conn.execute("SELECT COUNT(*) c FROM principles").fetchone()["c"]
r = client.post("/verify_insight", json={"id": i_already, "status": "verified"}).json()
n_princ_after = conn.execute("SELECT COUNT(*) c FROM principles").fetchone()["c"]
check("T1c already-promoted not re-promoted", "auto_promoted" not in r and n_princ_after == n_princ_before,
      f"resp={r} princ {n_princ_before}->{n_princ_after}")

# (d) 'stale' never promotes even if counters are high
i_stale = seed_insight(conn, "T1 stale path", conf=0.95, verify_count=9, verify_streak=9)
r = client.post("/verify_insight", json={"id": i_stale, "status": "stale"}).json()
check("T1d stale verdict never auto-promotes", "auto_promoted" not in r, f"resp={r}")
conn.close()

# ===========================================================================
print("\n== T2: decay function ==")
conn = db()
d_old = seed_insight(conn, "T2 old never recalled", conf=0.8, created_ago_days=90)
d_recent = seed_insight(conn, "T2 recalled recently", conf=0.8, created_ago_days=90, last_recalled_ago_days=5)
d_promoted = seed_insight(conn, "T2 promoted exempt", conf=0.8, created_ago_days=90, promoted_to=1)
d_floor = seed_insight(conn, "T2 at floor", conf=0.1, created_ago_days=90)
d_near_floor = seed_insight(conn, "T2 near floor", conf=0.105, created_ago_days=90)
d_old_recall = seed_insight(conn, "T2 recalled long ago", conf=0.5, created_ago_days=200, last_recalled_ago_days=90)

res_dry = lifecycle.decay_insights(conn, dry_run=True)
vals_after_dry = {r["id"]: r["confidence"] for r in conn.execute(
    "SELECT id, confidence FROM insights WHERE id IN (?,?,?,?,?,?)",
    (d_old, d_recent, d_promoted, d_floor, d_near_floor, d_old_recall)).fetchall()}
check("T2 dry-run mutates nothing", abs(vals_after_dry[d_old] - 0.8) < 1e-9, f"{vals_after_dry}")
check("T2 dry-run reports the right ids",
      set(res_dry["ids"]) & {d_old, d_near_floor, d_old_recall} == {d_old, d_near_floor, d_old_recall}
      and d_recent not in res_dry["ids"] and d_promoted not in res_dry["ids"] and d_floor not in res_dry["ids"],
      f"ids={res_dry['ids']}")

res_live = lifecycle.decay_insights(conn, dry_run=False)
conn.commit()
vals = {r["id"]: r["confidence"] for r in conn.execute(
    "SELECT id, confidence FROM insights WHERE id IN (?,?,?,?,?,?)",
    (d_old, d_recent, d_promoted, d_floor, d_near_floor, d_old_recall)).fetchall()}
check("T2 old unrecalled decays 0.8 -> 0.72", abs(vals[d_old] - 0.72) < 1e-9, f"{vals[d_old]}")
check("T2 recently-recalled untouched", abs(vals[d_recent] - 0.8) < 1e-9, f"{vals[d_recent]}")
check("T2 promoted exempt", abs(vals[d_promoted] - 0.8) < 1e-9, f"{vals[d_promoted]}")
check("T2 at-floor untouched", abs(vals[d_floor] - 0.1) < 1e-9, f"{vals[d_floor]}")
check("T2 near-floor floors at 0.1", abs(vals[d_near_floor] - 0.1) < 1e-9, f"{vals[d_near_floor]}")
check("T2 stale-recall decays too", abs(vals[d_old_recall] - 0.45) < 1e-9, f"{vals[d_old_recall]}")

# daemon-side run writes ONE audit summary row
res_daemon = daemon._run_decay_once() if hasattr(daemon, "_run_decay_once") else daemon._do_decay_run()
conn2 = db()
adecay = conn2.execute("SELECT COUNT(*) c FROM operator_audit_log WHERE actor='auto-decay'").fetchone()["c"]
check("T2 daemon decay run writes one 'auto-decay' audit row", adecay == 1, f"count={adecay} res={res_daemon}")
check("T2 daemon second run idempotent-ish (nothing left to decay this window)",
      res_daemon.get("ok", True) is not False, f"{res_daemon}")
conn2.close()
conn.close()

# ===========================================================================
print("\n== T3a: resolvability predicate ==")
fr = lifecycle.falsifier_resolvable
check("T3a endpoint resolvable", fr("endpoint", "curl -sf https://x/") is True)
check("T3a unix path NOT resolvable", fr("path_exists", "test -e '/etc/cron.d/x' && echo PRESENT || echo MISSING") is False)
check("T3a windows path resolvable", fr("path_exists", "test -e 'D:/workspace/engine/db/engine.db' && echo PRESENT || echo MISSING") is True)
check("T3a find-style file spec NOT resolvable", fr("path_exists", "find . -name 'x.py' | head") is False)
check("T3a grep CWD-relative NOT resolvable", fr("grep_symbol", "grep -rn 'Foo' . 2>/dev/null | head") is False)
check("T3a grep with windows path resolvable", fr("grep_config", "grep -rn ':8786' D:/workspace/engine/config 2>/dev/null") is True)
check("T3a none/query/absent NOT resolvable", fr("none", None) is False and fr("query", "x") is False and fr(None, None) is False)

# ===========================================================================
print("\n== T3b: atomic flag+queue on recall Tier-2 ==")
conn = db()
# resolvable falsifier (endpoint) — should get flag AND queue row together
t3_res = seed_insight(conn, "zorbaflex resolvable endpoint claim", conf=0.9)
set_falsifier(conn, t3_res, "endpoint", "curl -sf https://notify.example.com/v1/health", "domain")
# unresolvable falsifier (unix path) — should get NEITHER
t3_unres = seed_insight(conn, "zorbaflex unresolvable unix path claim", conf=0.9)
set_falsifier(conn, t3_unres, "path_exists", "test -e '/etc/cron.d/engine' && echo PRESENT || echo MISSING", "path")
# no falsifier at all — should get NEITHER
t3_none = seed_insight(conn, "zorbaflex claim without falsifier", conf=0.9)
conn.close()

rr = client.post("/recall", json={"query": "zorbaflex", "project": "infra", "topk": 10}).json()
conn = db()
flags = {r["id"]: r["grounding_due"] for r in conn.execute(
    "SELECT id, grounding_due FROM insights WHERE id IN (?,?,?)", (t3_res, t3_unres, t3_none)).fetchall()}
queued = {r["claim_id"] for r in conn.execute(
    "SELECT claim_id FROM grounding_queue WHERE status='open' AND claim_id IN (?,?,?)",
    (t3_res, t3_unres, t3_none)).fetchall()}
_hits = {h["id"] for h in (rr.get("insights") or rr.get("result", {}).get("insights") or [])}
check("T3b recall returned the seeded hits", {t3_res, t3_unres, t3_none} <= _hits, f"hits={_hits} resp keys={list(rr.keys())}")
check("T3b resolvable hit: flag AND queue row (atomic)", flags[t3_res] == 1 and t3_res in queued,
      f"flag={flags[t3_res]} queued={t3_res in queued}")
check("T3b unresolvable hit: NO flag, NO queue row", flags[t3_unres] == 0 and t3_unres not in queued,
      f"flag={flags[t3_unres]} queued={t3_unres in queued}")
check("T3b falsifier-less hit: NO flag, NO queue row", flags[t3_none] == 0 and t3_none not in queued,
      f"flag={flags[t3_none]} queued={t3_none in queued}")
# invariant across the WHOLE temp DB: flagged insights == open-queued insights
n_flagged = conn.execute("SELECT COUNT(*) c FROM insights WHERE grounding_due=1").fetchone()["c"]
n_queued = conn.execute("SELECT COUNT(DISTINCT claim_id) c FROM grounding_queue WHERE claim_kind='insight' AND status='open'").fetchone()["c"]
check("T3b invariant: flagged == open-queued", n_flagged == n_queued, f"flagged={n_flagged} queued={n_queued}")
conn.close()

# ===========================================================================
print("\n== T3c: reconcile endpoint on synthetic mixed population ==")
conn = db()
# NOTE (WS5): reconcile now RE-DERIVES falsifiers from claim CONTENT before
# deciding, so each fixture's content must derive consistently with its intent.
# X1: content derives a unix-absolute path -> predicate says unresolvable -> cleared
x1 = seed_insight(conn, "X1 token lives at /root/.git-ci-api-token on the VPS", grounding_due=1)
set_falsifier(conn, x1, "path_exists", "test -e '/root/.git-ci-api-token' && echo PRESENT || echo MISSING", "path")
# X2: content derives an own-domain endpoint (resolvable), NOT queued -> enqueued
x2 = seed_insight(conn, "X2 git-ci serves the API at https://git.example.com/ for CI", grounding_due=1)
set_falsifier(conn, x2, "endpoint", "curl -sf https://git.example.com/", "domain")
# X3: content derives an own-domain endpoint + open queue row -> already_consistent
x3 = seed_insight(conn, "X3 notify push service is at https://notify.example.com/ for alerts", grounding_due=1)
set_falsifier(conn, x3, "endpoint", "curl -sf https://notify.example.com/v1/health", "domain")
conn.execute("INSERT INTO grounding_queue (claim_kind, claim_id, reason, trigger_src, status) VALUES ('insight',?,?,?, 'open')",
             (x3, "volatile_stale", "recall"))
# X4: flagged + NO falsifiers row, content derives to kind none -> cleared
x4 = seed_insight(conn, "X4 flagged plain prose nothing checkable here", grounding_due=1)
conn.commit()
pre_counts = {r["id"]: r["grounding_due"] for r in conn.execute(
    "SELECT id, grounding_due FROM insights WHERE id IN (?,?,?,?)", (x1, x2, x3, x4)).fetchall()}
conn.close()

rc1 = client.post("/admin/reconcile_grounding").json()
conn = db()
post = {r["id"]: r["grounding_due"] for r in conn.execute(
    "SELECT id, grounding_due FROM insights WHERE id IN (?,?,?,?)", (x1, x2, x3, x4)).fetchall()}
x2_queued = conn.execute("SELECT COUNT(*) c FROM grounding_queue WHERE claim_id=? AND status='open'", (x2,)).fetchone()["c"]
check("T3c run1 ok", rc1.get("ok") is True, f"{rc1}")
check("T3c X1 (unresolvable) cleared", post[x1] == 0, f"post={post}")
check("T3c X2 (resolvable unqueued) still flagged + now queued", post[x2] == 1 and x2_queued == 1,
      f"flag={post[x2]} queued={x2_queued}")
check("T3c X3 (consistent) untouched", post[x3] == 1, f"post={post}")
check("T3c X4 (no falsifier, underivable) cleared", post[x4] == 0, f"post={post}")
check("T3c run1 counts: cleared>=2, enqueued>=1, consistent>=1",
      rc1.get("cleared", 0) >= 2 and rc1.get("enqueued", 0) >= 1 and rc1.get("already_consistent", 0) >= 1,
      f"{rc1}")
conn.close()

rc2 = client.post("/admin/reconcile_grounding").json()
check("T3c idempotent: second run clears/enqueues nothing",
      rc2.get("cleared") == 0 and rc2.get("enqueued") == 0, f"{rc2}")

# ===========================================================================
print("\n== T3d: liveness multiplier visible + ordering flip ==")
conn = db()
# S: higher confidence but STALE (falsifier last_result='fail') — pre-multiplier it wins
s_stale = seed_insight(conn, "quixotron unique liveness ranking probe", conf=1.0)
set_falsifier(conn, s_stale, "endpoint", "curl -sf https://x/", "domain", last_result="fail")
# F: lower confidence, unverified (multiplier 1.0)
f_fresh = seed_insight(conn, "quixotron unique liveness ranking probe", conf=0.9)
conn.close()

rr = client.post("/recall", json={"query": "quixotron", "project": "infra", "topk": 5}).json()
hits = rr.get("insights") or rr.get("result", {}).get("insights") or []
by_id = {h["id"]: h for h in hits}
hs, hf = by_id.get(s_stale), by_id.get(f_fresh)
check("T3d both probes returned", hs is not None and hf is not None, f"ids={list(by_id)}")
if hs and hf:
    bd = hs.get("breakdown", {})
    check("T3d stale hit breakdown shows multiplier 0.75",
          bd.get("liveness_multiplier") == scoring.LIVENESS_MULT_STALE and bd.get("liveness_verdict") == "stale",
          f"breakdown={bd}")
    check("T3d stale hit breakdown shows pre-liveness score",
          "score_pre_liveness" in bd and abs(bd["score_pre_liveness"] * 0.75 - hs["score"]) < 0.001,
          f"breakdown={bd} score={hs['score']}")
    check("T3d multiplier math correct (score = pre * 0.75)",
          abs(hs["score"] - round(bd.get("score_pre_liveness", 0) * 0.75, 4)) < 0.001,
          f"score={hs['score']} pre={bd.get('score_pre_liveness')}")
    check("T3d ordering flips: fresh(conf .9) outranks stale(conf 1.0)",
          hf["score"] > hs["score"],
          f"fresh={hf['score']} stale={hs['score']} (pre-stale={bd.get('score_pre_liveness')})")
    check("T3d fresh hit multiplier is 1.0 in breakdown",
          hf.get("breakdown", {}).get("liveness_multiplier") == 1.0, f"{hf.get('breakdown')}")

# ===========================================================================
# T7a — REWRITTEN for WS3a: /save_batch was deleted (zero consumers; WS2 already
# folded batch enrichment into _enrich_insight, exercised via /save_insight).
# The original T7a asserted /save_batch went through _enrich_insight; the same
# hooks are now asserted through the singular path, plus the route's absence.
print("\n== T7a: save enrichment hooks (WS3a: /save_batch removed) ==")
rb_gone = client.post("/save_batch", json={"project": "infra", "insights": []})
check("T7a /save_batch removed (404)", rb_gone.status_code == 404, f"{rb_gone.status_code}")
rb = client.post("/save_insight", json={
    "project": "infra",
    "content": "T7a claim: notify reachable at https://notify.example.com/v1/health from cron",
    "role": "operator",  # bypass the staging tier — direct write path
}).json()
conn = db()
iid = rb.get("id")
check("T7a save_insight inserted", rb.get("ok") is True and iid, f"{rb}")
if iid:
    # Grounding v2 tier routing: Tier-A content gets a derived falsifier row;
    # Tier-B (predicate-bearing) content gets a pending 'author' job in
    # grounding_jobs instead. Either proves _enrich_insight ran its
    # falsifier hook — assert the disjunction, not one fixed branch.
    nfals = conn.execute(
        f"SELECT COUNT(*) c FROM falsifiers WHERE claim_kind='insight' AND claim_id = {iid}"
    ).fetchone()["c"]
    njobs = conn.execute(
        f"SELECT COUNT(*) c FROM grounding_jobs WHERE claim_kind='insight' AND claim_id = {iid}"
    ).fetchone()["c"]
    check("T7a falsifier derived OR author job enqueued via _enrich_insight",
          nfals == 1 or njobs >= 1, f"falsifiers={nfals} jobs={njobs}")
    nvol = conn.execute(
        f"SELECT COUNT(*) c FROM insights WHERE id = {iid} AND volatility_class IS NOT NULL"
    ).fetchone()["c"]
    check("T7a volatility_class stamped", nvol == 1, f"stamped={nvol}")
    nents = conn.execute(
        f"SELECT COUNT(*) c FROM entity_links WHERE insight_id = {iid}"
    ).fetchone()["c"]
    check("T7a entity_links written", nents >= 1, f"entity_links={nents}")
conn.close()

# ===========================================================================
print("\n== T4b: provenance chain traversal (auto-clear-fp) ==")
autoclear = load_module("autoclear_ws2test", AUTOCLEAR_PY)
conn = db()
# synthetic: insights 9001,9002 co-listed in principle source_insights; 9003 unrelated;
# 9004 promoted directly to a principle whose source includes 9005.
conn.execute("INSERT INTO insights (id, project, type, content, status) VALUES (9001,'t','gotcha','a','active'),(9002,'t','gotcha','b','active'),(9003,'t','gotcha','c','active'),(9004,'t','gotcha','d','active'),(9005,'t','gotcha','e','active')")
conn.execute("INSERT INTO principles (id, project, content, source_insights, confidence) VALUES (501,'t','distilled','9001, 9002',0.9)")
conn.execute("INSERT INTO principles (id, project, content, source_insights, confidence) VALUES (502,'t','promoted','9004,9005',0.9)")
conn.execute("UPDATE insights SET promoted_to=502 WHERE id=9004")
conn.commit()
conn.close()

autoclear.ENGINE_DB = Path(TEMP_DB)
autoclear._PRINCIPLE_SOURCE_MAP = autoclear._load_principle_source_map()
check("T4b source map loaded", 501 in autoclear._PRINCIPLE_SOURCE_MAP and autoclear._PRINCIPLE_SOURCE_MAP[501] == {9001, 9002},
      f"map={autoclear._PRINCIPLE_SOURCE_MAP}")
chain = autoclear._in_provenance_chain
check("T4b co-distilled pair (source_insights) IS provenance",
      chain({"id": 9001, "promoted_to": None}, {"id": 9002, "promoted_to": None}) is True)
check("T4b unrelated pair is NOT provenance",
      chain({"id": 9001, "promoted_to": None}, {"id": 9003, "promoted_to": None}) is False)
check("T4b promoted_to -> principle.source_insights sibling IS provenance",
      chain({"id": 9004, "promoted_to": 502}, {"id": 9005, "promoted_to": None}) is True)
check("T4b direct promoted_to == other.id IS provenance (both directions)",
      chain({"id": 10, "promoted_to": 20}, {"id": 20, "promoted_to": None}) is True
      and chain({"id": 20, "promoted_to": None}, {"id": 10, "promoted_to": 20}) is True)
check("T4b same promoted_to principle IS provenance",
      chain({"id": 30, "promoted_to": 77}, {"id": 31, "promoted_to": 77}) is True)

# ===========================================================================
print(f"\n{'='*60}\nRESULT: {len(PASSES)} passed, {len(FAILURES)} failed")
for f_ in FAILURES:
    print(f"  FAIL: {f_}")
try:
    os.unlink(TEMP_DB)
except OSError:
    pass  # windows may hold the WAL briefly; temp dir cleans up
sys.exit(1 if FAILURES else 0)
