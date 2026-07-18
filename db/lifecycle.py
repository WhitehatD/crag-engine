"""crag-anchor lifecycle helpers shared by the daemon, the operator CLI, and cron
(WS2 T2 + T3a). Two concerns live here because both are "designed lifecycle
rules that may mutate" (doctrine: detection flags, LIFECYCLE resolves):

  1. decay_insights()      — the confidence-decay rule (trust must be able to
     fall). Called by the daemon's weekly _decay_loop AND by crag-anchor-cli's
     `decay` command so there is exactly ONE implementation.
  2. falsifier_resolvable() — the ONE predicate that decides whether a claim's
     falsifier can be honestly checked from the laptop. Used by the daemon's
     recall Tier-2 (only flag grounding_due when checkable) AND by the
     groundskeeper cron (replacing its inline skip logic) AND by the reconcile
     endpoint. One definition = the 949-vs-9 divergence cannot recur.

Pure-ish: functions take an open sqlite3 connection; they never open/close it
and never touch the network. Import-safe from db/ (same dir as scoring.py).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import scoring


def _utcnow_iso() -> str:
    """Canonical TEXT timestamp: offset-aware UTC ISO-8601 ('T' separator).
    Never SQLite datetime('now') — its space-separated naive format sorts
    before ISO-T lexically and corrupts same-day comparisons/orderings."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# T3a — the single resolvability predicate
# ---------------------------------------------------------------------------

_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")  # C:/ , D:\ , ...
# Extract the tested path out of a path_exists spec: test -e '<path>' ...
_TEST_E_RE = re.compile(r"test -e '([^']+)'")
# Extract the source file out of a grep spec:  grep -rn '...' <path> ...
# (best-effort — grep_config/grep_symbol specs may or may not name a windows path)


def _is_windows_abs(path: str) -> bool:
    return bool(path) and bool(_WIN_ABS_RE.match(path.strip()))


def falsifier_resolvable(kind: Optional[str], spec: Optional[str],
                         entity_type: Optional[str] = None) -> bool:
    """TIER-A GATE: can this Tier-A mechanical falsifier be checked from the laptop?

    This is the shared predicate for the Tier-A mechanical path.  It answers
    "can the groundskeeper cron probe this claim right now?"  It is used by:
      - recall Tier-2: only sets grounding_due when YES (Tier-A resolvable) so the
        groundskeeper never receives unflaggable work.
      - groundskeeper: its skip logic delegates here rather than re-implementing.
      - /admin/reconcile_grounding: same gate, same answer ("flagged ⟺ checkable").

    Tier-B claims (predicate-bearing, authored by LLM with a recipe) do NOT go
    through this predicate.  When a claim's falsifier tier='B', the groundskeeper
    dispatches it to POST /ground/jobs/enqueue and the daemon worker pool + LLM
    adjudicator owns it — the claim is agentically groundable regardless of
    whether this function returns True.  The invariant "flagged ⟺ checkable"
    becomes "flagged ⟺ (Tier-A resolvable OR Tier-B agentically groundable)".

    Returns True only for:
      endpoint      → YES (curl reaches VPS/domains from the laptop).
      path_exists   → only if the spec names a WINDOWS-absolute path (D:/…, C:\\…).
                      Unix-absolute (/etc, /root/…) lives on the VPS → NO.
      grep_config /
      grep_symbol   → only if the spec references a windows-resolvable path;
                      a bare `grep … .` (CWD-relative) is NOT reliably resolvable
                      from a cron with arbitrary CWD → NO.
      none | query  → NO (agent/ground_check handles).

    Fail-closed: unknown kinds → NO. A NO means "leave it honest as
    'unverified'; do not manufacture a grounding_due flag we can never clear".
    """
    if not kind or kind in ("none", "query"):
        return False
    spec = spec or ""

    # Comment-only specs (e.g. the service-entity falsifier "# health-check
    # service '<x>' …") execute as a no-op: bash exits 0 → FAKE PASS. A spec
    # that runs nothing is not a check (WS5 quality fix).
    if not spec.strip() or spec.lstrip().startswith("#"):
        return False

    if kind == "endpoint":
        return True

    if kind == "path_exists":
        m = _TEST_E_RE.search(spec)
        target = m.group(1) if m else ""
        if not target:
            # find-based file spec (kind=path_exists from entity_type 'file') —
            # searches CWD recursively, not laptop-definitive → NO.
            return False
        if target.startswith("/"):
            return False  # unix-absolute → VPS/remote
        return _is_windows_abs(target)

    if kind in ("grep_config", "grep_symbol"):
        # Only resolvable if the spec pins a windows-absolute path to grep.
        # Auto-derived specs grep '.' (CWD) or config-globs → not laptop-definitive.
        return any(_is_windows_abs(tok) for tok in spec.split())

    return False


# ---------------------------------------------------------------------------
# T2 — the decay rule (one implementation for daemon loop + CLI)
# ---------------------------------------------------------------------------

def _select_decay_candidates(conn: sqlite3.Connection, project: Optional[str],
                             window_days: int) -> list:
    """Active, non-promoted insights not recalled within window_days, conf > floor.

    'not recalled' = last_recalled_at older than the window (or NULL and created
    older than the window). Promoted insights (promoted_to NOT NULL) are exempt —
    their trust lives in the principle now. Principles are NEVER decayed here.
    """
    win = f"-{int(window_days)} days"
    base = (
        "SELECT id, confidence, last_recalled_at, created_at FROM insights "
        "WHERE status='active' AND promoted_to IS NULL AND confidence > ? "
        "AND ( (last_recalled_at IS NULL AND created_at < datetime('now', ?)) "
        "   OR (last_recalled_at IS NOT NULL AND last_recalled_at < datetime('now', ?)) )"
    )
    params: list = [scoring.DECAY_FLOOR, win, win]
    if project:
        base += " AND project = ?"
        params.append(project)
    return conn.execute(base, params).fetchall()


def decay_insights(conn: sqlite3.Connection, project: Optional[str] = None,
                   window_days: int = scoring.DECAY_WINDOW_DAYS,
                   factor: float = scoring.DECAY_FACTOR,
                   floor: float = scoring.DECAY_FLOOR,
                   dry_run: bool = False) -> dict:
    """Apply the decay rule. Returns {ok, dry_run, decayed_insights, ids, preview}.

    When dry_run=False the caller is responsible for committing (daemon loop and
    CLI both commit). The function does NOT write operator_audit_log — the daemon
    loop writes ONE summary row (T2); the CLI stays audit-free (operator tool).
    """
    rows = _select_decay_candidates(conn, project, window_days)
    ids: list[int] = []
    preview: list[dict] = []
    for row in rows:
        old_conf = row["confidence"] if row["confidence"] is not None else 0.5
        new_conf = round(max(floor, old_conf * factor), 4)
        if new_conf >= round(old_conf, 4):
            continue  # already at/below floor — nothing to shrink
        ids.append(row["id"])
        if dry_run:
            if len(preview) < 20:
                preview.append({
                    "id": row["id"],
                    "current_confidence": round(old_conf, 4),
                    "new_confidence": new_conf,
                    "last_recalled_at": row["last_recalled_at"],
                })
        else:
            conn.execute(
                "UPDATE insights SET confidence = ?, updated_at = ? WHERE id = ?",
                (new_conf, _utcnow_iso(), row["id"]),
            )
    return {
        "ok": True,
        "dry_run": dry_run,
        "decayed_insights": len(ids),
        "ids": ids,
        "preview": preview,
    }
