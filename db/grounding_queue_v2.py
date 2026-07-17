# coding: utf-8
"""Grounding v2 — durable job queue helpers and async worker logic.

Kept in db/ (alongside grounding_author.py, lifecycle.py) so it can be
imported by the daemon, tests, and future CLI tools alike.

Public API
----------
enqueue_job(conn, claim_kind, claim_id, job_type, priority=0) -> bool
    INSERT OR IGNORE a pending job into grounding_jobs.  Returns True if
    actually inserted, False if deduplicated (already pending).

run_recipe_steps(recipe) -> list[str]
    Execute each step in recipe['steps'] read-only via bash subprocess.
    Returns parallel list of verbatim output strings (empty string on error).
    The _FORBIDDEN guard is checked AGAIN here as defence-in-depth.

fetch_prior_history(conn, claim_kind, claim_id, limit=5) -> list[dict]
    Return the N most recent grounding_history rows for a claim.

append_history(conn, claim_kind, claim_id, job_type, verdict,
               reasoning, evidence, recipe_version) -> None
    Append one row to grounding_history.

drain_one_job(conn, llm) -> bool
    Claim and process the oldest pending job (priority DESC, enqueued_at ASC).
    Returns True if a job was processed, False if queue was empty.
    Fail-open: LLM failure or step error → job marked failed, no crash.

House style: pure functions take an open sqlite3 connection (never open/close).
Timestamps: _utcnow_iso() from lifecycle.py. NEVER SQLite datetime('now').
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("crag-engine")

# Same-process mutual exclusion for the claim critical section. The daemon
# runs all grounding workers as `run_in_executor(None, _drain)` calls inside
# ONE process (a thread-pool, not separate processes) — see engine_daemon.py's
# grounding loop. BEGIN IMMEDIATE below is correct cross-process protection,
# but empirically two threads of the SAME process can occasionally both
# report a successful "BEGIN IMMEDIATE" against the same file on this
# platform's SQLite build (observed intermittently under a synchronized
# threading.Barrier in test_a3c1_grounding_pipeline.py::T_CLAIM_RACE — not
# reproducible when run sequentially or across separate processes). A plain
# in-process lock removes that ambiguity entirely for the case that actually
# matters in production (same-process thread-pool contention) without
# weakening the cross-process guarantee BEGIN IMMEDIATE still provides.
_CLAIM_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Bootstrap: make db/ importable when this module is loaded directly
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from lifecycle import _utcnow_iso  # noqa: E402
import grounding_author  # noqa: E402
import grounding_config  # noqa: E402
import grounding_cost  # noqa: E402
import llm_client  # noqa: E402

# ---------------------------------------------------------------------------
# Write-guard (same as groundskeeper.py:85-89 [Tier-A path, NOT updated by
# this pass] and grounding_author.py — kept in sync with the latter here)
# ---------------------------------------------------------------------------

# Safe redirect forms that must NOT trip the bare '>' guard below — see
# grounding_author.py::_strip_safe_redirects for the full rationale.
_SAFE_REDIRECT_RE = re.compile(
    r"[0-9]?>{1,2}\s*/dev/null"   # 2>/dev/null, >/dev/null, 2>>/dev/null
    r"|&>{1,2}\s*/dev/null"       # &>/dev/null, &>>/dev/null (both streams)
    r"|[0-9]>&[0-9]"              # 2>&1, 1>&2 (fd duplication — no file write)
)


def _strip_safe_redirects(step: str) -> str:
    """Remove fd-to-devnull / fd-dup redirects so they don't trip the bare
    '>' guard. Any '>' surviving this strip is a real file-write redirect."""
    return _SAFE_REDIRECT_RE.sub(" ", step)


_FORBIDDEN = (
    " rm ", " mv ", " cp ", " dd ", ">", ">>", "tee ", "rmdir", "del ",
    "DELETE", "DROP", "INSERT", "UPDATE", "-X POST", "-X PUT", "-X DELETE",
    "-X PATCH", "--request ", "--data", "--upload-file",
    "curl -o", "curl -O", "--output", "git push", "git commit",
    "chmod", "chown", "kill ",
)

# Secret-exfiltration guard — see grounding_author.py::_SECRET_PATTERNS for
# the per-pattern rationale (kept identical here as the defence-in-depth copy
# that runs immediately before actual subprocess execution).
_SECRET_PATTERNS = (
    re.compile(r"\bget secret\b", re.I),               # kubectl/gcloud "get secret <name>" — dumps a Secret resource
    re.compile(r"(?=.*\bjsonpath\b)(?=.*\bsecret\b)", re.I),  # jsonpath extraction of a Secret field
    re.compile(r"\bbase64\s+(-d|--decode)\b", re.I),    # decoding a base64-encoded secret/credential blob
    re.compile(r"\bcat\b.*\.env\b(?!\.example)"),       # `cat .env` (real dotenv) — grep and .env.example stay allowed
    re.compile(r"/\.credentials(?:\.json)?\b"),         # credential-store files, e.g. ~/.claude/.credentials.json
    re.compile(r"--token\b", re.I),                     # inline token/PAT arguments
    re.compile(r"\bauthorization\s*:", re.I),           # echoing an Authorization: header value
)

_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_PROBE_TIMEOUT_SEC = int(os.environ.get("CRAG_ENGINE_GROUNDING_STEP_TIMEOUT", "20"))

# ---------------------------------------------------------------------------
# Job queue helpers
# ---------------------------------------------------------------------------

def enqueue_job(
    conn,
    claim_kind: str,
    claim_id: int,
    job_type: str,
    priority: int = 0,
) -> bool:
    """INSERT OR IGNORE a pending grounding job.

    The UNIQUE partial index on (claim_kind, claim_id, job_type) WHERE
    status='pending' ensures at most one pending job per trio.  Returns True
    if a new row was inserted, False if silently deduplicated.
    """
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO grounding_jobs
                (claim_kind, claim_id, job_type, status, attempts, priority, enqueued_at)
            VALUES (?, ?, ?, 'pending', 0, ?, ?)
            """,
            (claim_kind, claim_id, job_type, priority, _utcnow_iso()),
        )
        return cur.rowcount > 0
    except Exception as exc:
        logger.debug("grounding_queue_v2: enqueue_job failed: %s", exc)
        return False


def recover_orphaned_jobs(conn, max_age_min: int = 30) -> int:
    """Reset jobs stuck in status='running' back to 'pending'.

    A daemon crash/restart mid-job leaves its claimed rows in 'running'
    forever — nothing else ever touches them (observed 2026-07-06: two
    author jobs stuck 'running' since the previous day's process). Called
    once at daemon startup; also safe to call periodically. `attempts` is
    preserved so the retry budget still applies. Cutoff is built in Python
    (principle #124 — never SQLite datetime('now') vs ISO-T comparison).
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_min)).isoformat()
    try:
        cur = conn.execute(
            """
            UPDATE grounding_jobs
            SET status='pending', started_at=NULL
            WHERE status='running' AND COALESCE(started_at, enqueued_at) < ?
            """,
            (cutoff,),
        )
        conn.commit()
        n = cur.rowcount or 0
        if n:
            logger.info("grounding_queue_v2: recovered %d orphaned running job(s)", n)
        return n
    except Exception as exc:
        logger.warning("grounding_queue_v2: recover_orphaned_jobs failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Step execution (READ-ONLY subprocess)
# ---------------------------------------------------------------------------

def _is_read_only(step: str) -> bool:
    s = f" {_strip_safe_redirects(step)} "
    if any(tok in s for tok in _FORBIDDEN):
        return False
    if any(pat.search(step) for pat in _SECRET_PATTERNS):
        return False
    return True


def run_recipe_steps(recipe: dict) -> list[str]:
    """Execute each step in the recipe read-only.  Returns a parallel list of
    verbatim output strings (empty on timeout/error/write-guard failure)."""
    steps = (recipe or {}).get("steps", [])
    outputs: list[str] = []
    for step in steps:
        if not isinstance(step, str) or not step.strip():
            outputs.append("")
            continue
        # Defence-in-depth write guard (recipe was validated on authoring but
        # re-check here before executing)
        if not _is_read_only(step):
            logger.warning(
                "grounding_queue_v2: step contains write token — SKIPPED: %r", step[:120]
            )
            outputs.append("<skipped: write-token detected>")
            continue
        try:
            p = subprocess.run(
                ["bash", "-lc", step],
                capture_output=True, text=True,
                timeout=_PROBE_TIMEOUT_SEC,
                creationflags=_CREATE_NO_WINDOW,
            )
            out = ((p.stdout or "") + (p.stderr or "")).strip()[:600]
        except subprocess.TimeoutExpired:
            out = "<timeout>"
        except Exception as exc:
            out = f"<exec-error: {exc}>"
        outputs.append(out)
    return outputs


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def fetch_prior_history(conn, claim_kind: str, claim_id: int, limit: int = 5) -> list[dict]:
    """Return the N most recent grounding_history rows (oldest first within cap)."""
    try:
        rows = conn.execute(
            """
            SELECT ts, job_type, verdict, reasoning, evidence, recipe_version
            FROM grounding_history
            WHERE claim_kind=? AND claim_id=?
            ORDER BY ts DESC LIMIT ?
            """,
            (claim_kind, claim_id, limit),
        ).fetchall()
        # Reverse so they are chronological (oldest first) for the prompt
        return [dict(r) for r in reversed(rows)]
    except Exception as exc:
        logger.debug("grounding_queue_v2: fetch_prior_history failed: %s", exc)
        return []


def append_history(
    conn,
    claim_kind: str,
    claim_id: int,
    job_type: str,
    verdict: Optional[str],
    reasoning: Optional[str],
    evidence: Optional[str],
    recipe_version: Optional[int],
) -> None:
    """Append one row to grounding_history (append-only — never updated)."""
    try:
        conn.execute(
            """
            INSERT INTO grounding_history
                (claim_kind, claim_id, ts, job_type, verdict, reasoning, evidence, recipe_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (claim_kind, claim_id, _utcnow_iso(), job_type,
             verdict, reasoning, evidence, recipe_version),
        )
    except Exception as exc:
        logger.warning("grounding_queue_v2: append_history failed: %s", exc)


# ---------------------------------------------------------------------------
# Worker: drain one job
# ---------------------------------------------------------------------------

_MAX_ATTEMPTS = 3  # fallback-only; primary source is grounding_config.get_config().max_attempts
# Mutable 1-element list (not a plain float) so the budget-exceeded warning
# throttle below can rebind it from inside drain_one_job without `global`.
_last_budget_warn = [0.0]


def drain_one_job(conn, llm: Any) -> bool:
    """Claim and process the oldest pending job.

    Returns True if a job was processed (regardless of outcome), False if
    the queue is empty OR the claim was lost to a concurrent worker.
    Always fail-open: no exception leaves this function.

    Concurrency note: a bare "UPDATE ... WHERE status='pending'" is exclusive
    in isolation, but Python's sqlite3 module only auto-opens a (deferred)
    transaction before DML statements, never before a SELECT. Two threads can
    each run the SELECT with no transaction open, then both attempt the
    UPDATE at roughly the same instant; under a rollback-journal connection
    with no busy_timeout, the loser's UPDATE raises "database is locked"
    (SQLITE_BUSY) rather than blocking — and the old code treated that
    exception as "we tried, return True", which double-counted the job as
    processed by both threads (~20% of runs under a synchronized Barrier).
    Fix: (1) set busy_timeout so a losing writer blocks instead of erroring,
    (2) wrap SELECT+UPDATE in one BEGIN IMMEDIATE transaction so the claim is
    atomic across connections/threads, not just within the single UPDATE
    statement, and (3) treat a lock-loss on BEGIN IMMEDIATE itself as "lost
    the race" (return False), never True.
    """
    t_start = time.perf_counter()
    cfg = grounding_config.get_config()

    # --- Budget gate (Phase 1b / insight #3339): pause_on_budget=true and
    # today's usage over either daily cap means this worker refuses to start
    # a new LLM-calling job — fail-SAFE, before any attempt is consumed, so
    # the job stays pending and gets picked up once the budget resets. Checked
    # BEFORE claiming so a paused worker never increments a job's attempts
    # counter for a job it never actually processed.
    try:
        if grounding_cost.budget_exceeded(conn, cfg):
            now_ts = time.monotonic()
            if now_ts - _last_budget_warn[0] > 60:
                _last_budget_warn[0] = now_ts
                logger.warning(
                    "grounding_queue_v2: daily LLM budget exceeded "
                    "(pause_on_budget=true) — worker pausing, no job claimed"
                )
            return False
    except Exception as exc:
        logger.debug("grounding_queue_v2: budget_exceeded check failed (fail-open): %s", exc)

    # Defensive: some callers (tests) open connections without setting this.
    # Harmless / idempotent if the caller already set it (e.g. get_db()).
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass

    # --- Claim the oldest pending job atomically. The in-process lock gives
    # deterministic exclusion for the thread-pool-within-one-process pattern
    # the daemon actually uses; BEGIN IMMEDIATE gives (best-effort) exclusion
    # across separate processes touching the same DB file.
    with _CLAIM_LOCK:
        # BEGIN IMMEDIATE acquires the RESERVED (write) lock BEFORE the
        # SELECT, so no other connection can also acquire it until this
        # transaction commits/rolls back. That closes the SELECT-then-UPDATE
        # race window entirely (not just the UPDATE's own CAS).
        try:
            conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            msg = str(exc).lower()
            if "database is locked" in msg or "already an active transaction" in msg:
                # Lost the race for the write lock (or nested-transaction
                # misuse by a caller) — this worker claimed nothing, not
                # "processed".
                try:
                    conn.rollback()
                except Exception:
                    pass
                return False
            logger.debug("grounding_queue_v2: BEGIN IMMEDIATE failed: %s", exc)
            return False

        try:
            job = conn.execute(
                """
                SELECT id, claim_kind, claim_id, job_type, attempts, priority
                FROM grounding_jobs
                WHERE status='pending'
                ORDER BY priority DESC, enqueued_at ASC
                LIMIT 1
                """,
            ).fetchone()
        except Exception as exc:
            logger.debug("grounding_queue_v2: queue read failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
            return False

        if job is None:
            try:
                conn.rollback()  # nothing to claim; release the write lock promptly
            except Exception:
                pass
            return False

        job_id = job["id"]
        claim_kind = job["claim_kind"]
        claim_id = job["claim_id"]
        job_type = job["job_type"]
        # attempts will be incremented atomically in the UPDATE below

        # --- Claim: UPDATE ... WHERE status='pending', still inside the same
        # BEGIN IMMEDIATE transaction — no other connection can be mid-claim
        # here, and no other same-process thread can even be inside this
        # `with` block concurrently.
        now = _utcnow_iso()
        try:
            cur = conn.execute(
                "UPDATE grounding_jobs SET status='running', started_at=?, attempts=attempts+1"
                " WHERE id=? AND status='pending'",
                (now, job_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                # Lost the race — another worker claimed this job; bail out.
                return False
        except Exception as exc:
            logger.warning("grounding_queue_v2: failed to claim job %s: %s", job_id, exc)
            try:
                conn.rollback()
            except Exception:
                pass
            return False
        # Successful claim (rowcount == 1): fall through past the `with`
        # block to process the job below.

    # Read back the incremented attempts counter (the atomic UPDATE owns it now).
    try:
        attempts = conn.execute(
            "SELECT attempts FROM grounding_jobs WHERE id=?", (job_id,)
        ).fetchone()["attempts"]
    except Exception:
        attempts = 1  # safe fallback; logging only

    logger.info(
        "grounding DECISION job=%s claim=%s#%s type=%s status=running "
        "attempt=%s/%s reason=worker_claimed",
        job_id, claim_kind, claim_id, job_type, attempts, cfg.max_attempts,
    )

    # --- Grounding v3: claim-kind jobs verify an atomic claim, not an insight
    # narrative. Dispatch to claim_verify and finish here (no insight content /
    # falsifier flow applies). Fail-soft; TransientLLMError requeues.
    if claim_kind == "claim":
        llm_client.clear_last_usage()
        try:
            import claim_verify
            claim_verify.verify_claim(conn, claim_id, llm)
            _record_llm_cost(conn, "claim_verify", claim_kind, claim_id)
        except llm_client.TransientLLMError as exc:
            transient_status = "failed" if attempts >= cfg.max_attempts else "pending"
            _finish_job(conn, job_id, transient_status, f"transient_llm_error: {exc}",
                        t_start, claim_kind, claim_id, job_type, attempts)
            return True
        except Exception as exc:
            _finish_job(conn, job_id, "failed", f"claim_verify error: {exc}",
                        t_start, claim_kind, claim_id, job_type, attempts)
            return True
        _finish_job(conn, job_id, "done", None, t_start, claim_kind, claim_id, job_type, attempts)
        return True

    # --- Fetch claim content ---
    try:
        tbl = "insights" if claim_kind == "insight" else "principles"
        claim_row = conn.execute(
            f"SELECT content FROM {tbl} WHERE id=?", (claim_id,)
        ).fetchone()
        claim_content = (claim_row["content"] or "") if claim_row else ""
    except Exception as exc:
        _finish_job(conn, job_id, "failed", f"claim read error: {exc}", t_start,
                    claim_kind, claim_id, job_type, attempts)
        return True

    if not claim_content:
        _finish_job(conn, job_id, "failed", "empty claim content", t_start,
                    claim_kind, claim_id, job_type, attempts)
        return True

    # --- Fetch entity links for tier classification ---
    try:
        from entity_extract import extract_entities
        entities = extract_entities(claim_content)
    except Exception:
        entities = []

    # --- Branch on job_type ---
    # Reset the usage sidecar for this job so a job that makes no LLM call
    # (e.g. an auto-verify resolve with no draft_correction) doesn't inherit
    # a PRIOR job's usage left over on this worker thread — see
    # llm_client.clear_last_usage() docstring.
    llm_client.clear_last_usage()
    error_msg = None

    try:
        if job_type == "author":
            error_msg = _handle_author(conn, claim_kind, claim_id, claim_content, entities, llm)
        elif job_type == "reground":
            error_msg = _handle_reground(conn, claim_kind, claim_id, claim_content, entities, llm)
        elif job_type == "resolve":
            error_msg = _handle_resolve(conn, claim_kind, claim_id, claim_content, entities, llm)
        else:
            error_msg = f"unknown job_type={job_type!r}"
    except llm_client.TransientLLMError as exc:
        # THE resilience fix (2026-07-16): a transient transport failure
        # (401 mid-refresh / 429 rate-limit / connection blip that survived
        # call_with_retry's bounded retries) is NOT a verdict. The call sites
        # re-raise it past their broad `except Exception` handlers precisely so
        # it lands here — never as a grounding_history 'uncertain' row that
        # would re-flag the claim and churn the queue forever.
        #
        # Requeue: leave the job PENDING so a later tick retries it once the
        # token has refreshed / the rate-limit window has passed. Honour
        # grounding.max_attempts so a genuinely dead endpoint still terminates
        # (goes 'failed') instead of infinite-looping. No append_history call
        # is made on this path — that is the whole point.
        transient_status = "failed" if attempts >= cfg.max_attempts else "pending"
        reason = f"transient_llm_error: {exc}"
        logger.warning(
            "grounding job=%s claim=%s#%s type=%s TRANSIENT LLM failure "
            "(attempt=%s/%s) -> %s (no verdict recorded): %s",
            job_id, claim_kind, claim_id, job_type, attempts, cfg.max_attempts,
            transient_status, exc,
        )
        _finish_job(conn, job_id, transient_status, reason, t_start,
                    claim_kind, claim_id, job_type, attempts)
        return True

    if error_msg:
        # Terminal, no-retry case: the LLM declared the claim mechanically
        # unverifiable (would require reading a secret or a mutation to
        # check). Retrying won't change that answer, so this goes straight
        # to 'failed' after this single attempt instead of cycling through
        # _MAX_ATTEMPTS retries for a result that can never succeed.
        is_unverifiable = error_msg.startswith("mechanically_unverifiable")
        final_status = "failed" if (attempts >= cfg.max_attempts or is_unverifiable) else "pending"
        _finish_job(conn, job_id, final_status,
                    error_msg, t_start, claim_kind, claim_id, job_type, attempts)
    else:
        _finish_job(conn, job_id, "done", None, t_start,
                    claim_kind, claim_id, job_type, attempts)

    return True


def _record_llm_cost(conn, stage: str, claim_kind: str, claim_id: int) -> None:
    """Read the thread-local usage sidecar left by the LLM call this handler
    just made and append one row to llm_cost_ledger. No-op if no LLM call
    was actually made this job (llm_client.clear_last_usage() at the top of
    drain_one_job leaves model=None until a real call populates it) — never
    raises (grounding_cost.record_call is itself defensive)."""
    usage = llm_client.get_last_usage()
    if usage["model"] is None:
        return
    grounding_cost.record_call(
        conn,
        provider=usage["provider"],
        model=usage["model"],
        stage=stage,
        tokens_in=usage["tokens_in"],
        tokens_out=usage["tokens_out"],
        claim_kind=claim_kind,
        claim_id=claim_id,
    )
    try:
        conn.commit()
    except Exception:
        pass


def _handle_author(
    conn,
    claim_kind: str,
    claim_id: int,
    claim_content: str,
    entities: list,
    llm: Any,
) -> Optional[str]:
    """Author a Tier-B recipe. Returns error string or None on success.

    The error string, when present, is one of the four honest failure-reason
    codes from grounding_author.author_recipe() — see that function's
    docstring. drain_one_job() checks for a 'mechanically_unverifiable'
    prefix to route the job straight to a terminal state (no retry).
    """
    result, reason = grounding_author.author_recipe(claim_content, entities, llm)
    _record_llm_cost(conn, "author", claim_kind, claim_id)
    if result is None:
        # mechanically_unverifiable is a real answer, not a transient failure —
        # record it in the reasoning trail even though no falsifier was created.
        # It's also TERMINAL in a stronger sense than the other failure reasons:
        # the claim can never be mechanically checked, so mark it durably
        # 'judgment' (grounding_resolve.mark_judgment) so it stops being
        # re-flagged for mechanical/reground work — this is the fix for the
        # 449-flagged-claims noise floor (many carry junk existence falsifiers
        # on judgment-only content: meta-principles, operator preferences).
        if reason and reason.startswith("mechanically_unverifiable"):
            append_history(conn, claim_kind, claim_id, "author", None, reason, None, None)
            try:
                import grounding_resolve
                grounding_resolve.mark_judgment(conn, claim_kind, claim_id, reason)
            except Exception as exc:
                logger.warning("grounding_queue_v2: mark_judgment failed: %s", exc)
            try:
                conn.commit()
            except Exception:
                pass
        return reason or "malformed_output: author_recipe returned no result and no reason"

    fq = result.get("falsification_question", "")
    recipe_json_str = __import__("json").dumps(result.get("recipe", {}))
    now = _utcnow_iso()

    # Upsert the falsifier row with Tier-B data
    try:
        existing = conn.execute(
            "SELECT id FROM falsifiers WHERE claim_kind=? AND claim_id=?",
            (claim_kind, claim_id),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE falsifiers
                SET tier='B', falsification_question=?, recipe=?,
                    authored_by='llm', recipe_version=COALESCE(recipe_version,0)+1,
                    updated_at=?
                WHERE claim_kind=? AND claim_id=?
                """,
                (fq, recipe_json_str, now, claim_kind, claim_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO falsifiers
                    (claim_kind, claim_id, kind, spec, entity, entity_type,
                     derived, tier, falsification_question, recipe, authored_by,
                     recipe_version, created_at, updated_at)
                VALUES (?, ?, 'none', NULL, NULL, NULL,
                        0, 'B', ?, ?, 'llm', 1, ?, ?)
                """,
                (claim_kind, claim_id, fq, recipe_json_str, now, now),
            )
        conn.commit()
    except Exception as exc:
        return f"falsifier upsert failed: {exc}"

    # Append history row
    append_history(conn, claim_kind, claim_id, "author", None,
                   f"Authored recipe: {fq[:200]}", None, 1)
    try:
        conn.commit()
    except Exception:
        pass

    return None


def _handle_reground(
    conn,
    claim_kind: str,
    claim_id: int,
    claim_content: str,
    entities: list,
    llm: Any,
) -> Optional[str]:
    """Execute the recipe steps and adjudicate. Returns error string or None."""
    import json as _json

    # Load the falsifier / recipe for this claim
    try:
        fal_row = conn.execute(
            "SELECT tier, recipe, recipe_version FROM falsifiers WHERE claim_kind=? AND claim_id=?",
            (claim_kind, claim_id),
        ).fetchone()
    except Exception as exc:
        return f"falsifier read failed: {exc}"

    recipe = {}
    recipe_version = 1

    if fal_row and fal_row["recipe"]:
        try:
            recipe = _json.loads(fal_row["recipe"])
            recipe_version = fal_row["recipe_version"] or 1
        except Exception:
            recipe = {}

    if not recipe or not recipe.get("steps"):
        # No recipe yet — enqueue an author job instead
        inserted = enqueue_job(conn, claim_kind, claim_id, "author", priority=1)
        try:
            conn.commit()
        except Exception:
            pass
        return None if inserted else "no recipe and author job already pending"

    # Execute steps
    step_outputs = run_recipe_steps(recipe)

    # Fetch prior history for CoT re-feeding
    prior_history = fetch_prior_history(conn, claim_kind, claim_id, limit=5)

    # Adjudicate
    adj = grounding_author.adjudicate(
        claim=claim_content,
        recipe=recipe,
        step_outputs=step_outputs,
        prior_history=prior_history,
        llm=llm,
    )
    _record_llm_cost(conn, "adjudicate", claim_kind, claim_id)
    verdict = adj.get("verdict", "uncertain")
    reasoning = adj.get("reasoning", "")
    evidence = adj.get("evidence", "")

    # Persist history row
    append_history(conn, claim_kind, claim_id, "reground",
                   verdict, reasoning, evidence, recipe_version)

    # Update falsifier last_verdict + last_result
    now = _utcnow_iso()
    try:
        conn.execute(
            """
            UPDATE falsifiers
            SET last_verdict=?, last_result=?, last_run_at=?, updated_at=?
            WHERE claim_kind=? AND claim_id=?
            """,
            (verdict,
             "pass" if verdict == "pass" else ("fail" if verdict == "fail" else "error"),
             now, now, claim_kind, claim_id),
        )
    except Exception as exc:
        logger.debug("grounding_queue_v2: last_verdict update failed: %s", exc)

    # Update claim grounding columns based on verdict
    tbl = "insights" if claim_kind == "insight" else "principles"
    try:
        if verdict == "pass":
            conn.execute(
                f"UPDATE {tbl} SET grounded_at=?, grounding_due=0 WHERE id=?",
                (now, claim_id),
            )
            # Resolve any open grounding_queue row
            conn.execute(
                "UPDATE grounding_queue SET status='resolved', resolved_at=?, "
                "resolved_by='worker', resolution='falsifier_pass' "
                "WHERE claim_kind=? AND claim_id=? AND status='open'",
                (now, claim_kind, claim_id),
            )
        elif verdict == "fail":
            conn.execute(
                f"UPDATE {tbl} SET grounding_due=1 WHERE id=?",
                (claim_id,),
            )
    except Exception as exc:
        logger.debug("grounding_queue_v2: claim update failed: %s", exc)

    try:
        conn.commit()
    except Exception:
        pass

    # Chain: a rendered verdict (pass/fail/uncertain) is DETECTION. Enqueue the
    # 'resolve' job so the autonomous resolution policy (grounding_resolve.py)
    # actually DOES something with it instead of leaving the claim sitting in
    # grounding_queue for a human to notice — this is the fix for "reground
    # barely runs and nothing consumes its verdict" (5 real verdicts/24h vs
    # 3948 author-nulls, pre-autoresolve). Best-effort: a failure here just
    # means the claim stays flagged for the next sweep to pick up.
    try:
        enqueue_job(conn, claim_kind, claim_id, "resolve", priority=4)
        conn.commit()
    except Exception as exc:
        logger.debug("grounding_queue_v2: resolve chain-enqueue failed: %s", exc)

    return None


def _handle_resolve(
    conn,
    claim_kind: str,
    claim_id: int,
    claim_content: str,
    entities: list,
    llm: Any,
) -> Optional[str]:
    """Dispatch a 'resolve' job to grounding_resolve.resolve_claim() — the
    graded autonomous resolution policy (verify-on-pass / auto-correct-low-
    stakes-insight-fail / escalate-to-proposal for everything else). Returns
    an error string on failure or None on success (same _handle_* contract
    as _handle_author/_handle_reground). `entities` is accepted for dispatch-
    signature symmetry but unused — resolve_claim reasons over the ALREADY-
    RENDERED verdict/reasoning/evidence in grounding_history, not raw content.
    """
    try:
        import grounding_resolve
        result = grounding_resolve.resolve_claim(conn, claim_kind, claim_id, claim_content, llm)
        # resolve_claim only calls draft_correction (the one LLM call in this
        # path) on the auto-correct branch — the auto-verify/proposal-only
        # branches make no LLM call at all, so _record_llm_cost's model=None
        # guard correctly no-ops for those.
        _record_llm_cost(conn, "correction", claim_kind, claim_id)
        return result
    except llm_client.TransientLLMError:
        # Let a transient LLM failure propagate to drain_one_job's requeue
        # handler — do NOT collapse it into a terminal 'resolve_error' string,
        # which would consume an attempt and eventually mark the job 'failed'
        # off a recoverable blip.
        raise
    except Exception as exc:
        return f"resolve_error: {exc}"


def _finish_job(
    conn,
    job_id: int,
    final_status: str,
    error_msg: Optional[str],
    t_start: float,
    claim_kind: str,
    claim_id: int,
    job_type: str,
    attempts: int,
) -> None:
    """Mark a job done/failed/pending (for retry) and emit a DECISION log line."""
    duration_ms = round((time.perf_counter() - t_start) * 1000)
    now = _utcnow_iso()
    # If pending (retry path), reset to pending with exponential back-off via
    # priority demotion (lower priority = drained later)
    if final_status == "pending":
        new_priority = max(0, 10 - attempts * 3)  # 7, 4, 1 on attempts 1, 2, 3
        try:
            conn.execute(
                "UPDATE grounding_jobs SET status='pending', priority=?, last_error=? WHERE id=?",
                (new_priority, (error_msg or "")[:500], job_id),
            )
            conn.commit()
        except Exception:
            pass
    else:
        try:
            conn.execute(
                "UPDATE grounding_jobs SET status=?, finished_at=?, last_error=? WHERE id=?",
                (final_status, now, (error_msg or "")[:500], job_id),
            )
            conn.commit()
        except Exception:
            pass

    logger.info(
        "grounding DECISION job=%s claim=%s#%s type=%s status=%s "
        "reason=%s duration_ms=%s",
        job_id, claim_kind, claim_id, job_type, final_status,
        (error_msg or "ok")[:120], duration_ms,
    )
