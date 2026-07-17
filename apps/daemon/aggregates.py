"""Read-model aggregates for the surface consumers (CLI, console, cloud, ops).

ONE contract, four consumers. `crag status`/`inbox`/`why`, the embedded
console nav, the app.crag.sh snapshot push, and the ops Infra overlay all read
these same shapes — so they are defined ONCE here and mounted as a router on
the daemon. See infra-playbook docs/system-integration-map.md §2.

Design invariants:
- READ-ONLY. Nothing here mutates state; every function takes an open sqlite
  connection and returns plain dicts. This keeps the surfaces safe to call at
  any cadence and trivially testable without a running daemon.
- FAIL-SOFT. A missing table or an unmigrated DB yields empty/degraded data
  with an explicit marker, never a 500. A fresh `crag-engine up` on an empty DB
  must render, not crash (the evaluator's first-run path).
- NO NEW VERDICT LOGIC. Trust/claim-health reuses claim_layer so there is one
  source of truth for "is this claim live"; we never re-derive liveness here.

The daemon owns HTTP concerns (executor offload, request logging). This module
owns the data. `router` is included by engine_daemon.py with one line.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter

# The daemon injects these at import-mount time (see engine_daemon.py). We take
# them as module globals rather than importing the 10k-line daemon (circular).
# Set via `aggregates.bind(get_db=..., table_exists=..., claim_layer=...)`.
_get_db = None
_table_exists = None
_claim_layer = None  # may be None if the claim layer isn't available


def bind(*, get_db, table_exists, claim_layer) -> None:
    """Wire the daemon's DB accessors into this module. Called once at startup."""
    global _get_db, _table_exists, _claim_layer
    _get_db = get_db
    _table_exists = table_exists
    _claim_layer = claim_layer


router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------
# Pure builders — take a connection, return a dict. Unit-tested in isolation.
# ----------------------------------------------------------------------------

def build_overview(conn, project: Optional[str] = None) -> dict:
    """The cockpit payload: trust score, corpus counts, needs-you total,
    today's loop activity, coarse health. ONE round-trip for `crag status`
    and the console home."""
    proj_and = " AND project = ?" if project else ""
    proj_params: list = [project] if project else []

    counts = {"insights": 0, "principles": 0, "claims": 0}
    if _table_exists(conn, "insights"):
        counts["insights"] = conn.execute(
            f"SELECT COUNT(*) FROM insights WHERE status='active'{proj_and}",
            proj_params,
        ).fetchone()[0]
    if _table_exists(conn, "principles"):
        counts["principles"] = conn.execute(
            "SELECT COUNT(*) FROM principles WHERE superseded_by IS NULL"
            + (" AND project = ?" if project else ""),
            proj_params,
        ).fetchone()[0]

    trust = _trust_score(conn)
    if _table_exists(conn, "claims"):
        counts["claims"] = trust["active_claims"]

    return {
        "trust_score": {"value": trust["value"], "verified": trust["verified"],
                        "active_claims": trust["active_claims"]},
        "counts": counts,
        "needs_you": {"total": _needs_you_total(conn, project)},
        "today": _today_activity(conn, project),
        "health": {"daemon": "ok"},  # deep health lives in /stats + ops /infra
        "generated_at": _now_iso(),
    }


def _trust_score(conn) -> dict:
    """Verified fraction of active claims — THE number this product raises.
    'verified' = a claim whose liveness verdict is 'fresh' or 'axiomatic'
    (terminal-true). Reuses claim_layer's verdict so there is one definition."""
    if not _table_exists(conn, "claims") or _claim_layer is None:
        return {"value": None, "verified": 0, "active_claims": 0}
    rows = conn.execute(
        "SELECT predicate_class, grounding_due, grounded_at, last_verdict "
        "FROM claims WHERE status='active'"
    ).fetchall()
    total = len(rows)
    if total == 0:
        return {"value": None, "verified": 0, "active_claims": 0}
    verified = 0
    for r in rows:
        v = _claim_layer._claim_verdict(dict(r))
        if v in ("fresh", "axiomatic"):
            verified += 1
    return {"value": round(verified / total, 3), "verified": verified,
            "active_claims": total}


def _today_activity(conn, project: Optional[str]) -> dict:
    """captured / verified / promoted / compiled counts for the local day.
    Fail-soft: any table absent contributes 0."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = {"captured": 0, "verified": 0, "promoted": 0}
    if _table_exists(conn, "insights_staging"):
        out["captured"] = conn.execute(
            "SELECT COUNT(*) FROM insights_staging WHERE substr(created_at,1,10)=?",
            (day,),
        ).fetchone()[0]
    if _table_exists(conn, "principles"):
        out["promoted"] = conn.execute(
            "SELECT COUNT(*) FROM principles WHERE substr(created_at,1,10)=?",
            (day,),
        ).fetchone()[0]
    if _table_exists(conn, "claims"):
        out["verified"] = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE last_verdict='pass' "
            "AND substr(grounded_at,1,10)=?",
            (day,),
        ).fetchone()[0]
    return out


# ----------------------------------------------------------------------------
# Inbox — the union of everything awaiting a human decision, one shape.
# ----------------------------------------------------------------------------

def build_inbox(conn, project: Optional[str] = None, limit: int = 100) -> dict:
    """Everything that needs a human, unified. Each item is kind-prefixed and
    self-describing (title + why + evidence + actions) so the CLI, console, and
    cloud render it without kind-specific branching."""
    items: list = []
    items += _inbox_dispositions(conn, project)
    items += _inbox_proposals(conn)
    items += _inbox_contradictions(conn)
    items += _inbox_stale_rules(conn, project)
    # Newest-first, stable; cap for payload sanity.
    items.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    return {"items": items[: max(1, min(limit, 500))], "total": len(items)}


def _needs_you_total(conn, project: Optional[str]) -> int:
    return build_inbox(conn, project, limit=500)["total"]


def _inbox_dispositions(conn, project: Optional[str]) -> list:
    if not _table_exists(conn, "insights_staging"):
        return []
    sql = ("SELECT id, source, project, reason, tier, deadline, created_at "
           "FROM insights_staging WHERE status='pending' AND tier='t2'")
    params: list = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        out.append({
            "id": f"disposition:{d['id']}",
            "kind": "t2_disposition",
            "title": f"Staged insight from {d['source']} awaits approval",
            "why": d.get("reason") or "Tier-2 capture requires human approval.",
            "evidence": {"source": d["source"], "project": d.get("project")},
            "actions": ["approve", "reject", "defer"],
            "created_at": d.get("created_at"),
            "deadline": d.get("deadline"),
        })
    return out


def _inbox_proposals(conn) -> list:
    if not _table_exists(conn, "resolution_proposals"):
        return []
    rows = conn.execute(
        "SELECT id, claim_kind, claim_id, proposed_action, reasoning, stakes, created_at "
        "FROM resolution_proposals WHERE status='pending'"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "id": f"proposal:{d['id']}",
            "kind": "grounding_proposal",
            "title": f"Grounding proposes to {d['proposed_action']} "
                     f"{d['claim_kind']} #{d['claim_id']}",
            "why": d.get("reasoning") or "A grounding run produced a resolution proposal.",
            "evidence": {"stakes": d.get("stakes"), "action": d["proposed_action"]},
            "actions": ["approve", "reject", "defer"],
            "created_at": d.get("created_at"),
            "deadline": None,
        })
    return out


def _inbox_contradictions(conn) -> list:
    if not _table_exists(conn, "claim_contradictions"):
        return []
    rows = conn.execute(
        "SELECT id, claim_a_id, claim_b_id, reason, score, detected_at "
        "FROM claim_contradictions WHERE status='open'"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "id": f"contradiction:{d['id']}",
            "kind": "contradiction",
            "title": f"Claims #{d['claim_a_id']} and #{d['claim_b_id']} may contradict",
            "why": d.get("reason") or "Detected as a possible claim-level contradiction.",
            "evidence": {"claim_a": d["claim_a_id"], "claim_b": d["claim_b_id"],
                         "score": d.get("score")},
            "actions": ["adjudicate", "dismiss"],
            "created_at": d.get("detected_at"),
            "deadline": None,
        })
    return out


def _inbox_stale_rules(conn, project: Optional[str]) -> list:
    """Compiled rules (principles) whose claim-health has drifted — the rule is
    live in governance but its evidence went stale, so it needs a re-distill."""
    out = []
    for rule in build_rules(conn, project)["rules"]:
        if rule.get("stale"):
            out.append({
                "id": f"stale_rule:{rule['principle_id']}",
                "kind": "stale_rule",
                "title": f"Compiled rule (principle #{rule['principle_id']}) has stale evidence",
                "why": f"claim_health={rule['claim_health']} — re-distill recommended.",
                "evidence": {"claim_health": rule["claim_health"],
                             "confidence": rule["confidence"]},
                "actions": ["re-distill", "defer"],
                "created_at": rule.get("adopted"),
                "deadline": None,
            })
    return out


# ----------------------------------------------------------------------------
# Rules — the distilled-rule inventory (memory become law). The payoff view.
# ----------------------------------------------------------------------------

def build_rules(conn, project: Optional[str] = None) -> dict:
    """Active principles with their live claim-health — the compiled governance
    inventory. `stale=True` marks a rule whose evidence drifted."""
    if not _table_exists(conn, "principles"):
        return {"rules": []}
    where = "WHERE superseded_by IS NULL"
    params: list = []
    if project:
        where += " AND project = ?"
        params.append(project)
    rows = conn.execute(
        f"SELECT id, content, confidence, project, tags FROM principles {where} ORDER BY id",
        params,
    ).fetchall()
    rules = []
    for r in rows:
        health = "unverified"
        if _claim_layer is not None:
            try:
                health = _claim_layer.claim_rollup(conn, "principle", r["id"]).get(
                    "verdict") or "unverified"
            except Exception:
                health = "unverified"
        rules.append({
            "principle_id": r["id"],
            "text": r["content"],
            "confidence": r["confidence"],
            "project": r["project"],
            "claim_health": health,
            "stale": health in ("stale", "revalidating"),
        })
    return {"rules": rules}


# ----------------------------------------------------------------------------
# Console modules — nav is DATA. Core returns the OSS surfaces; the ops overlay
# appends its Infra module by overriding CORE_MODULES (see docs §3).
# ----------------------------------------------------------------------------

CORE_MODULES = [
    {"id": "memory", "title": "Memory", "icon": "brain", "route": "/", "panels": ["overview"]},
    {"id": "inbox", "title": "Needs You", "icon": "inbox", "route": "/inbox", "panels": ["inbox"]},
    {"id": "browser", "title": "Browser", "icon": "search", "route": "/memory", "panels": ["corpus"]},
    {"id": "rules", "title": "Rules", "icon": "scale", "route": "/rules", "panels": ["rules"]},
    {"id": "systems", "title": "Systems", "icon": "cpu", "route": "/systems", "panels": ["grounding", "capture"]},
]


def build_modules() -> dict:
    """The nav manifest the console renders itself from. Extended, not forked,
    by the private ops overlay."""
    return {"modules": list(CORE_MODULES)}


# ----------------------------------------------------------------------------
# HTTP surface — thin async wrappers. DB work offloaded to a thread; the daemon
# already runs handlers on its loop, so we keep executor use consistent.
# ----------------------------------------------------------------------------

import asyncio  # noqa: E402  (local to keep the pure section import-clean)


async def _run(fn, *args):
    # The connection MUST be opened inside the executor thread: sqlite3
    # defaults to check_same_thread=True, so a conn created on the event-loop
    # thread cannot be used in the worker thread (matches the daemon's own
    # handler idiom — open, use, close, all within the offloaded function).
    def _work():
        conn = _get_db()
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    return await asyncio.get_event_loop().run_in_executor(None, _work)


@router.get("/overview")
async def overview(project: Optional[str] = None):
    return {"ok": True, **await _run(build_overview, project)}


@router.get("/inbox")
async def inbox(project: Optional[str] = None, limit: int = 100):
    return {"ok": True, **await _run(lambda c, p: build_inbox(c, p, limit), project)}


@router.get("/rules")
async def rules(project: Optional[str] = None):
    return {"ok": True, **await _run(build_rules, project)}


@router.get("/console/modules")
async def console_modules():
    # Pure/static; no DB round-trip.
    return {"ok": True, **build_modules()}
