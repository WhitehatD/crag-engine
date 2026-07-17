#!/usr/bin/env python3
# coding: utf-8
"""Grounding transient-LLM resilience test suite (2026-07-16 fix).

Standalone (no pytest — mirrors test_grounding_llm_config.py style).

Root cause under test: the grounding loop re-verifies claims via an LLM
(Haiku/anthropic-oauth by default). Two
transient transport failures used to be recorded AS A VERDICT
({"verdict":"uncertain","reasoning":"LLM call failed: <exc>"}), which re-flagged
the claim so the queue could never drain:
  * 401 authentication_error — OAuth token mid-refresh.
  * 429 rate_limit_error     — shared Headroom bucket throttled.

The fix: llm_client.call_with_retry retries these transient classes and, on
exhaustion, raises the DISTINCT TransientLLMError. The grounding call sites
re-raise it; drain_one_job requeues the job (attempt++, honouring
grounding.max_attempts) WITHOUT writing a grounding_history verdict row.

Covers:
  T_RETRY   401-then-200 -> one retry, succeeds, cached oauth client invalidated
  T_429     persistent 429 -> raises TransientLLMError (NOT an uncertain verdict)
  T_CONN    connection error then 200 -> one retry, succeeds
  T_BADREQ  non-transient 400 -> propagates unchanged (not retried, not Transient)
  T_ADJ     adjudicate re-raises TransientLLMError (no 'uncertain' dict returned)
  T_DRAIN   drain_one_job on TransientLLMError: job left PENDING, NO history row

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_grounding_transient_llm.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_DB = REPO_ROOT / "db" / "engine.db"
MIGRATIONS = REPO_ROOT / "db" / "migrations"
DB_DIR = REPO_ROOT / "db"
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

import anthropic  # noqa: E402
import httpx  # noqa: E402

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
# Real anthropic error factories (so call_with_retry's `except` clauses match)
# ---------------------------------------------------------------------------

_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _auth_error():
    resp = httpx.Response(401, request=_REQ)
    return anthropic.AuthenticationError("auth mid-refresh", response=resp, body=None)


def _rate_error(retry_after: str | None = None):
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    resp = httpx.Response(429, request=_REQ, headers=headers)
    return anthropic.RateLimitError("throttled", response=resp, body=None)


def _conn_error():
    return anthropic.APIConnectionError(message="connreset", request=_REQ)


def _bad_request():
    resp = httpx.Response(400, request=_REQ)
    return anthropic.BadRequestError("temperature deprecated", response=resp, body=None)


# ---------------------------------------------------------------------------
# Fake LLM: raises a scripted sequence of exceptions, then returns a response.
# ---------------------------------------------------------------------------

class _FakeContent:
    def __init__(self, text): self.text = text


class _FakeUsage:
    def __init__(self, inp=10, out=5):
        self.input_tokens = inp
        self.output_tokens = out


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeContent(text)]
        self.usage = _FakeUsage()


class _ScriptedLLM:
    """messages.create(...) raises/returns the next item in `script` per call."""
    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.calls = 0

    @property
    def messages(self): return self

    def create(self, **kwargs):
        self.calls += 1
        item = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item


import llm_client  # noqa: E402

# Silence the real backoff sleeps so the suite runs fast.
_SLEEP_PATCH = mock.patch.object(llm_client.time, "sleep", lambda *_a, **_k: None)
_SLEEP_PATCH.start()


# ===========================================================================
# T_RETRY: 401 then 200 -> succeeds with exactly one retry
# ===========================================================================
print("\n== T_RETRY: 401-then-200 retries once and succeeds ==")

# Seed a fake cached oauth client so we can prove invalidate_oauth_client fired.
llm_client._clients_by_provider["anthropic-oauth"] = object()
llm_client._client_token = "stale-token"

llm_ok = _ScriptedLLM([_auth_error(), _FakeResp("OK")])
# get_client() is called inside call_with_retry after a 401 to rebuild the
# client; patch it to hand back the same scripted stub so the retry proceeds.
with mock.patch.object(llm_client, "get_client", return_value=llm_ok):
    try:
        resp = llm_client.call_with_retry(
            llm_ok, model="m", max_tokens=64,
            messages=[{"role": "user", "content": "x"}],
        )
        got = resp.content[0].text
        check("T_RETRY_a: returns response after one retry", got == "OK", f"got={got!r}")
    except Exception as exc:
        check("T_RETRY_a: returns response after one retry", False, f"raised {exc!r}")

check("T_RETRY_b: exactly two create() calls (fail + success)", llm_ok.calls == 2,
      f"calls={llm_ok.calls}")
check("T_RETRY_c: cached oauth client invalidated on 401",
      llm_client._clients_by_provider.get("anthropic-oauth") is None
      and llm_client._client_token is None,
      f"cache={llm_client._clients_by_provider.get('anthropic-oauth')!r} "
      f"token={llm_client._client_token!r}")


# ===========================================================================
# T_429: persistent 429 -> raises TransientLLMError (not a verdict)
# ===========================================================================
print("\n== T_429: persistent 429 raises TransientLLMError ==")

llm_429 = _ScriptedLLM([_rate_error("0"), _rate_error("0"), _rate_error("0"), _rate_error("0")])
try:
    llm_client.call_with_retry(
        llm_429, model="m", max_tokens=64,
        messages=[{"role": "user", "content": "x"}],
    )
    check("T_429_a: raises on persistent 429", False, "no exception raised")
except llm_client.TransientLLMError as exc:
    check("T_429_a: raises TransientLLMError", True)
    check("T_429_b: status_code=429 on the raised error", exc.status_code == 429,
          f"status_code={exc.status_code}")
except Exception as exc:
    check("T_429_a: raises TransientLLMError", False, f"raised wrong type {type(exc).__name__}")

check("T_429_c: bounded to 3 tries (no infinite loop)", llm_429.calls == 3,
      f"calls={llm_429.calls}")


# ===========================================================================
# T_CONN: connection error then 200 -> one retry, succeeds
# ===========================================================================
print("\n== T_CONN: connection error retries once and succeeds ==")

llm_conn = _ScriptedLLM([_conn_error(), _FakeResp("RECOVERED")])
try:
    resp = llm_client.call_with_retry(
        llm_conn, model="m", max_tokens=64,
        messages=[{"role": "user", "content": "x"}],
    )
    check("T_CONN_a: recovers after one connection-error retry",
          resp.content[0].text == "RECOVERED", f"text={resp.content[0].text!r}")
except Exception as exc:
    check("T_CONN_a: recovers after one connection-error retry", False, f"raised {exc!r}")


# ===========================================================================
# T_BADREQ: a genuine 400 is NOT transient — propagates unchanged
# ===========================================================================
print("\n== T_BADREQ: non-transient 400 propagates (not retried) ==")

llm_400 = _ScriptedLLM([_bad_request(), _FakeResp("shouldnt reach")])
try:
    llm_client.call_with_retry(
        llm_400, model="m", max_tokens=64,
        messages=[{"role": "user", "content": "x"}],
    )
    check("T_BADREQ_a: 400 propagates", False, "no exception raised")
except llm_client.TransientLLMError:
    check("T_BADREQ_a: 400 is NOT wrapped as TransientLLMError", False,
          "wrongly classified transient")
except anthropic.BadRequestError:
    check("T_BADREQ_a: 400 propagates unchanged", True)
except Exception as exc:
    check("T_BADREQ_a: 400 propagates unchanged", False, f"raised {type(exc).__name__}")

check("T_BADREQ_b: 400 not retried (single call)", llm_400.calls == 1,
      f"calls={llm_400.calls}")


# ===========================================================================
# T_ADJ: adjudicate re-raises TransientLLMError, never returns 'uncertain'
# ===========================================================================
print("\n== T_ADJ: adjudicate re-raises TransientLLMError (no uncertain dict) ==")

import grounding_author  # noqa: E402

llm_adj = _ScriptedLLM([_rate_error("0"), _rate_error("0"), _rate_error("0")])
try:
    adj = grounding_author.adjudicate(
        claim="port 8788 is a router service",
        recipe={"steps": [{"cmd": "echo ok"}]},
        step_outputs=["ok"],
        prior_history=[],
        llm=llm_adj,
    )
    # If it returned a dict, the bug is back — a transient failure became a verdict.
    check("T_ADJ_a: does NOT return an uncertain verdict on 429", False,
          f"returned {adj!r} instead of raising")
except llm_client.TransientLLMError:
    check("T_ADJ_a: re-raises TransientLLMError instead of verdict", True)
except Exception as exc:
    check("T_ADJ_a: re-raises TransientLLMError instead of verdict", False,
          f"raised {type(exc).__name__}: {exc}")


# ===========================================================================
# T_DRAIN: drain_one_job on TransientLLMError -> job PENDING, NO history row
# ===========================================================================
print("\n== T_DRAIN: transient failure requeues job, writes no verdict ==")


def _apply_migration(conn, path):
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


def _build_temp_db():
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    stmts = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db", prefix="transient-")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for s in stmts:
        try:
            conn.execute(s)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    for mig in ("026_grounding_v2.sql", "028_grounding_autoresolve.sql",
                "030_grounding_llm_cost.sql"):
        p = MIGRATIONS / mig
        if p.exists():
            _apply_migration(conn, p)
    conn.commit()
    return conn, path


import grounding_queue_v2  # noqa: E402
import grounding_config  # noqa: E402

conn, db_path = _build_temp_db()
try:
    # Seed a claim + a recipe/falsifier so drain_one_job routes to reground
    # (which calls adjudicate -> our scripted 429).
    conn.execute(
        "INSERT INTO insights (id, content, confidence, created_at) "
        "VALUES (9911, 'port 8788 is a router service', 0.5, datetime('now'))"
    )
    conn.execute(
        "INSERT INTO falsifiers (claim_kind, claim_id, kind, spec, tier, recipe, recipe_version) "
        "VALUES ('insight', 9911, 'recipe', '{}', 'B', "
        "'{\"steps\": [{\"cmd\": \"echo ok\", \"expect\": \"ok\"}]}', 1)"
    )
    # Enqueue a reground job for the claim.
    grounding_queue_v2.enqueue_job(conn, "insight", 9911, "reground", priority=5)
    conn.commit()

    history_before = conn.execute(
        "SELECT COUNT(*) c FROM grounding_history WHERE claim_kind='insight' AND claim_id=9911"
    ).fetchone()["c"]

    # An LLM that always 429s -> adjudicate raises TransientLLMError ->
    # drain_one_job requeue path (attempts=1 < max_attempts so PENDING).
    llm_drain = _ScriptedLLM([_rate_error("0")] * 6)

    # run_recipe_steps may try to actually run the echo; patch it to a no-op so
    # the test stays hermetic and the LLM is the only failure surface.
    with mock.patch.object(grounding_queue_v2, "run_recipe_steps",
                           return_value=["ok"]):
        did_work = grounding_queue_v2.drain_one_job(conn, llm_drain)

    check("T_DRAIN_a: drain_one_job returned True (processed a job)", did_work is True,
          f"did_work={did_work!r}")

    job = conn.execute(
        "SELECT status, attempts, last_error FROM grounding_jobs "
        "WHERE claim_kind='insight' AND claim_id=9911 AND job_type='reground' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    cfg = grounding_config.get_config()
    expected = "failed" if (job["attempts"] >= cfg.max_attempts) else "pending"
    check("T_DRAIN_b: job requeued PENDING (not terminal 'done')",
          job["status"] == expected and job["status"] != "done",
          f"status={job['status']} attempts={job['attempts']} max={cfg.max_attempts}")
    check("T_DRAIN_c: last_error names the transient failure",
          "transient" in (job["last_error"] or "").lower(),
          f"last_error={job['last_error']!r}")

    history_after = conn.execute(
        "SELECT COUNT(*) c FROM grounding_history WHERE claim_kind='insight' AND claim_id=9911"
    ).fetchone()["c"]
    check("T_DRAIN_d: NO grounding_history verdict row written on transient failure",
          history_after == history_before,
          f"before={history_before} after={history_after}")

    # And prove the OLD bug would have written an 'uncertain' row: confirm no
    # 'uncertain' verdict exists for this claim.
    uncertain = conn.execute(
        "SELECT COUNT(*) c FROM grounding_history "
        "WHERE claim_kind='insight' AND claim_id=9911 AND verdict='uncertain'"
    ).fetchone()["c"]
    check("T_DRAIN_e: no 'uncertain' verdict row exists for the claim",
          uncertain == 0, f"uncertain_rows={uncertain}")
finally:
    conn.close()
    try:
        os.remove(db_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
_SLEEP_PATCH.stop()

print("\n" + "=" * 60)
print(f"PASS: {len(PASSES)}   FAIL: {len(FAILURES)}")
if FAILURES:
    print("\nFAILURES:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
