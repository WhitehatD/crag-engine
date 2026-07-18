#!/usr/bin/env python
"""
crag-anchor SQLite memory backend -- CLI helper
Repository pattern: skills call this script instead of raw file I/O.

Usage:
    python db/engine-cli.py <command> [args...]

Commands:
    init                          Initialize/migrate the database
    migrate                       Apply unapplied migration files from migrations/ dir
    add-session   <json>          Add a session diary entry
    add-insight   <json>          Add an insight/decision (with dedup guard)
    search        <query> [opts]  Full-text search across all tables
    recall        <query> [opts]  Hybrid FTS5 + confidence recall (--project, --topk, --session-id)
    get-sessions  <project> [n]   Get recent sessions for a project
    get-insights  <project>       Get insights for a project
    get-context   <project>       Get project context
    set-context   <json>          Upsert project context
    stats                         Show database statistics

  Event Queue:
    add-event     <json>          Queue an event (source, event_type, summary, priority)
    get-events    [opts]          Get pending events (--project, --priority, --limit)
    claim-event   <json>          Claim an event for processing (id, claimed_by)
    complete-event <json>         Mark event completed/failed (id, status, result)
    import-events <file>          Import events from JSONL file (VPS queue sync)

  Layered Knowledge:
    verify-insight-v2 <json>      Verify with confidence scoring (id, status)
    auto-prune                    Archive low-confidence and long-stale insights
    get-principles <project>      Get distilled principles
    promote-insight <json>        Manually promote insight to principle (id)
    distill        <json>         Merge multiple insights into one principle (insight_ids, content)

  Token Ledger:
    add-token-record <json>       Record token usage (project, tokens_in, tokens_out, ...)
    cost-report    [opts]         Token/cost report (--project, --days)
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_DIR = Path(__file__).parent
DB_PATH = DB_DIR / "engine.db"
SCHEMA_PATH = DB_DIR / "schema.sql"

# WS2 T2 — crag-anchor-cli lives in db/, so db/ is on sys.path[0] when run as a script;
# ensure it's importable even if invoked oddly. Shares the decay rule with the daemon.
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))
import lifecycle  # noqa: E402 — decay_insights (shared with daemon _decay_loop)
import scoring  # noqa: E402 — WS2 T6: single source of scoring/lifecycle constants


def _utcnow_iso() -> str:
    """Canonical TEXT timestamp: offset-aware UTC ISO-8601 ('T' separator).
    NEVER SQLite datetime('now') in write statements — its space-separated
    naive format sorts before ISO-T lexically and corrupts same-day
    comparisons/orderings (2026-07-02 supersede-burst class)."""
    return datetime.now(timezone.utc).isoformat()


def _normalize_ts(value):
    """Boundary-normalize a caller-supplied timestamp (e.g. pending_events
    expires_at from external event producers) to canonical offset-aware ISO-T.
    Accepts ISO-T or 'YYYY-MM-DD HH:MM:SS'; naive values are assumed UTC.
    Returns None unchanged; raises the standard error path on garbage."""
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        print(json.dumps({"ok": False, "error": f"Invalid timestamp: {value!r} (want ISO-8601)"}))
        sys.exit(1)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_json_arg(raw: str) -> dict:
    """Parse JSON input with a clean error on failure."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"Invalid JSON input: {e}"}))
        sys.exit(1)


def require_fields(data: dict, *fields: str) -> None:
    """Validate required fields exist in data dict."""
    for f in fields:
        if f not in data or data[f] is None:
            print(json.dumps({"ok": False, "error": f"Missing required field: {f}"}))
            sys.exit(1)


def compute_shingles(text: str, k: int = 3) -> set:
    """Compute word-level k-gram shingles for Jaccard similarity."""
    words = text.lower().split()
    if len(words) < k:
        return set(words)
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two shingle sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def check_fts5(conn) -> bool:
    """Check if FTS5 is available in this SQLite build."""
    try:
        conn.execute("SELECT fts5(?)", ("test",))
        return True
    except Exception:
        try:
            conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
            conn.execute("DROP TABLE IF EXISTS _fts5_probe")
            return True
        except Exception:
            return False


def _table_exists(conn, table_name: str) -> bool:
    """Check if a table exists in the database."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    return row is not None


def get_db():
    """Get database connection, initializing schema if needed."""
    is_new = not DB_PATH.exists()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if is_new:
        conn.executescript(SCHEMA_PATH.read_text())
    return conn


def cmd_init(_args):
    """Initialize or re-apply schema."""
    conn = get_db()
    conn.executescript(SCHEMA_PATH.read_text())
    conn.close()
    print(json.dumps({"ok": True, "db": str(DB_PATH)}))


def cmd_add_session(args):
    """Add a session diary entry."""
    data = parse_json_arg(args.json)
    require_fields(data, "project")
    conn = get_db()
    conn.execute(
        """INSERT INTO sessions (project, date, accomplished, files_changed, commits,
           decisions, problems, next_steps, duration, raw_markdown)
           VALUES (:project, :date, :accomplished, :files_changed, :commits,
           :decisions, :problems, :next_steps, :duration, :raw_markdown)""",
        {
            "project": data["project"],
            "date": data.get("date", ""),
            "accomplished": data.get("accomplished", ""),
            "files_changed": data.get("files_changed", ""),
            "commits": data.get("commits", ""),
            "decisions": data.get("decisions", ""),
            "problems": data.get("problems", ""),
            "next_steps": data.get("next_steps", ""),
            "duration": data.get("duration", ""),
            "raw_markdown": data.get("raw_markdown", ""),
        },
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    print(json.dumps({"ok": True, "id": row_id}))


def cmd_add_insight(args):
    """Add an insight or decision (with cosine dedup guard, Jaccard fallback)."""
    data = parse_json_arg(args.json)
    require_fields(data, "content")
    force = data.get("force", False)
    new_content = data["content"]
    project = data.get("project")

    conn = get_db()

    # --- Dedup guard: cosine similarity >= 0.85 against last 300 active insights ---
    if not force:
        if project:
            candidates = conn.execute(
                "SELECT id, content, embedding FROM insights WHERE project = ? AND status = 'active' ORDER BY id DESC LIMIT 300",
                (project,),
            ).fetchall()
        else:
            candidates = conn.execute(
                "SELECT id, content, embedding FROM insights WHERE project IS NULL AND status = 'active' ORDER BY id DESC LIMIT 300",
            ).fetchall()

        duplicates = []
        # Try cosine dedup first (requires numpy + fastembed)
        cosine_used = False
        try:
            import numpy as np
            sys.path.insert(0, str(DB_DIR))
            from embed import embed_text
            embedded_candidates = [r for r in candidates if r["embedding"] is not None]
            if embedded_candidates:
                new_vec = np.frombuffer(embed_text(new_content), dtype="float32")
                matrix = np.vstack([np.frombuffer(r["embedding"], dtype="float32") for r in embedded_candidates])
                sims = matrix @ new_vec  # L2-normalized -> dot == cosine
                for i, sim in enumerate(sims):
                    if sim >= scoring.DEDUP_COSINE_THRESHOLD:
                        row = embedded_candidates[i]
                        duplicates.append({"id": row["id"], "content": row["content"][:120], "similarity": round(float(sim), 3)})
                cosine_used = True
        except Exception:
            pass

        # Jaccard fallback if cosine unavailable
        if not cosine_used:
            new_shingles = compute_shingles(new_content)
            for row in candidates:
                sim = jaccard(new_shingles, compute_shingles(row["content"]))
                if sim >= scoring.DEDUP_JACCARD_THRESHOLD:
                    duplicates.append({"id": row["id"], "content": row["content"][:120], "similarity": round(sim, 3)})

        if duplicates:
            conn.close()
            print(json.dumps({
                "ok": False,
                "duplicate": True,
                "candidates": duplicates,
                "message": f"Near-duplicate detected ({len(duplicates)} match(es)). Use force:true to insert anyway.",
            }))
            sys.exit(0)

    conn.execute(
        """INSERT INTO insights (project, type, content, context, tags, status, source_file, supersedes, verified_at,
                                 created_at, updated_at)
           VALUES (:project, :type, :content, :context, :tags, :status, :source_file, :supersedes, :verified_at,
                   :created_at, :updated_at)""",
        {
            "project": project,
            "type": data.get("type", "decision"),
            "content": new_content,
            "context": data.get("context", ""),
            "tags": data.get("tags", ""),
            "status": data.get("status", "active"),
            "source_file": data.get("source_file"),
            "supersedes": data.get("supersedes"),
            "verified_at": _normalize_ts(data.get("verified_at")),
            "created_at": _utcnow_iso(),
            "updated_at": _utcnow_iso(),
        },
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    recall_count = conn.execute(
        "SELECT COUNT(*) FROM recall_events WHERE insight_id = ?", (row_id,)
    ).fetchone()[0] if _table_exists(conn, "recall_events") else 0

    # Auto-embed (Phase 3) -- non-blocking, silently skipped if fastembed unavailable
    auto_embedded = False
    try:
        sys.path.insert(0, str(DB_DIR))
        from embed import embed_text
        emb_bytes = embed_text(data["content"])
        conn.execute("UPDATE insights SET embedding = ? WHERE id = ?", (emb_bytes, row_id))
        conn.commit()
        auto_embedded = True
    except Exception:
        auto_embedded = False

    conn.close()
    print(json.dumps({"ok": True, "id": row_id, "recall_count": recall_count, "auto_embedded": auto_embedded}))


def cmd_update_insight(args):
    """Update an existing insight (status, content, verified_at)."""
    data = parse_json_arg(args.json)
    require_fields(data, "id")
    conn = get_db()
    sets = []
    params = {}
    for field in ["status", "content", "context", "tags", "source_file", "verified_at", "supersedes"]:
        if field in data:
            sets.append(f"{field} = :{field}")
            params[field] = data[field]
    if not sets:
        print(json.dumps({"ok": False, "error": "No fields to update"}))
        sys.exit(1)
    sets.append("updated_at = :updated_at")
    params["updated_at"] = _utcnow_iso()
    params["id"] = data["id"]
    cur = conn.execute(f"UPDATE insights SET {', '.join(sets)} WHERE id = :id", params)
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        print(json.dumps({"ok": False, "error": f"Insight not found: {data['id']}"}))
    else:
        print(json.dumps({"ok": True, "id": data["id"]}))


def cmd_verify_insight(args):
    """Mark an insight as verified now, or flag it stale."""
    data = parse_json_arg(args.json)
    require_fields(data, "id", "status")
    conn = get_db()
    if data["status"] == "verified":
        conn.execute(
            "UPDATE insights SET status = 'active', verified_at = ?, updated_at = ? WHERE id = ?",
            (_utcnow_iso(), _utcnow_iso(), data["id"]),
        )
    elif data["status"] == "stale":
        conn.execute(
            "UPDATE insights SET status = 'stale', updated_at = ? WHERE id = ?",
            (_utcnow_iso(), data["id"]),
        )
    elif data["status"] == "invalidated":
        conn.execute(
            "UPDATE insights SET status = 'invalidated', updated_at = ? WHERE id = ?",
            (_utcnow_iso(), data["id"]),
        )
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "id": data["id"], "status": data["status"]}))


def cmd_stale_insights(args):
    """Find insights older than N days that haven't been re-verified."""
    conn = get_db()
    days = args.days or 7
    rows = conn.execute(
        """SELECT id, project, type, content, source_file, tags, status, created_at, verified_at
           FROM insights
           WHERE (project = ? OR project IS NULL)
             AND status = 'active'
             AND (verified_at IS NULL OR julianday('now') - julianday(verified_at) > ?)
             AND julianday('now') - julianday(created_at) > ?
           ORDER BY created_at ASC""",
        (args.project, days, days),
    ).fetchall()
    conn.close()
    print(json.dumps({"stale": [dict(r) for r in rows], "count": len(rows)}))


def cmd_search(args):
    """Full-text search across sessions, insights, and project_context."""
    query = f"%{args.query}%"
    limit = args.limit or 10
    project_filter = args.project

    conn = get_db()
    results = []

    # Search sessions
    sql = "SELECT id, project, date, accomplished, decisions, next_steps FROM sessions WHERE "
    params = []
    conditions = ["(accomplished LIKE ? OR decisions LIKE ? OR commits LIKE ? OR next_steps LIKE ? OR problems LIKE ?)"]
    params.extend([query] * 5)
    if project_filter:
        conditions.append("project = ?")
        params.append(project_filter)
    sql += " AND ".join(conditions) + " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    for row in conn.execute(sql, params):
        results.append({
            "type": "session",
            "id": row["id"],
            "project": row["project"],
            "date": row["date"],
            "accomplished": (row["accomplished"] or "")[:200],
            "decisions": (row["decisions"] or "")[:200],
        })

    # Search insights
    sql = "SELECT id, project, type, content, tags FROM insights WHERE content LIKE ?"
    params = [query]
    if project_filter:
        sql += " AND project = ?"
        params.append(project_filter)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    for row in conn.execute(sql, params):
        results.append({
            "type": "insight",
            "id": row["id"],
            "project": row["project"],
            "insight_type": row["type"],
            "content": row["content"][:200],
            "tags": row["tags"],
        })

    conn.close()
    print(json.dumps({"results": results, "count": len(results)}))


def cmd_get_sessions(args):
    """Get recent sessions for a project."""
    conn = get_db()
    limit = args.limit or 5
    rows = conn.execute(
        """SELECT id, project, date, accomplished, files_changed, commits,
           decisions, problems, next_steps, duration
           FROM sessions WHERE project = ? ORDER BY date DESC LIMIT ?""",
        (args.project, limit),
    ).fetchall()
    conn.close()
    sessions = [dict(r) for r in rows]
    print(json.dumps({"sessions": sessions, "count": len(sessions)}))


def cmd_get_insights(args):
    """Get insights for a project with optional filtering."""
    conn = get_db()
    conditions = ["(project = ? OR project IS NULL)"]
    params = [args.project]
    if getattr(args, "active_only", False):
        conditions.append("status = 'active'")
    if getattr(args, "min_confidence", None):
        conditions.append("confidence >= ?")
        params.append(args.min_confidence)
    sort = "confidence DESC" if getattr(args, "sort", None) == "confidence" else "created_at DESC"
    limit_clause = f"LIMIT {args.limit}" if getattr(args, "limit", None) else ""
    rows = conn.execute(
        f"SELECT * FROM insights WHERE {' AND '.join(conditions)} ORDER BY {sort} {limit_clause}",
        params,
    ).fetchall()
    conn.close()
    print(json.dumps({"insights": [dict(r) for r in rows], "count": len(rows)}))


def cmd_get_context(args):
    """Get project context."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM project_context WHERE project = ?", (args.project,)
    ).fetchone()
    conn.close()
    if row:
        print(json.dumps(dict(row)))
    else:
        print(json.dumps({"project": args.project, "status": "no context saved"}))


def cmd_set_context(args):
    """Upsert project context."""
    data = parse_json_arg(args.json)
    require_fields(data, "project")
    conn = get_db()
    conn.execute(
        """INSERT INTO project_context (project, status, current_branch, last_session_date,
           architecture_decisions, known_issues, backlog, updated_at)
           VALUES (:project, :status, :current_branch, :last_session_date,
           :architecture_decisions, :known_issues, :backlog, :updated_at)
           ON CONFLICT(project) DO UPDATE SET
           status = COALESCE(:status, status),
           current_branch = COALESCE(:current_branch, current_branch),
           last_session_date = COALESCE(:last_session_date, last_session_date),
           architecture_decisions = COALESCE(:architecture_decisions, architecture_decisions),
           known_issues = COALESCE(:known_issues, known_issues),
           backlog = COALESCE(:backlog, backlog),
           updated_at = :updated_at""",
        {
            "updated_at": _utcnow_iso(),
            "project": data["project"],
            "status": data.get("status"),
            "current_branch": data.get("current_branch"),
            "last_session_date": data.get("last_session_date"),
            "architecture_decisions": data.get("architecture_decisions"),
            "known_issues": data.get("known_issues"),
            "backlog": data.get("backlog"),
        },
    )
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "project": data["project"]}))


# ============================================================
# Event Queue commands
# ============================================================


def cmd_add_event(args):
    """Add an event to the pending queue."""
    data = parse_json_arg(args.json)
    require_fields(data, "source", "event_type", "summary")
    conn = get_db()
    conn.execute(
        """INSERT INTO pending_events (project, source, event_type, summary, payload, priority, expires_at)
           VALUES (:project, :source, :event_type, :summary, :payload, :priority, :expires_at)""",
        {
            "project": data.get("project"),
            "source": data["source"],
            "event_type": data["event_type"],
            "summary": data["summary"],
            "payload": data.get("payload", ""),
            "priority": data.get("priority", "normal"),
            "expires_at": _normalize_ts(data.get("expires_at")),
        },
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    print(json.dumps({"ok": True, "id": row_id}))


def cmd_get_events(args):
    """Get pending events, optionally filtered."""
    conn = get_db()
    conditions = ["status = 'pending'"]
    params = []
    if args.project:
        conditions.append("(project = ? OR project IS NULL)")
        params.append(args.project)
    if args.priority:
        conditions.append("priority = ?")
        params.append(args.priority)
    # Expire old events
    conn.execute(
        "UPDATE pending_events SET status = 'expired' WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?",
        (_utcnow_iso(),),
    )
    conn.commit()
    rows = conn.execute(
        f"SELECT * FROM pending_events WHERE {' AND '.join(conditions)} ORDER BY "
        "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, "
        "created_at ASC LIMIT ?",
        params + [args.limit],
    ).fetchall()
    conn.close()
    print(json.dumps({"events": [dict(r) for r in rows], "count": len(rows)}))


def cmd_claim_event(args):
    """Claim a pending event for processing."""
    data = parse_json_arg(args.json)
    require_fields(data, "id")
    conn = get_db()
    cur = conn.execute(
        "UPDATE pending_events SET status = 'claimed', claimed_by = ?, claimed_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (data.get("claimed_by", "unknown"), _utcnow_iso(), data["id"]),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        print(json.dumps({"ok": False, "error": "Event not found or already claimed"}))
    else:
        print(json.dumps({"ok": True, "id": data["id"]}))


def cmd_complete_event(args):
    """Mark an event as completed or failed."""
    data = parse_json_arg(args.json)
    require_fields(data, "id", "status")
    if data["status"] not in ("completed", "failed"):
        print(json.dumps({"ok": False, "error": "Status must be 'completed' or 'failed'"}))
        sys.exit(1)
    conn = get_db()
    cur = conn.execute(
        "UPDATE pending_events SET status = ?, result = ?, completed_at = ? WHERE id = ?",
        (data["status"], data.get("result", ""), _utcnow_iso(), data["id"]),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        print(json.dumps({"ok": False, "error": f"Event not found: {data['id']}"}))
    else:
        print(json.dumps({"ok": True, "id": data["id"], "status": data["status"]}))


def cmd_bulk_expire_events(args):
    """Expire all pending events matching optional project/priority filter."""
    conn = get_db()
    filters = ["status = 'pending'"]
    params = []
    if args.project:
        filters.append("project = ?")
        params.append(args.project)
    if args.priority:
        filters.append("priority = ?")
        params.append(args.priority)
    cur = conn.execute(
        f"UPDATE pending_events SET status = 'expired', completed_at = ? WHERE {' AND '.join(filters)}",
        [_utcnow_iso()] + params,
    )
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "expired": cur.rowcount}))


def cmd_import_events(args):
    """Import events from a JSONL file (VPS queue sync). Deduplicates by (project, source, event_type, created_at, summary)."""
    from pathlib import Path as P

    path = P(args.file)
    if not path.exists():
        print(json.dumps({"ok": True, "imported": 0, "message": "No queue file found"}))
        return
    conn = get_db()
    count = 0
    skipped = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            project = data.get("project")
            source = data.get("source", "import")
            event_type = data.get("event_type", "custom")
            summary = data.get("summary", "Imported event")
            created_at = data.get("created_at", None)
            # Deduplicate: skip if identical event already exists
            existing = conn.execute(
                """SELECT id FROM pending_events
                   WHERE project=? AND source=? AND event_type=? AND summary=? AND created_at=?
                   LIMIT 1""",
                (project, source, event_type, summary, created_at),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO pending_events (project, source, event_type, summary, payload, priority, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    project,
                    source,
                    event_type,
                    summary,
                    data.get("payload", ""),
                    data.get("priority", "normal"),
                    created_at,
                ),
            )
            count += 1
    conn.commit()
    conn.close()
    # Truncate the file after successful import
    open(path, "w").close()
    print(json.dumps({"ok": True, "imported": count, "skipped": skipped}))


# ============================================================
# Layered Knowledge commands
# ============================================================


def cmd_verify_insight_v2(args):
    """Enhanced verify-insight with confidence scoring."""
    data = parse_json_arg(args.json)
    require_fields(data, "id", "status")
    conn = get_db()

    row = conn.execute("SELECT confidence, verify_count, verify_streak FROM insights WHERE id = ?", (data["id"],)).fetchone()
    if not row:
        print(json.dumps({"ok": False, "error": f"Insight not found: {data['id']}"}))
        conn.close()
        return

    confidence = row["confidence"] or 0.5
    verify_count = (row["verify_count"] or 0) + 1
    verify_streak = row["verify_streak"] or 0

    if data["status"] == "verified":
        confidence = min(1.0, confidence + scoring.VERIFY_INSIGHT_UP)
        verify_streak += 1
        conn.execute(
            """UPDATE insights SET status = 'active', verified_at = ?,
               confidence = ?, verify_count = ?, verify_streak = ?, updated_at = ?
               WHERE id = ?""",
            (_utcnow_iso(), round(confidence, 2), verify_count, verify_streak,
             _utcnow_iso(), data["id"]),
        )
    elif data["status"] in ("stale", "invalidated"):
        confidence = max(0.0, confidence - scoring.VERIFY_INSIGHT_DOWN)
        verify_streak = 0
        conn.execute(
            """UPDATE insights SET status = ?, confidence = ?, verify_count = ?,
               verify_streak = 0, updated_at = ? WHERE id = ?""",
            (data["status"], round(confidence, 2), verify_count, _utcnow_iso(), data["id"]),
        )

    conn.commit()

    # Auto-promote: confidence >= 0.85 AND verified 3+ times AND streak >= 2
    promoted = False
    if (confidence >= scoring.AUTO_PROMOTE_MIN_CONFIDENCE
            and verify_count >= scoring.AUTO_PROMOTE_MIN_VERIFY_COUNT
            and verify_streak >= scoring.AUTO_PROMOTE_MIN_VERIFY_STREAK):
        insight = conn.execute("SELECT * FROM insights WHERE id = ? AND promoted_to IS NULL", (data["id"],)).fetchone()
        if insight:
            conn.execute(
                """INSERT INTO principles (project, content, source_insights, confidence, tags,
                                           created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (insight["project"], insight["content"], str(data["id"]), round(confidence, 2), insight["tags"],
                 _utcnow_iso(), _utcnow_iso()),
            )
            principle_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE insights SET promoted_to = ? WHERE id = ?", (principle_id, data["id"]))
            conn.commit()
            promoted = True

    conn.close()
    result = {
        "ok": True, "id": data["id"], "confidence": round(confidence, 2),
        "verify_count": verify_count, "verify_streak": verify_streak,
    }
    if promoted:
        result["promoted"] = True
        result["principle_id"] = principle_id
    print(json.dumps(result))


def cmd_auto_prune(args):
    """Archive low-confidence insights and long-stale ones."""
    conn = get_db()
    # Prune low confidence (< 0.2) with at least 2 verifications
    low_conf = conn.execute(
        "UPDATE insights SET status = 'archived' WHERE confidence < 0.2 AND verify_count >= 2 AND status != 'archived'"
    )
    # Prune stale for 30+ days
    long_stale = conn.execute(
        """UPDATE insights SET status = 'archived'
           WHERE status = 'stale'
           AND julianday('now') - julianday(COALESCE(updated_at, created_at)) > 30"""
    )
    # Prune invalidated
    invalidated = conn.execute(
        "UPDATE insights SET status = 'archived' WHERE status = 'invalidated'"
    )
    conn.commit()
    total = low_conf.rowcount + long_stale.rowcount + invalidated.rowcount
    conn.close()
    print(json.dumps({
        "ok": True, "pruned": total,
        "low_confidence": low_conf.rowcount,
        "long_stale": long_stale.rowcount,
        "invalidated": invalidated.rowcount,
    }))


def cmd_get_principles(args):
    """Get principles for a project."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM principles WHERE project = ? OR project IS NULL ORDER BY confidence DESC",
        (args.project,),
    ).fetchall()
    conn.close()
    print(json.dumps({"principles": [dict(r) for r in rows], "count": len(rows)}))


def cmd_stale_principles(args):
    """Return principles not updated in the last N days."""
    days = args.days or 30
    conn = get_db()
    rows = conn.execute(
        """SELECT id, project, content, confidence, updated_at FROM principles
           WHERE (project = ? OR project IS NULL)
           AND julianday('now') - julianday(updated_at) > ?
           ORDER BY updated_at ASC""",
        (args.project, days),
    ).fetchall()
    conn.close()
    print(json.dumps({"ok": True, "count": len(rows), "stale": [dict(r) for r in rows]}))


def cmd_promote_insight(args):
    """Manually promote an insight to a principle."""
    data = parse_json_arg(args.json)
    require_fields(data, "id")
    conn = get_db()
    insight = conn.execute("SELECT * FROM insights WHERE id = ?", (data["id"],)).fetchone()
    if not insight:
        print(json.dumps({"ok": False, "error": f"Insight not found: {data['id']}"}))
        conn.close()
        return
    if insight["promoted_to"]:
        print(json.dumps({"ok": False, "error": f"Already promoted to principle {insight['promoted_to']}"}))
        conn.close()
        return
    content = data.get("content", insight["content"])
    conn.execute(
        """INSERT INTO principles (project, content, source_insights, confidence, tags,
                                   created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (insight["project"], content, str(data["id"]), scoring.PROMOTE_SEED_CONFIDENCE, insight["tags"],
         _utcnow_iso(), _utcnow_iso()),
    )
    principle_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "UPDATE insights SET promoted_to = ?, updated_at = ? WHERE id = ?",
        (principle_id, _utcnow_iso(), data["id"]),
    )
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "principle_id": principle_id, "source_insight": data["id"]}))


def cmd_distill(args):
    """Distill multiple related insights into a single principle."""
    data = parse_json_arg(args.json)
    require_fields(data, "insight_ids", "content")
    conn = get_db()
    ids = list(dict.fromkeys(data["insight_ids"]))  # dedup, preserve order
    rows = conn.execute(
        f"SELECT id, project, tags FROM insights WHERE id IN ({','.join('?' * len(ids))})", ids,
    ).fetchall()
    if len(rows) != len(ids):
        found = {r["id"] for r in rows}
        missing = [i for i in ids if i not in found]
        print(json.dumps({"ok": False, "error": f"Insights not found: {missing}"}))
        conn.close()
        return
    project = rows[0]["project"]
    all_tags = set()
    for r in rows:
        if r["tags"]:
            all_tags.update(t.strip() for t in r["tags"].split(","))
    conn.execute(
        """INSERT INTO principles (project, content, source_insights, confidence, tags,
                                   created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (project, data["content"], ",".join(str(i) for i in ids),
         scoring.PROMOTE_SEED_CONFIDENCE, ",".join(sorted(all_tags)),
         _utcnow_iso(), _utcnow_iso()),
    )
    principle_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for i in ids:
        conn.execute("UPDATE insights SET promoted_to = ?, updated_at = ? WHERE id = ?", (principle_id, _utcnow_iso(), i))
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "principle_id": principle_id, "distilled_from": ids}))


# ============================================================
# Token Ledger commands
# ============================================================


def cmd_add_token_record(args):
    """Record token usage for a session."""
    data = parse_json_arg(args.json)
    require_fields(data, "project")
    conn = get_db()
    conn.execute(
        """INSERT INTO token_ledger (session_id, project, task_summary, tokens_in, tokens_out,
           cache_hits, cache_misses, rtk_savings_pct, headroom_savings_pct, wall_time_sec, model,
           cache_read_tokens, cache_write_tokens, fresh_input_tokens)
           VALUES (:session_id, :project, :task_summary, :tokens_in, :tokens_out,
           :cache_hits, :cache_misses, :rtk_savings_pct, :headroom_savings_pct, :wall_time_sec, :model,
           :cache_read_tokens, :cache_write_tokens, :fresh_input_tokens)""",
        {
            "session_id": data.get("session_id"),
            "project": data["project"],
            "task_summary": data.get("task_summary", ""),
            "tokens_in": data.get("tokens_in", 0),
            "tokens_out": data.get("tokens_out", 0),
            "cache_hits": data.get("cache_hits", 0),
            "cache_misses": data.get("cache_misses", 0),
            "rtk_savings_pct": data.get("rtk_savings_pct", 0),
            "headroom_savings_pct": data.get("headroom_savings_pct", 0),
            "wall_time_sec": data.get("wall_time_sec", 0),
            "model": data.get("model"),
            # Phase 11: cache split — 0 when not provided (backward-compatible)
            "cache_read_tokens": data.get("cache_read_tokens", 0),
            "cache_write_tokens": data.get("cache_write_tokens", 0),
            "fresh_input_tokens": data.get("fresh_input_tokens", 0),
        },
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    print(json.dumps({"ok": True, "id": row_id}))


def cmd_cost_report(args):
    """Generate a cost/token report with aggregation."""
    conn = get_db()
    conditions = []
    params = []
    if args.project:
        conditions.append("project = ?")
        params.append(args.project)
    if args.days:
        conditions.append("julianday('now') - julianday(created_at) <= ?")
        params.append(args.days)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Totals
    totals = conn.execute(
        f"""SELECT COUNT(*) as sessions, SUM(tokens_in) as total_in, SUM(tokens_out) as total_out,
            SUM(cache_hits) as total_cache_hits, SUM(wall_time_sec) as total_wall_sec,
            AVG(rtk_savings_pct) as avg_rtk, AVG(headroom_savings_pct) as avg_headroom
            FROM token_ledger {where}""",
        params,
    ).fetchone()

    # Per-project breakdown
    by_project = conn.execute(
        f"""SELECT project, COUNT(*) as sessions, SUM(tokens_in) as tokens_in,
            SUM(tokens_out) as tokens_out, SUM(cache_hits) as cache_hits,
            AVG(rtk_savings_pct) as avg_rtk, AVG(headroom_savings_pct) as avg_headroom
            FROM token_ledger {where} GROUP BY project ORDER BY tokens_in DESC""",
        params,
    ).fetchall()

    # Trend (last 7 entries)
    trend = conn.execute(
        f"""SELECT date(created_at) as day, SUM(tokens_in) as tokens_in, SUM(tokens_out) as tokens_out,
            COUNT(*) as sessions FROM token_ledger {where}
            GROUP BY date(created_at) ORDER BY day DESC LIMIT 7""",
        params,
    ).fetchall()

    conn.close()
    print(json.dumps({
        "totals": dict(totals) if totals else {},
        "by_project": [dict(r) for r in by_project],
        "trend": [dict(r) for r in trend],
    }))


def cmd_salvage_staged(args):
    """Salvage pending staged insights: dedup check, insert non-dupes, mark dupes.

    For each pending row in insights_staged:
      - Run cosine/jaccard dedup against live active insights
      - Non-duplicate → INSERT into insights (preserve original created_at,
        project, tags += ',salvaged-staging')
      - Duplicate → UPDATE decision='rejected-dup-at-salvage'

    --dry-run: report counts without writing.
    Approved/rejected/auto-rejected rows are left as historical record.
    """
    dry_run = args.dry_run
    conn = get_db()

    # Get all pending rows
    rows = conn.execute(
        "SELECT * FROM insights_staged WHERE decision='pending' ORDER BY id"
    ).fetchall()
    total = len(rows)
    if total == 0:
        conn.close()
        print(json.dumps({"ok": True, "total": 0, "inserted": 0, "dup_rejected": 0, "dry_run": dry_run}))
        return

    # Load active insights for dedup comparison
    candidates = conn.execute(
        "SELECT id, content, embedding FROM insights WHERE status = 'active' ORDER BY id DESC LIMIT 500"
    ).fetchall()

    # Try loading embedding support
    cosine_available = False
    embed_fn = None
    np = None
    try:
        import numpy as _np
        np = _np
        sys.path.insert(0, str(DB_DIR))
        from embed import embed_text
        embed_fn = embed_text
        embedded_cands = [r for r in candidates if r["embedding"] is not None]
        if embedded_cands:
            cosine_available = True
            cand_ids = [r["id"] for r in embedded_cands]
            cand_matrix = np.vstack([np.frombuffer(r["embedding"], dtype="float32") for r in embedded_cands])
    except Exception:
        pass

    inserted = 0
    dup_rejected = 0
    details = []

    for row in rows:
        content = row["content"]
        is_dup = False
        dup_id = None
        sim_score = 0.0

        # Cosine dedup
        if cosine_available and embed_fn and np:
            try:
                new_vec = np.frombuffer(embed_fn(content), dtype="float32")
                sims = cand_matrix @ new_vec
                max_idx = int(np.argmax(sims))
                max_sim = float(sims[max_idx])
                if max_sim >= scoring.DEDUP_COSINE_THRESHOLD:
                    is_dup = True
                    dup_id = cand_ids[max_idx]
                    sim_score = round(max_sim, 3)
            except Exception:
                pass

        # Jaccard fallback
        if not cosine_available and not is_dup:
            new_shingles = compute_shingles(content)
            for cand in candidates:
                sim = jaccard(new_shingles, compute_shingles(cand["content"]))
                if sim >= scoring.DEDUP_JACCARD_THRESHOLD:
                    is_dup = True
                    dup_id = cand["id"]
                    sim_score = round(sim, 3)
                    break

        # Cache embedding bytes for this row so the salvaged row is immediately
        # visible to the semantic recall arm. embed_fn is available whenever
        # the fastembed import succeeded, even if no candidates had embeddings.
        row_emb_bytes = None
        if embed_fn:
            try:
                row_emb_bytes = embed_fn(content)
            except Exception:
                pass

        if is_dup:
            dup_rejected += 1
            detail = {
                "staged_id": row["id"],
                "action": "rejected-dup-at-salvage",
                "dup_of": dup_id,
                "similarity": sim_score,
                "content_preview": content[:80],
            }
            details.append(detail)
            if not dry_run:
                conn.execute(
                    "UPDATE insights_staged SET decision='rejected-dup-at-salvage', "
                    "decision_reason=? WHERE id=?",
                    (f"dup of insight #{dup_id} (sim={sim_score})", row["id"]),
                )
        else:
            inserted += 1
            # Merge tags: original + salvaged-staging marker
            orig_tags = row["tags"] or ""
            merged_tags = (orig_tags + ",salvaged-staging").strip(",")
            detail = {
                "staged_id": row["id"],
                "action": "insert",
                "content_preview": content[:80],
            }
            details.append(detail)
            if not dry_run:
                conn.execute(
                    """INSERT INTO insights (project, type, content, context, tags,
                       status, source_file, created_at, updated_at)
                       VALUES (?,?,?,?,?, 'active',?,?,?)""",
                    (
                        row["project"],
                        row["type"] or "decision",
                        content,
                        "",
                        merged_tags,
                        row["source_file"],
                        row["created_at"] or _utcnow_iso(),
                        _utcnow_iso(),
                    ),
                )
                new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                # Store embedding so the row surfaces in semantic recall immediately.
                # Uses the same raw float32 bytes format as the daemon's _embed_one().
                if row_emb_bytes is not None:
                    conn.execute(
                        "UPDATE insights SET embedding = ? WHERE id = ?",
                        (row_emb_bytes, new_id),
                    )
                conn.execute(
                    "UPDATE insights_staged SET decision='salvaged', "
                    "decision_reason=? WHERE id=?",
                    (f"inserted as insight #{new_id}", row["id"]),
                )
                detail["insight_id"] = new_id

    if not dry_run:
        conn.commit()
    conn.close()

    result = {
        "ok": True,
        "dry_run": dry_run,
        "total_pending": total,
        "inserted": inserted,
        "dup_rejected": dup_rejected,
        "details": details,
    }
    if not dry_run and embed_fn is None:
        result["next_step"] = "run backfill-embeddings"
    print(json.dumps(result))


def cmd_brief(args):
    """Output compact JSON brief for a project -- principles, top insights, last session."""
    try:
        conn = get_db()
        project = args.project

        # All principles, truncated at 120 chars each
        principles_rows = conn.execute(
            "SELECT content FROM principles WHERE project = ? OR project IS NULL ORDER BY confidence DESC",
            (project,),
        ).fetchall()
        principles = [r["content"][:120] for r in principles_rows]

        # Top 5 insights by confidence, active only
        insight_rows = conn.execute(
            """SELECT type, content, confidence FROM insights
               WHERE (project = ? OR project IS NULL) AND status = 'active'
               ORDER BY confidence DESC LIMIT 5""",
            (project,),
        ).fetchall()
        top_insights = [
            {"type": r["type"], "content": r["content"][:120], "confidence": r["confidence"]}
            for r in insight_rows
        ]

        # Most recent session accomplished field, 120 chars
        session_row = conn.execute(
            "SELECT date, accomplished FROM sessions WHERE project = ? ORDER BY date DESC LIMIT 1",
            (project,),
        ).fetchone()
        last_session = None
        if session_row and session_row["accomplished"]:
            last_session = f"{session_row['date']}: {session_row['accomplished']}"[:120]

        conn.close()
        print(json.dumps({
            "project": project,
            "principles": principles,
            "top_insights": top_insights,
            "last_session": last_session,
        }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_db_maintenance(args):
    """Delete old completed/expired events from pending_events."""
    try:
        days = args.delete_completed_before
        conn = get_db()
        cur = conn.execute(
            "DELETE FROM pending_events WHERE status IN ('completed', 'expired') AND created_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),),
        )
        conn.commit()
        deleted = cur.rowcount
        conn.close()
        print(json.dumps({"ok": True, "deleted": deleted}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_stats(_args):
    """Show database statistics."""
    conn = get_db()
    sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    insights = conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
    active_insights = conn.execute("SELECT COUNT(*) FROM insights WHERE status = 'active'").fetchone()[0]
    principles = conn.execute("SELECT COUNT(*) FROM principles").fetchone()[0]
    projects = conn.execute("SELECT COUNT(*) FROM project_context").fetchone()[0]
    pending_events = conn.execute("SELECT COUNT(*) FROM pending_events WHERE status = 'pending'").fetchone()[0]
    token_records = conn.execute("SELECT COUNT(*) FROM token_ledger").fetchone()[0]

    by_project = conn.execute(
        "SELECT project, COUNT(*) as cnt FROM sessions GROUP BY project ORDER BY cnt DESC"
    ).fetchall()

    # Insight health
    avg_confidence = conn.execute(
        "SELECT AVG(confidence) FROM insights WHERE status = 'active'"
    ).fetchone()[0]

    conn.close()
    print(json.dumps({
        "sessions": sessions,
        "insights": insights,
        "active_insights": active_insights,
        "principles": principles,
        "avg_insight_confidence": round(avg_confidence, 2) if avg_confidence else 0,
        "projects": projects,
        "pending_events": pending_events,
        "token_records": token_records,
        "sessions_by_project": {r["project"]: r["cnt"] for r in by_project},
        "db_path": str(DB_PATH),
        "db_size_kb": round(DB_PATH.stat().st_size / 1024, 1) if DB_PATH.exists() else 0,
    }))


def cmd_recall(args):
    """Hybrid recall: 0.50 cosine + 0.35 FTS5 + 0.15 confidence.

    Falls back to Phase 1 weights (0.65 FTS5 + 0.35 confidence) when
    embeddings unavailable (fastembed not installed or no backfill yet).
    """
    import numpy as np

    query = args.query.strip()
    project = getattr(args, "project", None)
    topk = getattr(args, "topk", 5) or 5
    session_id = getattr(args, "session_id", None)
    snippet = getattr(args, "snippet", False)

    conn = get_db()

    # --- Try semantic embedding for the query ---
    query_vec = None
    embed_available = False
    try:
        sys.path.insert(0, str(DB_DIR))  # ensure embed.py is importable
        from embed import embed_text
        query_vec = np.frombuffer(embed_text(query), dtype="float32")
        embed_available = True
    except Exception:
        pass

    # --- Load embeddings from DB and compute cosine in batch ---
    cosine_scores = {}  # insight_id -> cosine score
    if query_vec is not None:
        project_filter_sql = "(project = ? OR project IS NULL)" if project else "1=1"
        emb_params = [project] if project else []
        emb_rows = conn.execute(
            f"""SELECT id, embedding FROM insights
                WHERE status='active' AND embedding IS NOT NULL
                AND {project_filter_sql}""",
            emb_params,
        ).fetchall()

        if emb_rows:
            ids = [r["id"] for r in emb_rows]
            matrix = np.vstack(
                [np.frombuffer(r["embedding"], dtype="float32") for r in emb_rows]
            )
            # MiniLM is L2-normalized, so dot product == cosine similarity
            sims = matrix @ query_vec  # shape (N,)
            cosine_scores = {ids[i]: float(sims[i]) for i in range(len(ids))}
        else:
            # No embeddings in DB; behave as if unavailable for scoring
            embed_available = False

    # --- FTS5 search ---
    fts_rows = []
    fts_scores = {}  # insight_id -> normalized FTS5 score in [0,1]
    try:
        fts_sql = """
            SELECT i.id, i.project, i.type, i.content, i.tags,
                   i.source_file, i.confidence, i.verify_count,
                   fts.rank AS fts_rank
            FROM insights_fts fts
            JOIN insights i ON i.id = fts.rowid
            WHERE insights_fts MATCH ?
              AND i.status = 'active'
        """
        fts_params = [query]
        if project:
            fts_sql += " AND (i.project = ? OR i.project IS NULL)"
            fts_params.append(project)
        fts_sql += " ORDER BY fts.rank LIMIT 50"
        fts_rows = conn.execute(fts_sql, fts_params).fetchall()
    except Exception:
        # Fall back to LIKE if FTS5 table not yet built
        fts_sql = """
            SELECT id, project, type, content, tags, source_file,
                   confidence, verify_count, -1.0 AS fts_rank
            FROM insights
            WHERE content LIKE ? AND status = 'active'
        """
        like_params = [f"%{query}%"]
        if project:
            fts_sql += " AND (project = ? OR project IS NULL)"
            like_params.append(project)
        fts_sql += " LIMIT 50"
        fts_rows = conn.execute(fts_sql, like_params).fetchall()

    if fts_rows:
        ranks = [r["fts_rank"] for r in fts_rows]
        min_r, max_r = min(ranks), max(ranks)
        rng = max_r - min_r if max_r != min_r else 1.0
        for r in fts_rows:
            # FTS5 rank: more negative = better. Invert so 1 = best.
            norm = 1.0 - (r["fts_rank"] - min_r) / rng
            fts_scores[r["id"]] = norm

    # --- Compute hybrid scores ---
    insight_list = []
    if embed_available and cosine_scores:
        # Candidate set = top-30 by cosine UNION all FTS5 hits
        cosine_top = sorted(
            cosine_scores.keys(), key=lambda x: cosine_scores[x], reverse=True
        )[:30]
        all_candidate_ids = list(set(cosine_top + [r["id"] for r in fts_rows]))

        if not all_candidate_ids:
            conn.close()
            print(json.dumps({
                "query": query,
                "insights": [],
                "principles": [],
                "count": 0,
                "embed_available": embed_available,
            }))
            return

        placeholders = ",".join("?" * len(all_candidate_ids))
        candidates = conn.execute(
            f"""SELECT id, project, type, content, tags, source_file, confidence
                FROM insights WHERE id IN ({placeholders})""",
            all_candidate_ids,
        ).fetchall()

        for r in candidates:
            cos = cosine_scores.get(r["id"], 0.0)
            fts = fts_scores.get(r["id"], 0.0)
            conf = r["confidence"] if r["confidence"] is not None else 0.5
            hybrid = round(scoring.HYBRID_W_COSINE * cos + scoring.HYBRID_W_FTS * fts
                           + scoring.HYBRID_W_CONF * conf, 4)
            insight_list.append({
                "kind": "insight",
                "id": r["id"],
                "type": r["type"],
                "project": r["project"],
                "content": r["content"],
                "tags": r["tags"],
                "source_file": r["source_file"],
                "confidence": conf,
                "score": hybrid,
                "cosine": round(cos, 4),
                "fts": round(fts, 4),
            })
    else:
        # FTS5-only fallback (Phase 1 weights)
        for r in fts_rows:
            fts = fts_scores.get(r["id"], 0.0)
            conf = r["confidence"] if r["confidence"] is not None else 0.5
            insight_list.append({
                "kind": "insight",
                "id": r["id"],
                "type": r["type"],
                "project": r["project"],
                "content": r["content"],
                "tags": r["tags"],
                "source_file": r["source_file"],
                "confidence": conf,
                "score": round(scoring.NOEMB_W_FTS * fts + scoring.NOEMB_W_CONF * conf, 4),
            })

    insight_list.sort(key=lambda x: x["score"], reverse=True)
    insight_list = insight_list[:topk]

    # --- Principle search (simple LIKE, principles are small) ---
    p_params = [f"%{query}%"]
    p_sql = "SELECT id, project, content, tags, confidence FROM principles WHERE content LIKE ?"
    if project:
        p_sql += " AND (project = ? OR project IS NULL)"
        p_params.append(project)
    p_sql += " ORDER BY confidence DESC LIMIT 5"
    principle_rows = conn.execute(p_sql, p_params).fetchall()

    # --- Log to recall ledger ---
    if session_id and insight_list and _table_exists(conn, "recall_events"):
        for rank, hit in enumerate(insight_list):
            conn.execute(
                "INSERT INTO recall_events (insight_id, session_id, query, hit_rank) VALUES (?, ?, ?, ?)",
                (hit["id"], session_id, query, rank),
            )
        conn.commit()

    conn.close()

    # Apply snippet mode (truncate content, drop tags/source_file)
    if snippet:
        for item in insight_list:
            item["content"] = item["content"][:200]
            item.pop("tags", None)
            item.pop("source_file", None)

    print(json.dumps({
        "query": query,
        "insights": insight_list,
        "principles": [
            {"id": r["id"], "content": r["content"], "confidence": r["confidence"]}
            for r in principle_rows
        ],
        "count": len(insight_list) + len(principle_rows),
        "embed_available": embed_available,
    }))


def cmd_hot_insights(args):
    """Return top-K insights by recent recall frequency + confidence.

    Used by pre-start to load a small 'working memory' set instead of dumping all insights.
    Scoring: 2 * recall_hits_in_last_N_days + confidence (raw, no normalization).
    """
    project = args.project
    limit = getattr(args, "limit", None) or 10
    days = getattr(args, "days", None) or 30

    conn = get_db()
    rows = conn.execute(
        """SELECT i.id, i.project, i.type, i.content, i.tags,
                  i.source_file, i.confidence, i.verify_count,
                  COALESCE(r.recall_count, 0) AS recall_hits
           FROM insights i
           LEFT JOIN (
               SELECT insight_id, COUNT(*) AS recall_count
               FROM recall_events
               WHERE recalled_at > ?
               GROUP BY insight_id
           ) r ON r.insight_id = i.id
           WHERE (i.project = ? OR i.project IS NULL) AND i.status = 'active'
           ORDER BY (COALESCE(r.recall_count, 0) * 2.0 + i.confidence) DESC
           LIMIT ?""",
        # Python ISO cutoff — never SQLite datetime('now',...) as a boundary
        # against canonical ISO-T columns (principle #121).
        ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(), project, limit),
    ).fetchall()
    conn.close()
    print(json.dumps({"insights": [dict(r) for r in rows], "count": len(rows)}))


def cmd_backfill_embeddings(args):
    """Embed all active insights that don't have an embedding yet."""
    sys.path.insert(0, str(DB_DIR))
    try:
        from embed import embed_batch
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

    conn = get_db()
    rows = conn.execute(
        "SELECT id, content FROM insights WHERE status='active' AND embedding IS NULL"
    ).fetchall()
    total = len(rows)

    if total == 0:
        conn.close()
        print(json.dumps({"ok": True, "backfilled": 0, "message": "All active insights already embedded"}))
        return

    batch_size = getattr(args, "batch", None) or 100
    updated = 0

    for i in range(0, total, batch_size):
        batch = rows[i : i + batch_size]
        texts = [r["content"] for r in batch]
        try:
            emb_bytes_list = embed_batch(texts)
        except Exception as e:
            conn.close()
            print(json.dumps({"ok": False, "error": str(e), "processed": updated}))
            sys.exit(1)

        for row, emb_bytes in zip(batch, emb_bytes_list):
            conn.execute(
                "UPDATE insights SET embedding = ? WHERE id = ?",
                (emb_bytes, row["id"]),
            )
        conn.commit()
        updated += len(batch)
        # Progress goes to stdout (JSON only -- stderr may be intercepted by RTK)
        print(json.dumps({"progress": updated, "total": total, "pct": round(updated / total * 100, 1)}), flush=True)

    conn.close()
    print(json.dumps({"ok": True, "backfilled": updated}))


def cmd_backfill_falsifiers(args):
    """Re-derive every stored falsifier against the CURRENT derivation rules.

    One-shot corrective backfill after a derive_falsifier logic change (WS5
    2026-07-02: entity quality gates for k8s-vocab domains, reverse-package
    names, third-party/hallucinated endpoints, private/reserved IPs, and
    comment-only no-op specs). Same class as backfill-embeddings — it fixes
    stored DERIVED state; it never touches confidence (detection != resolution).

    For each falsifier row: re-derive from claim content.
      - new kind 'none'      -> DELETE the row (no checkable entity remains)
      - (kind, spec) changed -> UPDATE + reset last_run_at/last_result/last_detail
                                (the old probe result was against a stale spec)
    Then for each OPEN grounding_queue row whose (re-derived) falsifier is now
    'none' or not laptop-resolvable, CLOSE it (resolution='falsifier_corrected')
    and clear the claim's grounding_due — nothing actionable remains, so it must
    not sit in the agent's review queue nor be re-probed as garbage next cron.
    """
    sys.path.insert(0, str(DB_DIR))
    from entity_extract import derive_falsifier
    from lifecycle import falsifier_resolvable

    dry = getattr(args, "dry_run", False)
    conn = get_db()
    tbl_for = {"insight": "insights", "principle": "principles"}

    updated = deleted = unchanged = cleared_stale = 0
    fals = conn.execute(
        "SELECT id, claim_kind, claim_id, kind, spec, last_result FROM falsifiers").fetchall()
    for f in fals:
        src = tbl_for.get(f["claim_kind"])
        if not src:
            continue
        row = conn.execute(f"SELECT content FROM {src} WHERE id=?", (f["claim_id"],)).fetchone()
        if row is None:
            continue
        d = derive_falsifier(row["content"] or "")
        nk, ns = d.get("kind"), d.get("spec")
        if nk == "none":
            deleted += 1
            if not dry:
                conn.execute("DELETE FROM falsifiers WHERE id=?", (f["id"],))
        elif (f["kind"], f["spec"]) != (nk, ns):
            updated += 1
            if not dry:
                conn.execute(
                    "UPDATE falsifiers SET kind=?, spec=?, entity=?, entity_type=?, "
                    "last_run_at=NULL, last_result=NULL, last_detail=NULL, updated_at=? WHERE id=?",
                    (nk, ns, d.get("entity"), d.get("entity_type"), _utcnow_iso(), f["id"]))
        elif not falsifier_resolvable(nk, ns, d.get("entity_type")) and f["last_result"] is not None:
            # Spec unchanged but it's not laptop-resolvable, yet it carries a probe
            # result — that result is stale/dishonest (the laptop can't run this).
            cleared_stale += 1
            if not dry:
                conn.execute(
                    "UPDATE falsifiers SET last_run_at=NULL, last_result=NULL, last_detail=NULL WHERE id=?",
                    (f["id"],))
        else:
            unchanged += 1

    # Close orphaned open queue rows + clear their flags.
    closed = 0
    q = conn.execute(
        "SELECT id, claim_kind, claim_id FROM grounding_queue WHERE status='open'"
    ).fetchall()
    for qr in q:
        src = tbl_for.get(qr["claim_kind"])
        if not src:
            continue
        row = conn.execute(f"SELECT content FROM {src} WHERE id=?", (qr["claim_id"],)).fetchone()
        if row is None:
            continue
        d = derive_falsifier(row["content"] or "")
        if not falsifier_resolvable(d.get("kind"), d.get("spec"), d.get("entity_type")):
            closed += 1
            if not dry:
                conn.execute(
                    "UPDATE grounding_queue SET status='resolved', resolved_at=?, "
                    "resolved_by='backfill-falsifiers', resolution='falsifier_corrected' WHERE id=?",
                    (_utcnow_iso(), qr["id"]))
                conn.execute(
                    f"UPDATE {src} SET grounding_due=0 WHERE id=?", (qr["claim_id"],))

    if not dry:
        conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "dry_run": dry, "falsifiers_updated": updated,
                      "falsifiers_deleted": deleted, "falsifiers_unchanged": unchanged,
                      "stale_results_cleared": cleared_stale, "queue_rows_closed": closed}))


# ============================================================
# Phase 4 Lifecycle & Hygiene commands
# ============================================================

def cmd_backfill_grounding_v2(args):
    """Backfill Grounding v2 tier classification for all active insights + principles.

    Iterates every active (non-superseded) insight and principle, classifies its
    content using grounding_author.classify_tier, then:
      Tier A: (re)derive the mechanical falsifier inline using the existing
              derive_falsifier path (same as backfill-falsifiers). Cheap + synchronous.
      Tier B: INSERT OR IGNORE an 'author' job into grounding_jobs. The dedup
              partial index prevents double-enqueue. This command NEVER calls
              the LLM itself — the daemon worker pool drains the queue over time,
              naturally rate-smoothed.

    Flags: --project, --limit, --dry-run. Progress every 100 claims.
    """
    sys.path.insert(0, str(DB_DIR))
    from grounding_author import classify_tier
    from entity_extract import derive_falsifier

    dry = getattr(args, "dry_run", False)
    limit = getattr(args, "limit", None)
    project = getattr(args, "project", None)

    conn = get_db()

    tbl_ok = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='grounding_jobs'"
    ).fetchone()
    if not tbl_ok and not dry:
        print(json.dumps({"ok": False,
                          "error": "grounding_jobs table not found — run crag-anchor-cli migrate"}))
        conn.close()
        return

    proj_clause = " AND project = ?" if project else ""
    proj_args = [project] if project else []

    ins_sql = (
        "SELECT id, project, content FROM insights "
        "WHERE superseded_by IS NULL AND (status IS NULL OR status='active')"
        + proj_clause
    )
    pri_sql = (
        "SELECT id, project, content FROM principles WHERE superseded_by IS NULL"
        + proj_clause
    )

    claims = []
    for r in conn.execute(ins_sql, proj_args).fetchall():
        claims.append(("insight", r["id"], r["project"], r["content"]))
    for r in conn.execute(pri_sql, proj_args).fetchall():
        claims.append(("principle", r["id"], r["project"], r["content"]))

    if limit:
        claims = claims[:limit]

    counts = {"tier_a": 0, "tier_b_enqueued": 0, "tier_b_dedup": 0, "tier_a_updated": 0}
    processed = 0

    for kind, claim_id, _proj, content in claims:
        tier = classify_tier(content or "", [])
        if tier == "A":
            counts["tier_a"] += 1
            if not dry:
                d = derive_falsifier(content or "")
                if d.get("kind") != "none":
                    existing = conn.execute(
                        "SELECT id FROM falsifiers WHERE claim_kind=? AND claim_id=?",
                        (kind, claim_id),
                    ).fetchone()
                    ts = _utcnow_iso()
                    if existing:
                        conn.execute(
                            "UPDATE falsifiers SET kind=?, spec=?, entity=?, entity_type=?,"
                            " tier='A', authored_by='mechanical', updated_at=? WHERE id=?",
                            (d.get("kind"), d.get("spec"), d.get("entity"),
                             d.get("entity_type"), ts, existing["id"]),
                        )
                    else:
                        conn.execute(
                            "INSERT OR IGNORE INTO falsifiers"
                            " (claim_kind, claim_id, kind, spec, entity, entity_type,"
                            "  tier, authored_by, created_at, updated_at)"
                            " VALUES (?,?,?,?,?,?,'A','mechanical',?,?)",
                            (kind, claim_id, d.get("kind"), d.get("spec"),
                             d.get("entity"), d.get("entity_type"), ts, ts),
                        )
                    counts["tier_a_updated"] += 1
        else:
            # Tier B: enqueue 'author' job — NEVER call LLM here.
            if not dry:
                from grounding_queue_v2 import enqueue_job  # local import avoids circular at module level
                inserted = enqueue_job(conn, kind, claim_id, "author", priority=1)
                if inserted:
                    counts["tier_b_enqueued"] += 1
                else:
                    counts["tier_b_dedup"] += 1
            else:
                counts["tier_b_enqueued"] += 1  # dry-run: count as would-enqueue

        processed += 1
        if processed % 100 == 0:
            if not dry:
                conn.commit()
            print(f"  ... processed {processed}/{len(claims)}", flush=True)

    if not dry:
        conn.commit()
    conn.close()

    print(json.dumps({
        "ok": True,
        "dry_run": dry,
        "project": project,
        "total_processed": processed,
        "tier_a_classified": counts["tier_a"],
        "tier_a_falsifiers_written": counts["tier_a_updated"],
        "tier_b_jobs_enqueued": counts["tier_b_enqueued"],
        "tier_b_jobs_dedup": counts["tier_b_dedup"],
    }))


def cmd_backfill_graph_v2(args):
    """Graph v2 backfill (migration 027): normalize entity_links, populate entity_canonical.

    For each row in entity_links:
      - Run normalize(entity_type, entity) from entity_normalize.py.
      - REJECT: entity_links row gets canonical_entity_id=NULL; no entity_canonical insert.
        Tag: rejected_reason written to a local report (row NOT modified — append-only).
      - ACCEPT: upsert into entity_canonical; UPDATE entity_links.canonical_entity_id.

    Also seeds claim_relations from existing structural relationships:
      - superseded_by → REPLACES
      - promoted_to provenance (source_insights) → REFINES
      - arena losers (arena_events) → CONTRADICTS
      - contradiction pairs (contradictions) → CONTRADICTS

    Reports before/after orphan + distinct-entity stats.
    --dry-run: compute but do NOT write. --project: filter entity_links by joined insight project.
    """
    sys.path.insert(0, str(DB_DIR))
    try:
        from entity_normalize import normalize
    except ImportError as e:
        print(json.dumps({"ok": False, "error": f"entity_normalize import failed: {e}"}))
        return

    dry = getattr(args, "dry_run", False)
    project = getattr(args, "project", None)

    conn = get_db()

    # Check migration 027 tables exist
    tbl_ok = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_canonical'"
    ).fetchone()
    if not tbl_ok and not dry:
        print(json.dumps({"ok": False,
                          "error": "entity_canonical table missing — run crag-anchor-cli migrate"}))
        conn.close()
        return

    # --- Before stats ---
    total_links = conn.execute("SELECT COUNT(*) FROM entity_links").fetchone()[0]
    orphan_before = conn.execute(
        "SELECT COUNT(*) FROM entity_links WHERE canonical_entity_id IS NULL"
    ).fetchone()[0]
    distinct_before = conn.execute(
        "SELECT COUNT(DISTINCT lower(entity) || '|' || entity_type) FROM entity_links"
    ).fetchone()[0]

    # --- Normalize entity_links ---
    project_join = ""
    project_cond = ""
    project_arg = []
    if project:
        project_join = "LEFT JOIN insights i ON i.id = el.insight_id "
        project_cond = " AND (i.project = ? OR el.insight_id IS NULL)"
        project_arg = [project]

    rows = conn.execute(
        f"SELECT el.id, el.entity, el.entity_type FROM entity_links el "
        f"{project_join} WHERE el.canonical_entity_id IS NULL{project_cond}",
        project_arg,
    ).fetchall()

    stats = {"accepted": 0, "rejected": 0, "already_done": 0, "errors": 0}
    rejected_reasons: list[dict] = []

    for row in rows:
        try:
            norm = normalize(row["entity_type"], row["entity"])
        except Exception:
            stats["errors"] += 1
            continue

        if norm["reject"]:
            stats["rejected"] += 1
            rejected_reasons.append({
                "id": row["id"],
                "entity": row["entity"],
                "entity_type": row["entity_type"],
                "reason": norm["reason"],
            })
            # append-only: do NOT delete or mark the entity_links row
        else:
            stats["accepted"] += 1
            canonical = norm["canonical"]
            if not dry:
                try:
                    # Find existing canonical row by (entity_type, canonical) first —
                    # so /opt/crag-anchor and /crag-anchor share one canonical_entity_id.
                    existing = conn.execute(
                        "SELECT id FROM entity_canonical WHERE entity_type=? AND canonical=?",
                        (row["entity_type"], canonical),
                    ).fetchone()
                    if existing:
                        ec_id = existing["id"]
                        # Register this raw_value as an alias if not already the canonical entry
                        # (INSERT OR IGNORE so we don't overwrite the existing canonical row)
                        try:
                            conn.execute(
                                "INSERT OR IGNORE INTO entity_canonical "
                                "(entity_type, raw_value, canonical) VALUES (?, ?, ?)",
                                (row["entity_type"], row["entity"], canonical),
                            )
                        except Exception:
                            pass
                    else:
                        conn.execute(
                            """INSERT INTO entity_canonical (entity_type, raw_value, canonical)
                               VALUES (?, ?, ?)
                               ON CONFLICT(entity_type, raw_value) DO UPDATE
                               SET canonical = excluded.canonical""",
                            (row["entity_type"], row["entity"], canonical),
                        )
                        ec_row = conn.execute(
                            "SELECT id FROM entity_canonical WHERE entity_type=? AND raw_value=?",
                            (row["entity_type"], row["entity"]),
                        ).fetchone()
                        ec_id = ec_row["id"] if ec_row else None

                    if ec_id:
                        conn.execute(
                            "UPDATE entity_links SET canonical_entity_id=? WHERE id=?",
                            (ec_id, row["id"]),
                        )
                except Exception:
                    stats["errors"] += 1

    # --- Seed claim_relations (mechanical only, no LLM) ---
    cr_stats = {"replaces": 0, "refines": 0, "contradicts": 0}

    if not dry and tbl_ok:
        # 1. superseded_by → REPLACES (insight superseded by another insight)
        sup_rows = conn.execute(
            "SELECT id, superseded_by FROM insights WHERE superseded_by IS NOT NULL"
        ).fetchall()
        for r in sup_rows:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO claim_relations
                       (claim_a_kind, claim_a_id, relation_type, claim_b_kind, claim_b_id)
                       VALUES ('insight', ?, 'REPLACES', 'insight', ?)""",
                    (r["superseded_by"], r["id"]),  # winner REPLACES loser
                )
                cr_stats["replaces"] += 1
            except Exception:
                pass

        # 2. source_insights provenance → REFINES (insight promoted to principle)
        prom_rows = conn.execute(
            "SELECT id, source_insights FROM principles WHERE source_insights IS NOT NULL"
        ).fetchall()
        for r in prom_rows:
            try:
                src_ids = json.loads(r["source_insights"])
                for src_id in (src_ids if isinstance(src_ids, list) else [src_ids]):
                    conn.execute(
                        """INSERT OR IGNORE INTO claim_relations
                           (claim_a_kind, claim_a_id, relation_type, claim_b_kind, claim_b_id)
                           VALUES ('principle', ?, 'REFINES', 'insight', ?)""",
                        (r["id"], int(src_id)),
                    )
                    cr_stats["refines"] += 1
            except Exception:
                pass

        # 3. Contradiction pairs → CONTRADICTS (table may not exist in all deployments)
        cont_tbl = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contradictions'"
        ).fetchone()
        cont_rows = conn.execute(
            "SELECT insight_a_id, insight_b_id FROM contradictions "
            "WHERE status IS NULL OR status != 'resolved'"
        ).fetchall() if cont_tbl else []
        for r in cont_rows:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO claim_relations
                       (claim_a_kind, claim_a_id, relation_type, claim_b_kind, claim_b_id,
                        confidence)
                       VALUES ('insight', ?, 'CONTRADICTS', 'insight', ?, 0.9)""",
                    (r["insight_a_id"], r["insight_b_id"]),
                )
                cr_stats["contradicts"] += 1
            except Exception:
                pass

        conn.commit()

    # --- Entity relations seeding heuristic: ip+port, service+port, domain+ip co-occurrence ---
    #
    # PART E advisory 2: the original heuristic required EXACTLY 1 candidate on
    # each side ("len(ports) == 1"). Real infra insights routinely co-mention
    # several ports/services in one sentence ("the proxy (8787) and
    # the router (8788) both restart via ..."), so the exactly-1 gate
    # silently produced ZERO entity_relations for the busiest, most
    # cross-referenced entities — verified via /graph/impact('8787','port')
    # returning hop1_neighbors=0 despite port 8787 appearing in 19 insights,
    # every one of which also mentioned >=1 other port. Measured corpus
    # histogram: port/service/ip/domain counts per insight are overwhelmingly
    # 1-5 (the "doc-dump" long tail is 10+), so relax the gate to a bounded
    # cross-product (<= _ER_MAX_PER_TYPE per side) instead of exactly-1. This
    # trades a bit of pairing precision (a doc mentioning 3 ports x 2 services
    # yields some incorrect cross pairs) for actually populating the graph —
    # acceptable for a MECHANICAL heuristic (migration 027 comment: "Tier-B
    # LLM authoring is future work").
    _ER_MAX_PER_TYPE = 5
    er_stats = {"ip_port": 0, "svc_port": 0, "domain_ip": 0}

    if not dry and tbl_ok:
        insight_rows = conn.execute(
            "SELECT DISTINCT insight_id FROM entity_links WHERE insight_id IS NOT NULL"
        ).fetchall()
        for irow in insight_rows:
            iid = irow["insight_id"]
            ents = conn.execute(
                """SELECT ec.id as ec_id, ec.entity_type, ec.canonical
                   FROM entity_links el
                   JOIN entity_canonical ec ON ec.id = el.canonical_entity_id
                   WHERE el.insight_id = ?""",
                (iid,),
            ).fetchall()
            by_type: dict[str, list] = {}
            for e in ents:
                by_type.setdefault(e["entity_type"], []).append(e)

            # ip USES_PORT port (bounded cross-product, both sides <= cap)
            ips = by_type.get("ip", [])
            ports = by_type.get("port", [])
            if 1 <= len(ips) <= _ER_MAX_PER_TYPE and 1 <= len(ports) <= _ER_MAX_PER_TYPE:
                for ip in ips:
                    for port in ports:
                        try:
                            conn.execute(
                                """INSERT OR IGNORE INTO entity_relations
                                   (entity_a_id, relation_type, entity_b_id, metadata)
                                   VALUES (?, 'USES_PORT', ?, ?)""",
                                (ip["ec_id"], port["ec_id"],
                                 json.dumps({"source_insight": iid})),
                            )
                            er_stats["ip_port"] += 1
                        except Exception:
                            pass

            # service USES_PORT port (bounded cross-product)
            svcs = by_type.get("service", [])
            if 1 <= len(svcs) <= _ER_MAX_PER_TYPE and 1 <= len(ports) <= _ER_MAX_PER_TYPE:
                for svc in svcs:
                    for port in ports:
                        try:
                            conn.execute(
                                """INSERT OR IGNORE INTO entity_relations
                                   (entity_a_id, relation_type, entity_b_id, metadata)
                                   VALUES (?, 'USES_PORT', ?, ?)""",
                                (svc["ec_id"], port["ec_id"],
                                 json.dumps({"source_insight": iid})),
                            )
                            er_stats["svc_port"] += 1
                        except Exception:
                            pass

            # domain RESOLVES_TO ip (bounded cross-product)
            domains = by_type.get("domain", [])
            if 1 <= len(domains) <= _ER_MAX_PER_TYPE and 1 <= len(ips) <= _ER_MAX_PER_TYPE:
                for domain in domains:
                    for ip in ips:
                        try:
                            conn.execute(
                                """INSERT OR IGNORE INTO entity_relations
                                   (entity_a_id, relation_type, entity_b_id, metadata)
                                   VALUES (?, 'RESOLVES_TO', ?, ?)""",
                                (domain["ec_id"], ip["ec_id"],
                                 json.dumps({"source_insight": iid})),
                            )
                            er_stats["domain_ip"] += 1
                        except Exception:
                            pass

        if er_stats["ip_port"] or er_stats["svc_port"] or er_stats["domain_ip"]:
            conn.commit()

    # --- After stats ---
    if dry:
        orphan_after = orphan_before - stats["accepted"]
        distinct_after = distinct_before  # approximate (no writes)
    else:
        orphan_after = conn.execute(
            "SELECT COUNT(*) FROM entity_links WHERE canonical_entity_id IS NULL"
        ).fetchone()[0]
        distinct_after = conn.execute(
            "SELECT COUNT(*) FROM entity_canonical"
        ).fetchone()[0]

    conn.close()

    print(json.dumps({
        "ok": True,
        "dry_run": dry,
        "entity_links_total": total_links,
        "processed": len(rows),
        "accepted": stats["accepted"],
        "rejected": stats["rejected"],
        "errors": stats["errors"],
        "orphan_before": orphan_before,
        "orphan_after": orphan_after,
        "distinct_entities_before": distinct_before,
        "distinct_entities_after": distinct_after,
        "claim_relations_seeded": cr_stats,
        "entity_relations_seeded": er_stats,
        "rejected_sample": rejected_reasons[:10],
    }))


def cmd_decay(args):
    """Decay confidence of insights not recalled recently.

    WS2 T2 — the insight-decay rule now lives in db/lifecycle.py, SHARED with the
    daemon's weekly _decay_loop (one implementation, two callers). Behavioral
    change vs the old inline copy: promoted insights (promoted_to NOT NULL) are
    exempt — their trust lives in the principle. Principle flagging (pending
    events for re-verification) remains CLI-only below.
    """
    project = args.project
    dry_run = args.dry_run
    insight_days = args.insight_days
    principle_days = args.principle_days

    conn = get_db()

    decay_res = lifecycle.decay_insights(
        conn, project=project, window_days=insight_days, dry_run=dry_run,
    )
    decayed_count = decay_res["decayed_insights"]
    dry_run_details = decay_res.get("preview", [])

    # Flag stale principles (no recall in principle_days)
    principles_flagged = 0
    if project:
        p_rows = conn.execute(
            """SELECT p.id, p.content, p.updated_at
               FROM principles p
               WHERE p.project = ?
                 AND p.updated_at < datetime('now', ? )""",
            (project, f"-{principle_days} days"),
        ).fetchall()
    else:
        p_rows = conn.execute(
            """SELECT p.id, p.content, p.updated_at
               FROM principles p
               WHERE p.updated_at < ?""",
            ((datetime.now(timezone.utc) - timedelta(days=principle_days)).isoformat(),),
        ).fetchall()

    for p_row in p_rows:
        if not dry_run:
            # Add a pending event to flag this principle for re-verification
            conn.execute(
                """INSERT OR IGNORE INTO pending_events
                   (project, source, event_type, summary, payload, priority)
                   VALUES (?, 'engine-decay', 'principle_re_verify', ?, ?, 'normal')""",
                (
                    project,
                    f"Principle #{p_row['id']} has not been updated in {principle_days}+ days",
                    json.dumps({"principle_id": p_row["id"], "content_preview": p_row["content"][:80]}),
                ),
            )
        principles_flagged += 1

    if not dry_run:
        conn.commit()
    conn.close()

    result = {
        "ok": True,
        "dry_run": dry_run,
        "decayed_insights": decayed_count,
        "principles_flagged": principles_flagged,
    }
    if dry_run and dry_run_details:
        result["preview"] = dry_run_details
    print(json.dumps(result))


def cmd_distill_candidates(args):
    """Find near-duplicate insight clusters as candidates for distillation."""
    project = args.project
    threshold = args.threshold
    limit = args.limit

    conn = get_db()

    # Load all active insights with embeddings for this project
    rows = conn.execute(
        """SELECT id, content, embedding FROM insights
           WHERE status = 'active' AND project = ? AND embedding IS NOT NULL
             AND (promoted_to IS NULL)
           ORDER BY id""",
        (project,),
    ).fetchall()
    conn.close()

    if len(rows) < 2:
        print(json.dumps({"ok": True, "threshold": threshold, "pairs": [], "message": "Not enough embedded insights"}))
        return

    try:
        import numpy as np
    except ImportError:
        print(json.dumps({"ok": False, "error": "numpy not available -- required for distill-candidates"}))
        return

    ids = [r["id"] for r in rows]
    contents = [r["content"] for r in rows]
    matrix = np.vstack([np.frombuffer(r["embedding"], dtype="float32") for r in rows])  # N x 384

    # Pairwise cosine: vectors are L2-normalized -> dot product == cosine
    cosine_matrix = matrix @ matrix.T  # N x N

    pairs = []
    n = len(ids)
    for i in range(n):
        for j in range(i + 1, n):  # upper triangle only (unique unordered pairs)
            sim = float(cosine_matrix[i, j])
            if sim >= threshold:
                pairs.append({
                    "id_a": ids[i],
                    "id_b": ids[j],
                    "similarity": round(sim, 4),
                    "content_a": contents[i][:80],
                    "content_b": contents[j][:80],
                })

    # Sort by similarity descending
    pairs.sort(key=lambda x: x["similarity"], reverse=True)
    pairs = pairs[:limit]

    print(json.dumps({"ok": True, "threshold": threshold, "pairs": pairs, "total_insights_scanned": n}))


def cmd_suggest_tags(args):
    """Suggest existing tags based on semantic similarity to input content."""
    content = args.content
    project = args.project
    limit = args.limit

    try:
        import numpy as np
    except ImportError:
        print(json.dumps({"ok": False, "error": "numpy not available -- required for suggest-tags"}))
        return

    sys.path.insert(0, str(DB_DIR))
    try:
        from embed import embed_text
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"embed unavailable: {e}"}))
        return

    conn = get_db()

    # Load embedded insights for the project
    if project:
        rows = conn.execute(
            """SELECT id, tags, embedding FROM insights
               WHERE status = 'active' AND project = ? AND embedding IS NOT NULL AND tags IS NOT NULL AND tags != ''
               ORDER BY id DESC LIMIT 500""",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, tags, embedding FROM insights
               WHERE status = 'active' AND embedding IS NOT NULL AND tags IS NOT NULL AND tags != ''
               ORDER BY id DESC LIMIT 500""",
        ).fetchall()

    conn.close()

    if not rows:
        print(json.dumps({"ok": True, "suggested_tags": [], "based_on": [], "message": "No embedded insights with tags found"}))
        return

    new_vec = np.frombuffer(embed_text(content), dtype="float32")
    ids = [r["id"] for r in rows]
    matrix = np.vstack([np.frombuffer(r["embedding"], dtype="float32") for r in rows])
    sims = matrix @ new_vec

    # Get top 20 most similar
    top_indices = sims.argsort()[::-1][:20]
    tag_counts = {}
    based_on = []
    for idx in top_indices:
        sim = float(sims[idx])
        based_on.append({"id": ids[idx], "similarity": round(sim, 4)})
        tag_str = rows[idx]["tags"] or ""
        for tag in [t.strip() for t in tag_str.split(",") if t.strip()]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Sort by frequency, take top `limit`
    suggested = sorted(tag_counts, key=lambda t: tag_counts[t], reverse=True)[:limit]

    print(json.dumps({
        "ok": True,
        "suggested_tags": suggested,
        "based_on": based_on,
    }))


def cmd_recall_stats(args):
    """Show engine usage telemetry: hottest insights, top queries, dead weight, promotion candidates."""
    project = getattr(args, "project", None)
    days = getattr(args, "days", None) or 7

    conn = get_db()

    # Hottest insights (most recall_events in last N days)
    hot = conn.execute(
        """SELECT i.id, i.type, i.content, i.confidence,
                   COUNT(re.id) AS hits,
                   COUNT(DISTINCT re.session_id) AS distinct_sessions
            FROM recall_events re
            JOIN insights i ON i.id = re.insight_id
            WHERE re.recalled_at > ? AND i.status='active'
                  AND (? IS NULL OR i.project = ? OR i.project IS NULL)
            GROUP BY i.id ORDER BY hits DESC LIMIT 10""",
        ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(), project, project),
    ).fetchall()

    # Top queries (dedup)
    queries = conn.execute(
        """SELECT query, COUNT(*) AS times, COUNT(DISTINCT session_id) AS distinct_sessions
            FROM recall_events
            WHERE recalled_at > ? AND query IS NOT NULL
            GROUP BY query ORDER BY times DESC LIMIT 10""",
        ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),),
    ).fetchall()

    # Dead weight: active insights with 0 recalls in last 30 days, created >30d ago
    dead = conn.execute(
        """SELECT i.id, i.type, substr(i.content, 1, 80) AS snippet, i.confidence, i.created_at
            FROM insights i
            WHERE i.status='active'
                  AND i.created_at < datetime('now', '-30 days')
                  AND (? IS NULL OR i.project = ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM recall_events re
                      WHERE re.insight_id = i.id
                        AND re.recalled_at > datetime('now', '-30 days')
                  )
            ORDER BY i.confidence ASC, i.created_at ASC LIMIT 20""",
        (project, project),
    ).fetchall()

    # Cross-project promotion candidates: insights recalled from 3+ DISTINCT sessions
    promote = conn.execute(
        """SELECT i.id, i.project, substr(i.content, 1, 100) AS snippet,
                   COUNT(DISTINCT re.session_id) AS distinct_sessions,
                   COUNT(re.id) AS total_recalls
            FROM recall_events re
            JOIN insights i ON i.id = re.insight_id
            WHERE i.status='active' AND i.project IS NOT NULL
                  AND re.recalled_at > datetime('now', '-30 days')
            GROUP BY i.id
            HAVING distinct_sessions >= 3
            ORDER BY distinct_sessions DESC, total_recalls DESC LIMIT 10""",
    ).fetchall()

    conn.close()
    print(json.dumps({
        "ok": True, "project": project, "days": days,
        "hot_insights": [dict(r) for r in hot],
        "top_queries": [dict(r) for r in queries],
        "dead_weight": [dict(r) for r in dead],
        "cross_project_promote_candidates": [dict(r) for r in promote],
    }))


# ── Phase 13 — Memory arena (supersede edges + adjudication) ──────────────────

import math
from pathlib import Path as _Path


def _arena_score_recency(rows: list[dict]) -> dict:
    """Return {id: score}; newest insight wins."""
    return {r["id"]: r["updated_at"] or r["created_at"] or "" for r in rows}


def _arena_score_confidence(rows: list[dict]) -> dict:
    """Return {id: score}; highest (confidence × log(1+verify_count)) wins."""
    out = {}
    for r in rows:
        c = float(r["confidence"] or 0.0)
        v = int(r["verify_count"] or 0)
        out[r["id"]] = c * math.log1p(v)
    return out


def _arena_score_evidence(rows: list[dict]) -> dict:
    """Return {id: score}; 1.0 if source_file exists on disk, 0.0 otherwise.

    Cannot guarantee the file CONTAINS the claim (would require LLM judgment),
    but presence-of-file is a strong signal that the source is still live —
    insights pointing at deleted files are stale by definition.
    """
    out = {}
    for r in rows:
        path = (r["source_file"] or "").strip()
        if not path:
            out[r["id"]] = 0.0
            continue
        try:
            out[r["id"]] = 1.0 if _Path(path).exists() else 0.0
        except OSError:
            out[r["id"]] = 0.0
    return out


def _arena_winner_by_strategy(rows: list[dict], strategy: str) -> tuple[int | None, str]:
    """Return (winner_id_or_None, rationale).  None means strategy is indecisive."""
    if not rows:
        return None, "empty input"
    if strategy == "recency":
        scores = _arena_score_recency(rows)
    elif strategy == "confidence":
        scores = _arena_score_confidence(rows)
    elif strategy == "evidence":
        scores = _arena_score_evidence(rows)
    else:
        return None, f"unknown strategy: {strategy}"
    # Winner is the strict max. Ties are indecisive.
    max_score = max(scores.values())
    top = [iid for iid, s in scores.items() if s == max_score]
    if len(top) != 1:
        return None, f"{strategy}: tied between {top}"
    return top[0], f"{strategy}: winner #{top[0]} score={max_score}"


def _arena_auto(rows: list[dict]) -> tuple[int | None, str, dict]:
    """Run all three sub-strategies; require majority agreement.

    Returns (winner_id_or_None, rationale, per_strategy_results).
    """
    results = {}
    for s in ("recency", "confidence", "evidence"):
        w, r = _arena_winner_by_strategy(rows, s)
        results[s] = {"winner": w, "rationale": r}
    votes = {}
    for s, payload in results.items():
        w = payload["winner"]
        if w is None:
            continue
        votes[w] = votes.get(w, 0) + 1
    if not votes:
        return None, "auto: all strategies indecisive", results
    top_id, top_votes = max(votes.items(), key=lambda kv: kv[1])
    if top_votes >= 2:
        return top_id, f"auto: #{top_id} won {top_votes}/3 strategies", results
    return None, "auto: three-way split, no majority", results


def cmd_arena(args):
    """Adjudicate between N insights; mark losers as superseded by the winner.

    JSON shape:
      {"insight_ids":[...], "strategy":"auto|recency|confidence|evidence|merge",
       "project":"myproject", "merged_content":"..."(strategy=merge only),
       "dry_run":false}

    Strategy 'merge' creates a NEW insight from `merged_content` and marks
    every input as superseded by the new one (similar to distill but at the
    insight level, with explicit edges).
    """
    data = parse_json_arg(args.json)
    require_fields(data, "insight_ids", "strategy")
    ids = list(dict.fromkeys(int(i) for i in data["insight_ids"]))
    if len(ids) < 2:
        print(json.dumps({"ok": False, "error": "need at least 2 insight_ids"}))
        return
    strategy = data["strategy"]
    dry_run = bool(data.get("dry_run", False))

    conn = get_db()
    placeholders = ",".join("?" * len(ids))
    rows = [dict(r) for r in conn.execute(
        f"""SELECT id, project, type, content, confidence,
                   COALESCE(verify_count, 0) AS verify_count,
                   source_file, tags, created_at, updated_at,
                   superseded_by
            FROM insights WHERE id IN ({placeholders})""",
        ids,
    ).fetchall()]
    if len(rows) != len(ids):
        found = {r["id"] for r in rows}
        missing = [i for i in ids if i not in found]
        print(json.dumps({"ok": False, "error": f"Insights not found: {missing}"}))
        conn.close()
        return
    # Skip already-superseded inputs unless caller really insists.
    already = [r["id"] for r in rows if r["superseded_by"]]
    if already and not data.get("allow_resupersede"):
        print(json.dumps({"ok": False, "error": f"Already superseded: {already}. Pass allow_resupersede:true to override."}))
        conn.close()
        return

    project = (data.get("project") or rows[0]["project"]) or None
    now = datetime.now(timezone.utc).isoformat()

    if strategy == "merge":
        require_fields(data, "merged_content")
        if dry_run:
            print(json.dumps({"ok": True, "verdict": "MERGED", "dry_run": True,
                              "would_supersede": ids}))
            conn.close()
            return
        # Combine tags from inputs, dedup
        all_tags = set()
        for r in rows:
            if r["tags"]:
                all_tags.update(t.strip() for t in r["tags"].split(","))
        # Type: prefer the most common, fall back to "architecture"
        types = [r["type"] for r in rows if r["type"]]
        new_type = max(set(types), key=types.count) if types else "architecture"
        conn.execute(
            """INSERT INTO insights (project, type, content, tags, source_file, confidence, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 0.7, ?, ?)""",
            (project, new_type, data["merged_content"],
             ",".join(sorted(all_tags)),
             rows[0]["source_file"] or "", now, now),
        )
        merged_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Mark all inputs as superseded
        for i in ids:
            conn.execute(
                """UPDATE insights
                   SET superseded_by = ?, superseded_at = ?,
                       supersede_reason = 'merged-by-arena', updated_at = ?
                   WHERE id = ?""",
                (merged_id, now, now, i),
            )
        conn.execute(
            """INSERT INTO arena_events (ts, project, input_insight_ids, winner_insight_id,
                                          strategy, rationale, merged_insight_id, verdict)
               VALUES (?, ?, ?, ?, 'merge', ?, ?, 'MERGED')""",
            (now, project, json.dumps(ids), None,
             f"merged {len(ids)} insights into new #{merged_id}", merged_id),
        )
        conn.commit()
        conn.close()
        print(json.dumps({"ok": True, "verdict": "MERGED", "merged_id": merged_id,
                          "superseded": ids}))
        return

    # Single-strategy or auto adjudication
    if strategy == "auto":
        winner, rationale, per = _arena_auto(rows)
    elif strategy in ("recency", "confidence", "evidence"):
        winner, rationale = _arena_winner_by_strategy(rows, strategy)
        per = None
    else:
        print(json.dumps({"ok": False, "error": f"unknown strategy: {strategy}"}))
        conn.close()
        return

    if winner is None:
        # Log the ambiguous adjudication for audit but make NO changes.
        if not dry_run:
            conn.execute(
                """INSERT INTO arena_events (ts, project, input_insight_ids, winner_insight_id,
                                              strategy, rationale, verdict)
                   VALUES (?, ?, ?, NULL, ?, ?, 'AMBIGUOUS')""",
                (now, project, json.dumps(ids), strategy, rationale),
            )
            conn.commit()
        conn.close()
        out = {"ok": True, "verdict": "AMBIGUOUS", "rationale": rationale}
        if per:
            out["per_strategy"] = per
        print(json.dumps(out))
        return

    losers = [i for i in ids if i != winner]
    if dry_run:
        conn.close()
        out = {"ok": True, "verdict": "WINNER", "winner": winner, "losers": losers,
               "dry_run": True, "rationale": rationale}
        if per:
            out["per_strategy"] = per
        print(json.dumps(out))
        return

    for loser in losers:
        conn.execute(
            """UPDATE insights
               SET superseded_by = ?, superseded_at = ?,
                   supersede_reason = ?, updated_at = ?
               WHERE id = ?""",
            (winner, now, f"arena:{strategy}", now, loser),
        )
    conn.execute(
        """INSERT INTO arena_events (ts, project, input_insight_ids, winner_insight_id,
                                      strategy, rationale, verdict)
           VALUES (?, ?, ?, ?, ?, ?, 'WINNER')""",
        (now, project, json.dumps(ids), winner, strategy, rationale),
    )
    conn.commit()
    conn.close()
    out = {"ok": True, "verdict": "WINNER", "winner": winner, "losers": losers,
           "rationale": rationale}
    if per:
        out["per_strategy"] = per
    print(json.dumps(out))


def cmd_supersede(args):
    """Manually mark loser_id as superseded by winner_id."""
    data = parse_json_arg(args.json)
    require_fields(data, "loser_id", "winner_id")
    loser = int(data["loser_id"])
    winner = int(data["winner_id"])
    if loser == winner:
        print(json.dumps({"ok": False, "error": "loser_id and winner_id must differ"}))
        return
    reason = data.get("reason", "manual")
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    # Validate both exist
    for iid, label in [(loser, "loser"), (winner, "winner")]:
        if conn.execute("SELECT 1 FROM insights WHERE id = ?", (iid,)).fetchone() is None:
            print(json.dumps({"ok": False, "error": f"{label} #{iid} not found"}))
            conn.close()
            return
    conn.execute(
        """UPDATE insights
           SET superseded_by = ?, superseded_at = ?,
               supersede_reason = ?, updated_at = ?
           WHERE id = ?""",
        (winner, now, f"manual:{reason}", now, loser),
    )
    # Reason left as 'manual:...' so the audit log makes it findable.
    conn.execute(
        """INSERT INTO arena_events (ts, project, input_insight_ids, winner_insight_id,
                                      strategy, rationale, verdict)
           VALUES (?, NULL, ?, ?, 'manual', ?, 'WINNER')""",
        (now, json.dumps([loser, winner]), winner, reason),
    )
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "superseded": loser, "by": winner, "reason": reason}))


def cmd_unsupersede(args):
    """Reverse a supersede edge — restore an insight to active status."""
    iid = int(args.id)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    row = conn.execute("SELECT superseded_by FROM insights WHERE id = ?", (iid,)).fetchone()
    if row is None:
        print(json.dumps({"ok": False, "error": f"#{iid} not found"}))
        conn.close()
        return
    if row["superseded_by"] is None:
        print(json.dumps({"ok": True, "noop": True, "id": iid}))
        conn.close()
        return
    conn.execute(
        """UPDATE insights
           SET superseded_by = NULL, superseded_at = NULL,
               supersede_reason = NULL, updated_at = ?
           WHERE id = ?""",
        (now, iid),
    )
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "restored": iid}))


def cmd_audit_contradictions(args):
    """List insights flagged by the contradiction detector (suspect_of edges).

    The engine stores contradiction edges as the suspect_of /
    suspect_reason / suspect_score / suspect_detected_at columns on the
    `insights` table (set by contradiction.py when a new save triggers a
    high-similarity-low-entailment match against an existing insight).

    Returns only non-superseded rows so the operator's work queue is finite.
    """
    project = args.project if hasattr(args, "project") and args.project else None
    conn = get_db()
    sql = (
        "SELECT id, project, type, confidence, "
        "       substr(content, 1, 200) AS snippet, "
        "       suspect_of, suspect_score, suspect_detected_at, "
        "       created_at, updated_at "
        "FROM insights "
        "WHERE suspect_of IS NOT NULL AND superseded_by IS NULL"
    )
    params = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY suspect_detected_at DESC LIMIT 200"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    print(json.dumps({
        "ok": True,
        "count": len(rows),
        "contradictions": [dict(r) for r in rows],
    }))


def cmd_audit_drift(args):
    """Find non-superseded insights whose content contains a stale claim.

    JSON shape: {"pattern":"198.51.100.7", "project":"myproject"}
    Returns a list of candidate insights to supersede or update. Caller picks.
    """
    data = parse_json_arg(args.json)
    require_fields(data, "pattern")
    pattern = data["pattern"]
    project = data.get("project")
    conn = get_db()
    sql = (
        "SELECT id, project, type, confidence, "
        "       substr(content, 1, 240) AS snippet, "
        "       source_file, tags, created_at "
        "FROM insights "
        "WHERE content LIKE ? AND superseded_by IS NULL"
    )
    params = ["%" + pattern + "%"]
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY created_at DESC LIMIT 200"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    print(json.dumps({
        "ok": True,
        "pattern": pattern,
        "count": len(rows),
        "matches": [dict(r) for r in rows],
    }))


def cmd_bulk_promote(args):
    """Promote N insights to principles in one call.

    JSON shape: {"insight_ids":[2170, 2178, 2185], "reason":"..."}
    Mirrors the existing promote-insight behavior; just looped + transactional.
    """
    data = parse_json_arg(args.json)
    require_fields(data, "insight_ids")
    ids = list(dict.fromkeys(int(i) for i in data["insight_ids"]))
    conn = get_db()
    promoted = []
    skipped = []
    now = datetime.now(timezone.utc).isoformat()
    for iid in ids:
        row = conn.execute(
            "SELECT id, project, content, tags, promoted_to FROM insights WHERE id = ?", (iid,)
        ).fetchone()
        if row is None:
            skipped.append({"id": iid, "reason": "not found"})
            continue
        if row["promoted_to"]:
            skipped.append({"id": iid, "reason": f"already promoted to principle #{row['promoted_to']}"})
            continue
        conn.execute(
            """INSERT INTO principles (project, content, source_insights, confidence, tags,
                                       created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (row["project"], row["content"], str(iid), scoring.PROMOTE_SEED_CONFIDENCE,
             row["tags"] or "", _utcnow_iso(), _utcnow_iso()),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE insights SET promoted_to = ?, updated_at = ? WHERE id = ?",
            (pid, now, iid),
        )
        promoted.append({"insight_id": iid, "principle_id": pid})
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "promoted": promoted, "skipped": skipped}))


def cmd_clear_suspect(args):
    """Clear the contradiction-detector flag (suspect_of/reason/score) from an insight.

    Use after `audit-contradictions` when the flagged pair is a FALSE POSITIVE
    (different topics that the detector mistook for contradictions). This keeps
    both insights live without forcing an arena adjudication that would
    incorrectly supersede a valid one.

    Phase 13 finding (2026-05-20): the Phase 9 contradiction detector has a
    high false-positive rate. Manual triage is required; this command is the
    "no, these don't actually contradict" path.
    """
    iid = int(args.id)
    conn = get_db()
    row = conn.execute("SELECT suspect_of FROM insights WHERE id = ?", (iid,)).fetchone()
    if row is None:
        print(json.dumps({"ok": False, "error": f"#{iid} not found"}))
        conn.close()
        return
    if row["suspect_of"] is None:
        print(json.dumps({"ok": True, "noop": True, "id": iid}))
        conn.close()
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE insights
           SET suspect_of = NULL, suspect_reason = NULL,
               suspect_score = NULL, suspect_detected_at = NULL,
               updated_at = ?
           WHERE id = ?""",
        (now, iid),
    )
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "cleared": iid}))


def cmd_supersede_chain(args):
    """Walk the supersede chain for an insight; return the canonical (live) ID."""
    iid = int(args.id)
    conn = get_db()
    chain = [iid]
    seen = {iid}
    cur = iid
    while True:
        row = conn.execute(
            "SELECT superseded_by FROM insights WHERE id = ?", (cur,)
        ).fetchone()
        if row is None:
            break
        nxt = row["superseded_by"]
        if nxt is None or nxt in seen:
            break
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
    conn.close()
    print(json.dumps({"ok": True, "chain": chain, "canonical": chain[-1]}))


# ── Phase 12 — Session metadata (claude session → project canonical mapping) ──

def cmd_register_session(args):
    """UPSERT a claude session_uuid → project mapping.

    Idempotent: re-running for the same session_uuid updates `last_seen_at`
    and (optionally) overwrites `project`/`cwd` if explicitly passed.

    Required: session_uuid, project
    Optional: cwd, source ('hook' | 'router' | 'manual'; default 'manual')

    Example:
      crag-anchor-cli register-session '{"session_uuid":"7ac46a6d-c425-44f6-81fb-572eb0dafc40","project":"myproject","cwd":"/path/to/project","source":"hook"}'
    """
    data = parse_json_arg(args.json)
    require_fields(data, "session_uuid", "project")
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    # Idempotent UPSERT: on conflict, update last_seen and project (latter
    # only if the new value isn't an empty string, so the hook's repeated
    # calls don't clobber a router-set project with a different one).
    conn.execute(
        """INSERT INTO session_meta (session_uuid, project, cwd, started_at, last_seen_at, source)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_uuid) DO UPDATE SET
             project      = CASE WHEN excluded.project != '' THEN excluded.project ELSE session_meta.project END,
             cwd          = COALESCE(NULLIF(excluded.cwd, ''), session_meta.cwd),
             last_seen_at = excluded.last_seen_at,
             source       = excluded.source""",
        (
            data["session_uuid"],
            data["project"],
            data.get("cwd", ""),
            now,
            now,
            data.get("source", "manual"),
        ),
    )
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "session_uuid": data["session_uuid"], "project": data["project"]}))


def cmd_lookup_session(args):
    """Return {session_uuid, project, cwd, started_at, last_seen_at, source} or null."""
    conn = get_db()
    row = conn.execute(
        "SELECT session_uuid, project, cwd, started_at, last_seen_at, source "
        "FROM session_meta WHERE session_uuid = ?",
        (args.session_uuid,),
    ).fetchone()
    conn.close()
    print(json.dumps(dict(row) if row else None))


def cmd_migrate(_args):
    """Apply any unapplied migration files from the migrations/ directory."""
    conn = get_db()
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()}

    migrations_dir = DB_DIR / "migrations"
    migration_files = sorted(migrations_dir.glob("*.sql"))

    applied_count = 0
    for mf in migration_files:
        # Extract version number from filename (e.g., 003_phase1_foundations.sql -> 3)
        try:
            version = int(mf.stem.split("_")[0])
        except ValueError:
            continue

        if version in applied:
            continue

        sql = mf.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
            conn.commit()
            applied_count += 1
            print(json.dumps({"ok": True, "applied": mf.name, "version": version}), flush=True)
        except Exception as e:
            conn.close()
            print(json.dumps({"ok": False, "error": str(e), "migration": mf.name}))
            sys.exit(1)

    conn.close()
    if applied_count == 0:
        print(json.dumps({"ok": True, "message": "Already up to date", "applied_count": 0}))
    else:
        print(json.dumps({"ok": True, "message": f"Applied {applied_count} migration(s)", "applied_count": applied_count}))


def main():
    parser = argparse.ArgumentParser(description="crag-anchor SQLite memory backend")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init")

    p = sub.add_parser("add-session")
    p.add_argument("json")

    p = sub.add_parser("add-insight")
    p.add_argument("json")

    p = sub.add_parser("update-insight")
    p.add_argument("json")

    p = sub.add_parser("verify-insight")
    p.add_argument("json")

    p = sub.add_parser("stale-insights")
    p.add_argument("project")
    p.add_argument("--days", type=int, default=7)

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--project", default=None)
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("get-sessions")
    p.add_argument("project")
    p.add_argument("--limit", type=int, default=5)

    p = sub.add_parser("get-insights")
    p.add_argument("project")
    p.add_argument("--active-only", action="store_true", help="Only return active insights")
    p.add_argument("--limit", type=int, default=None, help="Limit number of results")
    p.add_argument("--min-confidence", type=float, default=None, help="Minimum confidence score")
    p.add_argument("--sort", choices=["created_at", "confidence"], default="created_at")

    p = sub.add_parser("get-context")
    p.add_argument("project")

    p = sub.add_parser("set-context")
    p.add_argument("json")

    sub.add_parser("stats")

    # Event Queue commands
    p = sub.add_parser("add-event")
    p.add_argument("json")

    p = sub.add_parser("get-events")
    p.add_argument("--project", default=None)
    p.add_argument("--priority", default=None)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("claim-event")
    p.add_argument("json")

    p = sub.add_parser("complete-event")
    p.add_argument("json")

    p = sub.add_parser("import-events")
    p.add_argument("file")

    p = sub.add_parser("bulk-expire-events")
    p.add_argument("--project", default=None, help="Filter by project")
    p.add_argument("--priority", default=None, help="Filter by priority (critical/high/normal/low)")

    # Layered Knowledge commands
    p = sub.add_parser("verify-insight-v2")
    p.add_argument("json")

    p = sub.add_parser("auto-prune")

    p = sub.add_parser("get-principles")
    p.add_argument("project")

    p = sub.add_parser("stale-principles")
    p.add_argument("project")
    p.add_argument("--days", type=int, default=30, help="Principles not updated in N days")

    p = sub.add_parser("promote-insight")
    p.add_argument("json")

    p = sub.add_parser("distill")
    p.add_argument("json")

    # Brief & Maintenance commands
    p = sub.add_parser("brief")
    p.add_argument("project")

    p = sub.add_parser("db-maintenance")
    p.add_argument("--delete-completed-before", type=int, required=True, help="Delete completed/expired events older than N days")

    # Token Ledger commands
    p = sub.add_parser("add-token-record")
    p.add_argument("json")

    p = sub.add_parser("cost-report")
    p.add_argument("--project", default=None)
    p.add_argument("--days", type=int, default=None)

    p = sub.add_parser("recall")
    p.add_argument("query")
    p.add_argument("--project", default=None)
    p.add_argument("--topk", type=int, default=5)
    p.add_argument("--session-id", default=None, dest="session_id")
    p.add_argument("--snippet", action="store_true", help="Return only 200-char content snippets, omit tags/source_file")

    p = sub.add_parser("recall-stats")
    p.add_argument("--project", default=None)
    p.add_argument("--days", type=int, default=7)

    p = sub.add_parser("backfill-embeddings")
    p.add_argument("--batch", type=int, default=100, help="Batch size for embedding (default: 100)")

    p = sub.add_parser("backfill-falsifiers")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="Report re-derivation counts without writing")

    p = sub.add_parser("backfill-grounding-v2",
                       help="Classify all claims into Tier A/B; Tier-A gets mechanical falsifier, "
                            "Tier-B gets an 'author' job enqueued (no LLM calls here)")
    p.add_argument("--project", default=None, help="Scope to one project slug")
    p.add_argument("--limit", type=int, default=None, help="Max claims to process")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="Count and classify without writing DB or enqueueing")

    p = sub.add_parser("backfill-graph-v2",
                       help="Normalize entity_links → entity_canonical; seed claim_relations + entity_relations")
    p.add_argument("--dry-run", action="store_true", help="Compute stats without writing")
    p.add_argument("--project", default=None, help="Limit to entity_links for this project")

    p = sub.add_parser("salvage-staged",
                       help="Salvage pending staged insights: dedup check, insert non-dupes, mark dupes")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="Report counts without writing to DB")

    p = sub.add_parser("hot-insights")
    p.add_argument("project")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--days", type=int, default=30)

    sub.add_parser("migrate")

    # Phase 12 — Session metadata (claude session → project canonical mapping)
    p = sub.add_parser("register-session", help="UPSERT a session_uuid → project mapping")
    p.add_argument("json", help='JSON: {"session_uuid":"...","project":"...","cwd":"...","source":"hook|router|manual"}')

    p = sub.add_parser("lookup-session", help="Return the session_meta row for a session_uuid, or null")
    p.add_argument("session_uuid", help="Canonical claude session UUID")

    # Phase 13 — Memory arena (supersede edges + adjudication)
    p = sub.add_parser("arena", help="Adjudicate between N insights; mark losers as superseded")
    p.add_argument("json", help='JSON: {"insight_ids":[...], "strategy":"auto|recency|confidence|evidence|merge", "project":"...", "merged_content":"..."(merge only), "dry_run":false}')

    p = sub.add_parser("supersede", help="Manually mark loser_id superseded by winner_id")
    p.add_argument("json", help='JSON: {"loser_id":N, "winner_id":M, "reason":"..."}')

    p = sub.add_parser("unsupersede", help="Reverse a supersede edge — restore insight to active")
    p.add_argument("id", type=int, help="Insight ID")

    p = sub.add_parser("audit-contradictions", help="List unresolved contradiction-flagged insights")
    p.add_argument("--project", help="Filter by project")

    p = sub.add_parser("audit-drift", help="Find non-superseded insights matching a stale pattern")
    p.add_argument("json", help='JSON: {"pattern":"198.51.100.7", "project":"myproject"}')

    p = sub.add_parser("bulk-promote", help="Promote N insights to principles in one call")
    p.add_argument("json", help='JSON: {"insight_ids":[...], "reason":"..."}')

    p = sub.add_parser("supersede-chain", help="Walk supersede chain; return canonical (live) ID")
    p.add_argument("id", type=int, help="Insight ID")

    p = sub.add_parser("clear-suspect", help="Clear contradiction-detector flag (false-positive)")
    p.add_argument("id", type=int, help="Insight ID")

    # Phase 4 Lifecycle & Hygiene
    p = sub.add_parser("decay")
    p.add_argument("--project", default=None, help="Limit to a specific project")
    p.add_argument("--dry-run", action="store_true", help="Show what would be decayed without changing the DB")
    p.add_argument("--insight-days", type=int, default=60, help="Days without recall before decaying insight confidence")
    p.add_argument("--principle-days", type=int, default=90, help="Days without update before flagging principle for re-verification")

    p = sub.add_parser("distill-candidates")
    p.add_argument("--project", required=True, help="Project to scan")
    p.add_argument("--threshold", type=float, default=0.85, help="Cosine similarity threshold (default: 0.85)")
    p.add_argument("--limit", type=int, default=20, help="Max pairs to return")

    p = sub.add_parser("suggest-tags")
    p.add_argument("content", help="Content text to suggest tags for")
    p.add_argument("--project", default=None, help="Project scope for tag search")
    p.add_argument("--limit", type=int, default=5, help="Max tags to return")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmd_map = {
        "init": cmd_init,
        "add-session": cmd_add_session,
        "add-insight": cmd_add_insight,
        "update-insight": cmd_update_insight,
        "verify-insight": cmd_verify_insight,
        "verify-insight-v2": cmd_verify_insight_v2,
        "stale-insights": cmd_stale_insights,
        "search": cmd_search,
        "get-sessions": cmd_get_sessions,
        "get-insights": cmd_get_insights,
        "get-context": cmd_get_context,
        "set-context": cmd_set_context,
        "stats": cmd_stats,
        # Event Queue
        "add-event": cmd_add_event,
        "get-events": cmd_get_events,
        "claim-event": cmd_claim_event,
        "complete-event": cmd_complete_event,
        "import-events": cmd_import_events,
        "bulk-expire-events": cmd_bulk_expire_events,
        # Layered Knowledge
        "auto-prune": cmd_auto_prune,
        "get-principles": cmd_get_principles,
        "stale-principles": cmd_stale_principles,
        "promote-insight": cmd_promote_insight,
        "distill": cmd_distill,
        # Brief & Maintenance
        "brief": cmd_brief,
        "db-maintenance": cmd_db_maintenance,
        # Token Ledger
        "add-token-record": cmd_add_token_record,
        "cost-report": cmd_cost_report,
        # Phase 1 foundations
        "recall": cmd_recall,
        "migrate": cmd_migrate,
        "register-session": cmd_register_session,
        "lookup-session": cmd_lookup_session,
        "arena": cmd_arena,
        "supersede": cmd_supersede,
        "unsupersede": cmd_unsupersede,
        "audit-contradictions": cmd_audit_contradictions,
        "audit-drift": cmd_audit_drift,
        "bulk-promote": cmd_bulk_promote,
        "supersede-chain": cmd_supersede_chain,
        "clear-suspect": cmd_clear_suspect,
        # Phase 2 Embeddings
        "backfill-embeddings": cmd_backfill_embeddings,
        "backfill-falsifiers": cmd_backfill_falsifiers,
        "backfill-grounding-v2": cmd_backfill_grounding_v2,
        # Phase 3 Hot Insights
        "hot-insights": cmd_hot_insights,
        # Phase 4 Lifecycle & Hygiene
        "decay": cmd_decay,
        "distill-candidates": cmd_distill_candidates,
        "suggest-tags": cmd_suggest_tags,
        # Staging salvage
        "backfill-graph-v2": cmd_backfill_graph_v2,
        "salvage-staged": cmd_salvage_staged,
        # Phase 5 crag Anchor daemon
        "recall-stats": cmd_recall_stats,
    }
    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
