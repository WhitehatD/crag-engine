"""Deterministic session lifecycle — the P0 market wedge (design law 1-2).

"Invisible in, receipt out." A session's context-load and end-capture are
HTTP METHODS invoked by deterministic harness hooks (SessionStart / SessionEnd
command shims → the `crag` CLI → these routes), never a skill the agent must
remember. See infra-playbook docs/crag-system-design.md §3 laws 1-2.

Two endpoints, both thin and fast:

  GET  /session/start?project=  → ONE composed context payload the harness
       injects at session start: overview (reused from aggregates), the top
       few true-t2-ish inbox items ("needs you"), a stale-rules count, and the
       newest session diary row. REUSES aggregates.build_overview /
       build_inbox — no duplicated queries.

  POST /session/end  {project?, session_id?, summary?} → records a session-end
       marker in the `sessions` diary table (if its schema fits; else no-op)
       and returns the payoff numbers (captured/verified/promoted today, reused
       from aggregates._today_activity). FAST (<300 ms): no LLM, no embedding.

Design invariants (mirror aggregates.py EXACTLY):
- Pure builders take an open sqlite connection and return plain dicts.
- FAIL-SOFT: a missing table or unmigrated DB yields empty/degraded data, never
  a 500. The end-capture path in particular is fail-open — a hook must never
  break the user's session.
- NO NEW VERDICT LOGIC: everything trust/claim-related is reused from
  aggregates (which reuses claim_layer). One source of truth.

The daemon owns HTTP concerns (executor offload); this module owns the data.
`router` is mounted by engine_daemon.py right after the aggregates router.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

# Injected at import-mount time by engine_daemon.py via bind(). We also lean on
# the already-bound aggregates module (bound one line earlier in the daemon) so
# there is exactly one definition of overview / inbox / today-activity.
_get_db = None
_table_exists = None
_aggregates = None


def bind(*, get_db, table_exists, aggregates) -> None:
    """Wire the daemon's DB accessors + the bound aggregates module in.

    Called once at startup, AFTER aggregates.bind(), so build_overview /
    build_inbox / _today_activity are ready to reuse."""
    global _get_db, _table_exists, _aggregates
    _get_db = get_db
    _table_exists = table_exists
    _aggregates = aggregates


router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------
# Pure builders — take a connection, return a dict. Unit-tested in isolation.
# ----------------------------------------------------------------------------

def build_session_start(conn, project: Optional[str] = None) -> dict:
    """The context payload injected at session start. ONE composed round-trip:
      - overview: the full cockpit aggregate (trust, counts, today, needs-you)
      - needs_you_top: the first 3 true-t2-ish inbox items (human-decision
        surface), each self-describing (title + why) so the harness can render
        them as plain markdown
      - rules_stale_count: how many compiled rules have drifted evidence
      - last_session: the newest diary row, if the sessions table is present
    Reuses aggregates.build_overview / build_inbox — never re-queries."""
    overview = _aggregates.build_overview(conn, project)
    inbox = _aggregates.build_inbox(conn, project, limit=50)
    items = inbox.get("items", [])

    # "true-t2-ish": the items that genuinely await a human — t2 dispositions
    # and grounding proposals rank first (they block the loop), then the rest.
    def _rank(i: dict) -> int:
        k = i.get("kind")
        if k == "t2_disposition":
            return 0
        if k == "grounding_proposal":
            return 1
        return 2

    ranked = sorted(items, key=_rank)
    needs_you_top = [
        {"id": i.get("id"), "kind": i.get("kind"), "title": i.get("title"),
         "why": i.get("why")}
        for i in ranked[:3]
    ]

    rules_stale_count = sum(
        1 for i in items if i.get("kind") == "stale_rule"
    )

    return {
        "overview": overview,
        "needs_you_top": needs_you_top,
        "needs_you_total": inbox.get("total", 0),
        "rules_stale_count": rules_stale_count,
        "last_session": _last_session(conn, project),
        "generated_at": _now_iso(),
    }


def _last_session(conn, project: Optional[str]) -> Optional[dict]:
    """Newest row of the `sessions` diary table (if present). Fail-soft: absent
    table → None. We select a compact, stable subset the harness can render."""
    if not _table_exists(conn, "sessions"):
        return None
    where = ""
    params: list = []
    if project:
        where = "WHERE project = ?"
        params.append(project)
    row = conn.execute(
        "SELECT id, project, date, accomplished, next_steps, problems, created_at "
        f"FROM sessions {where} ORDER BY COALESCE(created_at, date) DESC, id DESC LIMIT 1",
        params,
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    return {
        "id": d.get("id"),
        "project": d.get("project"),
        "date": d.get("date"),
        "accomplished": d.get("accomplished"),
        "next_steps": d.get("next_steps"),
        "problems": d.get("problems"),
        "created_at": d.get("created_at"),
    }


def build_session_end(conn, project: Optional[str] = None,
                      session_id: Optional[str] = None,
                      summary: Optional[str] = None) -> dict:
    """Record a session-end marker (best-effort) and return the payoff numbers.

    The marker is a lightweight row in the `sessions` diary table so `crag why`
    / the console Sessions surface can show that a session closed. If the table
    is absent or its schema doesn't fit, we skip the INSERT (no-op) but STILL
    return the payoff — the receipt is the point, persistence is a bonus.

    FAST + fail-open: no LLM, no embedding, and any INSERT error is swallowed
    (recorded=False) rather than propagated. A hook must never break a session.
    """
    today = _aggregates._today_activity(conn, project)
    recorded = _record_end_marker(conn, project, session_id, summary)
    return {
        "recorded": recorded,
        "captured_today": today.get("captured", 0),
        "verified_today": today.get("verified", 0),
        "promoted_today": today.get("promoted", 0),
        "generated_at": _now_iso(),
    }


def _record_end_marker(conn, project: Optional[str], session_id: Optional[str],
                       summary: Optional[str]) -> bool:
    """INSERT a compact session-end row into `sessions`. Returns True on write,
    False if the table is absent or the write failed (both are acceptable —
    the payoff numbers are returned regardless)."""
    if not _table_exists(conn, "sessions"):
        return False
    proj = project or "unknown"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    accomplished = summary or "session-end (auto-captured by hook)"
    try:
        conn.execute(
            "INSERT INTO sessions (project, date, accomplished, session_uuid, "
            "auto_captured_at, created_at) VALUES (?,?,?,?,?,?)",
            (proj, day, accomplished, session_id, _now_iso(), _now_iso()),
        )
        conn.commit()
        return True
    except Exception:
        # Fail-open: schema mismatch on an older DB, locked WAL, etc. The
        # receipt (payoff numbers) is what the user sees — persistence is a
        # non-blocking bonus.
        try:
            conn.rollback()
        except Exception:
            pass
        return False


# ----------------------------------------------------------------------------
# HTTP surface — thin async wrappers. DB work offloaded to a thread; the conn is
# opened INSIDE the executor (sqlite check_same_thread), matching aggregates.py.
# ----------------------------------------------------------------------------

async def _run(fn, *args):
    def _work():
        conn = _get_db()
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    return await asyncio.get_event_loop().run_in_executor(None, _work)


@router.get("/session/start")
async def session_start(project: Optional[str] = None):
    return {"ok": True, **await _run(build_session_start, project)}


class SessionEndBody(BaseModel):
    project: Optional[str] = None
    session_id: Optional[str] = None
    summary: Optional[str] = None


@router.post("/session/end")
async def session_end(body: Optional[SessionEndBody] = None):
    b = body or SessionEndBody()
    return {"ok": True, **await _run(
        lambda c: build_session_end(c, b.project, b.session_id, b.summary))}
