# coding: utf-8
"""The Disposition Engine (docs/architecture.md REV 5 §5.2, REV 7 §7.1).

Every promotion in the loop — staging→insight, insight→principle,
principle→rule-eligible, `.gen` regen — is ONE shape: a proposed state
transition needing a decision. This module is that engine, scoped (per the
brief) to governing `insights_staging` rows written by write_gate.py's
route_to_staging() and the `/capture/event` receiver (migration 031/032).
`promote_insight` (insight->principle) and the daemon's auto-promotion
lifecycle already exist and are UNCHANGED — this module WRAPS/governs
promotion, it does not replace it.

Every governed transition carries four properties:
    { MCP tool (daemon endpoint) . policy tier . attribution . reversibility }

    T0  AUTO          clean provenance, unambiguous, high confidence
                       -> executes, zero humans, logged.
    T1  AGENT-        ambiguous dedup / supersedes low-conf / staging triage
        DELEGABLE      -> an agent may execute WHEN granted capability this
                          session (capability="granted"). Attributed to the agent.
    T2  HUMAN          secret-flagged / supersedes a high-conf principle /
                       crosses a governance boundary -> only capability=
                       "human_approved" may execute accept/merge; an agent
                       alone (capability="granted") gets `requires_human`.

Drain-SLA (non-negotiable): every staging entry ages toward a terminal-or-
safe-default outcome. drain_due() is the sweep: t0 past its deadline
auto-executes the policy default action; t1/t2 past deadline get the SAFE
default (`defer`, which extends the deadline and is logged) — NEVER a blind
auto-accept/merge of ambiguous or sensitive content.

House style (mirrors write_gate.py): pure-ish functions taking an open
sqlite3.Connection; NEVER raise to the caller (fail-soft — a disposition bug
must never corrupt the corpus); the CALLER commits (mirrors engine_daemon.py's
_do_supersede — this module only executes/stages the writes, so an exception
raised mid-resolve leaves nothing committed and the staging row stays
'pending', satisfying "on any internal error, leave the entry pending + log").
Timestamps via lifecycle._utcnow_iso().
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crag-engine")

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from lifecycle import _utcnow_iso  # noqa: E402

try:
    from write_gate import VALID_TYPES as _VALID_INSIGHT_TYPES
except Exception:  # pragma: no cover — write_gate always ships alongside this module
    _VALID_INSIGHT_TYPES = frozenset({
        "gotcha", "pattern", "architecture", "decision", "bug-fix",
        "tool", "feedback", "user-context", "project-context", "reference",
    })

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

VALID_TIERS = ("t0", "t1", "t2")
VALID_ACTIONS = ("accept", "reject", "merge", "defer")

# Pure-python fallback used whenever the disposition_policy table is missing/
# unreachable (e.g. unit tests passing an in-memory connection without the
# table, or a daemon running against a pre-033 db). Mirrors the seed rows in
# migration 033 exactly.
DEFAULT_POLICY_RULES: list[dict] = [
    {"source": None, "type": None, "reason_prefix": "secret_scan:",
     "tier": "t2", "default_action": "defer", "deadline_hours": 168},
    {"source": None, "type": None, "reason_prefix": "schema_gate:",
     "tier": "t1", "default_action": "defer", "deadline_hours": 72},
    {"source": None, "type": None, "reason_prefix": "dedup_ambiguous",
     "tier": "t1", "default_action": "defer", "deadline_hours": 72},
    {"source": None, "type": None, "reason_prefix": "lifecycle:supersede",
     "tier": "t1", "default_action": "defer", "deadline_hours": 72},
    {"source": None, "type": None, "reason_prefix": None,
     "tier": "t0", "default_action": "accept", "deadline_hours": 24},
]


def load_policy(conn: sqlite3.Connection) -> list[dict]:
    """Load disposition_policy rows, most-specific-first (non-NULL
    reason_prefix before the wildcard). Fail-soft: any error (missing table,
    corrupt row) falls back to DEFAULT_POLICY_RULES — a policy-load failure
    must never block classification."""
    try:
        rows = conn.execute(
            "SELECT source, type, reason_prefix, tier, default_action, deadline_hours "
            "FROM disposition_policy "
            "ORDER BY (reason_prefix IS NULL), (source IS NULL), (type IS NULL), id"
        ).fetchall()
        if not rows:
            return list(DEFAULT_POLICY_RULES)
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("disposition: load_policy failed (fail-soft to defaults): %s", exc)
        return list(DEFAULT_POLICY_RULES)


def classify_tier(entry: dict, policy: Optional[list[dict]] = None) -> dict:
    """Classify a staging entry (dict with at least 'reason', optionally
    'source'/'type') against policy rules. Returns
    {"tier","default_action","deadline_hours"}. First matching rule wins;
    policy is assumed pre-sorted most-specific-first (load_policy does this).
    Fail-closed to the t0/accept/24h wildcard default on any match failure —
    never raises."""
    rules = policy if policy is not None else DEFAULT_POLICY_RULES
    reason = (entry.get("reason") or "")
    source = entry.get("source")
    type_ = entry.get("type")
    try:
        for rule in rules:
            if rule.get("source") is not None and rule["source"] != source:
                continue
            if rule.get("type") is not None and rule["type"] != type_:
                continue
            prefix = rule.get("reason_prefix")
            if prefix is not None and not reason.startswith(prefix):
                continue
            return {
                "tier": rule.get("tier", "t0"),
                "default_action": rule.get("default_action", "accept"),
                "deadline_hours": int(rule.get("deadline_hours", 24)),
            }
    except Exception as exc:
        logger.warning("disposition: classify_tier raised (fail-closed to t0): %s", exc)
    return {"tier": "t0", "default_action": "accept", "deadline_hours": 24}


def compute_deadline(deadline_hours: int, now: Optional[str] = None) -> str:
    """now + deadline_hours, canonical ISO. `now` accepts the canonical
    _utcnow_iso() format; falls back to current time on parse failure."""
    try:
        base = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    except Exception:
        base = datetime.now(timezone.utc)
    return (base + timedelta(hours=deadline_hours)).isoformat()


# ---------------------------------------------------------------------------
# Capability gate — the T1/T2 human-in-the-loop boundary
# ---------------------------------------------------------------------------

def gate_check(tier: str, action: str, capability: Optional[str]) -> bool:
    """True if `capability` authorizes `action` at `tier`. `reject`/`defer`
    never touch the corpus (they either drop or postpone), so they are always
    allowed regardless of tier/capability — only `accept`/`merge` (writes
    that reach the corpus) are gated.

    t0: always allowed.
    t1: agent-delegable — capability in {"granted","human_approved"}.
    t2: human-only for corpus writes — capability must be "human_approved"
        ("granted" alone, i.e. an agent acting on its own, is NOT enough —
        this is the literal T2 rule: "human approves, the AGENT PREPARES it").
    """
    if action in ("reject", "defer"):
        return True
    if tier == "t0":
        return True
    if tier == "t1":
        return capability in ("granted", "human_approved")
    if tier == "t2":
        return capability == "human_approved"
    return False  # unknown tier -> fail closed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_entry(row: sqlite3.Row) -> dict:
    d = dict(row)
    return d


def _log_transition(conn: sqlite3.Connection, *, staging_id: Optional[int] = None,
                    insight_id: Optional[int] = None, claim_id: Optional[int] = None,
                    principle_id: Optional[int] = None, transition: str,
                    from_state: Optional[str], to_state: Optional[str],
                    actor: str, reason: Optional[str], tier: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO disposition_log "
        "(staging_id, insight_id, claim_id, principle_id, transition, from_state, "
        " to_state, actor, reason, tier, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (staging_id, insight_id, claim_id, principle_id, transition, from_state,
         to_state, actor, reason, tier, _utcnow_iso()),
    )


def _supersede_insight(conn: sqlite3.Connection, loser_id: int, winner_id: int,
                       reason: str, actor: str) -> dict:
    """Self-contained supersede write (mirrors engine_daemon.py::_do_supersede,
    duplicated here because disposition.py must not import the daemon module
    — same pattern as write_gate.py being self-contained). Used by resolve()
    action='merge' so the result is reversible via the EXISTING supersede/
    unsupersede mechanism (cmd_unsupersede / the `supersede` MCP tool)."""
    if loser_id == winner_id:
        return {"ok": False, "error": "loser_id and winner_id must differ"}
    for iid, label in ((loser_id, "loser"), (winner_id, "winner")):
        if conn.execute("SELECT 1 FROM insights WHERE id=?", (iid,)).fetchone() is None:
            return {"ok": False, "error": f"{label} #{iid} not found"}
    now = _utcnow_iso()
    reason = (reason or "disposition:merge")[:500]
    conn.execute(
        "UPDATE insights SET superseded_by=?, superseded_at=?, "
        "supersede_reason=?, updated_at=? WHERE id=?",
        (winner_id, now, f"disposition:{reason}", now, loser_id),
    )
    try:
        conn.execute(
            "INSERT INTO arena_events (ts, project, input_insight_ids, "
            " winner_insight_id, strategy, rationale, verdict) "
            "VALUES (?, NULL, ?, ?, 'merge', ?, 'MERGED')",
            (now, json.dumps([loser_id, winner_id]), winner_id, reason),
        )
    except Exception as exc:  # arena_events is audit-only; never block the merge
        logger.warning("disposition: arena_events insert failed (non-fatal): %s", exc)
    return {"ok": True, "superseded": loser_id, "by": winner_id}


# ---------------------------------------------------------------------------
# Tier stamping (lazy — write_gate.route_to_staging() stays unchanged)
# ---------------------------------------------------------------------------

def stamp_tier(conn: sqlite3.Connection, staging_id: int,
              policy: Optional[list[dict]] = None) -> dict:
    """Ensure a staging row has tier+deadline set; classify+persist if not.
    Idempotent — a row that already has a tier is returned unchanged. Never
    raises: on any failure returns {"ok": False, "error": ...} and the row is
    left exactly as it was found."""
    try:
        row = conn.execute(
            "SELECT id, source, project, reason, tier, deadline FROM insights_staging WHERE id=?",
            (staging_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": f"staging #{staging_id} not found"}
        entry = _row_to_entry(row)
        if entry.get("tier"):
            return {"ok": True, "tier": entry["tier"], "deadline": entry.get("deadline"),
                    "already_stamped": True}
        rules = policy if policy is not None else load_policy(conn)
        verdict = classify_tier(entry, rules)
        deadline = compute_deadline(verdict["deadline_hours"])
        conn.execute(
            "UPDATE insights_staging SET tier=?, deadline=? WHERE id=?",
            (verdict["tier"], deadline, staging_id),
        )
        return {"ok": True, "tier": verdict["tier"], "deadline": deadline,
                "default_action": verdict["default_action"], "already_stamped": False}
    except Exception as exc:
        logger.warning("disposition: stamp_tier(%s) failed (fail-soft, left pending): %s",
                       staging_id, exc)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# resolve() — the single write path for every governed staging decision
# ---------------------------------------------------------------------------

def resolve(conn: sqlite3.Connection, staging_id: int, action: str, actor: str,
           reason: Optional[str] = None, target_id: Optional[int] = None,
           policy: Optional[list[dict]] = None) -> dict:
    """Execute a disposition decision on an insights_staging row.

    action:
      accept — parse the staged payload {content,type,...}, insert into
               `insights` via the existing schema (does NOT re-run write_gate
               — a staging row already failed/queued through the gate once;
               re-gating here would be circular). Terminal: status='accepted'.
      reject — drop; the entry never reaches `insights` (stays memory-only,
               per docs/architecture.md T2 "safe default" language). Terminal:
               status='rejected'.
      merge  — requires target_id (an existing insights.id). Inserts the
               staged content as a NEW insight, then immediately supersedes it
               into target_id via _supersede_insight() — reversible with the
               ordinary unsupersede mechanism. Terminal: status='merged'.
      defer  — postpone: disposition='deferred', a FRESH deadline is stamped,
               `status` stays 'pending' so the row remains in the queue for a
               future decision. Not terminal by design (see module docstring
               on t1/t2 drain-SLA safe-default semantics).

    Caller MUST commit only when result["ok"] is True (mirrors
    engine_daemon.py's `if result.get("ok"): conn.commit()` pattern) — that is
    what makes "fail-soft, leaves the entry pending" true: an exception here
    aborts before any UPDATE lands, so an un-committed connection has nothing
    to roll forward.
    """
    if not actor or not str(actor).strip():
        return {"ok": False, "error": "actor is required (attribution invariant)"}
    if action not in VALID_ACTIONS:
        return {"ok": False, "error": f"action must be one of {VALID_ACTIONS}, got {action!r}"}

    try:
        row = conn.execute(
            "SELECT * FROM insights_staging WHERE id=?", (staging_id,)
        ).fetchone()
        if row is None:
            return {"ok": False, "error": f"staging #{staging_id} not found"}
        entry = _row_to_entry(row)

        if entry.get("status") != "pending":
            return {"ok": False, "error": f"staging #{staging_id} already {entry.get('status')!r}"}

        # Ensure tier is known (classify but don't persist mid-transaction
        # twice — stamp_tier() itself is idempotent and safe to call here).
        rules = policy if policy is not None else load_policy(conn)
        if not entry.get("tier"):
            stamp = stamp_tier(conn, staging_id, rules)
            if not stamp.get("ok"):
                return stamp
            entry["tier"] = stamp["tier"]
        tier = entry["tier"]
        now = _utcnow_iso()

        try:
            payload = json.loads(entry.get("payload") or "{}")
        except (TypeError, ValueError):
            payload = {}

        if action == "accept":
            content = (payload.get("content") or "").strip()
            if not content:
                return {"ok": False, "error": "staging payload has no 'content' to accept"}
            type_ = payload.get("type") or "gotcha"
            if type_ not in _VALID_INSIGHT_TYPES:
                type_ = "gotcha"
            cur = conn.execute(
                "INSERT INTO insights (project, type, content, tags, source_file, "
                " confidence, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0.5, 'active', ?, ?)",
                (entry.get("project"), type_, content, payload.get("tags"),
                 payload.get("source_file"), now, now),
            )
            insight_id = cur.lastrowid
            conn.execute(
                "UPDATE insights_staging SET status='accepted', disposition='accepted', "
                "decided_at=?, actor=? WHERE id=?",
                (now, actor, staging_id),
            )
            _log_transition(conn, staging_id=staging_id, insight_id=insight_id,
                            transition="staging->insight", from_state="pending",
                            to_state="accepted", actor=actor, reason=reason, tier=tier)
            return {"ok": True, "action": "accept", "staging_id": staging_id,
                    "insight_id": insight_id, "tier": tier}

        if action == "reject":
            conn.execute(
                "UPDATE insights_staging SET status='rejected', disposition='rejected', "
                "decided_at=?, actor=? WHERE id=?",
                (now, actor, staging_id),
            )
            _log_transition(conn, staging_id=staging_id, transition="staging->rejected",
                            from_state="pending", to_state="rejected", actor=actor,
                            reason=reason, tier=tier)
            return {"ok": True, "action": "reject", "staging_id": staging_id, "tier": tier}

        if action == "merge":
            if not target_id:
                return {"ok": False, "error": "merge requires target_id"}
            content = (payload.get("content") or "").strip()
            if not content:
                return {"ok": False, "error": "staging payload has no 'content' to merge"}
            type_ = payload.get("type") or "gotcha"
            if type_ not in _VALID_INSIGHT_TYPES:
                type_ = "gotcha"
            cur = conn.execute(
                "INSERT INTO insights (project, type, content, tags, source_file, "
                " confidence, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0.5, 'active', ?, ?)",
                (entry.get("project"), type_, content, payload.get("tags"),
                 payload.get("source_file"), now, now),
            )
            insight_id = cur.lastrowid
            merged = _supersede_insight(conn, insight_id, target_id,
                                        reason or "disposition:merge", actor)
            if not merged.get("ok"):
                return merged
            conn.execute(
                "UPDATE insights_staging SET status='merged', disposition='merged', "
                "decided_at=?, actor=? WHERE id=?",
                (now, actor, staging_id),
            )
            _log_transition(conn, staging_id=staging_id, insight_id=insight_id,
                            transition="staging->merged", from_state="pending",
                            to_state=f"merged_into:{target_id}", actor=actor,
                            reason=reason, tier=tier)
            return {"ok": True, "action": "merge", "staging_id": staging_id,
                    "insight_id": insight_id, "merged_into": target_id, "tier": tier}

        if action == "defer":
            verdict = classify_tier(entry, rules)
            new_deadline = compute_deadline(verdict["deadline_hours"], now)
            conn.execute(
                "UPDATE insights_staging SET disposition='deferred', decided_at=?, "
                "actor=?, deadline=? WHERE id=?",
                (now, actor, new_deadline, staging_id),
            )
            _log_transition(conn, staging_id=staging_id, transition="staging->deferred",
                            from_state="pending", to_state="deferred", actor=actor,
                            reason=reason, tier=tier)
            return {"ok": True, "action": "defer", "staging_id": staging_id,
                    "tier": tier, "deadline": new_deadline}

    except Exception as exc:
        logger.warning("disposition: resolve(staging_id=%s, action=%s) raised "
                       "(fail-soft, entry left pending): %s", staging_id, action, exc)
        return {"ok": False, "error": str(exc), "left_pending": True}

    return {"ok": False, "error": "unreachable"}  # pragma: no cover


# ---------------------------------------------------------------------------
# drain_due() — the SLA sweep. Nothing rots forever.
# ---------------------------------------------------------------------------

def drain_due(conn: sqlite3.Connection, now: Optional[str] = None) -> dict:
    """Force a terminal-or-safe-default action on every PENDING staging row
    whose deadline has passed.

      t0 past deadline -> auto-execute the policy default_action (normally
                           'accept'), actor='system:drain-sla'.
      t1/t2 past deadline -> SAFE default = 'defer' (never a blind auto-
                           accept/merge of ambiguous or sensitive content),
                           which stamps a fresh deadline and is fully logged
                           (disposition_log) — visible, auditable, and NOT
                           silent, unlike the old grounding queue that rotted
                           to 236 items unattended.

    Each row is processed in its own try/except so one bad row can't abort
    the sweep. Returns a summary; never raises."""
    now = now or _utcnow_iso()
    summary = {"ok": True, "now": now, "processed": 0,
               "by_tier": {"t0": 0, "t1": 0, "t2": 0}, "ids": [], "errors": []}
    try:
        rules = load_policy(conn)
        rows = conn.execute(
            "SELECT id FROM insights_staging WHERE status='pending' "
            "AND deadline IS NOT NULL AND deadline <= ?",
            (now,),
        ).fetchall()
    except Exception as exc:
        logger.warning("disposition: drain_due query failed (fail-soft, no-op sweep): %s", exc)
        summary["ok"] = False
        summary["errors"].append(str(exc))
        return summary

    for row in rows:
        sid = row["id"]
        try:
            stamp = stamp_tier(conn, sid, rules)
            if not stamp.get("ok"):
                summary["errors"].append(f"#{sid}: stamp failed: {stamp.get('error')}")
                continue
            tier = stamp["tier"]
            entry_row = conn.execute(
                "SELECT source, reason FROM insights_staging WHERE id=?", (sid,)
            ).fetchone()
            verdict = classify_tier(_row_to_entry(entry_row), rules)
            if tier == "t0":
                result = resolve(conn, sid, verdict["default_action"],
                                 actor="system:drain-sla", reason="sla_timeout_auto",
                                 policy=rules)
            else:
                result = resolve(conn, sid, "defer", actor="system:drain-sla",
                                 reason=f"sla_timeout_{tier}_safe_default", policy=rules)
            if result.get("ok"):
                conn.commit()
                summary["processed"] += 1
                summary["by_tier"][tier] = summary["by_tier"].get(tier, 0) + 1
                summary["ids"].append(sid)
            else:
                conn.rollback()
                summary["errors"].append(f"#{sid}: {result.get('error')}")
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("disposition: drain_due row #%s raised (skipped, fail-soft): %s",
                           sid, exc)
            summary["errors"].append(f"#{sid}: {exc}")
    return summary


# ---------------------------------------------------------------------------
# Operator decision history — the rev-7 (§7.1) learning substrate
# ---------------------------------------------------------------------------

def record_operator_decision(conn: sqlite3.Connection, entity_kind: str, entity_id: int,
                             decision: str, actor: Optional[str] = None,
                             decision_class: Optional[str] = None,
                             reason: Optional[str] = None) -> dict:
    """Append one human approve/reject to operator_decision_history. Fail-
    soft: never raises, returns {"ok": False, ...} on error. Caller commits."""
    if decision not in ("approve", "reject"):
        return {"ok": False, "error": "decision must be 'approve' or 'reject'"}
    try:
        cur = conn.execute(
            "INSERT INTO operator_decision_history "
            "(entity_kind, entity_id, decision_class, decision, actor, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entity_kind, entity_id, decision_class, decision, actor, reason, _utcnow_iso()),
        )
        return {"ok": True, "id": cur.lastrowid}
    except Exception as exc:
        logger.warning("disposition: record_operator_decision failed (fail-soft): %s", exc)
        return {"ok": False, "error": str(exc)}


def suggest_tier_from_history(conn: sqlite3.Connection, decision_class: str) -> Optional[dict]:
    """TODO(rev7): learn a tier default from the operator's own approve/
    reject track record for `decision_class` (docs/architecture.md REV 7
    §7.1 — 'consistently approve class X -> X's tier auto-raises'). NOT
    implemented — this is the documented hook + storage only
    (operator_decision_history, migration 033). Always returns None so every
    caller safely no-ops until the learning loop ships."""
    return None
