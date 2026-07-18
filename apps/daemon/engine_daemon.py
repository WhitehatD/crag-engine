#!/usr/bin/env python
"""
crag Anchor daemon
FastAPI service on 127.0.0.1:8786. Holds the embedding model in memory and
exposes the full memory surface over HTTP. The MCP server is a thin client to this.

Endpoints (core; see route decorators for the full surface):
  GET  /health          -- liveness + model_loaded flag (503 until model ready)
  GET  /stats           -- request counts, embed/recall timing, db info
  GET  /metrics         -- Prometheus-style text
  GET  /insight/{id}    -- full insight row
  GET  /recall_stats    -- usage telemetry
  POST /recall          -- hybrid semantic+FTS5+confidence search
  POST /recall_principle -- search principles only
  POST /save_insight    -- insert with cosine dedup + auto-embed
  POST /verify_insight  -- confidence +/-
  POST /distill         -- merge insights into principle
  POST /suggest_tags    -- semantic tag autosuggest
  POST /query/get_batch -- bulk fetch insights/principles by id (WS3a)
  POST /arena_batch     -- bulk arena adjudication (WS3a)
  POST /clear_suspect_batch -- bulk FP-flag clearing (WS3a)

Bind: 127.0.0.1:8786 by default (CRAG_ANCHOR_DAEMON_HOST/PORT to override).
Logs: <log_dir>/daemon.log (JSON, daily rotation, 7-day retention). log_dir
resolves via db/engine_paths.py (default <repo>/logs).
"""

import asyncio
import base64
import collections
import hmac
import json
import logging
import logging.handlers
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DAEMON_DIR = Path(__file__).parent
# Repo root since Tier-1 split (2026-05-17): apps/daemon/engine_daemon.py →
# parents = [apps/daemon, apps, <root>].  db/ and logs/ live at the crag-anchor root.
# NOTE: DB_DIR is where the daemon's sibling modules live (embed, scoring,
# entity_extract, engine_paths, …); it is ALWAYS repo-relative regardless of a
# CRAG_ANCHOR_HOME override, because those .py files ship with the package. Runtime
# DATA paths (DB_PATH, LOG_DIR, bind) come from db/engine_paths.py instead, which
# resolves env → stack.toml → repo-relative default (see that module).
CRAG_ANCHOR_ROOT = DAEMON_DIR.parent.parent
DB_DIR = CRAG_ANCHOR_ROOT / "db"

# Make embed.py, entity_extract.py, contradiction.py, transcript_tokens.py,
# and engine_paths.py importable BEFORE we resolve data paths from engine_paths.
sys.path.insert(0, str(DAEMON_DIR))  # transcript_tokens.py lives here
sys.path.insert(0, str(DB_DIR))

# WS-P (2026-07-17): data paths + bind now come from the shared resolver. With
# zero config on this machine these yield exactly the pre-packaging values
# (DB_PATH = <root>/db/engine.db, LOG_DIR = <root>/logs, 127.0.0.1:8786).
from engine_paths import get_paths as _get_engine_paths  # noqa: E402
_BP = _get_engine_paths()
DB_PATH = _BP.db_path
LOG_DIR = _BP.log_dir
LOG_PATH = LOG_DIR / "daemon.log"
# Debug flag: set CRAG_ANCHOR_DAEMON_DEBUG=1 to enable DEBUG-level logging
_DEBUG_MODE = os.environ.get("CRAG_ANCHOR_DAEMON_DEBUG", "0").strip() == "1"
VERSION = "25.0.0"  # keep in sync with highest applied migration phase
PORT = _BP.daemon_port
HOST = _BP.daemon_host
from entity_extract import (
    extract_entities,
    derive_falsifier,
    falsifier_for,
    classify_volatility,
)
from contradiction import detect_and_flag as _detect_contradictions  # Phase 9
# Graph v2 (migration 027): normalization + canonical entity gating
try:
    from entity_normalize import normalize as _normalize_entity
    _graph_v2_available = True
except ImportError:
    _graph_v2_available = False
    def _normalize_entity(entity_type, raw_value):  # type: ignore[misc]
        return {"canonical": raw_value, "reject": False, "reason": None}
import scoring  # WS2 T6 — single source of scoring/lifecycle constants
import lifecycle  # WS2 T2/T3a — shared decay + falsifier-resolvability

# Grounding v2 (A3): tier classification, recipe authoring, worker queue
try:
    import grounding_author as _grounding_author
    from grounding_author import classify_tier as _classify_tier
    from grounding_queue_v2 import enqueue_job as _gv2_enqueue_job
    from grounding_queue_v2 import drain_one_job as _gv2_drain_one_job
    import llm_client as _llm_client
    import grounding_config as _grounding_config
    import grounding_cost as _grounding_cost
    _GROUNDING_V2 = True
except ImportError as _gv2_err:
    _GROUNDING_V2 = False
    logger = None  # set up below; use print for this early warning
    import builtins
    builtins.print(f"[daemon] grounding v2 modules not found ({_gv2_err}) — running without Tier-B")

# Grounding v3 (Claim Layer): decompose/classify/canonicalize/rollup. Fail-soft
# — if the module or its migration is absent the daemon runs without claims.
try:
    import claim_layer as _claim_layer
    _CLAIM_LAYER = True
except ImportError as _cl_err:
    _CLAIM_LAYER = False
    import builtins
    builtins.print(f"[daemon] claim_layer (grounding v3) not found ({_cl_err}) — running without claims")

# Grounding v3 REV 3 — write-path governance (schema + secret-scan HARD gates,
# advisory provenance, TRACE-style lifecycle resolver, staging router). Fail-soft
# import: if absent the save path runs ungated exactly as before REV 3.
try:
    import write_gate as _write_gate
    _WRITE_GATE = True
except ImportError as _wg_err:
    _WRITE_GATE = False
    import builtins
    builtins.print(f"[daemon] write_gate (grounding v3 rev3) not found ({_wg_err}) — save path ungated")

# Disposition Engine (docs/architecture.md REV 5 §5.2 / REV 7 §7.1, migration
# 033) — governs every insights_staging transition (accept/reject/merge/
# defer) behind a T0/T1/T2 policy tier + drain-SLA. Fail-soft import: if
# absent the /disposition/* endpoints return a loud 'not available' error
# rather than crashing the daemon.
try:
    import disposition as _disposition
    _DISPOSITION = True
except ImportError as _disp_err:
    _DISPOSITION = False
    import builtins
    builtins.print(f"[daemon] disposition (control plane) not found ({_disp_err}) — /disposition/* disabled")

# Sync-folder corruption guard (docs/architecture.md REV 4 item 2). Fail-soft
# import: if the module is absent the daemon simply doesn't run the guard.
try:
    import sync_path_guard as _sync_path_guard
    _SYNC_GUARD = True
except ImportError as _sg_err:
    _SYNC_GUARD = False
    import builtins
    builtins.print(f"[daemon] sync_path_guard not found ({_sg_err}) — sync-folder DB guard disabled")

# Autonomic capture worker as a daemon lifespan task (docs/architecture.md REV
# 6/8: "the loop runs AROUND the agent"). Optional-import guarded so a missing
# capture package never breaks daemon startup — the daemon just runs without
# the in-process tailer and an operator can still schedule run_capture.py.
try:
    sys.path.insert(0, str(DB_DIR / "capture"))
    import run_capture as _run_capture           # noqa: E402
    from capture import config as _capture_config  # noqa: E402
    _CAPTURE_TASK = True
except Exception as _cap_err:  # broad: capture subtree has optional deps
    _CAPTURE_TASK = False
    import builtins
    builtins.print(f"[daemon] capture task modules not found ({_cap_err}) — in-process capture disabled")

# WS4 — canonical per-model pricing (packages/pricing/pricing.py). Used for
# model-aware ROI cost math instead of a hardcoded $3/$15 blend.
sys.path.insert(0, str(CRAG_ANCHOR_ROOT))
from packages.pricing.pricing import get_rates as _get_model_rates  # noqa: E402
_DEFAULT_COST_MODEL = "claude-fable-5"
import broadcaster as _broadcaster_module  # Phase 10
from broadcaster import add_subscriber, remove_subscriber, broadcast as _broadcast, HEARTBEAT_INTERVAL_SEC

# ---------------------------------------------------------------------------
# Logging -- structured JSON
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        # Merge extra fields attached by log calls
        for key in ("request_id", "endpoint", "duration_ms", "project", "topk", "method"):
            if hasattr(record, key):
                d[key] = getattr(record, key)
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d)


def _setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("crag-anchor")
    root.setLevel(logging.DEBUG if _DEBUG_MODE else logging.INFO)
    if root.handlers:
        return root  # already set up

    # Use RotatingFileHandler (size-based) instead of TimedRotatingFileHandler.
    # On Windows, TimedRotatingFileHandler calls os.rename() at midnight to rotate
    # the log file.  When the launcher script holds an open handle to daemon.log via
    # Start-Process -RedirectStandardOutput, os.rename() raises WinError 32 and the
    # handler enters a broken state — all subsequent writes are silently dropped.
    # RotatingFileHandler opens a NEW file for each backup (no rename of the active
    # log), which is safe even when another process holds the file open.
    handler = logging.handlers.RotatingFileHandler(
        str(LOG_PATH),
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    # Also emit to stderr so the launcher's -RedirectStandardError captures
    # startup tracebacks and uvicorn output that bypass the file handler.
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(JsonFormatter())
    root.addHandler(stderr_handler)
    return root


logger = _setup_logging()

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_start_time = time.time()
_model_loaded = False
_stats: dict[str, Any] = {
    "requests_served": 0,
    "embed_times_ms": [],
    "recall_times_ms": [],
    "last_restart": datetime.now(timezone.utc).isoformat(),
}

# Phase 4.5 — slow recall ring buffer.  In-memory only; survives daemon
# lifetime, not restarts.  Each entry: {ts, query, project, elapsed_ms, hits}.
# Capped at 200 entries; drops oldest when full.  Used by /recall_slow_log
# endpoint to power the RecallExplorerPage slow query view.
from collections import deque
_recall_slow_log: deque[dict[str, Any]] = deque(maxlen=200)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _bootstrap_empty_db() -> None:
    """Bring a brand-new DB to the current schema at daemon startup.

    Mirrors `crag-anchor-cli init` + `migrate`: applies db/schema.sql when the
    base schema is absent, then executes every unapplied db/migrations/NNN_*.sql
    in version order (version-checked against schema_version, so this is
    idempotent and a fast no-op on an already-migrated DB). Called once from
    lifespan startup; raises to the (fail-soft) caller on error.
    """
    schema_path = DB_DIR / "schema.sql"
    migrations_dir = DB_DIR / "migrations"
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        if not _table_exists(conn, "schema_version"):
            conn.executescript(schema_path.read_text(encoding="utf-8"))
            conn.commit()
            logger.info("fresh-DB bootstrap: applied base schema.sql at %s", DB_PATH)
        applied = {
            r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()
        }
        applied_count = 0
        for mf in sorted(migrations_dir.glob("*.sql")):
            try:
                version = int(mf.stem.split("_")[0])
            except ValueError:
                continue
            if version in applied:
                continue
            conn.executescript(mf.read_text(encoding="utf-8"))
            conn.commit()
            applied_count += 1
        if applied_count:
            logger.info("fresh-DB bootstrap: applied %d migration(s)", applied_count)
    finally:
        conn.close()


def _utcnow_iso() -> str:
    """CANONICAL timestamp for every TEXT timestamp column this daemon writes.

    Offset-aware UTC ISO-8601 with 'T' separator: `2026-07-02T18:06:45.123456+00:00`.
    NEVER use SQLite `datetime('now')` in daemon SQL — it produces
    `YYYY-MM-DD HH:MM:SS` (space separator, naive), and space (0x20) sorts BEFORE
    'T' (0x54), so lexical comparisons/orderings against ISO-T values are wrong
    for same-day timestamps. This corrupted supersede-burst watermark counting
    (fixed 2026-07-02) and silently mis-orders any mixed-format column.
    """
    return datetime.now(timezone.utc).isoformat()


def compute_shingles(text: str, k: int = 3) -> set:
    words = text.lower().split()
    if len(words) < k:
        return set(words)
    return {" ".join(words[i: i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Embedding helpers (synchronous, called from async endpoints via asyncio.run_in_executor)
# ---------------------------------------------------------------------------

def _load_model():
    """Load fastembed model. Called once at startup."""
    global _model_loaded
    from embed import get_model, embed_text
    get_model()  # triggers model load; result cached in embed module
    # Warmup: run one real embed so the ONNX/JIT runtime is fully compiled
    # before the first user recall.  Without this the first recall per process
    # pays an 800-1500ms cold-start penalty, breaching the p99 < 500ms SLO.
    # We call embed_text() directly (bypassing _embed_one) so the warmup time
    # is not counted in embed_times_ms stats.
    embed_text("warmup")
    _model_loaded = True
    logger.info("Embedding model loaded and warmed up", extra={})


def _embed_one(text: str) -> bytes:
    t0 = time.perf_counter()
    from embed import embed_text
    result = embed_text(text)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    _stats["embed_times_ms"].append(elapsed_ms)
    if len(_stats["embed_times_ms"]) > 1000:
        _stats["embed_times_ms"] = _stats["embed_times_ms"][-500:]
    return result


def _embed_batch_fn(texts: list) -> list:
    t0 = time.perf_counter()
    from embed import embed_batch
    result = embed_batch(texts)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    _stats["embed_times_ms"].append(elapsed_ms)
    if len(_stats["embed_times_ms"]) > 1000:
        _stats["embed_times_ms"] = _stats["embed_times_ms"][-500:]
    return result


# ---------------------------------------------------------------------------
# Core memory operations (synchronous helpers)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 25 — Grounded Memory: Tier-1 liveness stamp.
# PURE, O(1), NO I/O — safe to call on the recall hot path (p99 < 500ms SLO).
# Derives a freshness VERDICT from columns already on the row. The expensive
# falsifier PROOF (curl/ssh/grep) runs OFF-path in the groundskeeper cron
# (Tier 2). HARD INVARIANT: never do network/disk/process work here.
# ---------------------------------------------------------------------------
_VOLATILITY_TTL_DAYS = {"invariant": 180, "topology": 30, "observation": 7}


def _liveness_stamp(grounded_at, volatility_class, grounding_due,
                    falsifier_result=None, falsifier_kind=None) -> dict:
    """Compute a recall-time freshness stamp from pure column reads (no I/O)."""
    from datetime import datetime as _dt
    age_days = None
    if grounded_at:
        try:
            _s = str(grounded_at).replace("Z", "").replace("T", " ").strip()[:19]
            age_days = round((_dt.utcnow() - _dt.fromisoformat(_s)).total_seconds() / 86400.0, 1)
        except Exception:
            age_days = None
    fal = (falsifier_result or "pending") if falsifier_kind else "none"
    if fal == "fail":
        verdict = "stale"             # falsifier actively disproved the claim
    elif grounding_due:
        verdict = "revalidating"      # flagged/enqueued; truth not yet reconfirmed
    elif grounded_at is None:
        verdict = "unverified"        # never re-grounded since save
    else:
        ttl = _VOLATILITY_TTL_DAYS.get(volatility_class, 30)
        verdict = "fresh" if (age_days is not None and age_days <= ttl) else "aging"
    return {
        "verdict": verdict,
        "grounded_at": grounded_at,
        "age_days": age_days,
        "volatility_class": volatility_class,
        "grounding_due": bool(grounding_due),
        "falsifier": fal,
    }


# ---------------------------------------------------------------------------
# Rev-5 §5.5 — recall read-gate enforcement (server-side stale banner).
# Memory is NEVER hidden (doctrine) — we do NOT suppress stale hits. Instead we
# prepend an impossible-to-miss structured banner so a consumer that ignores
# the liveness block still sees the warning inline. One shared helper so both
# recall paths (_do_recall and the principle path) get identical enforcement.
# ---------------------------------------------------------------------------
_STALE_VERDICTS = {"stale", "revalidating"}


def _attach_stale_banner(item: dict, liveness: dict) -> dict:
    """If `liveness.verdict` is stale/revalidating, add a `stale_banner` field
    to `item` in place. No-op for fresh/aging/unverified. Returns `item`."""
    try:
        verdict = (liveness or {}).get("verdict")
        if verdict in _STALE_VERDICTS:
            since = (liveness or {}).get("grounded_at") or "unknown"
            item["stale_banner"] = (
                f"\u26a0 STALE-FLAGGED [{verdict}]: core claim failing since "
                f"{since}; verify before acting"
            )
    except Exception:
        pass  # banner is advisory — never break a recall over it
    return item


def _do_recall(query: str, project: Optional[str], topk: int, session_id: Optional[str], snippet: bool,
               role: Optional[str] = None, epic_tag: Optional[str] = None) -> dict:
    t0 = time.perf_counter()
    query = (query or "").strip()
    # Defensive: coerce and clamp topk (negative/zero -> 5, max 50)
    try:
        topk = int(topk)
    except (TypeError, ValueError):
        topk = 5
    if topk <= 0:
        topk = 5
    elif topk > 50:
        topk = 50
    if not query:
        return {"query": query, "insights": [], "principles": [], "count": 0, "embed_available": False}

    conn = get_db()

    # Cosine search
    cosine_scores: dict[int, float] = {}
    embed_available = False
    # Phase A recall-filter telemetry: track pre/post-supersede-filter candidate counts
    _rfe_pre: int = 0
    _rfe_post: int = 0
    if _model_loaded:
        try:
            query_vec = np.frombuffer(_embed_one(query), dtype="float32")
            embed_available = True
            proj_clause = "(i.project = ? OR i.project IS NULL)" if project else "1=1"
            emb_params = [project] if project else []
            # Count total embedded insights WITHOUT supersede filter (pre-filter)
            try:
                _rfe_pre = conn.execute(
                    f"SELECT COUNT(*) FROM insights WHERE status='active' AND embedding IS NOT NULL AND {proj_clause}",
                    emb_params,
                ).fetchone()[0]
            except Exception:
                pass
            emb_rows = conn.execute(
                f"""SELECT i.id, i.embedding FROM insights i
                    WHERE i.status='active' AND i.embedding IS NOT NULL
                          AND i.superseded_by IS NULL AND {proj_clause}""",
                emb_params,
            ).fetchall()
            _rfe_post = len(emb_rows)
            if emb_rows:
                ids = [r["id"] for r in emb_rows]
                matrix = np.vstack([np.frombuffer(r["embedding"], dtype="float32") for r in emb_rows])
                sims = matrix @ query_vec
                cosine_scores = {ids[i]: float(sims[i]) for i in range(len(ids))}
        except Exception as e:
            logger.warning("Cosine embed failed: %s", e)

    # FTS5 search
    fts_scores: dict[int, float] = {}
    fts_rows = []
    try:
        fts_sql = """SELECT i.id, fts.rank AS fts_rank
                     FROM insights_fts fts
                     JOIN insights i ON i.id = fts.rowid
                     WHERE insights_fts MATCH ? AND i.status='active'
                       AND i.superseded_by IS NULL"""
        fts_params = [query]
        if project:
            fts_sql += " AND (i.project = ? OR i.project IS NULL)"
            fts_params.append(project)
        fts_sql += " ORDER BY fts.rank LIMIT 50"
        fts_rows = conn.execute(fts_sql, fts_params).fetchall()
        if fts_rows:
            ranks = [r["fts_rank"] for r in fts_rows]
            min_r, max_r = min(ranks), max(ranks)
            rng = max_r - min_r if max_r != min_r else 1.0
            for r in fts_rows:
                fts_scores[r["id"]] = 1.0 - (r["fts_rank"] - min_r) / rng
    except Exception as _fts_exc:
        logger.warning("FTS5 recall degraded to cosine-only: %s", _fts_exc)

    # Candidate union
    cosine_top = sorted(cosine_scores, key=lambda x: cosine_scores[x], reverse=True)[:30]
    candidate_ids = list(set(cosine_top + [r["id"] for r in fts_rows]))

    if not candidate_ids:
        conn.close()
        return {"query": query, "insights": [], "principles": [], "count": 0, "embed_available": embed_available}

    placeholders = ",".join("?" * len(candidate_ids))
    # Phase 25 — grounding columns + falsifier status. Gated on the falsifiers
    # table so the daemon is safe to restart BEFORE or AFTER migrate 023 runs.
    _grounding_on = _table_exists(conn, "falsifiers")
    if _grounding_on:
        candidates = conn.execute(
            f"SELECT i.id, i.project, i.type, i.content, i.tags, i.source_file, i.confidence, "
            f"i.suspect_of, i.suspect_reason, i.suspect_score, i.suspect_detected_at, "
            f"i.volatility_class, i.grounded_at, i.grounding_due, "
            f"f.last_result AS falsifier_result, f.kind AS falsifier_kind, "
            f"f.spec AS falsifier_spec, f.entity_type AS falsifier_entity_type "
            f"FROM insights i "
            f"LEFT JOIN falsifiers f ON f.claim_kind='insight' AND f.claim_id=i.id "
            f"WHERE i.id IN ({placeholders})",
            candidate_ids,
        ).fetchall()
    else:
        candidates = conn.execute(
            f"SELECT id, project, type, content, tags, source_file, confidence, "
            f"suspect_of, suspect_reason, suspect_score, suspect_detected_at "
            f"FROM insights WHERE id IN ({placeholders})",
            candidate_ids,
        ).fetchall()

    insight_list = []
    for r in candidates:
        cos = cosine_scores.get(r["id"], 0.0)
        fts = fts_scores.get(r["id"], 0.0)
        conf = r["confidence"] if r["confidence"] is not None else 0.5
        if embed_available and cosine_scores:
            hybrid = round(scoring.HYBRID_W_COSINE * cos + scoring.HYBRID_W_FTS * fts
                           + scoring.HYBRID_W_CONF * conf, 4)
        else:
            hybrid = round(scoring.NOEMB_W_FTS * fts + scoring.NOEMB_W_CONF * conf, 4)

        # Phase 18 — score breakdown (always included; 3 extra floats, negligible overhead)
        if embed_available and cosine_scores:
            _breakdown = {
                "cosine_weighted":     round(scoring.HYBRID_W_COSINE * cos,  4),
                "fts_weighted":        round(scoring.HYBRID_W_FTS * fts,  4),
                "confidence_weighted": round(scoring.HYBRID_W_CONF * conf, 4),
                "formula": scoring.HYBRID_FORMULA,
            }
        else:
            _breakdown = {
                "cosine_weighted":     0.0,
                "fts_weighted":        round(scoring.NOEMB_W_FTS * fts,  4),
                "confidence_weighted": round(scoring.NOEMB_W_CONF * conf, 4),
                "formula": scoring.NOEMB_FORMULA,
            }
        item: dict = {
            "id": r["id"], "type": r["type"], "project": r["project"],
            "content": r["content"][:200] if snippet else r["content"],
            "confidence": conf, "score": hybrid,
            "cosine": round(cos, 4), "fts": round(fts, 4),
            "breakdown": _breakdown,
        }
        if not snippet:
            item["tags"] = r["tags"]
            item["source_file"] = r["source_file"]
        # Phase 9: contradiction annotation (read-only; does not affect rank)
        if r["suspect_of"]:
            item["_conflict"] = {
                "suspect_of": r["suspect_of"],
                "reason": r["suspect_reason"],
                "score": r["suspect_score"],
                "detected_at": r["suspect_detected_at"],
            }
        # Phase 25: Tier-1 liveness stamp (pure column reads — NO I/O on hot path)
        if _grounding_on:
            _liv = _liveness_stamp(
                r["grounded_at"], r["volatility_class"], r["grounding_due"],
                r["falsifier_result"], r["falsifier_kind"],
            )
            item["liveness"] = _liv
            # Rev-5 §5.5 — server-side stale banner (never suppresses the hit).
            _attach_stale_banner(item, _liv)
            # WS2 T3d — consume the grounding verdict in ranking. Transparent,
            # post-score multiplier (stale ×0.75, revalidating ×0.90; else ×1.0).
            # Recorded in the breakdown so the score change is never a black box.
            _mult = scoring.liveness_multiplier(_liv.get("verdict"))
            if _mult != 1.0:
                _pre = item["score"]
                item["score"] = round(_pre * _mult, 4)
                _breakdown["liveness_verdict"] = _liv.get("verdict")
                _breakdown["liveness_multiplier"] = _mult
                _breakdown["score_pre_liveness"] = _pre
            else:
                _breakdown["liveness_verdict"] = _liv.get("verdict")
                _breakdown["liveness_multiplier"] = 1.0
            # Grounding v3: claims_summary per hit (additive; never mutates the
            # existing liveness/score contract). Fail-soft — a missing claims
            # table or any error just omits the field. Cheap: one indexed join.
            if _CLAIM_LAYER and _table_exists(conn, "claims"):
                try:
                    _roll = _claim_layer.claim_rollup(conn, "insight", r["id"])
                    _cs = _roll.get("claims_summary", {})
                    if _cs.get("total"):
                        item["claims_summary"] = {
                            "total": _cs.get("total", 0),
                            "fresh": _cs.get("fresh", 0),
                            "stale": _cs.get("stale", 0),
                            "axiomatic": _cs.get("axiomatic", 0),
                            "claim_verdict": _roll.get("verdict"),
                            "fresh_fraction": _roll.get("fresh_fraction"),
                        }
                except Exception:
                    pass
            # Grounding v2: recall-as-trigger — aging/stale/unverified hits get
            # re-grounded off the hot path (single INSERT OR IGNORE, no LLM call).
            if _GROUNDING_V2 and _table_exists(conn, "grounding_jobs"):
                _liv_v = _liv.get("verdict", "")
                if _liv_v in ("aging", "stale", "unverified"):
                    try:
                        # r is sqlite3.Row — use bracket access, not .get().
                        # Explicit parentheses to avoid operator-precedence ambiguity
                        # between `or` and `not in`.
                        _has_recipe = bool(
                            r["falsifier_spec"]
                            or (r["falsifier_kind"] not in (None, "none"))
                        )
                        _jtype = "reground" if _has_recipe else "author"
                        _gv2_enqueue_job(conn, "insight", r["id"], _jtype)
                    except Exception:
                        pass  # never let recall-trigger fail recall itself
        insight_list.append(item)

    insight_list.sort(key=lambda x: x["score"], reverse=True)
    insight_list = insight_list[:topk]

    # Principles — hybrid: cosine over embedded principles + LIKE fallback
    principle_rows = []
    if _model_loaded and cosine_scores:
        try:
            # Cosine search over principles with embeddings
            p_proj_clause = "(project = ? OR project IS NULL)" if project else "1=1"
            p_emb_params = [project] if project else []
            _pg_cols = (", p.volatility_class, p.grounded_at, p.grounding_due, "
                        "f.last_result AS falsifier_result, f.kind AS falsifier_kind") if _grounding_on else ""
            _pg_join = (" LEFT JOIN falsifiers f ON f.claim_kind='principle' AND f.claim_id=p.id") if _grounding_on else ""
            p_emb_rows = conn.execute(
                f"SELECT p.id, p.project, p.content, p.tags, p.confidence, p.embedding, "
                f"p.suspect_of, p.suspect_reason, p.suspect_score, p.suspect_detected_at{_pg_cols} "
                f"FROM principles p{_pg_join} "
                f"WHERE p.embedding IS NOT NULL AND {p_proj_clause}",
                p_emb_params,
            ).fetchall()
            if p_emb_rows:
                p_ids = [r["id"] for r in p_emb_rows]
                p_matrix = np.vstack([np.frombuffer(r["embedding"], dtype="float32") for r in p_emb_rows])
                p_sims = p_matrix @ query_vec
                # Sort by cosine similarity, return top 5 above threshold
                scored = sorted(
                    [(p_ids[i], float(p_sims[i]), p_emb_rows[i]) for i in range(len(p_ids))],
                    key=lambda x: x[1], reverse=True
                )[:5]
                principle_rows = [r for _, sim, r in scored if sim > 0.15]
        except Exception as e:
            logger.warning("Principle cosine search failed: %s", e)

    if not principle_rows:
        # LIKE fallback (catches non-embedded principles + low-similarity semantic misses)
        p_params = [f"%{query}%"]
        _pg_cols = (", p.volatility_class, p.grounded_at, p.grounding_due, "
                    "f.last_result AS falsifier_result, f.kind AS falsifier_kind") if _grounding_on else ""
        _pg_join = (" LEFT JOIN falsifiers f ON f.claim_kind='principle' AND f.claim_id=p.id") if _grounding_on else ""
        p_sql = (
            "SELECT p.id, p.project, p.content, p.tags, p.confidence, "
            "p.suspect_of, p.suspect_reason, p.suspect_score, p.suspect_detected_at" + _pg_cols + " "
            "FROM principles p" + _pg_join + " WHERE p.content LIKE ?"
        )
        if project:
            p_sql += " AND (p.project = ? OR p.project IS NULL)"
            p_params.append(project)
        p_sql += " ORDER BY p.confidence DESC LIMIT 5"
        principle_rows = conn.execute(p_sql, p_params).fetchall()

    # Log recall events — deduped by session+query fingerprint to prevent spam inflation
    if session_id and insight_list and _table_exists(conn, "recall_events"):
        import hashlib
        fp = hashlib.sha256(f"{session_id}:{query.lower().strip()}".encode()).hexdigest()[:16]
        already_logged = conn.execute(
            "SELECT 1 FROM recall_events WHERE fingerprint = ? LIMIT 1", (fp,)
        ).fetchone()
        if not already_logged:
            for rank, hit in enumerate(insight_list):
                conn.execute(
                    """INSERT INTO recall_events (insight_id, session_id, query, hit_rank, fingerprint,
                                                  role, epic_tag)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (hit["id"], session_id, query, rank, fp if rank == 0 else None, role, epic_tag),
                )
            conn.commit()

    # Phase 25 / WS2 T3b — Tier-2 grounding enqueue: stale/aging/unverified hits
    # trigger re-grounding. HARD INVARIANT: DB-only, no network/disk/process I/O.
    # WS2 fixes the 949-vs-9 divergence: the flag and the queue row are now set
    # ATOMICALLY (both or neither) in ONE debounced branch, and ONLY when the
    # claim's falsifier is actually locally resolvable (else it can never clear —
    # so we leave it honestly 'unverified' rather than manufacture a dead flag).
    if _grounding_on and insight_list:
        try:
            # Build id -> (falsifier kind, spec, entity_type) from the candidate rows
            # (joined in the SELECT above — no extra query).
            _fal_by_id: dict = {}
            for _cr in candidates:
                _fal_by_id[_cr["id"]] = (
                    _cr["falsifier_kind"],
                    _cr["falsifier_spec"] if "falsifier_spec" in _cr.keys() else None,
                    _cr["falsifier_entity_type"] if "falsifier_entity_type" in _cr.keys() else None,
                )
            _one_hour_ago = (__import__("datetime").datetime.utcnow()
                             - __import__("datetime").timedelta(hours=1)
                             ).strftime("%Y-%m-%d %H:%M:%S")
            for _hit in insight_list:
                _liv = _hit.get("liveness", {})
                if _liv.get("verdict") not in ("stale", "aging", "unverified"):
                    continue
                _iid = _hit["id"]
                _fk, _fs, _fet = _fal_by_id.get(_iid, (None, None, None))
                # Resolvability gate (shared predicate): if we can't check it from
                # the local host, do NOT flag — keeps grounding_due honest.
                if not lifecycle.falsifier_resolvable(_fk, _fs, _fet):
                    continue
                # Debounce: skip if already flagged or grounded within 1h
                _chk = conn.execute(
                    "SELECT grounding_due, grounded_at FROM insights WHERE id=?", (_iid,)
                ).fetchone()
                if _chk and (
                    _chk["grounding_due"]
                    or (_chk["grounded_at"] and _chk["grounded_at"] >= _one_hour_ago)
                ):
                    continue
                # ATOMIC: flag + queue together (both or neither).
                conn.execute(
                    "INSERT OR IGNORE INTO grounding_queue "
                    "(claim_kind, claim_id, reason, trigger_src) VALUES ('insight',?,?,?)",
                    (_iid, "volatile_stale", "recall"),
                )
                conn.execute(
                    "UPDATE insights SET grounding_due=1 WHERE id=?", (_iid,)
                )
            conn.commit()
        except Exception as _gq_exc:
            logger.debug("Phase 25 Tier-2 enqueue skipped: %s", _gq_exc)

    # Phase A: recall filter telemetry — fire-and-forget, never raises.
    # Records pre/post supersede-filter candidate counts for /api/data_quality recall_filter_pct KPI.
    if _rfe_pre > 0 and _table_exists(conn, "recall_filter_events"):
        try:
            import hashlib as _hl
            from datetime import datetime as _dt, timezone as _tz
            _rfe_ts = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _rfe_fp = _hl.sha256((query or "").encode()).hexdigest()[:16]
            conn.execute(
                "INSERT INTO recall_filter_events "
                "(ts, project, candidates_pre_filter, candidates_post_filter, query_fingerprint) "
                "VALUES (?, ?, ?, ?, ?)",
                (_rfe_ts, project, _rfe_pre, _rfe_post, _rfe_fp),
            )
            conn.commit()
        except Exception as _rfe_exc:
            logger.debug("recall_filter_events insert skipped: %s", _rfe_exc)

    conn.close()

    elapsed_ms = (time.perf_counter() - t0) * 1000
    _stats["recall_times_ms"].append(elapsed_ms)
    if len(_stats["recall_times_ms"]) > 1000:
        _stats["recall_times_ms"] = _stats["recall_times_ms"][-500:]

    # Build principle dicts with Phase 9 conflict annotation
    principles_list = []
    for r in principle_rows:
        pd: dict = {"id": r["id"], "content": r["content"], "confidence": r["confidence"]}
        if r["suspect_of"]:
            pd["_conflict"] = {
                "suspect_of": r["suspect_of"],
                "reason": r["suspect_reason"],
                "score": r["suspect_score"],
                "detected_at": r["suspect_detected_at"],
            }
        # Phase 25: Tier-1 liveness stamp for principles (highest-trust layer)
        if _grounding_on:
            pd["liveness"] = _liveness_stamp(
                r["grounded_at"], r["volatility_class"], r["grounding_due"],
                r["falsifier_result"], r["falsifier_kind"],
            )
            # Rev-5 §5.5 — server-side stale banner (never suppresses the hit).
            _attach_stale_banner(pd, pd["liveness"])
        principles_list.append(pd)

    return {
        "query": query,
        "insights": insight_list,
        "principles": principles_list,
        "count": len(insight_list) + len(principles_list),
        "embed_available": embed_available,
    }


def _do_recall_principle(topic: str, project: Optional[str]) -> dict:
    topic = (topic or "").strip()
    conn = get_db()

    # Try cosine search first if model is loaded
    rows = []
    if _model_loaded and topic:
        try:
            topic_vec = np.frombuffer(_embed_one(topic), dtype="float32")
            p_proj_clause = "(project = ? OR project IS NULL)" if project else "1=1"
            p_params = [project] if project else []
            p_emb_rows = conn.execute(
                f"SELECT id, project, content, tags, confidence, source_insights, embedding, "
                f"suspect_of, suspect_reason, suspect_score, suspect_detected_at FROM principles "
                f"WHERE embedding IS NOT NULL AND superseded_by IS NULL AND {p_proj_clause}",
                p_params,
            ).fetchall()
            if p_emb_rows:
                p_ids = [r["id"] for r in p_emb_rows]
                p_matrix = np.vstack([np.frombuffer(r["embedding"], dtype="float32") for r in p_emb_rows])
                p_sims = p_matrix @ topic_vec
                scored = sorted(
                    [(p_ids[i], float(p_sims[i]), p_emb_rows[i]) for i in range(len(p_ids))],
                    key=lambda x: x[1], reverse=True
                )[:10]
                rows = [r for _, sim, r in scored if sim > 0.10]
        except Exception as e:
            logger.warning("Principle cosine search failed in recall_principle: %s", e)

    if not rows:
        # LIKE fallback
        params = [f"%{topic}%"]
        sql = (
            "SELECT id, project, content, tags, confidence, source_insights, "
            "suspect_of, suspect_reason, suspect_score, suspect_detected_at "
            "FROM principles WHERE content LIKE ?"
        )
        if project:
            sql += " AND (project = ? OR project IS NULL)"
            params.append(project)
        sql += " ORDER BY confidence DESC LIMIT 10"
        rows = conn.execute(sql, params).fetchall()

    conn.close()

    # Build result dicts including Phase 9 conflict annotation
    _PRINCIPLE_KEYS = ("id", "project", "content", "tags", "confidence", "source_insights")
    principles_out = []
    for r in rows:
        d = {k: r[k] for k in _PRINCIPLE_KEYS if k in r.keys()}
        if r["suspect_of"]:
            d["_conflict"] = {
                "suspect_of": r["suspect_of"],
                "reason": r["suspect_reason"],
                "score": r["suspect_score"],
                "detected_at": r["suspect_detected_at"],
            }
        principles_out.append(d)

    return {
        "topic": topic,
        "principles": principles_out,
        "count": len(principles_out),
    }


_SYNTHETIC_SESSION_RE = re.compile(r"^mcp-\d+$")


def _resolve_session_id(conn, session_id: Optional[str], project: Optional[str]) -> Optional[str]:
    """Map a missing/synthetic session id to the real claude session UUID.

    MCP callers send "mcp-<pid>" (or nothing); the SessionStart hook records the
    real UUID in session_meta keyed by project. When the incoming id is not a
    usable UUID, look up the most-recent session_meta row for this project within
    a 12h window. Returns the resolved UUID, or the original id unchanged if no
    match (never worse than the pre-fix NULL/synthetic value).
    """
    if session_id and not _SYNTHETIC_SESSION_RE.match(session_id):
        return session_id  # already a real id — trust it
    if not project:
        return session_id
    try:
        if not _table_exists(conn, "session_meta"):
            return session_id
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        row = conn.execute(
            """
            SELECT session_uuid FROM session_meta
            WHERE project = ? AND last_seen_at >= ?
            ORDER BY last_seen_at DESC
            LIMIT 1
            """,
            (project, cutoff),
        ).fetchone()
        if row and row["session_uuid"]:
            return row["session_uuid"]
    except Exception as exc:
        logger.debug("_resolve_session_id: lookup failed: %s", exc)
    return session_id


def _do_save_insight_fast(content: str, type_: str, tags: str, source_file: str,
                          project: Optional[str], force: bool,
                          role: Optional[str] = None, epic_tag: Optional[str] = None,
                          session_id: Optional[str] = None):
    """Fast path: dup check + INSERT + commit + embed. Returns (result_dict, emb_bytes).

    Slow work (entity extraction, contradiction detection, broadcast) moves to
    _do_save_insight_post which runs as a background task AFTER the response is
    returned to the client. This dropped p50 save_insight latency from
    ~600-1200ms (dominated by Haiku contradiction call) to ~80-150ms.
    """
    if not content or not content.strip():
        return {"ok": False, "error": "content is required"}, None

    conn = get_db()

    # REV 3 write-path governance (closed-loop.md): HARD gates run BEFORE
    # embedding/dedup/insert. Schema gate (size/type) and content secret-scan
    # are HARD -- a failure routes the write to insights_staging with a
    # machine-readable reason and the insight is NEVER persisted to `insights`.
    # Provenance is ADVISORY only (see write_gate.py docstring / T_DIRECT,
    # 2026-07-04 staging-tier removal) -- it is surfaced on success, not blocking.
    _gate_advisories: list = []
    try:
        import write_gate as _write_gate
        _schema_verdict = _write_gate.check_schema(content, type_, tags, source_file, session_id, role)
        if not _schema_verdict.ok:
            staged_id = _write_gate.route_to_staging(conn, content, type_, project, _schema_verdict.reason)
            conn.close()
            return {
                "ok": False, "staged": True, "staged_id": staged_id,
                "disposition": "staged",
                "reason": _schema_verdict.reason,
                "message": "Write rejected by schema gate, routed to insights_staging for review.",
            }, None
        _gate_advisories = list(_schema_verdict.advisories)
        _secret_hit = _write_gate.scan_content_secrets(content)
        if _secret_hit:
            _reason = f"secret_scan:{_secret_hit}"
            staged_id = _write_gate.route_to_staging(conn, content, type_, project, _reason)
            conn.close()
            return {
                "ok": False, "staged": True, "staged_id": staged_id,
                "disposition": "staged",
                "reason": _reason,
                "message": "Write rejected by secret scan, routed to insights_staging for review.",
            }, None
    except Exception as _gate_exc:
        # Fail-open: a write_gate bug must never block a legitimate save.
        logger.warning("save_insight: write_gate hard gates raised (fail-open): %s", _gate_exc)

    # Compute embedding ONCE (was previously computed twice -- once for dup check, once for storage)
    new_emb_bytes = None
    new_vec = None
    if _model_loaded:
        try:
            new_emb_bytes = _embed_one(content)
            new_vec = np.frombuffer(new_emb_bytes, dtype="float32")
        except Exception as e:
            logger.warning("save_insight embed failed: %s", e)

    candidates = []
    if not force:
        rows = conn.execute(
            """SELECT id, content, embedding FROM insights WHERE status='active' AND (project = ? OR project IS NULL)
               ORDER BY created_at DESC LIMIT 300""",
            (project,),
        ).fetchall()
        cosine_used = False
        if new_vec is not None:
            embedded = [r for r in rows if r["embedding"] is not None]
            if embedded:
                matrix = np.vstack([np.frombuffer(r["embedding"], dtype="float32") for r in embedded])
                sims = matrix @ new_vec
                for i, sim in enumerate(sims):
                    if sim >= scoring.DEDUP_COSINE_THRESHOLD:
                        candidates.append({
                            "id": embedded[i]["id"],
                            "content": embedded[i]["content"][:120],
                            "similarity": round(float(sim), 3),
                        })
                cosine_used = True
        if not cosine_used:
            shingles_new = compute_shingles(content)
            for row in rows:
                sim = jaccard(shingles_new, compute_shingles(row["content"]))
                if sim >= scoring.DEDUP_JACCARD_THRESHOLD:
                    candidates.append({"id": row["id"], "content": row["content"][:120], "similarity": round(sim, 2)})
        if candidates:
            conn.close()
            _top_dup = max(candidates, key=lambda c: c.get("similarity", 0))
            return {
                "ok": False, "duplicate": True, "candidates": candidates,
                "disposition": f"merged_into:{_top_dup['id']}",
                "message": "Similar insight exists. Use force=true to insert anyway, or distill instead.",
            }, None

    # session_id root fix (2026-07-06): MCP callers send a synthetic per-process
    # id ("mcp-<pid>") or nothing, so insights.session_id was mostly NULL/synthetic
    # — which broke the ROI epic→session join (COUNT(DISTINCT session_id) = 0) and
    # role derivation. Resolve to the real claude session UUID via session_meta,
    # written by the SessionStart hook keyed by project. Conservative: only when
    # the incoming id is empty or the synthetic mcp-<pid> form, and only from a
    # recent row (single-operator context = one active session per project).
    session_id = _resolve_session_id(conn, session_id, project)

    _now_save = _utcnow_iso()
    conn.execute(
        """INSERT INTO insights (project, type, content, tags, status, source_file,
                                 role, epic_tag, session_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
        (project, type_, content, tags, source_file, role, epic_tag, session_id,
         _now_save, _now_save),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    auto_embedded = False
    if new_emb_bytes is not None:
        try:
            conn.execute("UPDATE insights SET embedding = ? WHERE id = ?", (new_emb_bytes, row_id))
            conn.commit()
            auto_embedded = True
        except Exception as e:
            logger.warning("save_insight embed UPDATE failed for #%s: %s", row_id, e)

    conn.close()

    # REV 3 write-path governance: lifecycle resolver is ADVISORY-only here —
    # it annotates the response but never changes the insert decision already
    # made above (dedup guard is unchanged, T_DEDUP-tested). `candidates` at
    # this point is always [] (a non-empty list would have short-circuited to
    # the duplicate-rejection return above), so in practice this only surfaces
    # explicit "supersedes/replaces #N" verbs; noop/update dispositions are
    # not reachable post-insert by construction.
    # `disposition` mirrors the REV 3 brief's three-value contract
    # (accepted|staged|merged_into:<id>) across ALL save_insight outcomes —
    # "accepted" here because a row WAS inserted; "staged"/"merged_into:<id>"
    # are set on the HARD-gate and dedup-reject returns above respectively.
    # The richer TRACE-style lifecycle verdict (noop/update/supersede/new) is
    # advisory-only and surfaced separately under `lifecycle` so it never
    # collides with the enum callers may pattern-match on.
    _resp: dict = {"ok": True, "id": row_id, "auto_embedded": auto_embedded, "disposition": "accepted"}
    if _gate_advisories:
        _resp["advisories"] = _gate_advisories
    try:
        _lifecycle = _write_gate.resolve_lifecycle(content, candidates)
        if _lifecycle.get("action") != "new":
            _resp["lifecycle"] = _lifecycle
    except Exception as _lc_exc:
        logger.debug("save_insight: resolve_lifecycle failed (advisory, non-blocking): %s", _lc_exc)
    return _resp, new_emb_bytes


# ---------------------------------------------------------------------------
# Phase 25-D — Write-time grounding hooks
# ---------------------------------------------------------------------------

_INVALIDATION_RE = __import__("re").compile(
    r"\b(?:supersedes|replaces)\s+#(\d+)\b", __import__("re").IGNORECASE
)


def _write_time_grounding_hooks(conn, insight_id: int, content: str) -> None:
    """Apply Phase 25-D write-time effects after a new insight is saved.

    1. Volatility classify: set insights.volatility_class from content.
    2. Falsifier derive: store the (auto-derived) falsifier from the strongest
       entity_link so the groundskeeper can re-check this claim later.
    3. Invalidation-verb parse: 'supersedes #N' / 'replaces #N' → execute
       the supersede edge (set superseded_by=insight_id on the target insight).
       Conservative: only explicit 'supersedes/replaces #N' numeric form.
       Does NOT auto-supersede if target already has a superseded_by set.
    """
    if not content:
        return

    # Compute grounding availability LOCALLY (gated on the falsifiers table so this
    # is safe before/after migrate 023). Do NOT reference _do_recall's local flag.
    grounding_on = _table_exists(conn, "falsifiers")

    # --- 1. Volatility classification + 2. falsifier derivation ---
    if grounding_on:
        try:
            vc = classify_volatility(content)
            if vc:
                conn.execute(
                    "UPDATE insights SET volatility_class=? WHERE id=?", (vc, insight_id)
                )
        except Exception as _vc_err:
            logger.debug("Phase 25-D: volatility classify failed for insight %s: %s",
                         insight_id, _vc_err)
        try:
            # Grounding v2: classify Tier-A (mechanical existence) vs Tier-B (predicate).
            # Tier-A: derive_falsifier as before; Tier-B: enqueue an 'author' job
            # (zero added latency — the caller returns instantly).
            if _GROUNDING_V2 and _table_exists(conn, "grounding_jobs"):
                _ents = extract_entities(content)
                _tier = _classify_tier(content, _ents)
                if _tier == "A":
                    fal = derive_falsifier(content)
                    if fal.get("kind") and fal["kind"] != "none":
                        now = datetime.now(timezone.utc).isoformat()
                        _ground_upsert_falsifier(
                            conn, "insight", insight_id, fal["kind"], fal.get("spec"),
                            fal.get("entity"), fal.get("entity_type"),
                            None, None, now, derived=1,
                        )
                        conn.execute(
                            "UPDATE insights SET falsifier_id=(SELECT id FROM falsifiers "
                            "WHERE claim_kind='insight' AND claim_id=?) WHERE id=?",
                            (insight_id, insight_id),
                        )
                else:  # Tier-B: enqueue authoring
                    _gv2_enqueue_job(conn, "insight", insight_id, "author", priority=5)
            else:
                # Legacy Tier-A path (grounding_jobs table not yet migrated)
                fal = derive_falsifier(content)
                if fal.get("kind") and fal["kind"] != "none":
                    now = datetime.now(timezone.utc).isoformat()
                    _ground_upsert_falsifier(
                        conn, "insight", insight_id, fal["kind"], fal.get("spec"),
                        fal.get("entity"), fal.get("entity_type"),
                        None, None, now, derived=1,
                    )
                    conn.execute(
                        "UPDATE insights SET falsifier_id=(SELECT id FROM falsifiers "
                        "WHERE claim_kind='insight' AND claim_id=?) WHERE id=?",
                        (insight_id, insight_id),
                    )
        except Exception as _fd_err:
            logger.debug("Phase 25-D: falsifier derive failed for insight %s: %s",
                         insight_id, _fd_err)

    # --- 3. Invalidation-verb parse ---
    matched_any = False
    for m in _INVALIDATION_RE.finditer(content):
        target_id = int(m.group(1))
        if target_id == insight_id:
            continue  # self-reference guard
        try:
            target = conn.execute(
                "SELECT id, superseded_by FROM insights WHERE id=?", (target_id,)
            ).fetchone()
            if target and not target["superseded_by"]:
                conn.execute(
                    "UPDATE insights SET superseded_by=?, updated_at=? WHERE id=?",
                    (insight_id, _utcnow_iso(), target_id),
                )
                matched_any = True
                logger.info(
                    "Phase 25-D: prose-supersede edge: insight #%s superseded_by #%s "
                    "(verb: '%s')", target_id, insight_id, m.group(0)
                )
                try:
                    _audit_log(
                        "system", "prose_supersede", "insight", str(target_id),
                        {"superseded_by": insight_id, "verb_match": m.group(0)},
                        {"ok": True}, "write-time invalidation-verb parse", None,
                    )
                except Exception:
                    pass
        except Exception as _sv_err:
            logger.debug("Phase 25-D: prose-supersede failed for target #%s: %s",
                         target_id, _sv_err)

    if matched_any or grounding_on:
        try:
            conn.commit()
        except Exception:
            pass


def _enrich_insight(conn, insight_id: int, content: str, project: Optional[str],
                    emb_bytes: Optional[bytes] = None) -> None:
    """Single post-insert enrichment path shared by EVERY insight write.

    Given an already-inserted `insights` row, make it fully queryable:
      1. Embedding — stored so the row surfaces in semantic recall. If `emb_bytes`
         is supplied (fast-path save already computed it) it is reused; otherwise
         the embedding is computed here from `content`.
      2. Entity extraction (Phase 7) → entity_links.
      3. Contradiction detection (Phase 9) — flags suspect_of edges.
      4. Write-time grounding hooks (Phase 25-D) — volatility class + falsifier
         derivation + invalidation-verb prose-supersede.

    NEVER raises: each stage is independently guarded and logged. Callers that
    already returned a response to the client (background task) and callers that
    run inline (staged/approve) both use this so there is exactly ONE enrichment
    definition. Note: FTS is trigger-maintained on the insights table, so this
    function deliberately does NOT touch the FTS index.

    Uses the caller's `conn` so it can run inside an existing transaction
    (staged/approve) or on a dedicated bg connection (save_insight).
    """
    # 1. Embedding — compute if not supplied, then store.
    if emb_bytes is None and _model_loaded and content:
        try:
            emb_bytes = _embed_one(content)
        except Exception as e:
            logger.warning("_enrich_insight embed failed for #%s: %s", insight_id, e)
    if emb_bytes is not None:
        try:
            conn.execute("UPDATE insights SET embedding = ? WHERE id = ?", (emb_bytes, insight_id))
            conn.commit()
        except Exception as e:
            logger.warning("_enrich_insight embed UPDATE failed for #%s: %s", insight_id, e)

    # 2. Phase 7 + Graph v2: entity extraction with canonical gating (migration 027).
    # Rejected entities are stored in entity_links but NOT inserted into entity_canonical
    # and canonical_entity_id stays NULL — this closes the "junk still gets stored" hole.
    try:
        entities = extract_entities(content)
        for ent in entities:
            norm = _normalize_entity(ent["entity_type"], ent["entity"])
            if norm["reject"]:
                # Insert raw row (append-only doctrine); canonical_entity_id stays NULL.
                conn.execute(
                    "INSERT OR IGNORE INTO entity_links "
                    "(insight_id, entity, entity_type, raw_match) VALUES (?, ?, ?, ?)",
                    (insight_id, ent["entity"], ent["entity_type"], ent["raw_match"]),
                )
            else:
                # Upsert into entity_canonical then link.
                canonical = norm["canonical"]
                try:
                    # Prefer lookup by canonical form so D:/x and /x share one ec row.
                    existing = conn.execute(
                        "SELECT id FROM entity_canonical WHERE entity_type = ? AND canonical = ?",
                        (ent["entity_type"], canonical),
                    ).fetchone()
                    if existing:
                        ec_id = existing["id"]
                        # Register this raw_value pointing to the same canonical (alias)
                        conn.execute(
                            "INSERT OR IGNORE INTO entity_canonical "
                            "(entity_type, raw_value, canonical) VALUES (?, ?, ?)",
                            (ent["entity_type"], ent["entity"], canonical),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO entity_canonical (entity_type, raw_value, canonical)
                               VALUES (?, ?, ?)
                               ON CONFLICT(entity_type, raw_value) DO UPDATE
                               SET canonical = excluded.canonical""",
                            (ent["entity_type"], ent["entity"], canonical),
                        )
                        ec_row = conn.execute(
                            "SELECT id FROM entity_canonical WHERE entity_type = ? AND raw_value = ?",
                            (ent["entity_type"], ent["entity"]),
                        ).fetchone()
                        ec_id = ec_row["id"] if ec_row else None
                except Exception:
                    ec_id = None
                conn.execute(
                    """INSERT OR IGNORE INTO entity_links
                       (insight_id, entity, entity_type, raw_match, canonical_entity_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (insight_id, ent["entity"], ent["entity_type"], ent["raw_match"], ec_id),
                )
        conn.commit()
    except Exception as e:
        logger.warning("Entity extraction failed for insight %s: %s", insight_id, e)

    # 3. Phase 9: contradiction detection (Haiku via model-router proxy -- the slow part)
    try:
        if emb_bytes:
            flagged = _detect_contradictions(conn, "insight", insight_id, content, emb_bytes, project)
            if flagged:
                logger.info("Phase 9: flagged %d contradiction(s) for insight %d: %s",
                            len(flagged), insight_id, flagged)
    except Exception as e:
        logger.warning("Phase 9 contradiction detection failed for #%s: %s", insight_id, e)

    # 4. Phase 25-D: write-time grounding hooks. Runs AFTER entity_links are
    # populated so falsifier derivation can use them.
    try:
        _write_time_grounding_hooks(conn, insight_id, content)
    except Exception as e:
        logger.warning("Phase 25-D write-time hooks failed for insight %s: %s", insight_id, e)

    # 5. Grounding v3 (Claim Layer): decompose the narrative into atomic claims,
    # canonicalize+dedup into the shared pool, classify P1-P5, author predicates.
    # Runs AFTER entity extraction (step 2) so canonical entity links exist.
    # Fail-soft: never raises (save already returned 200); uses a BACKGROUND role
    # client (direct api.anthropic.com for oauth — never the interactive :8788
    # proxy). llm=None => rule-only classification + template predicates (still
    # 100%-covered), the daemon degrades cleanly when the LLM is unavailable.
    if _CLAIM_LAYER:
        try:
            src_row = conn.execute(
                "SELECT source_file FROM insights WHERE id=?", (insight_id,)
            ).fetchone()
            source_file = (src_row["source_file"] if src_row else None) or None
            role_llm = None
            try:
                role_llm = _claim_layer.get_role_client("decompose")
            except RuntimeError as _iso_err:
                # Misconfigured routing (background traffic would hit the
                # interactive proxy) — refuse the LLM path, fall back to rules.
                logger.error("claim_layer: routing isolation violation, using rule-only: %s", _iso_err)
            res = _claim_layer.process_insight_claims(
                conn, insight_id, content, source_file=source_file, llm=role_llm
            )
            if res.get("inserted") or res.get("linked"):
                logger.info(
                    "claim_layer: insight %s -> %d new + %d linked claim(s)",
                    insight_id, res.get("inserted", 0), res.get("linked", 0),
                )
            # 5b. Claim-level contradiction detector (behind config flag; no-op
            # scan until claim_contradiction_enabled=True — detect_for_claim
            # returns [] immediately when the flag is off, so this is cheap
            # even in the default/off state). Scans every claim_id returned
            # this call (both newly-inserted and re-linked — a linked claim's
            # peer set may have grown since it was last scanned, and the
            # UNIQUE(claim_a_id, claim_b_id) constraint + INSERT OR IGNORE in
            # claim_contradictions makes re-scanning idempotent).
            for _cid in res.get("claim_ids", []):
                try:
                    import claim_contradiction as _claim_contradiction
                    _claim_contradiction.detect_for_claim(conn, _cid)
                except Exception as _cc_err:
                    logger.debug("claim_contradiction: scan failed for claim %s: %s", _cid, _cc_err)
        except Exception as e:
            logger.warning("claim_layer (v3) pipeline failed for insight %s: %s", insight_id, e)


def _do_save_insight_post(row_id: int, content: str, project: Optional[str], emb_bytes: Optional[bytes]):
    """Slow path: entity extraction + contradiction detection + grounding. Runs
    in executor from the background task scheduled by the handler. NEVER raises --
    the save already returned 200 to the client, so any failure here is logged
    only. Delegates to the shared `_enrich_insight` write path (embedding already
    set by the fast path, so emb_bytes is passed through and not recomputed).
    """
    conn = get_db()
    _enrich_insight(conn, row_id, content, project, emb_bytes)
    conn.close()


def _do_promote_insight(conn, insight_id: int, actor: str = "operator",
                        content: Optional[str] = None, role: Optional[str] = None,
                        epic_tag: Optional[str] = None,
                        session_id: Optional[str] = None) -> dict:
    """Promote one insight to a principle on an ALREADY-OPEN conn.

    Shared by the /promote_insight endpoint (actor='operator') and the WS2 T1
    auto-promote gate inside _do_verify_insight (actor='auto-promote'). Does NOT
    open, commit, or close the connection — the caller owns the transaction so
    verify+promote+audit land atomically. Writes an operator_audit_log row.
    """
    row = conn.execute("SELECT * FROM insights WHERE id = ?", (insight_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"insight {insight_id} not found"}
    if row["promoted_to"]:
        return {"ok": False, "error": f"already promoted to principle {row['promoted_to']}"}
    body_content = content if content else row["content"]
    conn.execute(
        """INSERT INTO principles (project, content, source_insights, confidence, tags,
                                   role, epic_tag, session_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["project"], body_content, str(insight_id), scoring.PROMOTE_SEED_CONFIDENCE,
         row["tags"], role, epic_tag, session_id, _utcnow_iso(), _utcnow_iso()),
    )
    principle_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "UPDATE insights SET promoted_to = ?, updated_at = ? WHERE id = ?",
        (principle_id, _utcnow_iso(), insight_id),
    )
    # Every mutation auditable (doctrine).
    try:
        conn.execute(
            "INSERT INTO operator_audit_log (created_at, actor, action, target_class, target_id, payload, result, note, session_id) "
            "VALUES (?, ?, 'promote_insight', 'insight', ?, ?, 'ok', ?, ?)",
            (_utcnow_iso(), actor, insight_id,
             json.dumps({"principle_id": principle_id, "seed_confidence": scoring.PROMOTE_SEED_CONFIDENCE}),
             f"promoted insight {insight_id} -> principle {principle_id}", session_id),
        )
    except Exception as _aexc:
        logger.debug("promote audit-log write skipped: %s", _aexc)
    return {"ok": True, "principle_id": principle_id, "source_insight": insight_id}


def _do_verify_insight(id_: int, status: str) -> dict:
    if status not in ("verified", "stale"):
        return {"ok": False, "error": "status must be 'verified' or 'stale'"}
    conn = get_db()
    row = conn.execute(
        "SELECT id, confidence, verify_count, verify_streak, promoted_to FROM insights WHERE id = ?", (id_,)
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": f"insight {id_} not found"}

    conf = row["confidence"] or 0.5
    vcount = row["verify_count"] or 0
    streak = row["verify_streak"] or 0
    already_promoted = row["promoted_to"]

    if status == "verified":
        new_conf = min(1.0, conf + scoring.VERIFY_INSIGHT_UP)
        new_streak = streak + 1
        new_status = "active"
    else:
        new_conf = max(0.0, conf - scoring.VERIFY_INSIGHT_DOWN)
        new_streak = 0
        new_status = "stale" if new_conf < scoring.STALE_STATUS_FLOOR else "active"

    new_vcount = vcount + 1
    _verify_now = _utcnow_iso()
    conn.execute(
        """UPDATE insights SET confidence = ?, verify_count = ?, verify_streak = ?,
                                status = ?, verified_at = ?, updated_at = ?
           WHERE id = ?""",
        (new_conf, new_vcount, new_streak, new_status, _verify_now, _verify_now, id_),
    )

    # WS2 T1 — auto-promote gate: the DOCUMENTED lifecycle, previously vaporware.
    # A 'verified' bump that crosses ALL thresholds AND is not already promoted
    # auto-promotes to a principle in the SAME transaction, audited as 'auto-promote'.
    auto_promoted = None
    if (status == "verified" and already_promoted is None
            and new_conf >= scoring.AUTO_PROMOTE_MIN_CONFIDENCE
            and new_vcount >= scoring.AUTO_PROMOTE_MIN_VERIFY_COUNT
            and new_streak >= scoring.AUTO_PROMOTE_MIN_VERIFY_STREAK):
        promo = _do_promote_insight(conn, id_, actor="auto-promote")
        if promo.get("ok"):
            auto_promoted = promo["principle_id"]

    conn.commit()
    conn.close()
    result = {"ok": True, "id": id_, "new_confidence": round(new_conf, 3),
              "verify_count": new_vcount, "status": new_status}
    if auto_promoted is not None:
        result["auto_promoted"] = auto_promoted
    return result


def _do_distill(insight_ids: list, content: str, project: Optional[str],
                role: Optional[str] = None, epic_tag: Optional[str] = None,
                session_id: Optional[str] = None) -> dict:
    if not insight_ids or not content:
        return {"ok": False, "error": "insight_ids and content are required"}
    insight_ids = list(dict.fromkeys(insight_ids))
    conn = get_db()
    placeholders = ",".join("?" * len(insight_ids))
    rows = conn.execute(f"SELECT id, project FROM insights WHERE id IN ({placeholders})", insight_ids).fetchall()
    if len(rows) != len(insight_ids):
        conn.close()
        return {"ok": False, "error": "some insight_ids do not exist"}

    detected_project = project or rows[0]["project"]
    source_str = ",".join(str(r["id"]) for r in rows)

    conn.execute(
        """INSERT INTO principles (project, content, source_insights, confidence,
                                   role, epic_tag, session_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (detected_project, content, source_str, scoring.PROMOTE_SEED_CONFIDENCE,
         role, epic_tag, session_id, _utcnow_iso(), _utcnow_iso()),
    )
    conn.commit()
    principle_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        f"UPDATE insights SET promoted_to = ? WHERE id IN ({placeholders})",
        [principle_id] + insight_ids,
    )
    conn.commit()

    # Embed the new principle for semantic recall
    if _model_loaded:
        try:
            emb_bytes = _embed_one(content)
            conn.execute("UPDATE principles SET embedding = ? WHERE id = ?", (emb_bytes, principle_id))
            conn.commit()
        except Exception as e:
            logger.warning("Failed to embed new principle %s: %s", principle_id, e)

    # Phase 7: extract entities and link them to the principle
    try:
        entities = extract_entities(content)
        for ent in entities:
            conn.execute(
                "INSERT OR IGNORE INTO entity_links (principle_id, entity, entity_type, raw_match) VALUES (?, ?, ?, ?)",
                (principle_id, ent["entity"], ent["entity_type"], ent["raw_match"]),
            )
        conn.commit()
    except Exception as e:
        logger.warning("Entity extraction failed for principle %s: %s", principle_id, e)

    # Phase 9: contradiction detection for the new principle (graceful -- never blocks distill)
    try:
        emb_row = conn.execute("SELECT embedding FROM principles WHERE id = ?", (principle_id,)).fetchone()
        if emb_row and emb_row["embedding"]:
            flagged = _detect_contradictions(
                conn, "principle", principle_id, content, emb_row["embedding"], detected_project
            )
            if flagged:
                logger.info(
                    "Phase 9: distill flagged %d contradiction(s) for principle %d: %s",
                    len(flagged), principle_id, flagged,
                )
    except Exception as e:
        logger.warning("Phase 9 contradiction detection failed for principle %s: %s", principle_id, e)

    conn.close()
    return {"ok": True, "principle_id": principle_id, "source_count": len(insight_ids)}


def _do_suggest_tags(content: str, project: Optional[str], limit: int) -> dict:
    if not _model_loaded:
        return {"ok": False, "error": "embedding model not loaded yet"}
    conn = get_db()
    if project:
        rows = conn.execute(
            """SELECT id, tags, embedding FROM insights
               WHERE status='active' AND project=? AND embedding IS NOT NULL AND tags IS NOT NULL AND tags != ''
               ORDER BY id DESC LIMIT 500""",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, tags, embedding FROM insights
               WHERE status='active' AND embedding IS NOT NULL AND tags IS NOT NULL AND tags != ''
               ORDER BY id DESC LIMIT 500""",
        ).fetchall()
    conn.close()

    if not rows:
        return {"ok": True, "suggested_tags": [], "based_on": [], "message": "No embedded insights with tags found"}

    try:
        new_vec = np.frombuffer(_embed_one(content), dtype="float32")
    except Exception as e:
        return {"ok": False, "error": f"embed failed: {e}"}

    ids = [r["id"] for r in rows]
    matrix = np.vstack([np.frombuffer(r["embedding"], dtype="float32") for r in rows])
    sims = matrix @ new_vec

    top_indices = sims.argsort()[::-1][:20]
    tag_counts: dict[str, int] = {}
    based_on = []
    for idx in top_indices:
        sim = float(sims[idx])
        based_on.append({"id": ids[idx], "similarity": round(sim, 4)})
        for tag in [t.strip() for t in (rows[idx]["tags"] or "").split(",") if t.strip()]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    suggested = sorted(tag_counts, key=lambda t: tag_counts[t], reverse=True)[:limit]
    return {"ok": True, "suggested_tags": suggested, "based_on": based_on}


def _do_recall_stats(project: Optional[str], days: int) -> dict:
    conn = get_db()

    hot = conn.execute(
        """SELECT i.id, i.type, i.content, i.confidence,
                   COUNT(re.id) AS hits,
                   COUNT(DISTINCT re.session_id) AS distinct_sessions
            FROM recall_events re
            JOIN insights i ON i.id = re.insight_id
            WHERE re.recalled_at > ? AND i.status='active'
                  AND i.superseded_by IS NULL
                  AND (? IS NULL OR i.project = ? OR i.project IS NULL)
            GROUP BY i.id ORDER BY hits DESC LIMIT 10""",
        # Python ISO cutoff — SQLite datetime('now', ...) emits the space-format
        # boundary that mis-compares against canonical ISO-T rows (principle #121).
        ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(), project, project),
    ).fetchall()

    # WS4 honest metrics: recall_events writes ONE ROW PER HIT (topk rows per
    # invocation), so COUNT(*) over-counts a query's "times" by its topk. Count
    # DISTINCT invocations via COUNT(DISTINCT recalled_at); expose the raw row
    # count separately as hits_total.
    # Assumption: recalled_at is second-resolution, so two identical queries
    # fired within the same second collapse to one invocation. That collision
    # is negligible for usage telemetry (agents don't fire the same query twice
    # per second) and never inflates the count.
    queries = conn.execute(
        """SELECT query,
                  COUNT(DISTINCT recalled_at) AS times,
                  COUNT(*) AS hits_total,
                  COUNT(DISTINCT session_id) AS distinct_sessions
            FROM recall_events
            WHERE recalled_at > ? AND query IS NOT NULL
            GROUP BY query ORDER BY times DESC LIMIT 10""",
        ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),),
    ).fetchall()

    dead = conn.execute(
        """SELECT i.id, i.type, substr(i.content, 1, 80) AS snippet, i.confidence, i.created_at
            FROM insights i
            WHERE i.status='active' AND i.superseded_by IS NULL
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

    promote = conn.execute(
        """SELECT i.id, i.project, substr(i.content, 1, 100) AS snippet,
                   COUNT(DISTINCT re.session_id) AS distinct_sessions,
                   COUNT(re.id) AS total_recalls
            FROM recall_events re
            JOIN insights i ON i.id = re.insight_id
            WHERE i.status='active' AND i.superseded_by IS NULL AND i.project IS NOT NULL
                  AND re.recalled_at > datetime('now', '-30 days')
            GROUP BY i.id
            HAVING distinct_sessions >= 3
            ORDER BY distinct_sessions DESC, total_recalls DESC LIMIT 10""",
    ).fetchall()

    conn.close()
    return {
        "ok": True, "project": project, "days": days,
        "hot_insights": [dict(r) for r in hot],
        "top_queries": [dict(r) for r in queries],
        "dead_weight": [dict(r) for r in dead],
        "cross_project_candidates": [dict(r) for r in promote],
    }


# ---------------------------------------------------------------------------
# WS2 T2 — Decay loop (trust must be able to fall)
#
# Weekly cadence. Applies the shared decay rule (db/lifecycle.decay_insights):
# active, non-promoted insights not recalled in DECAY_WINDOW_DAYS → confidence
# *= DECAY_FACTOR (floored). Principles are NOT decayed. Writes ONE summary row
# to operator_audit_log per run (actor='auto-decay') so the mutation is auditable.
# The no-op _cross_project_promotion_task was REMOVED (dead ceremony — the
# candidates it merely logged remain visible via recall_stats).
# ---------------------------------------------------------------------------

DECAY_LOOP_INTERVAL_S = int(os.environ.get("CRAG_ANCHOR_DECAY_INTERVAL_S", str(7 * 86400)))  # weekly


def _run_decay_once() -> dict:
    """Blocking decay pass (all projects). Called in an executor by the loop."""
    conn = get_db()
    try:
        res = lifecycle.decay_insights(conn, project=None, dry_run=False)
        ids = res.get("ids", [])
        # ONE audit summary row per run (never per-insight — keeps the log honest).
        try:
            conn.execute(
                "INSERT INTO operator_audit_log (created_at, actor, action, target_class, target_id, payload, result, note) "
                "VALUES (?, 'auto-decay', 'decay', 'insight', NULL, ?, 'ok', ?)",
                (_utcnow_iso(), json.dumps({"count": len(ids), "ids": ids[:500],
                             "factor": scoring.DECAY_FACTOR,
                             "window_days": scoring.DECAY_WINDOW_DAYS}),
                 f"decayed {len(ids)} insight(s)"),
            )
        except Exception as _aexc:
            logger.debug("decay audit-log write skipped: %s", _aexc)
        conn.commit()
        return res
    finally:
        conn.close()


def _seconds_since_last_decay() -> float | None:
    """Age of the newest 'auto-decay' audit row (persistent last-run marker).

    Returns None if decay has never run. The audit row is written on EVERY
    decay pass (even count=0), so it is a reliable schedule marker across
    daemon restarts — a sleep-first loop would never fire on a machine that
    restarts more often than the interval (same dead-loop class WS2 fixed).
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT created_at FROM operator_audit_log "
            "WHERE actor='auto-decay' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    # created_at is TEXT in one of two historical formats: legacy SQLite
    # 'YYYY-MM-DD HH:MM:SS' (space, naive-UTC) or ISO-T (offset/microseconds
    # optional) — live corpora contain both (migration 025 note). Same
    # normalize-then-fromisoformat pattern as db/contradiction.py; the old
    # strptime raised on ISO-T values and the loop logged a parse warning on
    # every pass against a real corpus (2026-07-18 boot-test finding #4).
    ts = datetime.fromisoformat(str(row[0]).replace(" ", "T").replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


async def _decay_loop():
    """Weekly decay, restart-safe: runs on startup when overdue, else sleeps
    the remainder of the interval (persistent marker = auto-decay audit row)."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            since = await loop.run_in_executor(None, _seconds_since_last_decay)
        except Exception as e:
            logger.warning("decay_loop marker read failed: %s", e)
            since = 0.0  # fail toward sleeping a full interval, not tight-looping
        if since is None or since >= DECAY_LOOP_INTERVAL_S:
            try:
                res = await loop.run_in_executor(None, _run_decay_once)
                logger.info("decay_loop: decayed %d insight(s)", res.get("decayed_insights", 0))
            except Exception as e:
                logger.warning("decay_loop failed: %s", e)
            await asyncio.sleep(DECAY_LOOP_INTERVAL_S)
        else:
            await asyncio.sleep(DECAY_LOOP_INTERVAL_S - since)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

async def _retry_model_load_async(max_attempts: int = 10, interval_s: int = 60):
    """Background retry for model load after startup failure.

    Fires when the model could not be loaded during lifespan startup (e.g.
    because the ONNX snapshot dir was corrupted/empty).  Retries every
    interval_s seconds up to max_attempts times so that once the cache is
    repaired or the network comes back the daemon self-heals without a restart.
    """
    for attempt in range(1, max_attempts + 1):
        await asyncio.sleep(interval_s)
        if _model_loaded:
            return  # already loaded by a parallel attempt
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _load_model)
            logger.info("Embedding model loaded on retry attempt %d", attempt)
            return
        except Exception as exc:
            logger.warning("Model load retry %d/%d failed: %s", attempt, max_attempts, exc)
    logger.error(
        "Embedding model failed to load after %d retries — running permanently degraded",
        max_attempts,
    )


if _GROUNDING_V2:
    # grounding_config.get_config() already merges stack.toml [grounding] +
    # the same CRAG_ANCHOR_GROUNDING_WORKER_SLEEP/_CONCURRENCY env var names (env
    # always wins there too) — Phase 1b single-source-of-truth seam.
    _grounding_cfg_boot = _grounding_config.get_config()
    _GROUNDING_WORKER_SLEEP_S: float = _grounding_cfg_boot.poll_interval_sec
    _GROUNDING_WORKER_CONCURRENCY: int = _grounding_cfg_boot.worker_concurrency
    _GROUNDING_MIN_CALL_INTERVAL_S: float = _grounding_cfg_boot.min_call_interval_sec
else:
    _GROUNDING_WORKER_SLEEP_S: float = float(
        os.environ.get("CRAG_ANCHOR_GROUNDING_WORKER_SLEEP", "5")
    )
    _GROUNDING_WORKER_CONCURRENCY: int = int(
        os.environ.get("CRAG_ANCHOR_GROUNDING_WORKER_CONCURRENCY", "2")
    )
    _GROUNDING_MIN_CALL_INTERVAL_S: float = float(
        os.environ.get("CRAG_ANCHOR_GROUNDING_MIN_CALL_INTERVAL", "8")
    )


async def _grounding_worker_loop(worker_id: int) -> None:
    """Async grounding worker — drains the grounding_jobs queue.

    Mirrors the _decay_loop pattern: runs in asyncio background via
    asyncio.create_task, executes blocking DB + subprocess work in the
    thread pool executor, sleeps _GROUNDING_WORKER_SLEEP_S between drains
    (shorter sleep after a job was processed, full sleep when idle).
    Fail-open: any exception is logged and the loop continues.
    """
    loop = asyncio.get_event_loop()
    while True:
        try:
            if not _GROUNDING_V2:
                await asyncio.sleep(_GROUNDING_WORKER_SLEEP_S * 6)
                continue

            llm = _llm_client.get_client()

            def _drain():
                conn = get_db()
                try:
                    return _gv2_drain_one_job(conn, llm)
                finally:
                    conn.close()

            did_work = await loop.run_in_executor(None, _drain)
            # Pace LLM-calling drains: after a job was processed, wait
            # _GROUNDING_MIN_CALL_INTERVAL_S before the next one so the loop
            # can't burst-429 the shared Headroom bucket / Claude subscription
            # the interactive session depends on (breathing-cord protection).
            # With worker_concurrency=1 this is the global call rate limit:
            # ~1 job per interval. Idle -> normal poll sleep. Interval 0 falls
            # back to a small sleep to avoid a busy loop.
            if did_work:
                await asyncio.sleep(
                    _GROUNDING_MIN_CALL_INTERVAL_S if _GROUNDING_MIN_CALL_INTERVAL_S > 0 else 0.5
                )
            else:
                await asyncio.sleep(_GROUNDING_WORKER_SLEEP_S)
        except Exception as _we:
            logger.warning("grounding_worker[%d]: unhandled error: %s", worker_id, _we)
            await asyncio.sleep(_GROUNDING_WORKER_SLEEP_S)


if _GROUNDING_V2:
    _GROUNDING_SWEEP_INTERVAL_S: float = _grounding_cfg_boot.sweep_interval_sec
    _GROUNDING_SWEEP_BATCH: int = _grounding_cfg_boot.sweep_batch
else:
    _GROUNDING_SWEEP_INTERVAL_S: float = float(
        os.environ.get("CRAG_ANCHOR_GROUNDING_SWEEP_INTERVAL", "60")
    )
    _GROUNDING_SWEEP_BATCH: int = int(
        os.environ.get("CRAG_ANCHOR_GROUNDING_SWEEP_BATCH", "10")
    )


async def _grounding_sweep_loop() -> None:
    """Proactively funnel the EXISTING grounding_due=1 backlog into the job
    queue (author/reground), one bounded batch per claim_kind per tick.

    Before this loop, the only thing that ever enqueued author/reground work
    for a flagged claim was that claim happening to surface in a live /recall
    call with an aging/stale/unverified liveness verdict — most of the 449
    flagged claims (2026-07-05) never get recalled, so they never got
    enqueued at all ("reground barely runs": 5 real verdicts/24h vs 3948
    author-nulls). This loop drains that backlog directly via
    grounding_resolve.sweep_flagged_claims, bounded by _GROUNDING_SWEEP_BATCH
    per tick per claim_kind so a large backlog doesn't thundering-herd the
    LLM. Idempotent — the grounding_jobs pending-dedup index means re-sweeping
    an already-queued claim is a no-op.
    """
    loop = asyncio.get_event_loop()
    while True:
        try:
            if _GROUNDING_V2:
                def _sweep():
                    conn = get_db()
                    try:
                        import grounding_resolve
                        return grounding_resolve.sweep_flagged_claims(conn, limit=_GROUNDING_SWEEP_BATCH)
                    finally:
                        conn.close()

                n = await loop.run_in_executor(None, _sweep)
                if n:
                    logger.info("grounding sweep: enqueued %d job(s) from flagged backlog", n)
            await asyncio.sleep(_GROUNDING_SWEEP_INTERVAL_S)
        except Exception as _se:
            logger.warning("grounding_sweep_loop: unhandled error: %s", _se)
            await asyncio.sleep(_GROUNDING_SWEEP_INTERVAL_S)


async def _capture_task_loop():
    """In-process autonomic capture tailer (docs/architecture.md REV 6/8: "the
    loop runs AROUND the agent"). Reuses run_capture.run_once — we do NOT
    duplicate the tailer/scan logic. Each iteration is fail-soft (log +
    continue); the blocking scan runs in a thread pool so it never stalls the
    event loop. Gated on [capture].daemon_task_enabled; interval from config.
    """
    loop = asyncio.get_event_loop()
    try:
        cfg = _capture_config.get_config()
    except Exception as exc:
        logger.warning("capture task: could not read config, disabling: %s", exc)
        return
    if not getattr(cfg, "daemon_task_enabled", False):
        logger.info("capture task: disabled by config (daemon_task_enabled=false)")
        return
    interval = max(5.0, float(getattr(cfg, "daemon_task_interval_sec", 120.0)))
    logger.info("capture task: started (interval=%.0fs)", interval)
    # Small initial delay so startup (model load, worker spin-up) settles first.
    await asyncio.sleep(min(interval, 15.0))
    while True:
        try:
            report = await loop.run_in_executor(None, _run_capture.run_once)
            emitted = (report or {}).get("emit_summary", {}).get("emitted", 0)
            if emitted:
                logger.info("capture task: scan emitted %d candidate(s)", emitted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("capture task: scan iteration failed (continuing): %s", exc)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


# C3 — disposition drain sweep (docs/architecture.md rev 5.2 drain-SLA:
# "every staging entry ages toward a terminal state... No infinite pending,
# ever"). POST /disposition/drain existed but nothing called it — staging
# rows past deadline never aged on their own. In-process function call
# (never an HTTP self-POST). Env overrides (grounding_config house pattern):
#   CRAG_ANCHOR_DISPOSITION_DRAIN_ENABLED (default 1)
#   CRAG_ANCHOR_DISPOSITION_DRAIN_INTERVAL_SEC (default 3600, min clamp 60)
def _drain_sweep_config() -> tuple:
    enabled = os.environ.get("CRAG_ANCHOR_DISPOSITION_DRAIN_ENABLED", "1") not in (
        "0", "false", "False", "no")
    try:
        interval = float(os.environ.get("CRAG_ANCHOR_DISPOSITION_DRAIN_INTERVAL_SEC", "3600"))
    except ValueError:
        interval = 3600.0
    return enabled, max(60.0, interval)


async def _drain_sweep_loop():
    """Periodic disposition drain-SLA sweep. Fail-soft per iteration;
    transitions-only logging (anti-storm house rule): silent when nothing
    was due, one summary line when rows actually transitioned."""
    loop = asyncio.get_event_loop()
    enabled, interval = _drain_sweep_config()
    if not enabled:
        logger.info("drain sweep: disabled by config (CRAG_ANCHOR_DISPOSITION_DRAIN_ENABLED=0)")
        return
    if not _DISPOSITION:
        logger.info("drain sweep: disposition module unavailable — not started")
        return
    logger.info("drain sweep: started (interval=%.0fs)", interval)
    await asyncio.sleep(min(interval, 30.0))
    while True:
        try:
            def _drain():
                conn = get_db()
                try:
                    return _disposition.drain_due(conn)
                finally:
                    conn.close()
            result = await loop.run_in_executor(None, _drain)
            if (result or {}).get("processed", 0):
                logger.info("drain sweep: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("drain sweep: iteration failed (continuing): %s", exc)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    # REV 4 item 2 — sync-folder corruption guard. Refuse to start if the DB
    # resolves under a Dropbox/OneDrive/Google Drive/iCloud/Syncthing/.sync
    # tree (documented SQLite corruption class). Escape hatch:
    # CRAG_ANCHOR_ALLOW_SYNC_PATH=1 downgrades to a loud warning. Runs FIRST so a
    # misconfigured deployment fails fast before any DB work.
    if _SYNC_GUARD:
        _sync_path_guard.check_db_path(DB_PATH, logger=logger)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Fresh-DB bootstrap: on a brand-new (or pre-schema) DB, apply db/schema.sql
    # plus every numbered migration so `crag-anchor` works out of the box on a
    # clean clone — no separate `crag-anchor-cli migrate` step required.
    # Idempotent (version-checked against schema_version) and a no-op on any
    # already-migrated DB; fail-soft per house style (a bootstrap error leaves
    # the daemon degraded but alive, and endpoints report the missing tables).
    try:
        _bootstrap_empty_db()
    except Exception as _bexc:
        logger.error("fresh-DB bootstrap failed (continuing degraded): %s", _bexc)
    loop = asyncio.get_event_loop()
    # Load model in thread pool (blocking).
    # On failure: log the error and continue in degraded mode (_model_loaded
    # stays False).  A background coroutine retries every 60s so the daemon
    # self-heals once the cache is repaired or the network comes back.
    try:
        await loop.run_in_executor(None, _load_model)
    except Exception as exc:
        logger.error(
            "Embedding model failed to load at startup — starting in degraded mode: %s",
            exc,
        )
        asyncio.create_task(_retry_model_load_async())
    logger.info(f"daemon ready version={VERSION} port={PORT} model_loaded={_model_loaded}")
    asyncio.create_task(_decay_loop())  # WS2 T2 — weekly confidence decay
    if _GROUNDING_V2:
        # Startup recovery: a previous process that died mid-job leaves rows
        # stuck in status='running' forever (observed 2026-07-06: 2 jobs from
        # the prior day). Reset them to pending before workers start.
        def _recover():
            conn = get_db()
            try:
                import grounding_queue_v2
                return grounding_queue_v2.recover_orphaned_jobs(conn)
            finally:
                conn.close()
        try:
            recovered = await loop.run_in_executor(None, _recover)
            if recovered:
                logger.info("startup: recovered %d orphaned grounding job(s)", recovered)
        except Exception as _rexc:
            logger.warning("startup: orphaned-job recovery failed: %s", _rexc)
        for _wid in range(_GROUNDING_WORKER_CONCURRENCY):
            asyncio.create_task(_grounding_worker_loop(_wid))
        asyncio.create_task(_grounding_sweep_loop())  # A5 — autoresolve backlog sweep
        logger.info("grounding v2 workers started (concurrency=%d)", _GROUNDING_WORKER_CONCURRENCY)
    # REV 6/8 — in-process autonomic capture task. Stored (not fire-and-forget)
    # so it can be cancelled cleanly on shutdown. Gated by config inside the loop.
    _capture_task = None
    if _CAPTURE_TASK:
        _capture_task = asyncio.create_task(_capture_task_loop())
    # C3 — disposition drain-SLA sweep (same stored-task/cancel pattern).
    _drain_task = asyncio.create_task(_drain_sweep_loop())
    yield
    logger.info("daemon stopping")
    for _bg_name, _bg_task in (("capture task", _capture_task), ("drain sweep", _drain_task)):
        if _bg_task is None:
            continue
        _bg_task.cancel()
        try:
            await _bg_task
        except asyncio.CancelledError:
            pass
        except Exception as _cexc:
            logger.warning("%s: shutdown error (ignored): %s", _bg_name, _cexc)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="crag Anchor daemon", version=VERSION, lifespan=lifespan)

# Read-model aggregates for the surface consumers (CLI/console/cloud/ops) —
# /overview /inbox /rules /console/modules. ONE contract, four consumers
# (infra-playbook docs/system-integration-map.md §2). Fail-soft: if the module
# is absent the daemon simply runs without those routes.
try:
    import aggregates as _aggregates
    _aggregates.bind(
        get_db=get_db,
        table_exists=_table_exists,
        claim_layer=(_claim_layer if _CLAIM_LAYER else None),
    )
    app.include_router(_aggregates.router)
except ImportError as _agg_err:  # pragma: no cover
    import builtins
    builtins.print(f"[daemon] aggregates (surface read-model) not found ({_agg_err}) — /overview /inbox /rules disabled")

# Deterministic session lifecycle (P0 wedge — design laws 1-2). GET /session/start
# composes the context payload the harness injects at session start; POST
# /session/end records the end marker + returns the payoff numbers. Reuses the
# aggregates builders bound just above — one source of truth. Fail-soft: absent
# module simply means those two routes are disabled.
try:
    import session_lifecycle as _session_lifecycle
    _session_lifecycle.bind(
        get_db=get_db,
        table_exists=_table_exists,
        aggregates=_aggregates,
    )
    app.include_router(_session_lifecycle.router)
except (ImportError, NameError) as _sl_err:  # pragma: no cover
    import builtins
    builtins.print(f"[daemon] session_lifecycle not found ({_sl_err}) — /session/start /session/end disabled")


# ---------------------------------------------------------------------------
# Overlay module discovery — the dependency-inversion seam (docs/architecture.md
# §10). A private/superset overlay (e.g. the ops Infra module) self-registers
# with ZERO engine edits: it is a module object implementing an optional
# `bind(...)` (DB accessors + aggregates injected), an optional
# `register(aggregates)` (idempotent CORE_MODULES append → /console/modules), and
# an optional `router` (an APIRouter mounted here). This is exactly the shape the
# core `aggregates`/`session_lifecycle` modules already use, so no new contract
# is invented — an overlay adopts it as-is.
#
# Two discovery channels, unioned (an overlay may appear in both; loaded once):
#   1. Entry-point group `crag_anchor.modules` — for pip-installed overlays.
#   2. Env var CRAG_ANCHOR_MODULES — comma-separated importable module names, so
#      a dev/checkout overlay works WITHOUT the overlay pip-installing itself
#      (entry points require an installed distribution).
#
# PER-MODULE fail-soft: one broken module logs a single ERROR line naming the
# module + exception and is SKIPPED; the daemon still boots. No overlays => zero
# behavior change (the manifest returns the core modules only).
# ---------------------------------------------------------------------------
_OVERLAY_ENTRY_POINT_GROUP = "crag_anchor.modules"
_loaded_overlay_modules: list[str] = []


def _discover_overlay_specs() -> list[tuple[str, Any]]:
    """Return an ordered, de-duplicated list of (name, loader) pairs to mount.

    `loader` is a zero-arg callable returning the module object; resolving it is
    deferred so a broken entry-point load is caught per-module in the caller.
    Discovery itself is fail-soft: a failure enumerating either channel logs a
    warning and yields an empty list for that channel, never raising.
    """
    specs: list[tuple[str, Any]] = []
    seen: set[str] = set()

    # Channel 1 — entry points (installed distributions).
    try:
        from importlib.metadata import entry_points

        try:
            eps = entry_points(group=_OVERLAY_ENTRY_POINT_GROUP)  # py3.10+
        except TypeError:  # pragma: no cover — very old importlib.metadata
            eps = entry_points().get(_OVERLAY_ENTRY_POINT_GROUP, [])
        for ep in eps:
            name = f"entrypoint:{ep.name}"
            if name in seen:
                continue
            seen.add(name)
            specs.append((name, ep.load))
    except Exception as _ep_err:  # pragma: no cover — enumeration is best-effort
        logger.warning("overlay entry-point discovery failed (ignored): %s", _ep_err)

    # Channel 2 — env-var importable module names (dev/checkout overlays).
    import importlib

    raw = os.environ.get("CRAG_ANCHOR_MODULES", "") or ""
    for mod_name in (m.strip() for m in raw.split(",")):
        if not mod_name:
            continue
        name = f"env:{mod_name}"
        if name in seen:
            continue
        seen.add(name)
        specs.append((name, (lambda m=mod_name: importlib.import_module(m))))

    return specs


def _load_overlay_modules() -> None:
    """Resolve, bind, register, and mount each discovered overlay. Per-module
    fail-soft — a broken module never stops the daemon or its siblings."""
    for name, loader in _discover_overlay_specs():
        try:
            mod = loader()

            # Optional dependency injection. We introspect the accepted kwargs so
            # an overlay declares only what it needs (ops_infra takes get_db +
            # table_exists; a claim-aware overlay could also take aggregates).
            bind = getattr(mod, "bind", None)
            if callable(bind):
                import inspect

                available = {
                    "get_db": get_db,
                    "table_exists": _table_exists,
                    "aggregates": _aggregates if "_aggregates" in globals() else None,
                    "claim_layer": (_claim_layer if _CLAIM_LAYER else None),
                }
                try:
                    params = inspect.signature(bind).parameters
                    accepts_var_kw = any(
                        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
                    )
                    kwargs = (
                        available
                        if accepts_var_kw
                        else {k: v for k, v in available.items() if k in params}
                    )
                except (TypeError, ValueError):  # builtins / unintrospectable
                    kwargs = available
                bind(**kwargs)

            # Optional manifest registration — append to aggregates.CORE_MODULES
            # so the overlay's module appears in GET /console/modules, exactly as
            # the core does. Idempotent by the overlay's own register().
            register = getattr(mod, "register", None)
            if callable(register) and "_aggregates" in globals():
                register(_aggregates)

            # Optional router — mount the overlay's routes.
            overlay_router = getattr(mod, "router", None)
            if overlay_router is not None:
                app.include_router(overlay_router)

            _loaded_overlay_modules.append(name)
            logger.info("overlay module loaded: %s", name)
        except Exception as _ov_err:  # noqa: BLE001 — one bad overlay is skipped
            logger.error("overlay module '%s' failed to load — SKIPPED: %r", name, _ov_err)


_load_overlay_modules()


# ---------------------------------------------------------------------------
# Event journal — append-only ring buffer of state-change events.
#
# Publish: `await _sse_publish(event_type, payload_dict)` appends to the
#   journal; external consumers poll GET /events/since?ts=<last_seen> to pull
#   new events. The daemon-side live stream is GET /subscribe (broadcaster.py),
#   consumed by the MCP server's broadcast subscriber.
#
# Supported event_types (kept narrow — only meaningful state changes):
#   contradiction_added | arena_verdict | insight_saved | supersede_created
#   session_end | decay_candidate_added | operator_action
# ---------------------------------------------------------------------------
_event_journal: deque = collections.deque(maxlen=1000)


async def _sse_publish(event_type: str, payload: dict) -> None:
    """Append an event to the journal so pollers pick it up via /events/since.
    (/subscribe via broadcaster.py serves live MCP clients.)"""
    event = {"type": event_type, "ts": datetime.utcnow().isoformat() + "Z", **payload}
    _event_journal.append(event)


# ---------------------------------------------------------------------------
# Request ID middleware + request counter
# ---------------------------------------------------------------------------

@app.middleware("http")
async def request_middleware(request: Request, call_next):
    req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = req_id
    _stats["requests_served"] += 1
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    return response


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RecallBody(BaseModel):
    query: str
    project: Optional[str] = None
    topk: int = 5
    session_id: Optional[str] = None
    snippet: bool = False
    # Phase 1 (unified roadmap) — provenance of the caller for recall_events
    role: Optional[str] = None        # 'coordinator'|'subagent'|'operator'
    epic_tag: Optional[str] = None    # sprint/epic context for the recall


class RecallPrincipleBody(BaseModel):
    topic: str
    project: Optional[str] = None


class SaveInsightBody(BaseModel):
    content: str
    type: str = "gotcha"
    tags: str = ""
    source_file: str = ""
    project: Optional[str] = None
    force: bool = False
    # Phase 16-E: provenance fields (optional, nullable)
    role: Optional[str] = None        # 'coordinator'|'subagent'|'operator'
    epic_tag: Optional[str] = None    # e.g. 'phase-16', 'vps-migration'
    session_id: Optional[str] = None  # CLAUDE_SESSION_ID at save time


class SaveBatchBody(BaseModel):
    insights: list
    project: Optional[str] = None


class VerifyInsightBody(BaseModel):
    id: int
    status: str


class DistillBody(BaseModel):
    insight_ids: list
    content: str
    project: Optional[str] = None
    # Phase 1 (unified roadmap) — provenance for principles
    role: Optional[str] = None
    epic_tag: Optional[str] = None
    session_id: Optional[str] = None


class SuggestTagsBody(BaseModel):
    content: str
    project: Optional[str] = None
    limit: int = 5


class EmbedBody(BaseModel):
    text: Optional[str] = None
    texts: Optional[list] = None


class PromoteGlobalBody(BaseModel):
    insight_id: int


# Phase 13 — Memory arena bodies
class ArenaBody(BaseModel):
    insight_ids: list
    strategy: str
    project: Optional[str] = None
    merged_content: Optional[str] = None
    dry_run: bool = False
    allow_resupersede: bool = False
    # Phase 1 (unified roadmap) — provenance for arena_events
    role: Optional[str] = None
    epic_tag: Optional[str] = None
    session_id: Optional[str] = None


class SupersedeBody(BaseModel):
    loser_id: int
    winner_id: int
    reason: Optional[str] = "manual"
    # Phase 1 (unified roadmap) — provenance for the resulting arena_events row
    role: Optional[str] = None
    epic_tag: Optional[str] = None
    session_id: Optional[str] = None


class AuditDriftBody(BaseModel):
    pattern: str
    project: Optional[str] = None


class ClearSuspectBody(BaseModel):
    """Clear the Phase 9 contradiction-detector flag on one OR a pair of insights.

    Two usage shapes:
      - {"id": <int>}                   — clear on a single insight (CLI parity).
      - {"a_id": <int>, "b_id": <int>}  — clear on both members of a flagged pair
                                          (dashboard AdjudicationQueuePage shape).
    """
    id: Optional[int] = None
    a_id: Optional[int] = None
    b_id: Optional[int] = None
    reason: Optional[str] = None


class ClearSuspectBatchBody(BaseModel):
    """Bulk clear (WS3a). `pairs` items are {id} OR {a_id, b_id} dicts."""
    pairs: list[dict] = []
    reason: Optional[str] = None


class ArenaBatchBody(BaseModel):
    """Bulk arena (WS3a). `pairs` is a list of id-lists (each >= 2 ids)."""
    pairs: list[list[int]] = []
    strategy: str
    project: Optional[str] = None
    dry_run: bool = False
    merged_content: Optional[str] = None
    role: Optional[str] = None
    epic_tag: Optional[str] = None
    session_id: Optional[str] = None


class GetBatchBody(BaseModel):
    """Bulk get (WS3a). Fetch insights OR principles by id in one query."""
    kind: str = "insight"
    ids: list[int] = []


class PromoteInsightBody(BaseModel):
    """Promote an insight to a principle (fast-path, seeds at confidence 0.9)."""
    insight_id: int
    content: Optional[str] = None  # override content; if None, uses insight content as-is
    # Phase 1 (unified roadmap) — provenance for the resulting principle row
    role: Optional[str] = None
    epic_tag: Optional[str] = None
    session_id: Optional[str] = None


class UpdateInsightBody(BaseModel):
    """Patch an existing insight's content/tags/source_file without creating a new one."""
    id: int
    content: Optional[str] = None
    tags: Optional[str] = None
    source_file: Optional[str] = None


class UpdatePrincipleBody(BaseModel):
    """Patch a principle's content/confidence/tags in place. Re-embeds on content change.
    Mirror of UpdateInsightBody — closes the gap where principles had no edit path."""
    id: int
    content: Optional[str] = None
    # Reject out-of-range confidence at the body-parse layer (FastAPI 422) rather than
    # silently persisting garbage like 1.5 or -0.1 (defect found in 2026-05-28 verification).
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    tags: Optional[str] = None


class SupersedePrincipleBody(BaseModel):
    """Mark one principle superseded by another principle (winner-by-id)."""
    loser_id: int
    winner_id: int
    reason: Optional[str] = "manual"
    role: Optional[str] = None
    epic_tag: Optional[str] = None
    session_id: Optional[str] = None


class VerifyPrincipleBody(BaseModel):
    """Confirm/contradict a principle. verified: +0.05, stale: -0.1 (gentler than insights —
    principles are curated high-trust facts)."""
    id: int
    status: str


# --- Phase 25: Grounded Memory ---
class GroundEnqueueBody(BaseModel):
    """Flag a claim for re-grounding. Used by every trigger (recall Tier-2, git
    hooks, file-watch, periodic). Cheap upsert — sets grounding_due + a queue row."""
    claim_kind: str = "insight"          # insight | principle
    claim_id: int
    reason: str = "trigger"              # falsifier_fail | source_changed | volatile_stale | trigger:<class>
    trigger_src: Optional[str] = None    # recall | git | file-watch | periodic | cron | write
    detail: Optional[str] = None


class GroundEnqueueBySourceBody(BaseModel):
    """In-repo git trigger (Phase 25-C): a post-merge hook posts the paths changed
    by a merge/pull; the daemon maps each path -> claims (via source_file +
    file/path entity_links) and flags them. Server-side mapping so the hook stays
    a dumb fail-soft one-liner."""
    paths: list[str]
    trigger_src: Optional[str] = "git"
    reason: Optional[str] = "source_changed"
    detail: Optional[str] = None
    cap: Optional[int] = 100             # bound blast radius on broad/common paths


class GroundRecordBody(BaseModel):
    """The groundskeeper cron posts a falsifier RESULT here (it runs the probe in
    its own process; the daemon never does network/disk I/O). pass → re-ground;
    fail → flag (never auto-demote — that's the agent's call).

    Grounding v2 (A3): additionally accepts verdict/reasoning/evidence from the
    in-daemon LLM adjudication worker.  When these are provided, a
    grounding_history row is appended (append-only chain-of-thought trail).
    """
    claim_kind: str = "insight"
    claim_id: int
    kind: str                            # endpoint|grep_config|path_exists|grep_symbol|query|none
    spec: Optional[str] = None
    entity: Optional[str] = None
    entity_type: Optional[str] = None
    result: str                          # pass | fail | error | skip
    detail: Optional[str] = None
    grounded_against: Optional[str] = None  # sha / probe-hash on pass
    trigger_src: Optional[str] = "cron"
    # v2 fields (optional — backwards-compatible)
    verdict: Optional[str] = None        # pass | fail | uncertain  (LLM adjudication)
    reasoning: Optional[str] = None      # LLM chain-of-thought (persisted, never discarded)
    evidence: Optional[str] = None       # verbatim step output that drove the verdict
    recipe_version: Optional[int] = None


class GroundClearBody(BaseModel):
    """Agent resolves a grounding_queue row. resolution: 'verified' (claim re-grounded,
    closes row + stamps grounded_at) | 'dismissed' (false-positive falsifier) | 'noted'
    (acted via supersede/verify_insight elsewhere). NEVER mutates confidence here —
    demotion uses verify_insight/supersede (detection != resolution, #2194)."""
    claim_kind: str = "insight"
    claim_id: int
    resolution: str = "verified"         # verified | dismissed | noted
    grounded_against: Optional[str] = None
    reason: Optional[str] = None


class TokenRecordBody(BaseModel):
    """Record token usage for a session (post-start cost ledger entry)."""
    project: str
    session_id: Optional[str] = None
    task_summary: Optional[str] = ""
    tokens_in: Optional[int] = 0
    tokens_out: Optional[int] = 0
    cache_hits: Optional[int] = 0
    cache_misses: Optional[int] = 0
    rtk_savings_pct: Optional[float] = 0
    headroom_savings_pct: Optional[float] = 0
    wall_time_sec: Optional[float] = 0
    model: Optional[str] = None
    cache_read_tokens: Optional[int] = 0
    cache_write_tokens: Optional[int] = 0
    fresh_input_tokens: Optional[int] = 0
    # Phase 17 — empirical validation fields (migration 015)
    recall_hits: Optional[int] = 0      # recalls that materially changed agent approach
    recall_misses: Optional[int] = 0    # queries where recall returned nothing useful
    repeated_errors: Optional[int] = 0  # errors matching a previously-saved insight
    novel_saves: Optional[int] = 0      # net-new insights saved this session
    # Migration 022 -- per-session role for ROI rollup ('coordinator'|'subagent'|'operator')
    role: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _log_request(request: Request, endpoint: str, duration_ms: float, **extra):
    logger.info(
        endpoint,
        extra={"request_id": getattr(request.state, "request_id", "-"),
               "endpoint": endpoint, "duration_ms": round(duration_ms, 2), **extra},
    )


@app.get("/events/since")
async def events_since(request: Request, since: Optional[str] = None, limit: int = 200):
    """Drain events from the journal newer than `since` ISO timestamp.

    Poll-based event pull (no long-lived stream): a consumer records the last
    `ts` it saw and passes it back on the next call.

    Query params:
      since — ISO timestamp; events with ts > since returned (omit for ALL)
      limit — max events returned (default 200)

    Returns:
      {ok, events: [...], cursor: <ts of newest>, total_journaled: <int>}

    Idempotent: collector calls with last cursor; advances cursor on each batch.
    """
    t0 = time.perf_counter()
    snap = list(_event_journal)  # snapshot the deque
    if since:
        events = [e for e in snap if e.get("ts", "") > since]
    else:
        events = snap
    events = events[-limit:]  # tail (newest)
    cursor = events[-1]["ts"] if events else (since or "")
    _log_request(request, "/events/since", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content={
        "ok": True,
        "events": events,
        "cursor": cursor,
        "total_journaled": len(snap),
    })


@app.get("/health")
async def health(request: Request):
    t0 = time.perf_counter()
    payload = {
        "ok": _model_loaded,
        "model_loaded": _model_loaded,
        "db_path": str(DB_PATH),
        "version": VERSION,
        "uptime_seconds": round(time.time() - _start_time, 1),
    }
    status = 200 if _model_loaded else 503
    _log_request(request, "/health", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=payload, status_code=status)


# ── /fail_mode_check ─────────────────────────────────────────────────────────
# Structured self-check across the failure classes the daemon can introspect
# from within its own process (embedding backlog, DB corruption, token-ledger
# freshness). Classes that require an external vantage point (daemon-crash,
# MCP-offline) are necessarily external — they would require this very endpoint
# to NOT be responding — so we mark them `not_applicable` rather than
# fake-checking.

def _check_embedding_backlog() -> dict:
    """Backlog of insights with NULL embedding (embedding queue lag)."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT COUNT(*) FROM insights WHERE embedding IS NULL AND status='active'"
        ).fetchone()
        backlog = row[0]
        if backlog == 0:
            status, detail = "ok", "no backlog"
        elif backlog < 5:
            status, detail = "ok", f"{backlog} pending (normal)"
        elif backlog < 25:
            status, detail = "degraded", f"{backlog} pending — embedding queue lagging"
        else:
            status, detail = "down", f"{backlog} pending — run backfill-embeddings"
        return {"class": "embedding_backlog", "severity": "medium", "status": status, "detail": detail}
    except Exception as exc:
        return {"class": "embedding_backlog", "severity": "medium", "status": "error", "detail": str(exc)[:200]}


def _check_db_integrity() -> dict:
    """PRAGMA integrity_check — full scan is slow on large DBs; use quick_check."""
    try:
        conn = get_db()
        result = conn.execute("PRAGMA quick_check(1)").fetchone()
        status = "ok" if result and result[0] == "ok" else "down"
        return {"class": "db_corruption", "severity": "critical", "status": status,
                "detail": result[0] if result else "no result"}
    except Exception as exc:
        return {"class": "db_corruption", "severity": "critical", "status": "down",
                "detail": str(exc)[:200]}


def _check_token_ledger_freshness() -> dict:
    """Last token_ledger entry should be within 7d (low-severity hint, not a true failure)."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT MAX(created_at) FROM token_ledger"
        ).fetchone()
        if row is None or row[0] is None:
            return {"class": "token_ledger", "severity": "low", "status": "ok",
                    "detail": "no entries yet (cold start)"}
        last = row[0]
        return {"class": "token_ledger", "severity": "low", "status": "ok",
                "detail": f"last entry: {last}"}
    except Exception as exc:
        return {"class": "token_ledger", "severity": "low", "status": "error",
                "detail": str(exc)[:200]}


@app.get("/fail_mode_check")
async def fail_mode_check(request: Request):
    """Structured self-check across the failure classes the daemon can introspect.

    Returns 200 if all checks pass at their severity threshold; returns 503 if any
    CRITICAL check is down. HIGH/MEDIUM/LOW downs return 200 with details so
    callers can decide.
    """
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    # Run blocking checks in the executor pool to avoid stalling the event loop.
    checks = await asyncio.gather(
        loop.run_in_executor(None, _check_embedding_backlog),
        loop.run_in_executor(None, _check_db_integrity),
        loop.run_in_executor(None, _check_token_ledger_freshness),
    )

    not_applicable = [
        {"class": "daemon_crash", "severity": "high", "status": "not_applicable",
         "detail": "self-check impossible — if the daemon is down this endpoint cannot respond"},
        {"class": "mcp_server_offline", "severity": "high", "status": "not_applicable",
         "detail": "the MCP server runs in the agent process; check via `claude mcp list`"},
    ]
    all_checks = list(checks) + not_applicable

    critical_down = any(c["status"] == "down" and c["severity"] == "critical" for c in all_checks)
    status_counts = {}
    for c in all_checks:
        status_counts[c["status"]] = status_counts.get(c["status"], 0) + 1
    summary = " / ".join(f"{n} {s}" for s, n in sorted(status_counts.items()))

    payload = {
        "ok": not critical_down,
        "checks": all_checks,
        "summary": summary,
    }
    _log_request(request, "/fail_mode_check", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=payload, status_code=503 if critical_down else 200)


@app.get("/stats")
async def stats(request: Request):
    t0 = time.perf_counter()
    embed_times = _stats["embed_times_ms"]
    recall_times = _stats["recall_times_ms"]
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    conn = get_db()
    insight_counts = {
        "active": conn.execute("SELECT COUNT(*) FROM insights WHERE status='active'").fetchone()[0],
        "total": conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0],
        "principles": conn.execute("SELECT COUNT(*) FROM principles").fetchone()[0],
    }
    conn.close()

    payload = {
        "requests_served": _stats["requests_served"],
        "embed_avg_ms": round(sum(embed_times) / len(embed_times), 2) if embed_times else 0,
        "recall_avg_ms": round(sum(recall_times) / len(recall_times), 2) if recall_times else 0,
        "last_restart": _stats["last_restart"],
        "model_name": "sentence-transformers/all-MiniLM-L6-v2",
        "dims": 384,
        "db_size_bytes": db_size,
        "insight_counts": insight_counts,
        "model_loaded": _model_loaded,
        "uptime_seconds": round(time.time() - _start_time, 1),
    }
    _log_request(request, "/stats", (time.perf_counter() - t0) * 1000)
    return payload


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request):
    t0 = time.perf_counter()
    embed_times = _stats["embed_times_ms"]
    recall_times = _stats["recall_times_ms"]

    conn = get_db()
    active_insights = conn.execute("SELECT COUNT(*) FROM insights WHERE status='active'").fetchone()[0]
    conn.close()

    lines = [
        "# HELP crag_anchor_requests_total Total requests served",
        "# TYPE crag_anchor_requests_total counter",
        f"crag_anchor_requests_total {_stats['requests_served']}",
        "# HELP crag_anchor_model_loaded 1 if embedding model is loaded",
        "# TYPE crag_anchor_model_loaded gauge",
        f"crag_anchor_model_loaded {1 if _model_loaded else 0}",
        "# HELP crag_anchor_embed_avg_ms Average embedding time (ms)",
        "# TYPE crag_anchor_embed_avg_ms gauge",
        f"crag_anchor_embed_avg_ms {round(sum(embed_times)/len(embed_times), 2) if embed_times else 0}",
        "# HELP crag_anchor_recall_avg_ms Average recall time (ms)",
        "# TYPE crag_anchor_recall_avg_ms gauge",
        f"crag_anchor_recall_avg_ms {round(sum(recall_times)/len(recall_times), 2) if recall_times else 0}",
        "# HELP crag_anchor_active_insights Active insights count",
        "# TYPE crag_anchor_active_insights gauge",
        f"crag_anchor_active_insights {active_insights}",
        "# HELP crag_anchor_uptime_seconds Daemon uptime in seconds",
        "# TYPE crag_anchor_uptime_seconds gauge",
        f"crag_anchor_uptime_seconds {round(time.time() - _start_time, 1)}",
    ]
    _log_request(request, "/metrics", (time.perf_counter() - t0) * 1000)
    return "\n".join(lines) + "\n"


@app.get("/insight/{id}")
async def get_insight(id: int, request: Request):
    t0 = time.perf_counter()
    conn = get_db()
    row = conn.execute(
        """SELECT id, project, type, content, tags, source_file, confidence,
                  verify_count, verify_streak, status, created_at, updated_at, promoted_to
           FROM insights WHERE id = ?""",
        (id,),
    ).fetchone()
    conn.close()
    _log_request(request, f"/insight/{id}", (time.perf_counter() - t0) * 1000)
    if not row:
        return JSONResponse({"ok": False, "error": f"insight {id} not found"}, status_code=404)
    return {"ok": True, "insight": dict(row)}


@app.post("/query/get_batch")
async def query_get_batch(body: GetBatchBody, request: Request):
    """Bulk-fetch insights OR principles by id via a single WHERE id IN (...) query
    (WS3a: backs the merged MCP `get` tool, replacing the per-id Python loop).
    Insight columns mirror /insight/{id}; principle columns mirror
    /query/principles/{id} plus source_insights. Returns {ok, found, not_found}."""
    t0 = time.perf_counter()
    ids = [int(i) for i in (body.ids or [])]
    kind = (body.kind or "insight").lower()
    if kind not in ("insight", "principle"):
        return JSONResponse(
            {"ok": False, "error": f"kind must be insight|principle, got {kind!r}"},
            status_code=422,
        )
    if not ids:
        return {"ok": True, "found": [], "not_found": []}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        placeholders = ",".join("?" * len(ids))
        if kind == "insight":
            rows = conn.execute(
                f"""SELECT id, project, type, content, tags, source_file, confidence,
                           verify_count, verify_streak, status, created_at, updated_at,
                           promoted_to
                    FROM insights WHERE id IN ({placeholders})""",
                ids,
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT id, project, content, tags, confidence, source_insights,
                           created_at, updated_at
                    FROM principles WHERE id IN ({placeholders})""",
                ids,
            ).fetchall()
        conn.close()
        found = [dict(r) for r in rows]
        found_ids = {r["id"] for r in found}
        not_found = [i for i in ids if i not in found_ids]
        return {"ok": True, "found": found, "not_found": not_found}

    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/query/get_batch", (time.perf_counter() - t0) * 1000)
    return result


@app.post("/recall")
async def recall(body: RecallBody, request: Request):
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, _do_recall, body.query, body.project, body.topk, body.session_id, body.snippet,
        body.role, body.epic_tag,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    _log_request(request, "/recall", elapsed_ms,
                 project=body.project, topk=body.topk)
    # Phase 4.5 — record into slow log ring buffer
    try:
        hits = len((result if isinstance(result, dict) else {}).get("insights", []))
    except Exception:
        hits = 0
    _recall_slow_log.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "query": body.query[:200],
        "project": body.project,
        "elapsed_ms": round(elapsed_ms, 1),
        "hits": hits,
        "topk": body.topk,
        "session_id": body.session_id,
    })
    # WS4 — persist recall timing so /query/slo p99 has a durable source of
    # record (the ring buffer above is in-memory and resets on restart, a
    # no-black-box violation). Best-effort, non-blocking: never fail a recall
    # because timing persistence hiccuped. Migration 024 creates the table.
    try:
        _conn = get_db()
        try:
            _conn.execute(
                "INSERT INTO recall_timings (ts, duration_ms, project, topk) "
                "VALUES (?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    round(elapsed_ms, 1),
                    body.project,
                    body.topk,
                ),
            )
            _conn.commit()
        finally:
            _conn.close()
    except Exception:
        pass
    return result


@app.get("/recall_slow_log")
async def recall_slow_log(limit: int = 20, min_ms: float = 0):
    """Phase 4.5 — return recent recalls sorted by elapsed_ms desc.

    Query params:
      limit  — how many entries (default 20, max 200)
      min_ms — only return entries above this threshold (default 0)

    Returns: {ok, count, p50_ms, p95_ms, p99_ms, entries: [...]}
    """
    limit = min(max(int(limit), 1), 200)
    entries = sorted(list(_recall_slow_log), key=lambda e: e["elapsed_ms"], reverse=True)
    if min_ms > 0:
        entries = [e for e in entries if e["elapsed_ms"] >= min_ms]
    top = entries[:limit]

    # Compute simple percentiles for context
    all_ms = sorted([e["elapsed_ms"] for e in _recall_slow_log])
    def _p(arr: list[float], pct: float) -> float:
        if not arr:
            return 0.0
        idx = max(0, min(len(arr) - 1, int(len(arr) * pct / 100)))
        return arr[idx]

    return JSONResponse(content={
        "ok": True,
        "count": len(top),
        "total_in_buffer": len(_recall_slow_log),
        "p50_ms": round(_p(all_ms, 50), 1),
        "p95_ms": round(_p(all_ms, 95), 1),
        "p99_ms": round(_p(all_ms, 99), 1),
        "entries": top,
    })


@app.post("/recall_principle")
async def recall_principle(body: RecallPrincipleBody, request: Request):
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do_recall_principle, body.topic, body.project)
    _log_request(request, "/recall_principle", (time.perf_counter() - t0) * 1000, project=body.project)
    return result


async def _save_insight_bg(row_id: int, content: str, project: Optional[str],
                           type_: str, tags: str, emb_bytes: Optional[bytes]):
    """Fire-and-forget: runs the slow Phase 7/9/10 work AFTER the client has
    already received its 200. Never raises -- all failures logged only."""
    loop = asyncio.get_event_loop()
    # Phase 7 (entity links) + Phase 9 (contradiction detection via Haiku)
    try:
        await loop.run_in_executor(None, _do_save_insight_post, row_id, content, project, emb_bytes)
    except Exception as e:
        logger.warning("save_insight bg slow-path failed for #%d: %s", row_id, e)
    # Phase 10: SSE broadcast (async-native, no executor)
    try:
        await _broadcast(
            "insight_saved",
            {
                "insight_id": row_id,
                "project": project,
                "type": type_,
                "tags": tags,
                "content_preview": (content or "")[:120],
            },
            _persist_broadcast,
        )
    except Exception as e:
        logger.warning("Phase 10 broadcast (bg) failed for #%d: %s", row_id, e)


@app.post("/save_insight")
async def save_insight(body: SaveInsightBody, request: Request):
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()

    # Legacy `insights_staged` provenance-poor tier REMOVED (2026-07-04): 233
    # lifetime staged, 4% graduation, 79 stuck pending forever including
    # high-value coordinator saves. Quality control = grounding v2 +
    # verify/decay/contradiction loops. The dedup guard (in
    # _do_save_insight_fast) stays exactly as is.
    #
    # REV 3 (2026-07-17) adds a DIFFERENT, narrower staging tier: HARD
    # write-path gate failures (schema/type/size, live-credential secret
    # scan -- see write_gate.py) route to the NEW `insights_staging` table
    # (migration 031/032, machine-readable `reason` column). This is not a
    # resurrection of the old anti-pattern -- it only catches structurally
    # invalid or dangerous writes, never provenance-poor ones (T_DIRECT).

    # Fast path: dup check + INSERT + embed -- returns in ~80-150ms
    result, emb_bytes = await loop.run_in_executor(
        None, _do_save_insight_fast,
        body.content, body.type, body.tags, body.source_file, body.project, body.force,
        body.role, body.epic_tag, body.session_id
    )
    _log_request(request, "/save_insight", (time.perf_counter() - t0) * 1000, project=body.project)
    # Schedule slow work (entity links + contradiction + broadcast) as background
    # task. The response returns IMMEDIATELY -- client doesn't wait for Haiku.
    if isinstance(result, dict) and result.get("ok") and result.get("id"):
        asyncio.create_task(
            _save_insight_bg(result["id"], body.content, body.project, body.type, body.tags, emb_bytes)
        )
        # v2.6 SSE — broadcast insight_saved immediately (don't wait for bg task)
        asyncio.create_task(_sse_publish("insight_saved", {
            "id": result["id"],
            "type": body.type,
            "project": body.project or "",
            "preview": body.content[:80] if body.content else "",
        }))
    return result


@app.post("/verify_insight")
async def verify_insight(body: VerifyInsightBody, request: Request):
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do_verify_insight, body.id, body.status)
    _log_request(request, "/verify_insight", (time.perf_counter() - t0) * 1000)
    return result


@app.post("/distill")
async def distill(body: DistillBody, request: Request):
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do_distill, body.insight_ids, body.content, body.project,
                                        body.role, body.epic_tag, body.session_id)
    _log_request(request, "/distill", (time.perf_counter() - t0) * 1000, project=body.project)
    # Phase 10: broadcast principle_distilled — fail-open
    if isinstance(result, dict) and result.get("ok"):
        try:
            await _broadcast(
                "principle_distilled",
                {
                    "principle_id": result.get("principle_id"),
                    "project": body.project,
                    "content_preview": (body.content or "")[:120],
                    "source_insight_ids": body.insight_ids,
                },
                _persist_broadcast,
            )
        except Exception as _bc_exc:
            logger.warning("Phase 10 broadcast failed (distill): %s", _bc_exc)
    return result


@app.post("/suggest_tags")
async def suggest_tags(body: SuggestTagsBody, request: Request):
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do_suggest_tags, body.content, body.project, body.limit)
    _log_request(request, "/suggest_tags", (time.perf_counter() - t0) * 1000, project=body.project)
    return result


class RecallFeedbackBody(BaseModel):
    """Phase 4.4 — operator-flagged ranking quality signal."""
    query: str
    insight_id: Optional[int] = None
    feedback: str  # wrong_rank | irrelevant | missing | stale_content
    actual_rank: Optional[int] = None
    expected_rank: Optional[int] = None
    note: Optional[str] = None
    project: Optional[str] = None
    session_id: Optional[str] = None
    role: Optional[str] = None


@app.post("/recall_feedback")
async def recall_feedback(body: RecallFeedbackBody, request: Request):
    """Phase 4.4 — persist operator feedback on a recall result.

    Used by the RecallExplorerPage flag UI when an operator marks a
    result as wrong_rank / irrelevant / stale / missing. Feeds the
    weight-tuning loop and the per-insight audit trail.

    Returns: {ok, id, query}
    """
    valid = {"wrong_rank", "irrelevant", "missing", "stale_content"}
    if body.feedback not in valid:
        return JSONResponse(
            content={"ok": False, "error": f"feedback must be one of: {sorted(valid)}"},
            status_code=400,
        )

    note = (body.note or "")[:500]

    def _do():
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO recall_feedback "
            "(query, project, insight_id, feedback, actual_rank, expected_rank, note, session_id, role) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body.query,
                body.project,
                body.insight_id,
                body.feedback,
                body.actual_rank,
                body.expected_rank,
                note,
                body.session_id,
                body.role,
            ),
        )
        conn.commit()
        return cur.lastrowid

    fid = await asyncio.get_event_loop().run_in_executor(None, _do)
    return JSONResponse(content={"ok": True, "id": fid, "query": body.query})


@app.get("/recall_feedback")
async def list_recall_feedback(request: Request, limit: int = 50):
    """Phase 4.4 — list recent recall_feedback entries (for admin view)."""
    limit = min(max(int(limit), 1), 200)

    def _do():
        conn = get_db()
        rows = conn.execute(
            "SELECT id, created_at, query, project, insight_id, feedback, "
            "       actual_rank, expected_rank, note, session_id, role "
            "FROM recall_feedback ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    rows = await asyncio.get_event_loop().run_in_executor(None, _do)
    return JSONResponse(content={"ok": True, "rows": rows, "count": len(rows)})


class RecallExploreBody(BaseModel):
    """Phase 4.1 — batch recall_explain for the Recall Explorer page.

    Returns top-k insights with per-item breakdown (cosine/FTS/confidence).
    Used by the dashboard's RecallExplorerPage for visual interpretation.
    """
    query: str
    project: Optional[str] = None
    topk: int = 20


@app.post("/recall_explore")
async def recall_explore(body: RecallExploreBody, request: Request):
    """Phase 4.1 — batch recall + per-item breakdown for RecallExplorerPage.

    Runs recall(topk=max(body.topk, 20)) and for each returned insight
    computes: cosine_raw, fts_raw, confidence, hybrid_score, formula.
    Returns a ranked list suitable for rendering bar charts in the UI.

    Unlike /recall_explain (single insight deep-dive), this endpoint is
    optimised for breadth: explain ALL top candidates in one call.
    """
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()

    # Run recall to get ranked candidates
    topk_recall = max(body.topk, 20)
    recall_dict = _do_recall(body.query, body.project, topk_recall, None, True)
    candidates = recall_dict.get("insights", [])

    # Embed query for cosine decomposition
    try:
        q_emb_bytes = await loop.run_in_executor(None, _embed_one, body.query)
        q_emb = np.frombuffer(q_emb_bytes, dtype="float32")
        embed_available = True
    except Exception:
        q_emb = None
        embed_available = False

    # Fetch embeddings + confidence for each candidate
    conn = get_db()
    ids = [c["id"] for c in candidates[:topk_recall]]
    placeholders = ",".join("?" * len(ids)) if ids else "0"
    rows = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT id, confidence, embedding FROM insights WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    }

    # FTS scores for ALL top-200 candidates (for normalization)
    fts_map: dict[int, float] = {}
    try:
        fts_rows = conn.execute(
            "SELECT i.id, bm25(insights_fts) AS rank FROM insights_fts "
            "JOIN insights i ON i.id=insights_fts.rowid "
            "WHERE insights_fts MATCH ? ORDER BY rank LIMIT 200",
            (body.query,),
        ).fetchall()
        raw_map = {r["id"]: r["rank"] for r in fts_rows}
        if raw_map:
            worst = max(raw_map.values())
            best  = min(raw_map.values())
            for iid, raw in raw_map.items():
                if raw == 0.0:
                    fts_map[iid] = 0.0
                elif worst == best:
                    fts_map[iid] = 1.0
                else:
                    fts_map[iid] = max(0.0, (worst - raw) / (worst - best + 1e-9))
    except Exception as _fts_exc:
        logger.debug("recall_explore: FTS scoring degraded to cosine-only: %s", _fts_exc)

    conn.close()

    results = []
    for rank_idx, c in enumerate(candidates[:body.topk]):
        iid = c["id"]
        db_row = rows.get(iid)
        conf = float(db_row["confidence"]) if db_row and db_row["confidence"] is not None else 0.5

        cos_raw = 0.0
        if embed_available and q_emb is not None and db_row and db_row["embedding"]:
            i_emb = np.frombuffer(db_row["embedding"], dtype=np.float32)
            if i_emb.shape[0] == q_emb.shape[0]:
                cos_raw = float(np.dot(q_emb, i_emb) / (np.linalg.norm(q_emb) * np.linalg.norm(i_emb) + 1e-9))
                cos_raw = max(0.0, min(1.0, cos_raw))

        fts_raw = fts_map.get(iid, 0.0)

        if embed_available:
            hybrid = round(scoring.HYBRID_W_COSINE * cos_raw + scoring.HYBRID_W_FTS * fts_raw
                           + scoring.HYBRID_W_CONF * conf, 4)
            formula = scoring.HYBRID_FORMULA
        else:
            hybrid = round(scoring.NOEMB_W_FTS * fts_raw + scoring.NOEMB_W_CONF * conf, 4)
            formula = scoring.NOEMB_FORMULA

        results.append({
            "rank": rank_idx + 1,
            "id": iid,
            "score": c.get("score", hybrid),
            "content_preview": (c.get("content") or "")[:200],
            "type": c.get("type"),
            "project": c.get("project"),
            "confidence": conf,
            "breakdown": {
                "cosine_raw": round(cos_raw, 4) if embed_available else None,
                "cosine_weighted": round(scoring.HYBRID_W_COSINE * cos_raw, 4) if embed_available else None,
                "fts_raw": round(fts_raw, 4),
                "fts_weighted": round((scoring.HYBRID_W_FTS if embed_available else scoring.NOEMB_W_FTS) * fts_raw, 4),
                "confidence_weighted": round((scoring.HYBRID_W_CONF if embed_available else scoring.NOEMB_W_CONF) * conf, 4),
                "hybrid_score": hybrid,
                "formula": formula,
            },
            "url": f"/o/insight/{iid}",
        })

    elapsed_ms = (time.perf_counter() - t0) * 1000
    _log_request(request, "/recall_explore", elapsed_ms, query=body.query)
    return JSONResponse(content={
        "ok": True,
        "query": body.query,
        "project": body.project,
        "topk_requested": body.topk,
        "total_candidates": len(candidates),
        "embed_available": embed_available,
        "elapsed_ms": round(elapsed_ms, 1),
        "results": results,
    })


class RecallExplainBody(BaseModel):
    query: str
    insight_id: int
    project: Optional[str] = None


@app.post("/recall_explain")
async def recall_explain(body: RecallExplainBody, request: Request):
    """Phase 18 — explain why insight_id scored the way it did for query.

    Runs a full recall with topk=20 so we know the competitive landscape,
    then surfaces the breakdown for the requested insight even if it fell
    outside the top results. Returns 'found_in_recall' flag so the agent
    knows whether the insight was surfaced or not.
    """
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()

    # Validate insight exists + fetch embedding from same row
    conn = get_db()
    row = conn.execute(
        "SELECT id, content, type, project, confidence, source_file, embedding "
        "FROM insights WHERE id=? AND superseded_by IS NULL",
        (body.insight_id,),
    ).fetchone()
    if not row:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": f"insight {body.insight_id} not found or superseded"},
            status_code=404,
        )

    insight_conf = row["confidence"]

    # Embed query (_embed_one returns bytes; convert to np.float32 array)
    try:
        q_emb_bytes = await loop.run_in_executor(None, _embed_one, body.query)
        q_emb = np.frombuffer(q_emb_bytes, dtype="float32")
        embed_available = True
    except Exception:
        q_emb = None
        embed_available = False

    # FTS score for this specific insight (normalized vs the best/worst in top-200)
    fts_raw = 0.0
    try:
        fts_rows = conn.execute(
            "SELECT i.id, bm25(insights_fts) AS rank FROM insights_fts "
            "JOIN insights i ON i.id=insights_fts.rowid "
            "WHERE insights_fts MATCH ? ORDER BY rank LIMIT 200",
            (body.query,),
        ).fetchall()
        fts_map = {r["id"]: r["rank"] for r in fts_rows}
        raw = fts_map.get(body.insight_id, 0.0)
        if raw != 0.0 and len(fts_map) > 1:
            worst = max(fts_map.values())  # bm25: less-negative = worse
            best  = min(fts_map.values())  # bm25: more-negative = better
            fts_raw = max(0.0, (worst - raw) / (worst - best + 1e-9)) if worst != best else 1.0
        elif raw != 0.0:
            fts_raw = 1.0
    except Exception as _fts_exc:
        logger.debug("recall_explain: FTS scoring degraded: %s", _fts_exc)

    # Cosine score using insight's embedding (already fetched in row above)
    cos_raw = 0.0
    if embed_available and q_emb is not None and row["embedding"]:
        i_emb = np.frombuffer(row["embedding"], dtype=np.float32)
        if i_emb.shape[0] == q_emb.shape[0]:
            cos_raw = float(np.dot(q_emb, i_emb) / (np.linalg.norm(q_emb) * np.linalg.norm(i_emb) + 1e-9))
            cos_raw = max(0.0, min(1.0, cos_raw))
    conn.close()

    # Compute hybrid score and breakdown
    if embed_available:
        hybrid = round(scoring.HYBRID_W_COSINE * cos_raw + scoring.HYBRID_W_FTS * fts_raw
                       + scoring.HYBRID_W_CONF * insight_conf, 4)
        breakdown = {
            "cosine_raw":          round(cos_raw, 4),
            "cosine_weighted":     round(scoring.HYBRID_W_COSINE * cos_raw, 4),
            "fts_raw":             round(fts_raw, 4),
            "fts_weighted":        round(scoring.HYBRID_W_FTS * fts_raw, 4),
            "confidence_raw":      round(insight_conf, 4),
            "confidence_weighted": round(scoring.HYBRID_W_CONF * insight_conf, 4),
            "hybrid_score":        hybrid,
            "formula": scoring.HYBRID_FORMULA,
        }
    else:
        hybrid = round(scoring.NOEMB_W_FTS * fts_raw + scoring.NOEMB_W_CONF * insight_conf, 4)
        breakdown = {
            "cosine_raw":          None,
            "cosine_weighted":     None,
            "fts_raw":             round(fts_raw, 4),
            "fts_weighted":        round(scoring.NOEMB_W_FTS * fts_raw, 4),
            "confidence_raw":      round(insight_conf, 4),
            "confidence_weighted": round(scoring.NOEMB_W_CONF * insight_conf, 4),
            "hybrid_score":        hybrid,
            "formula": scoring.NOEMB_FORMULA,
        }

    # Run a wider recall (topk=20) to know the competitive landscape
    recall_dict = _do_recall(body.query, body.project, 20, None, True)
    candidates = recall_dict.get("insights", [])
    found_in_recall = any(c["id"] == body.insight_id for c in candidates)
    top_score = candidates[0]["score"] if candidates else 0.0
    insight_rank = next(
        (i + 1 for i, c in enumerate(candidates) if c["id"] == body.insight_id), None
    )

    # Diagnosis
    if found_in_recall:
        diagnosis = f"Insight #{body.insight_id} ranked #{insight_rank} in top-20 recall."
    elif hybrid < 0.10:
        diagnosis = (
            f"Score {hybrid:.4f} is very low. "
            f"Cosine={cos_raw:.3f} (content phrasing mismatch), "
            f"FTS={fts_raw:.3f} (low lexical overlap). "
            "Try updating the insight with query-aligned keywords via update_insight."
        )
    elif cos_raw < 0.25 and fts_raw < 0.20:
        diagnosis = (
            f"Score {hybrid:.4f} is below the top score ({top_score:.4f}). "
            "Both semantic (cosine) and lexical (FTS) signals are weak — "
            "the insight wording doesn't align with this query. "
            "Consider update_insight to add synonyms or paraphrases."
        )
    elif cos_raw > 0.35 and fts_raw < 0.10:
        diagnosis = (
            f"Score {hybrid:.4f}. Semantic match is reasonable (cosine={cos_raw:.3f}) "
            "but FTS5 doesn't find this insight — missing key terms from the query. "
            "Adding exact query terms to the insight content would improve recall."
        )
    elif cos_raw < 0.15 and fts_raw > 0.30:
        diagnosis = (
            f"Score {hybrid:.4f}. Lexical match is good (fts={fts_raw:.3f}) "
            "but semantic (cosine={cos_raw:.3f}) is weak — embedding may be stale. "
            "Try: python engine-cli.py backfill-embeddings"
        )
    else:
        diagnosis = (
            f"Score {hybrid:.4f} vs top score {top_score:.4f}. "
            "Within normal range but displaced by higher-scoring candidates. "
            "Increase confidence via verify_insight to boost recall priority."
        )

    _log_request(request, "/recall_explain", (time.perf_counter() - t0) * 1000,
                 insight_id=body.insight_id, found=found_in_recall)
    return {
        "ok": True,
        "insight_id": body.insight_id,
        "query": body.query,
        "found_in_recall": found_in_recall,
        "insight_rank": insight_rank,
        "top_score": top_score,
        "breakdown": breakdown,
        "diagnosis": diagnosis,
        "insight": {
            "id": row["id"], "content": row["content"][:300],
            "type": row["type"], "confidence": insight_conf,
            "source_file": row["source_file"],
        },
    }


@app.get("/recall_stats")
async def recall_stats(request: Request, project: Optional[str] = None, days: int = 7):
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do_recall_stats, project, days)
    _log_request(request, "/recall_stats", (time.perf_counter() - t0) * 1000, project=project)
    return result


# ---------------------------------------------------------------------------
# Phase 7: Entity Linking endpoints
# ---------------------------------------------------------------------------

class RecallByEntityBody(BaseModel):
    entity: str
    entity_type: Optional[str] = None
    project: Optional[str] = None
    limit: int = 20

@app.post("/recall_by_entity")
async def recall_by_entity(body: RecallByEntityBody, request: Request):
    """Recall insights + principles that reference a specific entity (port, IP, service, etc.).

    Graph v2 (migration 027): canonical-first resolution. The raw query value is
    normalized, then matched against ALL entity_canonical rows sharing that
    canonical form (an entity may have several alias rows with distinct raw
    values — e.g. '/opt/app/data' and '/data' both collapse
    to the same canonical path) and entity_links is filtered by
    canonical_entity_id. This gives alias unification for free and stops
    junk entities (e.g. '/main') from serving raw-string-match hits.

    Falls back to the pre-graph-v2 raw `el.entity = ?` match when:
      - entity_type was omitted (normalize() needs a type to classify), or
      - no canonical row exists yet for this value (not backfilled), or
      - the entity_canonical table doesn't exist at all (pre-027 schema —
        the live DB may not have migration 027 applied yet; this read path
        must never 500 on that, same fail-open style as the v2 enqueue
        triggers).
    """
    t0 = time.perf_counter()
    conn = get_db()

    canonical_ids: list[int] = []
    junk_rejected = False
    if body.entity_type:
        try:
            norm = _normalize_entity(body.entity_type, body.entity)
        except Exception:
            norm = {"canonical": body.entity, "reject": False, "reason": None}
        if norm.get("reject"):
            junk_rejected = True
        else:
            try:
                ec_rows = conn.execute(
                    "SELECT id FROM entity_canonical WHERE entity_type = ? AND canonical = ?",
                    (body.entity_type, norm["canonical"]),
                ).fetchall()
                canonical_ids = [r["id"] for r in ec_rows]
            except sqlite3.OperationalError:
                canonical_ids = []  # entity_canonical absent — pre-027 schema

    if junk_rejected:
        conn.close()
        _log_request(request, "/recall_by_entity", (time.perf_counter() - t0) * 1000,
                     project=body.project)
        return {"ok": True, "entity": body.entity, "entity_type": body.entity_type,
                "count": 0, "results": [], "note": "entity rejected as junk"}

    if canonical_ids:
        placeholders = ",".join("?" * len(canonical_ids))
        where = [f"el.canonical_entity_id IN ({placeholders})"]
        params: list = list(canonical_ids)
    else:
        # Fallback: no canonical row found (not yet backfilled, table absent,
        # or entity_type omitted) — raw-string match, same as pre-graph-v2.
        where = ["el.entity = ?"]
        # normalize entity for domain and service lookups
        params = [body.entity.lower() if body.entity_type in ("domain", "service") else body.entity]
        if body.entity_type:
            where.append("el.entity_type = ?")
            params.append(body.entity_type)

    project_clause = ""
    if body.project:
        project_clause = " AND (i.project = ? OR i.project IS NULL OR p.project = ? OR p.project IS NULL)"
        params.extend([body.project, body.project])

    sql = f"""
        SELECT
            el.entity, el.entity_type, el.raw_match,
            i.id AS insight_id, i.project AS insight_project, i.type AS insight_type,
            i.content AS insight_content, i.confidence AS insight_conf,
            p.id AS principle_id, p.project AS principle_project,
            p.content AS principle_content, p.confidence AS principle_conf
        FROM entity_links el
        LEFT JOIN insights i ON el.insight_id = i.id AND i.status = 'active'
        LEFT JOIN principles p ON el.principle_id = p.id
        WHERE {' AND '.join(where)}{project_clause}
        LIMIT ?
    """
    params.append(body.limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    results = []
    for r in rows:
        if r["insight_id"]:
            results.append({
                "kind": "insight", "id": r["insight_id"], "project": r["insight_project"],
                "type": r["insight_type"], "content": r["insight_content"],
                "confidence": r["insight_conf"],
                "entity": r["entity"], "entity_type": r["entity_type"], "raw": r["raw_match"],
            })
        elif r["principle_id"]:
            results.append({
                "kind": "principle", "id": r["principle_id"], "project": r["principle_project"],
                "content": r["principle_content"], "confidence": r["principle_conf"],
                "entity": r["entity"], "entity_type": r["entity_type"], "raw": r["raw_match"],
            })

    _log_request(request, "/recall_by_entity", (time.perf_counter() - t0) * 1000,
                 project=body.project)
    return {"ok": True, "entity": body.entity, "entity_type": body.entity_type,
            "count": len(results), "results": results}


# ── Phase 13 — Memory arena (supersede edges + adjudication) ──────────────────
#
# These endpoints delegate the heavy lifting to the crag-anchor-cli command functions
# imported from the db module.  Doing it this way keeps a single source of
# truth for the adjudication logic — crag-anchor-cli's cmd_arena() and the daemon
# /arena endpoint always agree because they ARE the same code path.

def _do_arena(conn, insight_ids: list, strategy: str, project: Optional[str],
              merged_content: Optional[str] = None, dry_run: bool = False,
              allow_resupersede: bool = False, role: Optional[str] = None,
              epic_tag: Optional[str] = None, session_id: Optional[str] = None) -> dict:
    """Adjudicate one pair/group on an ALREADY-OPEN conn. Does NOT commit/close —
    the caller owns the transaction. Shared by /arena (single) and /arena_batch
    (server-side loop). Writes one arena_events row per adjudication."""
    if not insight_ids or len(insight_ids) < 2:
        return {"ok": False, "error": "insight_ids requires at least 2 ids"}
    placeholders = ",".join("?" * len(insight_ids))
    rows = [dict(r) for r in conn.execute(
        f"""SELECT id, project, type, content, confidence,
                   COALESCE(verify_count, 0) AS verify_count,
                   source_file, tags, created_at, updated_at, superseded_by
            FROM insights WHERE id IN ({placeholders})""",
        insight_ids,
    ).fetchall()]
    if len(rows) != len(insight_ids):
        found = {r["id"] for r in rows}
        missing = [i for i in insight_ids if i not in found]
        return {"ok": False, "error": f"Insights not found: {missing}"}
    already = [r["id"] for r in rows if r["superseded_by"]]
    if already and not allow_resupersede:
        return {"ok": False, "error": f"Already superseded: {already}"}
    project = project or rows[0]["project"]
    now = datetime.now(timezone.utc).isoformat()

    # MERGE strategy: create a new insight, supersede all inputs
    if strategy == "merge":
        if not merged_content:
            return {"ok": False, "error": "merge strategy requires merged_content"}
        if dry_run:
            return {"ok": True, "verdict": "MERGED", "dry_run": True,
                    "would_supersede": insight_ids}
        all_tags = set()
        for r in rows:
            if r["tags"]:
                all_tags.update(t.strip() for t in r["tags"].split(","))
        types = [r["type"] for r in rows if r["type"]]
        new_type = max(set(types), key=types.count) if types else "architecture"
        conn.execute(
            """INSERT INTO insights (project, type, content, tags, source_file,
                                      confidence, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 0.7, 'active', ?, ?)""",
            (project, new_type, merged_content,
             ",".join(sorted(all_tags)),
             rows[0]["source_file"] or "", now, now),
        )
        merged_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for i in insight_ids:
            conn.execute(
                """UPDATE insights SET superseded_by=?, superseded_at=?,
                                        supersede_reason='merged-by-arena',
                                        updated_at=? WHERE id=?""",
                (merged_id, now, now, i),
            )
        conn.execute(
            """INSERT INTO arena_events (ts, project, input_insight_ids,
                                          winner_insight_id, strategy, rationale,
                                          merged_insight_id, verdict,
                                          role, epic_tag, session_id)
               VALUES (?, ?, ?, NULL, 'merge', ?, ?, 'MERGED', ?, ?, ?)""",
            (now, project, json.dumps(insight_ids),
             f"merged {len(insight_ids)} insights into new #{merged_id}",
             merged_id,
             role, epic_tag, session_id),
        )
        return {"ok": True, "verdict": "MERGED", "merged_id": merged_id,
                "superseded": insight_ids}

    # Adjudication strategies
    def score_recency(rs): return {r["id"]: r["updated_at"] or r["created_at"] or "" for r in rs}
    def score_confidence(rs):
        return {r["id"]: (float(r["confidence"] or 0.0)
                          * math.log1p(int(r["verify_count"] or 0)))
                for r in rs}
    def score_evidence(rs):
        out = {}
        for r in rs:
            p = (r["source_file"] or "").strip()
            try:
                out[r["id"]] = 1.0 if (p and Path(p).exists()) else 0.0
            except OSError:
                out[r["id"]] = 0.0
        return out

    def winner_by(strat):
        if strat == "recency":
            s = score_recency(rows)
        elif strat == "confidence":
            s = score_confidence(rows)
        elif strat == "evidence":
            s = score_evidence(rows)
        else:
            return None, f"unknown strategy: {strat}"
        mx = max(s.values())
        top = [iid for iid, v in s.items() if v == mx]
        if len(top) != 1:
            return None, f"{strat}: tied {top}"
        return top[0], f"{strat}: winner #{top[0]} score={mx}"

    if strategy == "auto":
        per = {}
        votes = {}
        for s in ("recency", "confidence", "evidence"):
            w, r = winner_by(s)
            per[s] = {"winner": w, "rationale": r}
            if w is not None:
                votes[w] = votes.get(w, 0) + 1
        if not votes:
            winner = None
            rationale = "auto: all strategies indecisive"
        else:
            top_id, top_votes = max(votes.items(), key=lambda kv: kv[1])
            if top_votes >= 2:
                winner = top_id
                rationale = f"auto: #{top_id} won {top_votes}/3"
            else:
                winner = None
                rationale = "auto: three-way split"
    else:
        winner, rationale = winner_by(strategy)
        per = None

    if winner is None:
        if not dry_run:
            conn.execute(
                """INSERT INTO arena_events (ts, project, input_insight_ids,
                                              winner_insight_id, strategy, rationale, verdict,
                                              role, epic_tag, session_id)
                   VALUES (?, ?, ?, NULL, ?, ?, 'AMBIGUOUS', ?, ?, ?)""",
                (now, project, json.dumps(insight_ids), strategy, rationale,
                 role, epic_tag, session_id),
            )
        out = {"ok": True, "verdict": "AMBIGUOUS", "rationale": rationale}
        if per:
            out["per_strategy"] = per
        return out

    losers = [i for i in insight_ids if i != winner]
    if dry_run:
        out = {"ok": True, "verdict": "WINNER", "winner": winner, "losers": losers,
               "dry_run": True, "rationale": rationale}
        if per:
            out["per_strategy"] = per
        return out
    for loser in losers:
        conn.execute(
            """UPDATE insights SET superseded_by=?, superseded_at=?,
                                    supersede_reason=?, updated_at=?
               WHERE id=?""",
            (winner, now, f"arena:{strategy}", now, loser),
        )
    conn.execute(
        """INSERT INTO arena_events (ts, project, input_insight_ids,
                                      winner_insight_id, strategy, rationale, verdict,
                                      role, epic_tag, session_id)
           VALUES (?, ?, ?, ?, ?, ?, 'WINNER', ?, ?, ?)""",
        (now, project, json.dumps(insight_ids), winner, strategy,
         rationale, role, epic_tag, session_id),
    )
    out = {"ok": True, "verdict": "WINNER", "winner": winner, "losers": losers,
           "rationale": rationale}
    if per:
        out["per_strategy"] = per
    return out


@app.post("/arena")
async def arena(body: ArenaBody, request: Request):
    """Adjudicate between insights; mark losers superseded by the winner."""
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    def _do():
        conn = get_db()
        result = _do_arena(
            conn, body.insight_ids, body.strategy, body.project,
            merged_content=body.merged_content, dry_run=body.dry_run,
            allow_resupersede=body.allow_resupersede,
            role=body.role, epic_tag=body.epic_tag, session_id=body.session_id,
        )
        if result.get("ok") and not body.dry_run:
            conn.commit()
        conn.close()
        return result
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/arena", (time.perf_counter() - t0) * 1000, project=body.project)
    # v2.6 SSE — broadcast arena_verdict to live subscribers
    if isinstance(result, dict) and result.get("ok") and not result.get("dry_run"):
        await _sse_publish("arena_verdict", {
            "verdict": result.get("verdict"),
            "winner": result.get("winner"),
            "losers": result.get("losers", []),
            "strategy": body.strategy,
            "project": body.project or "",
        })
    return result


def _do_supersede(conn, loser_id: int, winner_id: int, reason: str,
                  provenance: str = "manual", role: Optional[str] = None,
                  epic_tag: Optional[str] = None,
                  session_id: Optional[str] = None) -> dict:
    """SINGLE WRITE PATH for a manual insight supersede (WS3a pattern, mirrors
    _do_promote_insight). Every caller — /supersede, the operator-panel
    `supersede` action, and drift_resolve(mode=supersede) — MUST route through
    here. Guarantees, uniformly: (1) loser != winner + both-exist validation;
    (2) canonical _utcnow_iso() timestamps on superseded_at AND updated_at
    (never SQLite datetime('now') — format-deviant, breaks lexical watermark
    comparisons downstream); (3) provenance-prefixed supersede_reason
    ('manual:'/'operator:'/'drift:'); (4) an arena_events audit row so the
    supersede is visible to arena history, the collector snapshot, and the
    dashboard. Caller owns commit/close.
    """
    if loser_id == winner_id:
        return {"ok": False, "error": "loser_id and winner_id must differ"}
    for iid, label in [(loser_id, "loser"), (winner_id, "winner")]:
        if conn.execute("SELECT 1 FROM insights WHERE id=?", (iid,)).fetchone() is None:
            return {"ok": False, "error": f"{label} #{iid} not found"}
    now = _utcnow_iso()
    reason = (reason or provenance)[:500]
    conn.execute(
        """UPDATE insights SET superseded_by=?, superseded_at=?,
                                supersede_reason=?, updated_at=?
           WHERE id=?""",
        (winner_id, now, f"{provenance}:{reason}", now, loser_id),
    )
    conn.execute(
        """INSERT INTO arena_events (ts, project, input_insight_ids,
                                      winner_insight_id, strategy, rationale, verdict,
                                      role, epic_tag, session_id)
           VALUES (?, NULL, ?, ?, ?, ?, 'WINNER', ?, ?, ?)""",
        (now, json.dumps([loser_id, winner_id]), winner_id, provenance,
         reason, role, epic_tag, session_id),
    )
    return {"ok": True, "superseded": loser_id, "by": winner_id, "reason": reason}


@app.post("/supersede")
async def supersede(body: SupersedeBody, request: Request):
    """Manually mark loser_id superseded by winner_id."""
    loop = asyncio.get_event_loop()
    def _do():
        conn = get_db()
        try:
            result = _do_supersede(
                conn, body.loser_id, body.winner_id, body.reason or "manual",
                provenance="manual", role=body.role, epic_tag=body.epic_tag,
                session_id=body.session_id,
            )
            if result.get("ok"):
                conn.commit()
            return result
        finally:
            conn.close()
    result = await loop.run_in_executor(None, _do)
    # v2.6 SSE — broadcast supersede event
    if isinstance(result, dict) and result.get("ok"):
        await _sse_publish("supersede_created", {
            "loser_id": body.loser_id,
            "winner_id": body.winner_id,
            "reason": body.reason or "manual",
        })
    return result


@app.get("/audit_contradictions")
async def audit_contradictions(request: Request, project: Optional[str] = None):
    """List insights with suspect_of edges that aren't yet superseded."""
    loop = asyncio.get_event_loop()
    def _do():
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
        return {"ok": True, "count": len(rows),
                "contradictions": [dict(r) for r in rows]}
    return await loop.run_in_executor(None, _do)


@app.post("/audit_drift")
async def audit_drift(body: AuditDriftBody, request: Request):
    """Find non-superseded insights whose content matches a stale pattern."""
    loop = asyncio.get_event_loop()
    def _do():
        conn = get_db()
        sql = (
            "SELECT id, project, type, confidence, "
            "       substr(content, 1, 240) AS snippet, "
            "       source_file, tags, created_at "
            "FROM insights "
            "WHERE content LIKE ? AND superseded_by IS NULL"
        )
        params = ["%" + body.pattern + "%"]
        if body.project:
            sql += " AND project = ?"
            params.append(body.project)
        sql += " ORDER BY created_at DESC LIMIT 200"
        rows = conn.execute(sql, params).fetchall()
        return {"ok": True, "pattern": body.pattern, "count": len(rows),
                "matches": [dict(r) for r in rows]}
    return await loop.run_in_executor(None, _do)


def _clear_suspect_ids(conn, ids: list[int]) -> tuple[list[int], list[int], list[int]]:
    """SINGLE WRITE PATH for clearing Phase 9 suspect flags on insight ids.

    Used by /clear_suspect, /clear_suspect_batch, and the operator-panel
    `mark_fp` action. Uniformly NULLs ALL FOUR suspect columns (including
    suspect_detected_at — the panel's old inline copy left it dangling, so a
    "cleared" row still looked freshly detected) and stamps updated_at with the
    canonical ISO timestamp. Returns (cleared, noop, not_found). Caller owns
    commit/close.
    """
    now = _utcnow_iso()
    cleared: list[int] = []
    noop: list[int] = []
    not_found: list[int] = []
    for iid in ids:
        row = conn.execute(
            "SELECT suspect_of FROM insights WHERE id = ?", (iid,)
        ).fetchone()
        if row is None:
            not_found.append(iid)
            continue
        if row["suspect_of"] is None:
            noop.append(iid)
            continue
        conn.execute(
            """UPDATE insights
               SET suspect_of = NULL, suspect_reason = NULL,
                   suspect_score = NULL, suspect_detected_at = NULL,
                   updated_at = ?
               WHERE id = ?""",
            (now, iid),
        )
        cleared.append(iid)
    return cleared, noop, not_found


@app.post("/clear_suspect")
async def clear_suspect(body: ClearSuspectBody, request: Request):
    """Clear Phase 9 contradiction-detector flag on insight(s) — false-positive triage.

    Mirrors engine-cli.py cmd_clear_suspect. The Phase 9 detector flags
    topically-adjacent insights as contradiction candidates; operator manual
    triage decides TRUE contradiction (arena) vs FALSE positive (this endpoint).
    See insight #2194 for the workflow doctrine.
    """
    targets: list[int] = []
    if body.id is not None:
        targets.append(int(body.id))
    if body.a_id is not None:
        targets.append(int(body.a_id))
    if body.b_id is not None:
        targets.append(int(body.b_id))
    if not targets:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": "Provide either {id} or {a_id, b_id}"},
        )

    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            cleared, noop, not_found = _clear_suspect_ids(conn, targets)
            conn.commit()
            return {"ok": True, "cleared": cleared, "noop": noop,
                    "not_found": not_found, "reason": body.reason}
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.post("/clear_suspect_batch")
async def clear_suspect_batch(body: ClearSuspectBatchBody, request: Request):
    """Clear Phase 9 suspect flags across many pairs/ids in ONE server-side loop
    (WS3a: replaces the MCP-side Python loop over /clear_suspect). Each entry is
    {id} OR {a_id, b_id}. Returns {cleared, noop, not_found, errors, total_processed}."""
    entries = body.pairs or []
    default_reason = body.reason or "false-positive-batch"
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        cleared: list[int] = []
        noop: list[int] = []
        not_found: list[int] = []
        errors: list[dict] = []
        try:
            for entry in entries:
                targets: list[int] = []
                if entry.get("id") is not None:
                    targets.append(int(entry["id"]))
                if entry.get("a_id") is not None:
                    targets.append(int(entry["a_id"]))
                if entry.get("b_id") is not None:
                    targets.append(int(entry["b_id"]))
                if not targets:
                    errors.append({"entry": entry, "error": "provide {id} or {a_id, b_id}"})
                    continue
                c, n, nf = _clear_suspect_ids(conn, targets)
                cleared.extend(c)
                noop.extend(n)
                not_found.extend(nf)
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "cleared": cleared, "noop": noop,
                "not_found": not_found, "errors": errors,
                "total_processed": len(entries), "reason": default_reason}

    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/clear_suspect_batch", 0.0)
    return result


@app.post("/arena_batch")
async def arena_batch(body: ArenaBatchBody, request: Request):
    """Run _do_arena over N groups in ONE server-side loop (WS3a: replaces the
    MCP-side loop over /arena). One arena_events row per adjudication, as the
    singular endpoint. Each entry in `pairs` is a list of >= 2 insight ids.
    Returns {ok, results:[...], errors:[...], total_processed, dry_run}."""
    groups = body.pairs or []
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        results: list[dict] = []
        errors: list[dict] = []
        for grp in groups:
            if not isinstance(grp, list) or len(grp) < 2:
                errors.append({"pair": grp, "error": "each pair needs >= 2 ids"})
                continue
            try:
                r = _do_arena(
                    conn, grp, body.strategy, body.project,
                    merged_content=body.merged_content, dry_run=body.dry_run,
                    role=body.role, epic_tag=body.epic_tag, session_id=body.session_id,
                )
                results.append({"pair": grp, **r})
            except Exception as exc:
                errors.append({"pair": grp, "error": f"{type(exc).__name__}: {exc}"})
        if not body.dry_run:
            conn.commit()
        conn.close()
        return {"ok": True, "results": results, "errors": errors,
                "total_processed": len(groups), "dry_run": body.dry_run}

    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/arena_batch", 0.0, project=body.project)
    return result


# ===========================================================================
# Phase 25 — Grounded Memory endpoints.
# Division of labour: the DAEMON does DB ops only (no network/disk probes); the
# GROUNDSKEEPER CRON runs read-only probes in its own process and POSTs results
# to /ground/record; the AGENT resolves the review queue via /ground/audit +
# /ground/check + /ground/clear (+ existing verify_insight/supersede). The cron
# never mutates confidence — detection != resolution (#2194).
# ===========================================================================

def _ground_tbl(claim_kind: str) -> Optional[str]:
    return {"insight": "insights", "principle": "principles"}.get(claim_kind)


def _ground_enqueue_row(conn, claim_kind, claim_id, reason, trigger_src, detail, now):
    """Upsert an OPEN grounding_queue row (dedup) + set the claim's grounding_due flag."""
    tbl = _ground_tbl(claim_kind)
    if not tbl:
        return False
    existing = conn.execute(
        "SELECT id FROM grounding_queue WHERE claim_kind=? AND claim_id=? AND status='open'",
        (claim_kind, claim_id),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE grounding_queue SET reason=?, trigger_src=?, detail=?, enqueued_at=? WHERE id=?",
            (reason, trigger_src, detail, now, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO grounding_queue (claim_kind, claim_id, reason, trigger_src, detail, status, enqueued_at) "
            "VALUES (?,?,?,?,?, 'open', ?)",
            (claim_kind, claim_id, reason, trigger_src, detail, now),
        )
    conn.execute(f"UPDATE {tbl} SET grounding_due = 1 WHERE id = ?", (claim_id,))
    return True


def _ground_upsert_falsifier(conn, claim_kind, claim_id, fkind, spec, entity, entity_type,
                             result, detail, now, derived=1):
    """Upsert the (one) falsifier row for a claim with the latest run result."""
    existing = conn.execute(
        "SELECT id FROM falsifiers WHERE claim_kind=? AND claim_id=?",
        (claim_kind, claim_id),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE falsifiers SET kind=?, spec=?, entity=?, entity_type=?, derived=?, "
            "last_run_at=?, last_result=?, last_detail=?, updated_at=? WHERE id=?",
            (fkind, spec, entity, entity_type, derived, now, result, detail, now, existing["id"]),
        )
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO falsifiers (claim_kind, claim_id, kind, spec, entity, entity_type, derived, "
        "last_run_at, last_result, last_detail, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (claim_kind, claim_id, fkind, spec, entity, entity_type, derived, now, result, detail, now, now),
    )
    return cur.lastrowid


def _ground_audit_inline(conn, action, claim_kind, claim_id, payload, result, note):
    """Best-effort operator_audit_log write (Phase 25 actions)."""
    try:
        conn.execute(
            "INSERT INTO operator_audit_log (created_at, actor, action, target_class, target_id, payload, result, note) "
            "VALUES (?, 'groundskeeper', ?, ?, ?, ?, ?, ?)",
            (_utcnow_iso(), action, claim_kind, str(claim_id), json.dumps(payload), json.dumps(result), note),
        )
    except Exception:
        pass


@app.post("/ground/enqueue")
async def ground_enqueue(body: GroundEnqueueBody):
    """Flag a claim for re-grounding (used by every trigger class). Idempotent."""
    if not _ground_tbl(body.claim_kind):
        return JSONResponse(status_code=422, content={"ok": False, "error": "claim_kind must be insight|principle"})
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        if not _table_exists(conn, "grounding_queue"):
            return {"ok": False, "error": "grounding not migrated (run crag-anchor-cli migrate)"}
        now = datetime.now(timezone.utc).isoformat()
        ok = _ground_enqueue_row(conn, body.claim_kind, body.claim_id, body.reason,
                                 body.trigger_src, body.detail, now)
        conn.commit()
        return {"ok": ok, "claim_kind": body.claim_kind, "claim_id": body.claim_id, "enqueued": ok}

    return await loop.run_in_executor(None, _do)


@app.post("/ground/enqueue_by_source")
async def ground_enqueue_by_source(body: GroundEnqueueBySourceBody):
    """In-repo git trigger: map changed file paths -> affected claims, flag them.
    Fail-soft from the caller's side; bounded by `cap` to avoid broad-path blast."""
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        if not _table_exists(conn, "grounding_queue"):
            return {"ok": False, "error": "grounding not migrated (run crag-anchor-cli migrate)"}
        now = datetime.now(timezone.utc).isoformat()
        ins: set[int] = set()
        prin: set[int] = set()
        for raw in (body.paths or []):
            p = (raw or "").replace("\\", "/").strip()
            if not p:
                continue
            suffix = "%" + p              # repo-relative suffix match (precise for git paths)
            base = "%" + p.rsplit("/", 1)[-1]
            # source_file on insights (suffix match)
            for (iid,) in conn.execute(
                "SELECT id FROM insights WHERE status='active' AND source_file IS NOT NULL "
                "AND source_file LIKE ?", (suffix,)).fetchall():
                ins.add(iid)
            # file/path entity_links (suffix or basename)
            for r in conn.execute(
                "SELECT insight_id, principle_id FROM entity_links "
                "WHERE entity_type IN ('file','path') AND (entity LIKE ? OR entity LIKE ?)",
                (suffix, base)).fetchall():
                if r["insight_id"]:
                    ins.add(r["insight_id"])
                if r["principle_id"]:
                    prin.add(r["principle_id"])
        cap = max(1, int(body.cap or 100))
        enq = 0
        for cid in list(ins)[:cap]:
            if _ground_enqueue_row(conn, "insight", cid, body.reason, body.trigger_src, body.detail, now):
                enq += 1
        for cid in list(prin)[:cap]:
            if _ground_enqueue_row(conn, "principle", cid, body.reason, body.trigger_src, body.detail, now):
                enq += 1
        conn.commit()
        return {"ok": True, "matched_insights": len(ins), "matched_principles": len(prin), "enqueued": enq}

    return await loop.run_in_executor(None, _do)


@app.get("/ground/candidates")
async def ground_candidates(limit: int = 50, project: Optional[str] = None,
                            topology_stale_days: int = 30,
                            cold_only: bool = False):
    """The cron fetches its work list: claims flagged grounding_due=1 PLUS a sweep
    of topology claims that are unverified or stale. Returns each with a derived
    falsifier (the cron runs the probe). No-VCS repos are covered by this sweep.

    cold_only=True (Grounding v2): exclude claims that already have a pending or
    running job in grounding_jobs — the recall-trigger owns those (hot claims). The
    groundskeeper becomes the COLD-CLAIM BACKSTOP: it only processes claims that
    the recall-path has not already dispatched. Each candidate also carries a 'tier'
    field ('A'|'B') derived from the stored falsifier, so the groundskeeper can route
    Tier-A for local mechanical probing and Tier-B to the agentic queue.
    """
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        if not _table_exists(conn, "grounding_queue"):
            return {"ok": False, "error": "grounding not migrated"}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=topology_stale_days)).isoformat()
        proj_i = " AND (project = ? OR project IS NULL)" if project else ""

        # cold_only filter: exclude (claim_kind, claim_id) pairs that already have
        # a pending or running job in grounding_jobs. Implemented as a NOT EXISTS
        # subquery so we never need to load the full jobs table client-side.
        cold_i = (
            " AND NOT EXISTS (SELECT 1 FROM grounding_jobs gj"
            " WHERE gj.claim_kind='insight' AND gj.claim_id=insights.id"
            " AND gj.status IN ('pending','running'))"
            if cold_only and _GROUNDING_V2 and _table_exists(conn, "grounding_jobs")
            else ""
        )
        cold_p = (
            " AND NOT EXISTS (SELECT 1 FROM grounding_jobs gj"
            " WHERE gj.claim_kind='principle' AND gj.claim_id=principles.id"
            " AND gj.status IN ('pending','running'))"
            if cold_only and _GROUNDING_V2 and _table_exists(conn, "grounding_jobs")
            else ""
        )

        out = []
        sql_i = (
            "SELECT id, project, content, source_file, volatility_class, grounded_at, grounding_due "
            "FROM insights WHERE superseded_by IS NULL AND (status IS NULL OR status='active') "
            "AND (grounding_due = 1 OR (volatility_class='topology' AND (grounded_at IS NULL OR grounded_at < ?)))"
            + proj_i + cold_i +
            " ORDER BY grounding_due DESC, COALESCE(last_recalled_at,'') DESC LIMIT ?"
        )
        pi = [cutoff] + ([project] if project else []) + [limit]
        for r in conn.execute(sql_i, pi).fetchall():
            out.append(("insight", r))
        proj_p = " AND (project = ? OR project IS NULL)" if project else ""
        sql_p = (
            "SELECT id, project, content, NULL AS source_file, volatility_class, grounded_at, grounding_due "
            "FROM principles WHERE superseded_by IS NULL "
            "AND (grounding_due = 1 OR (volatility_class='topology' AND (grounded_at IS NULL OR grounded_at < ?)))"
            + proj_p + cold_p +
            " ORDER BY grounding_due DESC, confidence DESC LIMIT ?"
        )
        pp = [cutoff] + ([project] if project else []) + [limit]
        for r in conn.execute(sql_p, pp).fetchall():
            out.append(("principle", r))

        cands = []
        for kind, r in out[:limit]:
            fr = conn.execute(
                "SELECT kind, spec, entity, entity_type, tier FROM falsifiers WHERE claim_kind=? AND claim_id=?",
                (kind, r["id"]),
            ).fetchone()
            if fr:
                fal = {"kind": fr["kind"], "spec": fr["spec"], "entity": fr["entity"],
                       "entity_type": fr["entity_type"]}
                tier = fr["tier"] or "A"  # pre-v26 rows have tier=NULL, treat as Tier-A
            else:
                fal = derive_falsifier(r["content"] or "")
                tier = "A"  # un-authored claims are Tier-A until worker runs
            cands.append({
                "claim_kind": kind, "claim_id": r["id"], "project": r["project"],
                "volatility_class": r["volatility_class"], "grounded_at": r["grounded_at"],
                "grounding_due": bool(r["grounding_due"]), "source_file": r["source_file"],
                "falsifier": fal,
                "tier": tier,
            })
        return {"ok": True, "count": len(cands), "candidates": cands,
                "cold_only": cold_only}

    return await loop.run_in_executor(None, _do)


class GroundJobsEnqueueBody(BaseModel):
    """Enqueue a Tier-B claim into grounding_jobs (v2 worker pool). Used by the
    groundskeeper as a dispatcher when it encounters a Tier-B candidate — it does
    NOT probe locally; the daemon's worker pool + adjudicator owns Tier-B verdicts."""
    claim_kind: str = "insight"   # insight | principle
    claim_id: int
    job_type: str = "reground"    # reground | author
    priority: int = 3             # groundskeeper backstop is lower priority than recall-trigger (5)


@app.post("/ground/jobs/enqueue")
async def ground_jobs_enqueue(body: GroundJobsEnqueueBody):
    """Groundskeeper dispatch: enqueue a Tier-B claim into the v2 worker pool.
    Idempotent (INSERT OR IGNORE — dedup partial index prevents double-enqueue).
    Returns {ok, inserted, claim_kind, claim_id, job_type}."""
    if not _ground_tbl(body.claim_kind):
        return JSONResponse(status_code=422, content={"ok": False, "error": "claim_kind must be insight|principle"})
    if body.job_type not in ("reground", "author"):
        return JSONResponse(status_code=422, content={"ok": False, "error": "job_type must be reground|author"})
    if not _GROUNDING_V2:
        return {"ok": False, "error": "grounding_v2 not enabled"}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        if not _table_exists(conn, "grounding_jobs"):
            return {"ok": False, "error": "grounding_jobs table missing (run crag-anchor-cli migrate)"}
        inserted = _gv2_enqueue_job(conn, body.claim_kind, body.claim_id,
                                    body.job_type, priority=body.priority)
        conn.commit()
        return {"ok": True, "inserted": inserted,
                "claim_kind": body.claim_kind, "claim_id": body.claim_id,
                "job_type": body.job_type}

    return await loop.run_in_executor(None, _do)


@app.post("/ground/record")
async def ground_record(body: GroundRecordBody):
    """The cron posts a falsifier RESULT. pass → re-ground (grounded_at=now, clear
    flag, resolve queue). fail → flag (grounding_due=1 + open queue row). error/skip
    → record only. NEVER mutates confidence (the agent decides demotion)."""
    if not _ground_tbl(body.claim_kind):
        return JSONResponse(status_code=422, content={"ok": False, "error": "claim_kind must be insight|principle"})
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        if not _table_exists(conn, "falsifiers"):
            return {"ok": False, "error": "grounding not migrated"}
        tbl = _ground_tbl(body.claim_kind)
        now = datetime.now(timezone.utc).isoformat()
        _ground_upsert_falsifier(conn, body.claim_kind, body.claim_id, body.kind, body.spec,
                                 body.entity, body.entity_type, body.result, body.detail, now)
        if body.result == "pass":
            ga = body.grounded_against or f"probe:{body.kind}@{now[:19]}"
            conn.execute(f"UPDATE {tbl} SET grounded_at=?, grounded_against=?, grounding_due=0 WHERE id=?",
                         (now, ga, body.claim_id))
            conn.execute(
                "UPDATE grounding_queue SET status='resolved', resolved_at=?, resolved_by='cron', "
                "resolution='falsifier_pass' WHERE claim_kind=? AND claim_id=? AND status='open'",
                (now, body.claim_kind, body.claim_id),
            )
        elif body.result == "fail":
            _ground_enqueue_row(conn, body.claim_kind, body.claim_id, "falsifier_fail",
                                body.trigger_src or "cron", (body.detail or "")[:500], now)
        conn.commit()
        _ground_audit_inline(conn, "ground_record", body.claim_kind, body.claim_id,
                             {"kind": body.kind, "result": body.result},
                             {"result": body.result}, (body.detail or "")[:200])
        conn.commit()
        # Grounding v2 (A3): if verdict/reasoning/evidence provided, append history row.
        if _GROUNDING_V2 and body.verdict and _table_exists(conn, "grounding_history"):
            try:
                from grounding_queue_v2 import append_history as _append_history
                _append_history(
                    conn, body.claim_kind, body.claim_id,
                    "reground", body.verdict, body.reasoning,
                    body.evidence, body.recipe_version,
                )
                conn.commit()
            except Exception as _he:
                logger.debug("ground_record: grounding_history append failed: %s", _he)
        return {"ok": True, "claim_kind": body.claim_kind, "claim_id": body.claim_id, "result": body.result}

    return await loop.run_in_executor(None, _do)


@app.get("/ground/audit")
async def ground_audit(project: Optional[str] = None, limit: int = 50):
    """The agent's review queue (mirrors audit_contradictions). Open grounding_queue
    rows joined to claim snippets. Resolve with verify_insight/supersede + /ground/clear."""
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        if not _table_exists(conn, "grounding_queue"):
            return {"ok": True, "count": 0, "queue": []}
        rows = conn.execute(
            "SELECT id, claim_kind, claim_id, reason, trigger_src, detail, enqueued_at "
            "FROM grounding_queue WHERE status='open' ORDER BY enqueued_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        queue = []
        for q in rows:
            tbl = _ground_tbl(q["claim_kind"])
            claim = conn.execute(
                f"SELECT id, project, content, confidence, volatility_class, grounded_at FROM {tbl} WHERE id=?",
                (q["claim_id"],),
            ).fetchone() if tbl else None
            if claim is None:
                continue
            if project and claim["project"] is not None and claim["project"] != project:
                continue
            fr = conn.execute(
                "SELECT kind, spec, last_result, last_detail FROM falsifiers WHERE claim_kind=? AND claim_id=?",
                (q["claim_kind"], q["claim_id"]),
            ).fetchone()
            queue.append({
                "queue_id": q["id"], "claim_kind": q["claim_kind"], "claim_id": q["claim_id"],
                "reason": q["reason"], "trigger_src": q["trigger_src"], "enqueued_at": q["enqueued_at"],
                "project": claim["project"], "confidence": claim["confidence"],
                "volatility_class": claim["volatility_class"], "grounded_at": claim["grounded_at"],
                "snippet": (claim["content"] or "")[:240],
                "falsifier": ({"kind": fr["kind"], "spec": fr["spec"],
                               "last_result": fr["last_result"],
                               # truncate: probe output can be arbitrarily long and this
                               # queue is loaded into agent context (WS5 residual fix)
                               "last_detail": (fr["last_detail"] or "")[:160]} if fr
                              else derive_falsifier(claim["content"] or "")),
            })
        return {"ok": True, "count": len(queue), "queue": queue}

    return await loop.run_in_executor(None, _do)


@app.get("/ground/check")
async def ground_check(claim_kind: str = "insight", claim_id: int = 0):
    """Return a claim + its derived falsifier + liveness so the agent can RUN the
    falsifier (via Bash) and resolve. The daemon never runs the probe itself."""
    tbl = _ground_tbl(claim_kind)
    if not tbl:
        return JSONResponse(status_code=422, content={"ok": False, "error": "claim_kind must be insight|principle"})
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        grounding_on = _table_exists(conn, "falsifiers")
        extra = ", volatility_class, grounded_at, grounded_against, grounding_due" if grounding_on else ""
        row = conn.execute(
            f"SELECT id, project, content, source_file, confidence{extra} FROM {tbl} WHERE id=?",
            (claim_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": f"{claim_kind} {claim_id} not found"}
        fr = conn.execute(
            "SELECT kind, spec, entity, entity_type, last_result, last_detail, last_run_at "
            "FROM falsifiers WHERE claim_kind=? AND claim_id=?",
            (claim_kind, claim_id),
        ).fetchone() if grounding_on else None
        fal = dict(fr) if fr else derive_falsifier(row["content"] or "")
        live = None
        if grounding_on:
            live = _liveness_stamp(row["grounded_at"], row["volatility_class"], row["grounding_due"],
                                   fr["last_result"] if fr else None, fr["kind"] if fr else None)
        return {
            "ok": True, "claim_kind": claim_kind, "claim_id": claim_id,
            "project": row["project"], "content": row["content"],
            "source_file": row["source_file"], "confidence": row["confidence"],
            "volatility_class": (row["volatility_class"] if grounding_on else classify_volatility(row["content"] or "")),
            "falsifier": fal, "liveness": live,
            "hint": "Run the falsifier (read-only) via Bash; then verify_insight(stale) / supersede / "
                    "update_principle to fix, and clear_grounding to close the queue row.",
        }

    return await loop.run_in_executor(None, _do)


@app.post("/ground/clear")
async def ground_clear(body: GroundClearBody):
    """Agent closes a grounding_queue row. 'verified' re-stamps grounded_at;
    'dismissed' = false-positive falsifier; 'noted' = handled via supersede/verify
    elsewhere. NEVER changes confidence here (#2194 detection != resolution)."""
    tbl = _ground_tbl(body.claim_kind)
    if not tbl:
        return JSONResponse(status_code=422, content={"ok": False, "error": "claim_kind must be insight|principle"})
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        if not _table_exists(conn, "grounding_queue"):
            return {"ok": False, "error": "grounding not migrated"}
        now = datetime.now(timezone.utc).isoformat()
        status = "dismissed" if body.resolution == "dismissed" else "resolved"
        n = conn.execute(
            "UPDATE grounding_queue SET status=?, resolved_at=?, resolved_by='agent', resolution=? "
            "WHERE claim_kind=? AND claim_id=? AND status='open'",
            (status, now, body.resolution, body.claim_kind, body.claim_id),
        ).rowcount
        if body.resolution == "verified":
            ga = body.grounded_against or f"agent-verified@{now[:19]}"
            conn.execute(f"UPDATE {tbl} SET grounded_at=?, grounded_against=?, grounding_due=0 WHERE id=?",
                         (now, ga, body.claim_id))
        elif body.resolution == "dismissed":
            conn.execute(f"UPDATE {tbl} SET grounding_due=0 WHERE id=?", (body.claim_id,))
        conn.commit()
        _ground_audit_inline(conn, "ground_clear", body.claim_kind, body.claim_id,
                             {"resolution": body.resolution}, {"closed": n}, body.reason)
        conn.commit()
        return {"ok": True, "claim_kind": body.claim_kind, "claim_id": body.claim_id,
                "resolution": body.resolution, "closed": n}

    return await loop.run_in_executor(None, _do)


@app.post("/admin/reconcile_grounding")
async def reconcile_grounding(project: Optional[str] = None):
    """WS2 T3c — drain the flag/queue divergence (949 flags vs 9 queue rows).

    Idempotent. For every claim with grounding_due=1:
      1) if its falsifier FAILS the resolvability predicate (or is absent) →
         clear grounding_due (return it to an honest 'unverified'), and resolve
         any dangling open queue row for it.
      2) else (resolvable) → ensure exactly one OPEN grounding_queue row exists.
      3) already-consistent (resolvable + queued) → counted, untouched.

    Returns {ok, cleared, enqueued, already_consistent}. Every mutation audited.
    """
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "grounding_queue"):
                return {"ok": False, "error": "grounding not migrated"}
            now = datetime.now(timezone.utc).isoformat()
            cleared = 0
            enqueued = 0
            already = 0

            for kind, tbl in (("insight", "insights"), ("principle", "principles")):
                proj = " AND (project = ? OR project IS NULL)" if project else ""
                params = ([project] if project else [])
                rows = conn.execute(
                    f"SELECT id, content FROM {tbl} WHERE grounding_due = 1"
                    f" AND superseded_by IS NULL{proj}",
                    params,
                ).fetchall()
                for r in rows:
                    cid = r["id"]
                    # RE-DERIVE from current rules (WS5 quality fix): the stored
                    # falsifier may predate the entity quality gates (k8s vocab
                    # domains, Java packages, negated/hallucinated endpoints).
                    # If the re-derivation differs, update the stored row so the
                    # groundskeeper probes the corrected spec.
                    d = derive_falsifier(r["content"] or "")
                    fkind, fspec, fet = d.get("kind"), d.get("spec"), d.get("entity_type")
                    fr = conn.execute(
                        "SELECT id, kind, spec, entity_type FROM falsifiers WHERE claim_kind=? AND claim_id=?",
                        (kind, cid),
                    ).fetchone()
                    if fr and (fr["kind"], fr["spec"]) != (fkind, fspec):
                        if fkind == "none":
                            conn.execute(
                                "DELETE FROM falsifiers WHERE id=?", (fr["id"],))
                        else:
                            conn.execute(
                                "UPDATE falsifiers SET kind=?, spec=?, entity=?, entity_type=?, "
                                "last_run_at=NULL, last_result=NULL, last_detail=NULL WHERE id=?",
                                (fkind, fspec, d.get("entity"), fet, fr["id"]))

                    if lifecycle.falsifier_resolvable(fkind, fspec, fet):
                        # Ensure an open queue row exists; count consistency.
                        open_row = conn.execute(
                            "SELECT id FROM grounding_queue WHERE claim_kind=? AND claim_id=? AND status='open'",
                            (kind, cid),
                        ).fetchone()
                        if open_row:
                            already += 1
                        else:
                            _ground_enqueue_row(conn, kind, cid, "reconcile_enqueue",
                                                "reconcile", "flagged but unqueued", now)
                            enqueued += 1
                    else:
                        # Not locally resolvable → the flag can never clear via cron.
                        # Return it to honest 'unverified' and close any dangling row.
                        conn.execute(f"UPDATE {tbl} SET grounding_due = 0 WHERE id = ?", (cid,))
                        conn.execute(
                            "UPDATE grounding_queue SET status='resolved', resolved_at=?, "
                            "resolved_by='reconcile', resolution='unresolvable_falsifier' "
                            "WHERE claim_kind=? AND claim_id=? AND status='open'",
                            (now, kind, cid),
                        )
                        cleared += 1

            try:
                conn.execute(
                    "INSERT INTO operator_audit_log (created_at, actor, action, target_class, target_id, payload, result, note) "
                    "VALUES (?, 'reconcile', 'reconcile_grounding', 'grounding_queue', NULL, ?, 'ok', ?)",
                    (_utcnow_iso(), json.dumps({"cleared": cleared, "enqueued": enqueued,
                                 "already_consistent": already, "project": project}),
                     f"reconcile: cleared={cleared} enqueued={enqueued} consistent={already}"),
                )
            except Exception as _aexc:
                logger.debug("reconcile audit-log write skipped: %s", _aexc)
            conn.commit()
            return {"ok": True, "cleared": cleared, "enqueued": enqueued,
                    "already_consistent": already}
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


# ---------------------------------------------------------------------------
# Grounding v2 (A3+C1) — Observability endpoints
# ---------------------------------------------------------------------------

@app.get("/ground/jobs")
async def ground_jobs(
    status: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
):
    """Return grounding_jobs queue state, optionally filtered by status/project.

    Supports: status=pending|running|done|failed, project=infra (filters via
    insights.project for insight jobs). limit max 200.
    """
    if not _GROUNDING_V2:
        return {"ok": False, "error": "grounding v2 not available"}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "grounding_jobs"):
                return {"ok": False, "error": "grounding_jobs table not migrated"}
            limit_capped = min(int(limit), 200)
            wheres = []
            params: list = []
            if status:
                wheres.append("j.status=?")
                params.append(status)
            project_join = ""
            if project:
                project_join = (
                    "LEFT JOIN insights ON (j.claim_kind='insight' AND j.claim_id=insights.id) "
                    "LEFT JOIN principles ON (j.claim_kind='principle' AND j.claim_id=principles.id) "
                )
                wheres.append("(insights.project=? OR principles.project=?)")
                params.extend([project, project])
            where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
            rows = conn.execute(
                f"""
                SELECT j.id, j.claim_kind, j.claim_id, j.job_type, j.status,
                       j.attempts, j.priority, j.enqueued_at, j.started_at,
                       j.finished_at, j.last_error
                FROM grounding_jobs j {project_join}
                {where_clause}
                ORDER BY j.priority DESC, j.enqueued_at ASC
                LIMIT ?
                """,
                params + [limit_capped],
            ).fetchall()
            return {
                "ok": True,
                "count": len(rows),
                "jobs": [dict(r) for r in rows],
            }
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.get("/ground/history/{claim_kind}/{claim_id}")
async def ground_history(claim_kind: str, claim_id: int, limit: int = 20):
    """Return the grounding reasoning trail for a specific claim, newest first."""
    if not _GROUNDING_V2:
        return {"ok": False, "error": "grounding v2 not available"}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "grounding_history"):
                return {"ok": False, "error": "grounding_history table not migrated"}
            limit_capped = min(int(limit), 100)
            rows = conn.execute(
                """
                SELECT id, claim_kind, claim_id, ts, job_type, verdict,
                       reasoning, evidence, recipe_version
                FROM grounding_history
                WHERE claim_kind=? AND claim_id=?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (claim_kind, claim_id, limit_capped),
            ).fetchall()
            return {
                "ok": True,
                "claim_kind": claim_kind,
                "claim_id": claim_id,
                "count": len(rows),
                "history": [dict(r) for r in rows],
            }
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.get("/ground/stats")
async def ground_stats():
    """Grounding pipeline stats: queue depth by status, verdict split, throughput."""
    if not _GROUNDING_V2:
        return {"ok": False, "error": "grounding v2 not available"}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "grounding_jobs"):
                return {"ok": False, "error": "grounding_jobs table not migrated"}
            # Queue depth by status
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM grounding_jobs GROUP BY status"
            ).fetchall()
            by_status = {r["status"]: r["cnt"] for r in status_rows}
            # Honesty split (derived at READ time — no schema change): the
            # worker records LLM-declined claims as status='failed' with
            # last_error 'mechanically_unverifiable:...'. Those are safely
            # unverifiable, NOT pipeline failures, so split the failed bucket
            # into 'declined' vs true 'failed' for the operator surface.
            declined_cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM grounding_jobs "
                "WHERE status='failed' AND last_error LIKE 'mechanically_unverifiable%'"
            ).fetchone()
            declined = declined_cnt["cnt"] if declined_cnt else 0
            failed_total = by_status.get("failed", 0)
            failed_split = {
                "declined": declined,
                "failed": max(0, failed_total - declined),
            }
            # Oldest pending age (seconds)
            oldest = conn.execute(
                "SELECT enqueued_at FROM grounding_jobs WHERE status='pending' "
                "ORDER BY enqueued_at ASC LIMIT 1"
            ).fetchone()
            oldest_pending_age_sec = None
            if oldest and oldest["enqueued_at"]:
                try:
                    ea = datetime.fromisoformat(oldest["enqueued_at"])
                    oldest_pending_age_sec = round(
                        (datetime.now(timezone.utc) - ea).total_seconds()
                    )
                except Exception:
                    pass
            # Jobs done in last 24h
            try:
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            except Exception:
                cutoff = ""
            done_24h = conn.execute(
                "SELECT COUNT(*) AS cnt FROM grounding_jobs "
                "WHERE status='done' AND finished_at >= ?",
                (cutoff,),
            ).fetchone()
            jobs_24h = done_24h["cnt"] if done_24h else 0
            # Verdict distribution from history (last 24h)
            verdict_dist: dict = {}
            if _table_exists(conn, "grounding_history"):
                verdict_rows = conn.execute(
                    "SELECT verdict, COUNT(*) AS cnt FROM grounding_history "
                    "WHERE ts >= ? GROUP BY verdict",
                    (cutoff,),
                ).fetchall()
                verdict_dist = {r["verdict"]: r["cnt"] for r in verdict_rows}
            # Flagged claim counts
            flagged_i = conn.execute(
                "SELECT COUNT(*) AS cnt FROM insights WHERE grounding_due=1"
            ).fetchone()
            flagged_p = conn.execute(
                "SELECT COUNT(*) AS cnt FROM principles WHERE grounding_due=1"
            ).fetchone()

            # ---- Grounding v3 claim-layer stats (additive; fail-soft) ----
            v3: dict = {}
            if _table_exists(conn, "claims"):
                try:
                    v3 = _ground_stats_v3(conn, cutoff)
                except Exception as _v3e:
                    v3 = {"error": str(_v3e)}

            return {
                "ok": True,
                "queue_by_status": by_status,
                # queue_by_status.failed is left intact for schema honesty; this
                # derived split lets the operator surface show "Declined (safely
                # unverifiable)" separately from true failures.
                "queue_failed_split": failed_split,
                "oldest_pending_age_sec": oldest_pending_age_sec,
                "jobs_done_last_24h": jobs_24h,
                "verdict_dist_last_24h": verdict_dist,
                "flagged_claims": {
                    "insights": flagged_i["cnt"] if flagged_i else 0,
                    "principles": flagged_p["cnt"] if flagged_p else 0,
                },
                "worker_concurrency": _GROUNDING_WORKER_CONCURRENCY,
                "grounding_v2_enabled": _GROUNDING_V2,
                # v3 fields (empty {} on an un-backfilled DB). Old fields above
                # are kept one release for existing dashboard consumers.
                "coverage_by_class": v3.get("coverage_by_class", {}),
                "pass_rate_by_class": v3.get("pass_rate_by_class", {}),
                "p4_decisiveness": v3.get("p4_decisiveness"),
                "review_due_count": v3.get("review_due_count", 0),
                "dedup_ratio": v3.get("dedup_ratio"),
                "blast_radius_top": v3.get("blast_radius_top", []),
                "claim_layer_enabled": _CLAIM_LAYER,
                "write_gate_enabled": _WRITE_GATE,
            }
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


def _ground_stats_v3(conn, cutoff: str) -> dict:
    """Grounding v3 claim-layer stat block. Pure reads on the claims tables.
    coverage_by_class must be 100% by construction (every claim is classified);
    a non-100 total signals a classification bug."""
    total = conn.execute("SELECT COUNT(*) c FROM claims WHERE status='active'").fetchone()["c"]
    by_class = {r["predicate_class"]: r["c"] for r in conn.execute(
        "SELECT predicate_class, COUNT(*) c FROM claims WHERE status='active' GROUP BY predicate_class"
    ).fetchall()}
    classified = sum(v for k, v in by_class.items() if k)
    coverage_by_class = dict(by_class)
    coverage_by_class["_total"] = total
    coverage_by_class["_classified_pct"] = round(100.0 * classified / total, 1) if total else 100.0

    # pass rate per class over recent history.
    pass_rate: dict = {}
    if _table_exists(conn, "grounding_history"):
        rows = conn.execute(
            "SELECT c.predicate_class pc, gh.verdict v, COUNT(*) n "
            "FROM grounding_history gh JOIN claims c ON c.id=gh.claim_id "
            "WHERE gh.claim_kind='claim' AND gh.ts >= ? GROUP BY pc, v",
            (cutoff,),
        ).fetchall()
        agg: dict = {}
        for r in rows:
            d = agg.setdefault(r["pc"], {"pass": 0, "total": 0})
            d["total"] += r["n"]
            if r["v"] == "pass":
                d["pass"] += r["n"]
        pass_rate = {k: round(v["pass"] / v["total"], 3) for k, v in agg.items() if v["total"]}

    # P4 decisiveness: fraction of P4 verdicts that were pass|fail (not uncertain).
    p4_decisiveness = None
    if _table_exists(conn, "grounding_history"):
        p4 = conn.execute(
            "SELECT gh.verdict v, COUNT(*) n FROM grounding_history gh "
            "JOIN claims c ON c.id=gh.claim_id "
            "WHERE gh.claim_kind='claim' AND c.predicate_class='P4' AND gh.ts >= ? GROUP BY v",
            (cutoff,),
        ).fetchall()
        tot = sum(r["n"] for r in p4)
        decisive = sum(r["n"] for r in p4 if r["v"] in ("pass", "fail"))
        if tot:
            p4_decisiveness = round(decisive / tot, 3)

    review_due = conn.execute(
        "SELECT COUNT(*) c FROM claims WHERE status='active' AND predicate_class='P5' "
        "AND review_after IS NOT NULL AND review_after <= ?",
        (datetime.now(timezone.utc).isoformat(),),
    ).fetchone()["c"]

    # dedup ratio = links / (links + distinct claims). Approximated from parent-link
    # cardinality vs distinct claim count.
    links = conn.execute("SELECT COUNT(*) c FROM insight_claims").fetchone()["c"]
    dedup_ratio = round(1.0 - (total / links), 3) if links else None

    # blast-radius top: claims shared by the most parents.
    blast = conn.execute(
        "SELECT claim_id, COUNT(*) parents FROM insight_claims GROUP BY claim_id "
        "ORDER BY parents DESC LIMIT 5"
    ).fetchall()
    blast_radius_top = [{"claim_id": r["claim_id"], "parents": r["parents"]} for r in blast]

    return {
        "coverage_by_class": coverage_by_class,
        "pass_rate_by_class": pass_rate,
        "p4_decisiveness": p4_decisiveness,
        "review_due_count": review_due,
        "dedup_ratio": dedup_ratio,
        "blast_radius_top": blast_radius_top,
    }


class CaptureEventBody(BaseModel):
    source: str                         # gate_failure|hook_block|ci_red|transcript_extract|manual
    payload: dict                       # arbitrary structured capture
    project: Optional[str] = None
    dedup_key: Optional[str] = None


_CAPTURE_SOURCES = {"gate_failure", "hook_block", "ci_red", "transcript_extract", "manual"}

# rev-9 §9.2 — one-time unauthenticated-mode warning latch.
_CAPTURE_UNAUTH_WARNED = False


def _capture_event_token() -> str:
    """Resolve the configured shared secret for POST /capture/event.
    Precedence: [capture].auth_token_file (gitignored token file) > env
    CRAG_ANCHOR_CAPTURE_TOKEN > [capture].event_token > "" (fail-open). Reads via the
    capture config accessor (which owns the file-vs-inline precedence) when
    available; falls back to the raw env var so auth still works even if the
    capture package is absent."""
    if _CAPTURE_TASK:
        try:
            return str(_capture_config.effective_event_token() or "")
        except Exception:
            pass
    return str(os.environ.get("CRAG_ANCHOR_CAPTURE_TOKEN", "") or "")


def _authenticate_capture_event(request: "Request") -> Optional[JSONResponse]:
    """Enforce rev-9 §9.2 shared-secret auth on /capture/event.

    - Token configured: require header X-Capture-Token to match (constant-time
      compare via hmac.compare_digest). Mismatch/absent -> 401.
    - No token configured: fail-open (preserve current local single-user
      deployment) but log a ONE-TIME warning that events are unauthenticated.
    Returns a JSONResponse to short-circuit on 401, else None to proceed."""
    global _CAPTURE_UNAUTH_WARNED
    token = _capture_event_token()
    if not token:
        if not _CAPTURE_UNAUTH_WARNED:
            _CAPTURE_UNAUTH_WARNED = True
            logger.warning(
                "/capture/event is UNAUTHENTICATED (no [capture].event_token / "
                "CRAG_ANCHOR_CAPTURE_TOKEN set) — accepting loopback POSTs without a "
                "shared secret. Set a token to enable the rev-9 §9.2 guarantee."
            )
        return None
    presented = request.headers.get("X-Capture-Token", "") if request is not None else ""
    # Compare BYTES, not str: hmac.compare_digest(str, str) is ASCII-only and
    # raises TypeError on non-ASCII input — starlette decodes header bytes as
    # latin-1, so a single >=0x80 byte in a forged header would turn 401 into
    # an unhandled 500, and a non-ASCII configured token would be unusable
    # (verification finding, 2026-07-17). UTF-8-encoding both sides keeps the
    # constant-time property and makes the comparison total over all inputs.
    if not hmac.compare_digest(
        str(presented).encode("utf-8"), str(token).encode("utf-8")
    ):
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "invalid or missing X-Capture-Token"},
        )
    return None


@app.post("/capture/event")
async def capture_event(body: CaptureEventBody, request: Request):
    """Grounding v3 capture receiver (E2/E11 primitive). Validates + dedups a
    capture event and writes a staging row. Accepted rows later flow through the
    normal save path (operator triages via the dashboard Review surface — no MCP
    tool yet). Emitters (CI/hook/transcript taps) are a separate workstream; this
    is the RECEIVING side so taps can plug in.

    Dedup: against staging (dedup_key unique-when-pending/accepted) AND against
    existing insights by embedding near-dup (cheap best-effort, non-blocking)."""
    # rev-9 §9.2 — shared-secret auth (fail-open when no token configured).
    _auth = _authenticate_capture_event(request)
    if _auth is not None:
        return _auth
    # Advisory (backward-compat, documented): when auth is disabled, the
    # response carries a non-fatal notice so the caller/operator can SEE the
    # loopback POST was accepted without a shared secret.
    _unauth_advisory = None if _capture_event_token() else (
        "capture-event auth disabled (no [capture].auth_token_file / "
        "event_token / CRAG_ANCHOR_CAPTURE_TOKEN) — accepted without a shared secret"
    )
    loop = asyncio.get_event_loop()

    def _do():
        if body.source not in _CAPTURE_SOURCES:
            return {"ok": False, "error": f"unknown source {body.source!r}",
                    "valid": sorted(_CAPTURE_SOURCES)}
        conn = get_db()
        try:
            if not _table_exists(conn, "insights_staging"):
                return {"ok": False, "error": "insights_staging not migrated (run migration 031)"}
            now = _utcnow_iso()
            dedup_key = body.dedup_key
            if not dedup_key:
                # Derive a stable dedup key from source + payload text.
                import hashlib as _h
                blob = body.source + json.dumps(body.payload, sort_keys=True)[:2000]
                dedup_key = _h.sha1(blob.encode("utf-8")).hexdigest()
            # Staging dedup.
            existing = conn.execute(
                "SELECT id, status FROM insights_staging WHERE dedup_key=? "
                "AND status IN ('pending','accepted') LIMIT 1",
                (dedup_key,),
            ).fetchone()
            if existing:
                return {"ok": True, "deduped": True, "staging_id": existing["id"],
                        "status": existing["status"]}
            cur = conn.execute(
                "INSERT INTO insights_staging (source, project, payload, dedup_key, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (body.source, body.project, json.dumps(body.payload), dedup_key, now),
            )
            conn.commit()
            return {"ok": True, "deduped": False, "staging_id": cur.lastrowid, "status": "pending"}
        finally:
            conn.close()

    result = await loop.run_in_executor(None, _do)
    if _unauth_advisory and isinstance(result, dict):
        result["advisory"] = _unauth_advisory
    return result


# ---------------------------------------------------------------------------
# Disposition Engine (docs/architecture.md REV 5 §5.2 / REV 7 §7.1, migration
# 033). Governs insights_staging transitions (accept/reject/merge/defer)
# behind a T0/T1/T2 policy tier, mandatory attribution, and a drain-SLA.
# "Single action layer, two frontends" (REV 5 §5.5): the dashboard's future
# Review buttons and the MCP tools below both call these SAME endpoints —
# there is exactly one disposition implementation.
# ---------------------------------------------------------------------------

class DispositionResolveBody(BaseModel):
    staging_id: int
    action: str                          # accept|reject|merge|defer
    actor: str                           # attribution invariant — required
    reason: Optional[str] = None
    target_id: Optional[int] = None      # required for action='merge'
    capability: Optional[str] = None     # None|"granted"|"human_approved" — the T1/T2 gate


class DispositionPolicyBody(BaseModel):
    source: Optional[str] = None
    type: Optional[str] = None
    reason_prefix: Optional[str] = None
    tier: str
    default_action: str
    deadline_hours: int


def _disposition_unavailable():
    return {"ok": False, "error": "disposition engine not available "
                                   "(db/disposition.py failed to import — check daemon startup log)"}


@app.get("/disposition/list")
async def disposition_list(project: Optional[str] = None, tier: Optional[str] = None,
                           status: str = "pending", limit: int = 100):
    """List staging entries by tier (lazily stamping tier/deadline for any row
    that doesn't have one yet, so a fresh row is always classified before it's
    surfaced). `status` defaults to 'pending' (the live queue); pass
    status='' to see all terminal states too."""
    if not _DISPOSITION:
        return _disposition_unavailable()
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "insights_staging"):
                return {"ok": False, "error": "insights_staging not migrated"}
            sql = "SELECT * FROM insights_staging WHERE 1=1"
            params: list = []
            if status:
                sql += " AND status=?"
                params.append(status)
            if project:
                sql += " AND project=?"
                params.append(project)
            sql += " ORDER BY created_at ASC LIMIT ?"
            params.append(max(1, min(limit, 500)))
            rows = conn.execute(sql, params).fetchall()
            out = []
            dirty = False
            for r in rows:
                d = dict(r)
                if not d.get("tier"):
                    stamp = _disposition.stamp_tier(conn, d["id"])
                    if stamp.get("ok"):
                        d["tier"] = stamp["tier"]
                        d["deadline"] = stamp.get("deadline")
                        dirty = True
                out.append(d)
            if dirty:
                conn.commit()
            if tier:
                out = [d for d in out if d.get("tier") == tier]
            by_tier: dict = {}
            for d in out:
                by_tier[d.get("tier") or "unclassified"] = by_tier.get(d.get("tier") or "unclassified", 0) + 1
            return {"ok": True, "count": len(out), "by_tier": by_tier, "entries": out}
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.get("/disposition/triage/{staging_id}")
async def disposition_triage(staging_id: int):
    """staging_triage convenience surface: the staging row + its tier/deadline
    + the matched policy rule, so an agent (or the dashboard) can read both
    sides before deciding — mirrors the proven contradiction-FP-triage
    pattern of 'read both sides before resolving'."""
    if not _DISPOSITION:
        return _disposition_unavailable()
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM insights_staging WHERE id=?", (staging_id,)
            ).fetchone()
            if row is None:
                return {"ok": False, "error": f"staging #{staging_id} not found"}
            entry = dict(row)
            if not entry.get("tier"):
                stamp = _disposition.stamp_tier(conn, staging_id)
                if stamp.get("ok"):
                    conn.commit()
                    entry["tier"] = stamp["tier"]
                    entry["deadline"] = stamp.get("deadline")
            rules = _disposition.load_policy(conn)
            matched = _disposition.classify_tier(entry, rules)
            return {"ok": True, "entry": entry, "matched_rule": matched}
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.post("/disposition/resolve")
async def disposition_resolve(body: DispositionResolveBody):
    """Execute (or gate) a disposition decision. Capability gate: for
    action in {accept, merge} at tier t1/t2, `capability` must authorize the
    transition (disposition.gate_check) or this returns a 'requires_human'
    verdict WITHOUT executing anything — the T1/T2 boundary from
    docs/architecture.md REV 5 §5.2, enforced here so the dashboard and every
    MCP client share the identical gate (REV 5 §5.5)."""
    if not _DISPOSITION:
        return _disposition_unavailable()
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            stamp = _disposition.stamp_tier(conn, body.staging_id)
            if not stamp.get("ok"):
                conn.rollback()
                return stamp
            conn.commit()
            tier = stamp["tier"]

            if body.action in ("accept", "merge") and not _disposition.gate_check(
                tier, body.action, body.capability
            ):
                return {"ok": False, "disposition": "requires_human", "tier": tier,
                        "staging_id": body.staging_id, "action": body.action,
                        "error": f"tier={tier} action={body.action!r} requires human "
                                 f"approval (capability={body.capability!r} insufficient)"}

            result = _disposition.resolve(
                conn, body.staging_id, body.action, body.actor,
                reason=body.reason, target_id=body.target_id,
            )
            if result.get("ok"):
                conn.commit()
            else:
                conn.rollback()
            return result
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.post("/disposition/drain")
async def disposition_drain():
    """Run the drain-SLA sweep now (normally invoked by a scheduled sweep;
    exposed here so an operator/agent can force-drain on demand). Returns a
    summary; never raises (disposition.drain_due is fail-soft per row)."""
    if not _DISPOSITION:
        return _disposition_unavailable()
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            return _disposition.drain_due(conn)
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.get("/disposition/policy")
async def disposition_policy_get():
    """Current disposition policy rules, most-specific-first."""
    if not _DISPOSITION:
        return _disposition_unavailable()
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            return {"ok": True, "rules": _disposition.load_policy(conn)}
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.post("/disposition/policy")
async def disposition_policy_set(body: DispositionPolicyBody):
    """Upsert one policy rule, keyed by (source, type, reason_prefix) — the
    same unique index migration 033 defines. Never deletes the seeded
    wildcard row; only additive/updating."""
    if not _DISPOSITION:
        return _disposition_unavailable()
    if body.tier not in _disposition.VALID_TIERS:
        return {"ok": False, "error": f"tier must be one of {_disposition.VALID_TIERS}"}
    if body.default_action not in _disposition.VALID_ACTIONS:
        return {"ok": False, "error": f"default_action must be one of {_disposition.VALID_ACTIONS}"}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            now = _utcnow_iso()
            conn.execute(
                "INSERT INTO disposition_policy "
                "(source, type, reason_prefix, tier, default_action, deadline_hours, "
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(COALESCE(source, ''), COALESCE(type, ''), COALESCE(reason_prefix, '')) "
                "DO UPDATE SET tier=excluded.tier, default_action=excluded.default_action, "
                "deadline_hours=excluded.deadline_hours, updated_at=excluded.updated_at",
                (body.source, body.type, body.reason_prefix, body.tier, body.default_action,
                 body.deadline_hours, now, now),
            )
            conn.commit()
            return {"ok": True, "rules": _disposition.load_policy(conn)}
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.get("/api/objects/claim/{claim_id}")
async def object_claim(claim_id: int):
    """Dashboard object spine (class 10): a claim's full detail — text, predicate,
    parents (insights+principles), entities, verdict timeline, supersede lineage."""
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "claims"):
                return {"ok": False, "error": "claims table not migrated"}
            c = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if not c:
                return {"ok": False, "error": "claim not found"}
            claim = dict(c)
            try:
                claim["predicate_spec"] = json.loads(claim["predicate_spec"]) if claim.get("predicate_spec") else None
            except Exception:
                pass
            parents_i = [dict(r) for r in conn.execute(
                "SELECT insight_id AS id, role, weight FROM insight_claims WHERE claim_id=?",
                (claim_id,),
            ).fetchall()]
            parents_p = [dict(r) for r in conn.execute(
                "SELECT principle_id AS id, role, weight FROM principle_claims WHERE claim_id=?",
                (claim_id,),
            ).fetchall()]
            entities = [dict(r) for r in conn.execute(
                "SELECT entity, entity_type, canonical_entity_id FROM claim_entities WHERE claim_id=?",
                (claim_id,),
            ).fetchall()]
            timeline = []
            if _table_exists(conn, "grounding_history"):
                timeline = [dict(r) for r in conn.execute(
                    "SELECT ts, job_type, verdict, reasoning, evidence, lane, recipe_version "
                    "FROM grounding_history WHERE claim_kind='claim' AND claim_id=? ORDER BY ts DESC LIMIT 50",
                    (claim_id,),
                ).fetchall()]
            return {
                "ok": True,
                "url": f"/o/claim/{claim_id}",
                "claim": claim,
                "parents": {"insights": parents_i, "principles": parents_p},
                "entities": entities,
                "verdict_timeline": timeline,
                "superseded_by": claim.get("superseded_by"),
            }
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


# ---------------------------------------------------------------------------
# Console claim-layer browser endpoints (spec: apps/console /claims view).
#
# The claim layer is the substrate that has never had a first-class API. These
# three read-only endpoints power the console's Claims view + Contradictions tab.
# Verdict derivation reuses claim_layer._claim_verdict EXACTLY (imported, never
# duplicated) so the console and the rollup engine agree on liveness by
# construction. Claims carry no project column of their own — project is derived
# via the parent insight link (insight_claims -> insights.project).
# ---------------------------------------------------------------------------

def _claim_row_verdict(row: dict) -> str:
    """Single-claim verdict via the canonical rollup logic. Fail-soft to
    'unverified' if the claim layer module is unavailable (daemon degraded)."""
    if _CLAIM_LAYER:
        try:
            return _claim_layer._claim_verdict(row)
        except Exception:
            return "unverified"
    # Inline mirror of the P5 terminal + grounding_due mapping as a fallback.
    if row.get("predicate_class") == "P5":
        return "axiomatic"
    last = row.get("last_verdict")
    if row.get("grounding_due"):
        return "stale" if last == "fail" else "revalidating"
    if last == "pass" and row.get("grounded_at"):
        return "fresh"
    if last == "fail":
        return "stale"
    return "unverified"


@app.get("/claims")
async def list_claims(
    request: Request,
    predicate_class: str = "",
    verdict: str = "",
    entity: str = "",
    project: str = "",
    q: str = "",
    limit: int = 50,
    offset: int = 0,
):
    """Paged list over active claims for the console Claims browser.

    Filters (all optional, AND-combined):
      predicate_class  P1..P5 (alias: also accepts ?class= for convenience)
      entity           matches primary_entity OR any linked claim_entities.entity
      project          via parent insight (insight_claims -> insights.project)
      q                substring over claim text
      verdict          derived liveness (fresh|aging|unverified|stale|
                       revalidating|axiomatic) — applied post-derivation, so the
                       total reflects the pre-verdict filtered set and the page
                       is verdict-filtered client-visibly (documented below).

    Each row: id, text, predicate_class, verdict, primary_entity,
    primary_entity_type, grounded_at, insight_parents, principle_parents.
    Returns {ok, total, limit, offset, claims:[...]}.
    """
    t0 = time.perf_counter()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    # Accept ?class= as an alias for predicate_class (spec uses both spellings).
    pclass = predicate_class or request.query_params.get("class", "")

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "claims"):
                return {"ok": False, "error": "claims table not migrated",
                        "total": 0, "claims": []}
            where = ["c.status = 'active'"]
            params: list = []
            if pclass:
                where.append("c.predicate_class = ?")
                params.append(pclass)
            if q:
                where.append("c.text LIKE ?")
                params.append(f"%{q}%")
            if entity:
                where.append(
                    "(c.primary_entity = ? OR EXISTS "
                    "(SELECT 1 FROM claim_entities ce WHERE ce.claim_id = c.id "
                    "AND ce.entity = ?))"
                )
                params.extend([entity, entity])
            if project:
                where.append(
                    "EXISTS (SELECT 1 FROM insight_claims ic "
                    "JOIN insights i ON i.id = ic.insight_id "
                    "WHERE ic.claim_id = c.id AND i.project = ?)"
                )
                params.append(project)
            where_sql = " AND ".join(where)

            # Verdict is derived in Python (mirrors the rollup engine), so a
            # verdict filter cannot be expressed in SQL. When one is present we
            # scan the SQL-filtered set with MINIMAL columns (verdict inputs
            # only — cheap even at corpus scale), derive + filter, and page over
            # the matching ids, so `total` is the honest verdict-filtered count
            # and pages are never spuriously empty.
            if verdict:
                vrows = conn.execute(
                    f"""SELECT c.id, c.predicate_class, c.grounded_at,
                               c.grounding_due, c.last_verdict
                        FROM claims c WHERE {where_sql} ORDER BY c.id DESC""",
                    params,
                ).fetchall()
                match_ids = [r["id"] for r in vrows
                             if _claim_row_verdict(dict(r)) == verdict]
                total = len(match_ids)
                page_ids = match_ids[offset:offset + limit]
                if not page_ids:
                    return {"ok": True, "total": total, "limit": limit,
                            "offset": offset, "claims": []}
                ph = ",".join("?" * len(page_ids))
                id_where = f"c.id IN ({ph})"
                rows = conn.execute(
                    f"""SELECT c.id, c.text, c.predicate_class,
                               c.primary_entity, c.primary_entity_type,
                               c.grounded_at, c.grounding_due, c.last_verdict,
                               (SELECT COUNT(*) FROM insight_claims ic WHERE ic.claim_id = c.id)
                                   AS insight_parents,
                               (SELECT COUNT(*) FROM principle_claims pc WHERE pc.claim_id = c.id)
                                   AS principle_parents
                        FROM claims c
                        WHERE {id_where}
                        ORDER BY c.id DESC""",
                    page_ids,
                ).fetchall()
            else:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM claims c WHERE {where_sql}", params
                ).fetchone()[0]
                rows = conn.execute(
                    f"""SELECT c.id, c.text, c.predicate_class,
                               c.primary_entity, c.primary_entity_type,
                               c.grounded_at, c.grounding_due, c.last_verdict,
                               (SELECT COUNT(*) FROM insight_claims ic WHERE ic.claim_id = c.id)
                                   AS insight_parents,
                               (SELECT COUNT(*) FROM principle_claims pc WHERE pc.claim_id = c.id)
                                   AS principle_parents
                        FROM claims c
                        WHERE {where_sql}
                        ORDER BY c.id DESC
                        LIMIT ? OFFSET ?""",
                    params + [limit, offset],
                ).fetchall()

            claims = []
            for r in rows:
                d = dict(r)
                claims.append({
                    "id": d["id"],
                    "text": d["text"],
                    "predicate_class": d["predicate_class"],
                    "verdict": _claim_row_verdict(d),
                    "primary_entity": d["primary_entity"],
                    "primary_entity_type": d["primary_entity_type"],
                    "grounded_at": d["grounded_at"],
                    "insight_parents": d["insight_parents"],
                    "principle_parents": d["principle_parents"],
                })
            return {"ok": True, "total": total, "limit": limit,
                    "offset": offset, "claims": claims}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/claims", (time.perf_counter() - t0) * 1000)
    return result


@app.get("/claims/contradictions")
async def list_claim_contradictions(
    request: Request,
    status: str = "open",
    limit: int = 50,
    offset: int = 0,
):
    """Paged claim-level contradiction pairs for the console Contradictions tab.

    Each row embeds BOTH claims (id, text, predicate_class, derived verdict) plus
    the shared primary entity and detection metadata. Returns
    {ok, total, limit, offset, pairs:[...]}.
    """
    t0 = time.perf_counter()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "claim_contradictions"):
                return {"ok": False, "error": "claim_contradictions not migrated",
                        "total": 0, "pairs": []}
            where = ["1=1"]
            params: list = []
            if status:
                where.append("cc.status = ?")
                params.append(status)
            where_sql = " AND ".join(where)

            total = conn.execute(
                f"SELECT COUNT(*) FROM claim_contradictions cc WHERE {where_sql}",
                params,
            ).fetchone()[0]

            rows = conn.execute(
                f"""SELECT cc.id, cc.claim_a_id, cc.claim_b_id, cc.reason,
                           cc.score, cc.status, cc.detected_at, cc.resolved_at
                    FROM claim_contradictions cc
                    WHERE {where_sql}
                    ORDER BY cc.detected_at DESC, cc.id DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()

            def _claim_side(cid: int) -> Optional[dict]:
                cr = conn.execute(
                    "SELECT id, text, predicate_class, primary_entity, "
                    "grounded_at, grounding_due, last_verdict "
                    "FROM claims WHERE id = ?",
                    (cid,),
                ).fetchone()
                if not cr:
                    return None
                cd = dict(cr)
                return {
                    "id": cd["id"],
                    "text": cd["text"],
                    "predicate_class": cd["predicate_class"],
                    "verdict": _claim_row_verdict(cd),
                    "primary_entity": cd["primary_entity"],
                }

            pairs = []
            for r in rows:
                d = dict(r)
                a = _claim_side(d["claim_a_id"])
                b = _claim_side(d["claim_b_id"])
                shared = None
                if a and b and a.get("primary_entity") \
                        and a["primary_entity"] == b["primary_entity"]:
                    shared = a["primary_entity"]
                pairs.append({
                    "id": d["id"],
                    "status": d["status"],
                    "reason": d["reason"],
                    "score": d["score"],
                    "detected_at": d["detected_at"],
                    "resolved_at": d["resolved_at"],
                    "shared_entity": shared,
                    "claim_a": a,
                    "claim_b": b,
                })
            return {"ok": True, "total": total, "limit": limit,
                    "offset": offset, "pairs": pairs}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/claims/contradictions", (time.perf_counter() - t0) * 1000)
    return result


@app.get("/claims/{claim_id}")
async def get_claim_detail(request: Request, claim_id: int):
    """Full claim detail for the console drawer: the claim row + parsed
    predicate_spec, linked entities, parent insights/principles (id + text
    preview), and the last 10 grounding_history rows. Returns {ok, claim, ...}.
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "claims"):
                return {"ok": False, "error": "claims table not migrated"}
            cr = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
            if not cr:
                return {"ok": False, "error": "claim not found"}
            claim = dict(cr)
            spec = None
            if claim.get("predicate_spec"):
                try:
                    spec = json.loads(claim["predicate_spec"])
                except Exception:
                    spec = claim["predicate_spec"]
            claim["predicate_spec"] = spec
            claim["verdict"] = _claim_row_verdict(claim)

            entities = [dict(r) for r in conn.execute(
                "SELECT entity, entity_type, canonical_entity_id "
                "FROM claim_entities WHERE claim_id = ?",
                (claim_id,),
            ).fetchall()]

            insight_parents = [dict(r) for r in conn.execute(
                """SELECT ic.insight_id AS id, ic.role, ic.weight,
                          substr(i.content, 1, 200) AS preview, i.project
                   FROM insight_claims ic
                   LEFT JOIN insights i ON i.id = ic.insight_id
                   WHERE ic.claim_id = ?""",
                (claim_id,),
            ).fetchall()]

            principle_parents = [dict(r) for r in conn.execute(
                """SELECT pc.principle_id AS id, pc.role, pc.weight,
                          substr(p.content, 1, 200) AS preview, p.project
                   FROM principle_claims pc
                   LEFT JOIN principles p ON p.id = pc.principle_id
                   WHERE pc.claim_id = ?""",
                (claim_id,),
            ).fetchall()]

            history = []
            if _table_exists(conn, "grounding_history"):
                history = [dict(r) for r in conn.execute(
                    "SELECT ts, job_type, verdict, reasoning, evidence, lane, "
                    "recipe_version FROM grounding_history "
                    "WHERE claim_kind = 'claim' AND claim_id = ? "
                    "ORDER BY ts DESC LIMIT 10",
                    (claim_id,),
                ).fetchall()]

            return {
                "ok": True,
                "claim": claim,
                "predicate_spec": spec,
                "entities": entities,
                "parents": {"insights": insight_parents, "principles": principle_parents},
                "grounding_history": history,
            }
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/claims/{id}", (time.perf_counter() - t0) * 1000)
    return result


@app.get("/ground/claim_candidates")
async def ground_claim_candidates(limit: int = 50, project: Optional[str] = None):
    """Grounding v3 groundskeeper feed. Returns claims DUE for (re)verification:
    P1/P2/P3 that are grounding_due OR never grounded, plus P5 past review_after
    (surfaced, never queued). P4 is scheduled by the caller via enqueue. Excludes
    claims with a pending grounding job (the daemon worker owns hot claims)."""
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "claims"):
                return {"ok": False, "error": "claims table not migrated"}
            now = datetime.now(timezone.utc).isoformat()
            limit_capped = max(1, min(int(limit), 200))
            # Due verifiable claims (not P5), excluding those with a pending job.
            due = conn.execute(
                "SELECT id, predicate_class, primary_entity FROM claims c "
                "WHERE c.status='active' AND c.predicate_class IN ('P1','P2','P3','P4') "
                "AND (c.grounding_due=1 OR c.last_verdict IS NULL) "
                "AND NOT EXISTS (SELECT 1 FROM grounding_jobs gj "
                "  WHERE gj.claim_kind='claim' AND gj.claim_id=c.id AND gj.status='pending') "
                "ORDER BY c.grounding_due DESC, c.created_at ASC LIMIT ?",
                (limit_capped,),
            ).fetchall()
            review = conn.execute(
                "SELECT id, text, review_after FROM claims "
                "WHERE status='active' AND predicate_class='P5' AND review_after IS NOT NULL "
                "AND review_after <= ? ORDER BY review_after ASC LIMIT ?",
                (now, limit_capped),
            ).fetchall()
            return {
                "ok": True,
                "due": [dict(r) for r in due],
                "review_due": [dict(r) for r in review],
            }
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


# ---------------------------------------------------------------------------
# Autonomous resolution (migration 028) — proposals + reversible resolutions
# ---------------------------------------------------------------------------

@app.get("/ground/proposals")
async def ground_proposals(status: str = "pending", limit: int = 50):
    """Pending resolution proposals (or any status) awaiting human review.
    These are the verdicts the worker deliberately did NOT auto-apply —
    uncertain verdicts, principles, high-stakes insights, or a fail verdict
    with no confident LLM correction. Returns newest first."""
    if not _GROUNDING_V2:
        return {"ok": False, "error": "grounding v2 not available"}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "resolution_proposals"):
                return {"ok": False, "error": "resolution_proposals table not migrated (run migration 028)"}
            if status and status != "all":
                rows = conn.execute(
                    "SELECT * FROM resolution_proposals WHERE status=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM resolution_proposals ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return {"ok": True, "proposals": [dict(r) for r in rows], "count": len(rows)}
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


class GroundProposalDecideBody(BaseModel):
    decision: str          # approve | reject
    decided_by: str = "operator"


@app.post("/ground/proposals/{proposal_id}/decide")
async def ground_proposal_decide(proposal_id: int, body: GroundProposalDecideBody):
    """Approve or reject a pending resolution proposal.

    approve: applies proposed_action (verify | update | supersede | dismiss)
      to the claim, exactly like the worker would have if it had been
      confident enough to auto-apply — same code paths, just human-gated.
    reject:  discards the proposal, no claim mutation. The flag (if still
      open) stays open for the next sweep/resolve cycle to reconsider.
    Both are terminal — proposal.status becomes approved|rejected and the
    row stays (audit trail; nothing is ever deleted, per doctrine)."""
    if body.decision not in ("approve", "reject"):
        return JSONResponse(status_code=422, content={"ok": False, "error": "decision must be approve|reject"})
    if not _GROUNDING_V2:
        return {"ok": False, "error": "grounding v2 not available"}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "resolution_proposals"):
                return {"ok": False, "error": "resolution_proposals table not migrated (run migration 028)"}
            prop = conn.execute(
                "SELECT * FROM resolution_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if prop is None:
                return JSONResponse(status_code=404, content={"ok": False, "error": f"proposal {proposal_id} not found"})
            if prop["status"] != "pending":
                return JSONResponse(status_code=409, content={
                    "ok": False, "error": f"proposal {proposal_id} already decided (status={prop['status']})",
                })

            now = _utcnow_iso()
            claim_kind = prop["claim_kind"]
            claim_id = prop["claim_id"]
            tbl = _ground_tbl(claim_kind)

            if body.decision == "reject":
                conn.execute(
                    "UPDATE resolution_proposals SET status='rejected', decided_at=?, decided_by=? WHERE id=?",
                    (now, body.decided_by, proposal_id),
                )
                conn.commit()
                return {"ok": True, "proposal_id": proposal_id, "status": "rejected"}

            # approve — apply proposed_action
            action = prop["proposed_action"]
            if action == "verify" and tbl:
                conn.execute(
                    f"UPDATE {tbl} SET grounded_at=?, grounding_due=0 WHERE id=?",
                    (now, claim_id),
                )
            elif action == "update" and tbl and prop["proposed_content"]:
                conn.execute(
                    f"UPDATE {tbl} SET content=?, grounded_at=?, grounding_due=0, updated_at=? WHERE id=?",
                    (prop["proposed_content"], now, now, claim_id),
                )
            elif action == "dismiss":
                if tbl:
                    conn.execute(
                        f"UPDATE {tbl} SET grounding_due=0 WHERE id=?", (claim_id,),
                    )
            # action == 'supersede' is not auto-implementable generically here
            # (needs a NEW claim id to supersede TO) — approving a 'supersede'
            # proposal is a signal for the operator to run the existing
            # /supersede or /supersede_principle endpoint manually; we still
            # record the approval + clear the flag so it isn't re-flagged.
            if tbl:
                conn.execute(
                    "UPDATE grounding_queue SET status='resolved', resolved_at=?, resolved_by=?, "
                    "resolution=? WHERE claim_kind=? AND claim_id=? AND status='open'",
                    (now, body.decided_by, f"proposal-approved:{action}", claim_kind, claim_id),
                )
            conn.execute(
                "UPDATE resolution_proposals SET status='approved', decided_at=?, decided_by=? WHERE id=?",
                (now, body.decided_by, proposal_id),
            )
            conn.commit()
            return {"ok": True, "proposal_id": proposal_id, "status": "approved", "action_applied": action}
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.get("/ground/resolutions")
async def ground_resolutions(limit: int = 50):
    """Recent autonomous resolutions (auto_applied=1 rows) — the audit trail
    for mutations the worker made WITHOUT waiting for a human: verify-on-pass
    and low-stakes-insight auto-correction. Each row is a reversible handle
    (prior_content is the revert target) for POST /ground/resolutions/{id}/revert."""
    if not _GROUNDING_V2:
        return {"ok": False, "error": "grounding v2 not available"}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "resolution_proposals"):
                return {"ok": False, "error": "resolution_proposals table not migrated (run migration 028)"}
            rows = conn.execute(
                "SELECT * FROM resolution_proposals WHERE auto_applied=1 "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return {"ok": True, "resolutions": [dict(r) for r in rows], "count": len(rows)}
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.post("/ground/resolutions/{resolution_id}/revert")
async def ground_resolution_revert(resolution_id: int):
    """Revert an autonomous resolution (auto_applied=1 row).

    update  action: restores prior_content verbatim (undoes an auto-correction).
    verify  action: restores prior_confidence verbatim (undoes the confidence
      bump _apply_verify made) when the row carries a prior_confidence value
      (migration 028+ auto-verify rows always do; safety-verifier FIX4 — the
      pre-fix behaviour could not literally un-verify a confidence bump and
      only re-flagged the claim). Rows written before this column existed
      (prior_confidence IS NULL) fall back to that original re-flag-only
      behaviour — documented, not silent.
    Both actions ALSO re-flag the claim (grounding_due=1 + a fresh open
    grounding_queue row) so the next sweep re-examines it regardless of
    whether content/confidence could be restored.
    Marks the resolution_proposals row status='reverted' — the row itself is
    NEVER deleted (doctrine: nothing destroyed), only its status changes.
    Note: verify-action revert restores confidence only, not verify_count/
    verify_streak/verified_at — those are historical audit metadata, not the
    trust score itself, and are intentionally left as-is (same freshness-vs-
    trust distinction grounding_resolve.py's pass-branch fix documents)."""
    if not _GROUNDING_V2:
        return {"ok": False, "error": "grounding v2 not available"}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "resolution_proposals"):
                return {"ok": False, "error": "resolution_proposals table not migrated (run migration 028)"}
            row = conn.execute(
                "SELECT * FROM resolution_proposals WHERE id=? AND auto_applied=1",
                (resolution_id,),
            ).fetchone()
            if row is None:
                return JSONResponse(status_code=404, content={
                    "ok": False, "error": f"auto-applied resolution {resolution_id} not found",
                })
            if row["status"] == "reverted":
                return JSONResponse(status_code=409, content={
                    "ok": False, "error": f"resolution {resolution_id} already reverted",
                })

            now = _utcnow_iso()
            claim_kind = row["claim_kind"]
            claim_id = row["claim_id"]
            tbl = _ground_tbl(claim_kind)

            if row["proposed_action"] == "update" and tbl and row["prior_content"] is not None:
                conn.execute(
                    f"UPDATE {tbl} SET content=? WHERE id=?",
                    (row["prior_content"], claim_id),
                )
            elif row["proposed_action"] == "verify" and tbl:
                # FIX4: restore the exact pre-bump confidence captured by
                # _apply_verify at auto-verify time. NULL means the row
                # predates the prior_confidence column (migration 028 shipped
                # atomically with this fix, so in practice this is legacy-only)
                # — fall back silently to the old re-flag-only behaviour rather
                # than raising, since there is nothing to restore.
                try:
                    prior_conf = row["prior_confidence"]
                except (IndexError, KeyError):
                    prior_conf = None
                if prior_conf is not None:
                    conn.execute(
                        f"UPDATE {tbl} SET confidence=? WHERE id=?",
                        (prior_conf, claim_id),
                    )
            # Re-flag + re-open a grounding_queue row (sets grounding_due=1 too)
            # so the next sweep re-examines the reverted claim.
            if tbl:
                _ground_enqueue_row(
                    conn, claim_kind, claim_id,
                    reason="auto_resolution_reverted", trigger_src="worker",
                    detail=f"reverted resolution #{resolution_id}", now=now,
                )
            conn.execute(
                "UPDATE resolution_proposals SET status='reverted', decided_at=?, decided_by='operator-revert' WHERE id=?",
                (now, resolution_id),
            )
            conn.commit()
            return {"ok": True, "resolution_id": resolution_id, "status": "reverted"}
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


@app.get("/ground/economics")
async def ground_economics():
    """Grounding LLM economics (Phase 1b, migration 030 / insight #3339):
    active provider-agnostic config, today's budget status against the hard
    daily cap, and a 7-day spend breakdown by model and by pipeline stage.

    Read-only reflection of grounding_config.get_config() (stack.toml +
    env overrides, never hardcoded) and llm_cost_ledger. Absence of the
    ledger table (pre-030 DB) is reported honestly, not synthesized."""
    if not _GROUNDING_V2:
        return {"ok": False, "error": "grounding v2 not available"}
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        try:
            if not _table_exists(conn, "llm_cost_ledger"):
                return {"ok": False, "error": "llm_cost_ledger table not migrated (run migration 030)"}
            cfg = _grounding_config.get_config()
            budget = _grounding_cost.budget_status(conn, cfg)
            try:
                cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            except Exception:
                cutoff_7d = ""
            spend_7d = conn.execute(
                "SELECT COALESCE(SUM(est_cost_usd), 0) AS usd, COUNT(*) AS calls, "
                "COALESCE(SUM(tokens_in), 0) AS tokens_in, COALESCE(SUM(tokens_out), 0) AS tokens_out "
                "FROM llm_cost_ledger WHERE ts >= ?",
                (cutoff_7d,),
            ).fetchone()
            per_model = conn.execute(
                "SELECT model, provider, quota_type, COUNT(*) AS calls, "
                "SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, "
                "COALESCE(SUM(est_cost_usd), 0) AS est_cost_usd "
                "FROM llm_cost_ledger WHERE ts >= ? GROUP BY model, provider, quota_type "
                "ORDER BY calls DESC",
                (cutoff_7d,),
            ).fetchall()
            per_stage = conn.execute(
                "SELECT stage, COUNT(*) AS calls, "
                "SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, "
                "COALESCE(SUM(est_cost_usd), 0) AS est_cost_usd "
                "FROM llm_cost_ledger WHERE ts >= ? GROUP BY stage ORDER BY calls DESC",
                (cutoff_7d,),
            ).fetchall()
            # today's rollup — powers the dashboard "Calls today / Cost today"
            # stats. Same UTC-date derivation as grounding_cost._today_usage
            # (substr(ts,1,10)) so the numbers agree with budget_status.
            today_iso = datetime.now(timezone.utc).isoformat()[:10]
            today_row = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in), 0) AS tokens_in, "
                "COALESCE(SUM(tokens_out), 0) AS tokens_out, "
                "COALESCE(SUM(est_cost_usd), 0) AS est_cost_usd "
                "FROM llm_cost_ledger WHERE substr(ts, 1, 10) = ?",
                (today_iso,),
            ).fetchone()
            today_quota = conn.execute(
                "SELECT quota_type FROM llm_cost_ledger WHERE substr(ts, 1, 10) = ? "
                "ORDER BY id DESC LIMIT 1",
                (today_iso,),
            ).fetchone()
            return {
                "ok": True,
                "config": {
                    "provider": cfg.provider,
                    "model": cfg.model,
                    "escalation_model": cfg.escalation_model,
                    "escalation_enabled": cfg.escalation_enabled,
                    "auth_source": cfg.auth_source,
                    "base_url": cfg.base_url,
                    "author_max_tokens": cfg.author_max_tokens,
                    "adjudicate_max_tokens": cfg.adjudicate_max_tokens,
                    "correction_max_tokens": cfg.correction_max_tokens,
                    "temperature": cfg.temperature,
                    "timeout_sec": cfg.timeout_sec,
                    "worker_concurrency": cfg.worker_concurrency,
                },
                "budget": budget,
                "today": {
                    "calls": today_row["calls"],
                    "tokens_in": today_row["tokens_in"],
                    "tokens_out": today_row["tokens_out"],
                    "est_cost_usd": round(today_row["est_cost_usd"] or 0, 4),
                    "quota_type": today_quota["quota_type"] if today_quota else _grounding_cost._quota_type(cfg.provider),
                },
                "spend_7d": {
                    "usd": round(spend_7d["usd"] or 0, 4),
                    "calls": spend_7d["calls"],
                    "tokens_in": spend_7d["tokens_in"],
                    "tokens_out": spend_7d["tokens_out"],
                },
                "per_model_7d": [dict(r) for r in per_model],
                "per_stage_7d": [dict(r) for r in per_stage],
            }
        finally:
            conn.close()

    return await loop.run_in_executor(None, _do)


TASK_TYPE_SEEDS = {
    "audit": "code audit security review governance compliance IDOR validation",
    "deploy": "deployment CI/CD production staging blue-green workflow promote",
    "frontend": "React component CSS UI page layout form i18n locale frontend",
    "backend": "API endpoint database migration entity controller service repository backend",
    "infra": "infrastructure Docker Kubernetes terraform proxy nginx tunnel VPS server",
    "memory": "crag Anchor daemon recall insight principle embedding cognitive memory MCP",
    "watchdog": "watchdog stack health monitor scheduled-task restart hysteresis",
    "notification": "notification alert push topic webhook permission",
    "security": "security secret token credential vulnerability authentication authorization OIDC JWT",
}


class InjectForTaskBody(BaseModel):
    task_type: str
    project: Optional[str] = None
    limit: int = 5


@app.post("/inject_for_task")
async def inject_for_task(request: Request, body: InjectForTaskBody):
    """Return the pre-seeded insight/principle cluster for a given task_type."""
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()

    def _fetch():
        conn = get_db()
        # Try project-specific first
        rows = conn.execute(
            """SELECT tc.rank, tc.insight_id, tc.principle_id
               FROM task_clusters tc
               WHERE tc.task_type = ? AND tc.project = ?
               ORDER BY tc.rank ASC LIMIT ?""",
            (body.task_type, body.project, body.limit),
        ).fetchall() if body.project else []

        # Fall back to global if no project-specific rows
        if not rows:
            rows = conn.execute(
                """SELECT tc.rank, tc.insight_id, tc.principle_id
                   FROM task_clusters tc
                   WHERE tc.task_type = ? AND tc.project IS NULL
                   ORDER BY tc.rank ASC LIMIT ?""",
                (body.task_type, body.limit),
            ).fetchall()

        insights_out = []
        principles_out = []
        for r in rows:
            if r["insight_id"] is not None:
                row = conn.execute(
                    "SELECT id, project, type, content, tags, confidence FROM insights WHERE id = ?",
                    (r["insight_id"],),
                ).fetchone()
                if row:
                    insights_out.append({
                        "id": row["id"], "project": row["project"], "type": row["type"],
                        "content": row["content"], "tags": row["tags"], "confidence": row["confidence"],
                        "rank": r["rank"],
                    })
            elif r["principle_id"] is not None:
                row = conn.execute(
                    "SELECT id, project, content, tags, confidence FROM principles WHERE id = ?",
                    (r["principle_id"],),
                ).fetchone()
                if row:
                    principles_out.append({
                        "id": row["id"], "project": row["project"],
                        "content": row["content"], "tags": row["tags"], "confidence": row["confidence"],
                        "rank": r["rank"],
                    })
        conn.close()
        return insights_out, principles_out

    insights, principles = await loop.run_in_executor(None, _fetch)
    _log_request(request, "/inject_for_task", (time.perf_counter() - t0) * 1000,
                 project=body.project)
    return {
        "ok": True,
        "task_type": body.task_type,
        "project": body.project,
        "insights": insights,
        "principles": principles,
    }


@app.post("/admin/seed_task_clusters")
async def seed_task_clusters(request: Request):
    """Seed task_clusters table using cosine similarity against TASK_TYPE_SEEDS prompts."""
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()

    def _do_seed():
        if not _model_loaded:
            return {"ok": False, "error": "Embedding model not loaded yet"}

        conn = get_db()
        total_inserted = 0
        cluster_counts: dict[str, int] = {}

        # Pre-load all active insights with embeddings (skip superseded — Phase 13).
        all_insights = conn.execute(
            "SELECT id, project, content, embedding FROM insights "
            "WHERE status='active' AND embedding IS NOT NULL AND superseded_by IS NULL"
        ).fetchall()
        # Pre-load all principles with embeddings (skip superseded — Phase 13).
        all_principles = conn.execute(
            "SELECT id, project, content, embedding FROM principles "
            "WHERE embedding IS NOT NULL AND superseded_by IS NULL"
        ).fetchall()

        for task_type, seed_prompt in TASK_TYPE_SEEDS.items():
            cluster_counts[task_type] = 0
            seed_vec = np.frombuffer(_embed_one(seed_prompt), dtype="float32")

            # Score insights
            scored_insights = []
            for r in all_insights:
                vec = np.frombuffer(r["embedding"], dtype="float32")
                sim = float(seed_vec @ vec)
                if sim >= 0.30:
                    scored_insights.append((sim, r))
            scored_insights.sort(key=lambda x: x[0], reverse=True)
            top_insights = scored_insights[:8]

            # Score principles
            scored_principles = []
            for r in all_principles:
                vec = np.frombuffer(r["embedding"], dtype="float32")
                sim = float(seed_vec @ vec)
                if sim >= 0.30:
                    scored_principles.append((sim, r))
            scored_principles.sort(key=lambda x: x[0], reverse=True)
            top_principles = scored_principles[:8]

            # Insert: global (NULL project) for all top hits
            for rank, (sim, r) in enumerate(top_insights):
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_clusters (task_type, project, insight_id, rank) VALUES (?, NULL, ?, ?)",
                        (task_type, r["id"], rank),
                    )
                    total_inserted += conn.execute("SELECT changes()").fetchone()[0]
                    # Also insert project-specific row if insight has a project
                    if r["project"]:
                        conn.execute(
                            "INSERT OR IGNORE INTO task_clusters (task_type, project, insight_id, rank) VALUES (?, ?, ?, ?)",
                            (task_type, r["project"], r["id"], rank),
                        )
                        total_inserted += conn.execute("SELECT changes()").fetchone()[0]
                except Exception as e:
                    logger.warning("seed_task_clusters insight insert error: %s", e)

            for rank, (sim, r) in enumerate(top_principles):
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_clusters (task_type, project, principle_id, rank) VALUES (?, NULL, ?, ?)",
                        (task_type, r["id"], rank),
                    )
                    total_inserted += conn.execute("SELECT changes()").fetchone()[0]
                    if r["project"]:
                        conn.execute(
                            "INSERT OR IGNORE INTO task_clusters (task_type, project, principle_id, rank) VALUES (?, ?, ?, ?)",
                            (task_type, r["project"], r["id"], rank),
                        )
                        total_inserted += conn.execute("SELECT changes()").fetchone()[0]
                except Exception as e:
                    logger.warning("seed_task_clusters principle insert error: %s", e)

            conn.commit()
            cluster_counts[task_type] = conn.execute(
                "SELECT COUNT(*) FROM task_clusters WHERE task_type = ?", (task_type,)
            ).fetchone()[0]

        conn.close()
        return {"ok": True, "clusters": cluster_counts, "total_rows_inserted": total_inserted}

    result = await loop.run_in_executor(None, _do_seed)
    _log_request(request, "/admin/seed_task_clusters", (time.perf_counter() - t0) * 1000)
    return result


@app.get("/entities")
async def list_entities(request: Request, prefix: str = "", entity_type: Optional[str] = None, limit: int = 20):
    """Autocomplete-style: list distinct entities matching prefix."""
    t0 = time.perf_counter()
    conn = get_db()
    where = ["entity LIKE ?"]
    params = [f"{prefix}%"]
    if entity_type:
        where.append("entity_type = ?")
        params.append(entity_type)
    sql = f"""SELECT entity, entity_type, COUNT(*) AS occurrences
              FROM entity_links WHERE {' AND '.join(where)}
              GROUP BY entity, entity_type ORDER BY occurrences DESC LIMIT ?"""
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    _log_request(request, "/entities", (time.perf_counter() - t0) * 1000)
    return {"ok": True, "entities": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Phase 9: Contradiction scan backfill
# ---------------------------------------------------------------------------

@app.post("/admin/scan_contradictions")
async def scan_contradictions(request: Request):
    """Backfill: run contradiction detection on every insight/principle that has
    not been scanned yet (suspect_detected_at IS NULL).  Idempotent -- rows with
    a scan timestamp are skipped even if not flagged.  Use after Phase 9 install
    to process the existing ~1100 insights + ~94 principles.
    """
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()

    def _do():
        conn = get_db()
        scanned = 0
        flagged_total = 0

        for kind, table in [("insight", "insights"), ("principle", "principles")]:
            status_clause = "AND status='active'" if kind == "insight" else ""
            rows = conn.execute(
                f"SELECT id, project, content, embedding FROM {table} "
                f"WHERE embedding IS NOT NULL AND suspect_detected_at IS NULL {status_clause}"
            ).fetchall()

            for row in rows:
                try:
                    flagged = _detect_contradictions(
                        conn, kind, row["id"], row["content"], row["embedding"], row["project"]
                    )
                    flagged_total += len(flagged)
                except Exception as exc:
                    logger.warning("scan_contradictions: detection failed for %s %d: %s", kind, row["id"], exc)

                # Mark scanned even if no contradiction found, so we never re-scan unless forced
                try:
                    conn.execute(
                        f"UPDATE {table} SET suspect_detected_at = ? "
                        f"WHERE id = ? AND suspect_detected_at IS NULL",
                        (_utcnow_iso(), row["id"]),
                    )
                    conn.commit()
                    scanned += 1
                except Exception as exc:
                    logger.warning("scan_contradictions: timestamp update failed for %s %d: %s", kind, row["id"], exc)

        conn.close()
        return {"scanned": scanned, "flagged": flagged_total}

    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/admin/scan_contradictions", (time.perf_counter() - t0) * 1000)
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Phase 10: SSE broadcast helpers + endpoints
# ---------------------------------------------------------------------------

def _persist_broadcast(kind: str, payload_json: str, subscriber_count: int) -> None:
    """Write one row to broadcast_events for audit/stats purposes."""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO broadcast_events (kind, payload, subscriber_count) VALUES (?, ?, ?)",
            (kind, payload_json, subscriber_count),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("broadcast_events insert failed: %s", exc)


@app.get("/subscribe")
async def subscribe(request: Request):
    """Server-Sent Events stream of broadcast events.

    Client disconnect handled via `await request.is_disconnected()` check
    inside the loop. Heartbeat every HEARTBEAT_INTERVAL_SEC to keep the
    connection alive through proxies/firewalls.
    """
    queue = await add_subscriber()

    async def event_gen():
        try:
            yield f"event: hello\ndata: {{\"server\":\"crag-anchor\",\"version\":\"{VERSION}\"}}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_SEC)
                    yield f"event: broadcast\ndata: {msg}\n\n"
                except asyncio.TimeoutError:
                    # heartbeat — keeps proxy/firewall connection alive
                    yield ":keepalive\n\n"
        finally:
            await remove_subscriber(queue)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/query/insights")
async def query_insights(
    request: Request,
    q: str = "",
    project: str = "",
    type: str = "",
    limit: int = 50,
    offset: int = 0,
    order_by: str = "created_desc",
):
    """P2.2 — Paginated, searchable insights query.

    Supports full-text search via insights_fts when q is provided.
    project / type filters applied server-side.
    order_by: created_desc | confidence_desc | recalled_desc
    """
    t0 = time.perf_counter()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    def _do():
        conn = get_db()
        try:
            # Count total matching rows
            where_clauses = ["i.status = 'active'"]
            params: list = []

            if project:
                where_clauses.append("i.project = ?")
                params.append(project)
            if type:
                where_clauses.append("i.type = ?")
                params.append(type)

            if q:
                # Full-text search via FTS5 table if available
                try:
                    fts_ids = [
                        r[0] for r in conn.execute(
                            "SELECT rowid FROM insights_fts WHERE insights_fts MATCH ? LIMIT 5000",
                            (q,),
                        ).fetchall()
                    ]
                except Exception:
                    # Fallback: LIKE search
                    fts_ids = [
                        r[0] for r in conn.execute(
                            "SELECT id FROM insights WHERE content LIKE ? AND status = 'active' LIMIT 5000",
                            (f"%{q}%",),
                        ).fetchall()
                    ]

                if not fts_ids:
                    conn.close()
                    return {"rows": [], "total": 0, "limit": limit, "offset": offset}

                id_placeholders = ",".join("?" * len(fts_ids))
                where_clauses.append(f"i.id IN ({id_placeholders})")
                params.extend(fts_ids)

            where_sql = " AND ".join(where_clauses)

            total = conn.execute(
                f"SELECT COUNT(*) FROM insights i WHERE {where_sql}", params
            ).fetchone()[0]

            order_sql = {
                "created_desc": "i.created_at DESC",
                "confidence_desc": "i.confidence DESC",
                "recalled_desc": "COALESCE(i.last_recalled_at,'1970-01-01') DESC",
            }.get(order_by, "i.created_at DESC")

            # Liveness columns ride along (falsifiers LEFT JOIN — same pattern
            # as the recall hot path) so the console corpus table can render
            # verdict chips without a per-row round-trip.
            rows = conn.execute(
                f"""
                SELECT i.id,
                       substr(i.content, 1, 400) AS content,
                       i.type, i.tags, i.project, i.confidence,
                       i.created_at, i.last_recalled_at, i.suspect_of,
                       i.grounded_at, i.volatility_class, i.grounding_due,
                       f.last_result AS falsifier_result, f.kind AS falsifier_kind,
                       (SELECT COUNT(*) FROM recall_events re WHERE re.insight_id = i.id)
                           AS recall_count
                FROM insights i
                LEFT JOIN falsifiers f
                       ON f.claim_kind = 'insight' AND f.claim_id = i.id
                WHERE {where_sql}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()
            out_rows = []
            for r in rows:
                d = dict(r)
                d["liveness"] = _liveness_stamp(
                    d.pop("grounded_at", None), d.pop("volatility_class", None),
                    d.pop("grounding_due", 0), d.pop("falsifier_result", None),
                    d.pop("falsifier_kind", None),
                )
                out_rows.append(d)
            conn.close()
            return {
                "rows": out_rows,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        except Exception as exc:
            conn.close()
            raise exc

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/query/insights", (time.perf_counter() - t0) * 1000)
    return {"ok": True, **result}


@app.get("/query/insights/{insight_id}")
async def query_insight_detail(request: Request, insight_id: int):
    """P2.2 — Single insight detail."""
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        row = conn.execute(
            """
            SELECT i.id, i.content, i.type, i.tags, i.project, i.confidence,
                   i.created_at, i.last_recalled_at, i.suspect_of, i.status,
                   (SELECT COUNT(*) FROM recall_events re WHERE re.insight_id = i.id)
                       AS recall_count
            FROM insights i WHERE i.id = ?
            """,
            (insight_id,),
        ).fetchone()
        if row is None:
            conn.close()
            return None

        contradictions = conn.execute(
            """
            SELECT ce.id AS new_id, ce.old_id AS suspect_of_id,
                   ce.new_kind, ce.old_kind, ce.cosine_sim AS score,
                   ce.detected_at AS decided_at,
                   substr(COALESCE(i2.content,''), 1, 200) AS old_content
            FROM contradiction_events ce
            LEFT JOIN insights i2 ON ce.old_kind = 'insight' AND ce.old_id = i2.id
            WHERE ce.new_id = ? OR ce.old_id = ?
            ORDER BY ce.detected_at DESC LIMIT 5
            """,
            (insight_id, insight_id),
        ).fetchall()

        # Linked entities (console drawer mini-graph anchor) + claims rollup.
        entities: list = []
        if _table_exists(conn, "entity_links"):
            entities = [dict(r) for r in conn.execute(
                "SELECT entity, entity_type FROM entity_links "
                "WHERE insight_id = ? ORDER BY id LIMIT 20",
                (insight_id,),
            ).fetchall()]
        claims_summary = None
        if _CLAIM_LAYER and _table_exists(conn, "claims"):
            try:
                _roll = _claim_layer.claim_rollup(conn, "insight", insight_id)
                if _roll.get("claims_summary", {}).get("total"):
                    claims_summary = {
                        **_roll["claims_summary"],
                        "claim_verdict": _roll.get("verdict"),
                        "fresh_fraction": _roll.get("fresh_fraction"),
                    }
            except Exception:
                pass
        conn.close()
        return {"insight": dict(row), "entities": entities,
                "claims_summary": claims_summary,
                "contradictions": [dict(c) for c in contradictions]}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, f"/query/insights/{insight_id}", (time.perf_counter() - t0) * 1000)
    if result is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Insight {insight_id} not found"},
        )
    return {"ok": True, **result}


def _query_entity_detail_sync(entity_type: str, entity: str) -> dict:
    """Core entity-detail lookup shared by both the query-param route (new) and
    the legacy path-segment route. Returns the canonical envelope dict."""
    conn = get_db()
    # Insight backlinks
    insight_rows = conn.execute(
        """SELECT el.insight_id AS id,
                  substr(i.content, 1, 120) AS snippet,
                  i.type, i.project, i.confidence, i.created_at
           FROM entity_links el
           JOIN insights i ON el.insight_id = i.id
           WHERE el.entity = ? AND el.entity_type = ? AND el.insight_id IS NOT NULL
           ORDER BY i.created_at DESC LIMIT 50""",
        (entity, entity_type),
    ).fetchall()
    # Principle backlinks
    principle_rows = conn.execute(
        """SELECT el.principle_id AS id,
                  substr(p.content, 1, 120) AS snippet,
                  p.project, p.confidence, p.created_at
           FROM entity_links el
           JOIN principles p ON el.principle_id = p.id
           WHERE el.entity = ? AND el.entity_type = ? AND el.principle_id IS NOT NULL
           ORDER BY p.created_at DESC LIMIT 50""",
        (entity, entity_type),
    ).fetchall()
    # Aggregate stats
    agg = conn.execute(
        """SELECT MIN(COALESCE(i.created_at, p.created_at)) AS first_mention,
                  MAX(COALESCE(i.created_at, p.created_at)) AS last_mention,
                  COUNT(*) AS mention_count
           FROM entity_links el
           LEFT JOIN insights i ON el.insight_id = i.id
           LEFT JOIN principles p ON el.principle_id = p.id
           WHERE el.entity = ? AND el.entity_type = ?""",
        (entity, entity_type),
    ).fetchone()

    if not insight_rows and not principle_rows:
        return {"ok": False, "error": f"no entity '{entity_type}:{entity}' found"}

    return {
        "ok": True,
        "entity": {
            "entity_type": entity_type,
            "entity": entity,
            "insight_links": [dict(r) for r in insight_rows],
            "principle_links": [dict(r) for r in principle_rows],
            "first_mention_at": agg["first_mention"] if agg else None,
            "last_mention_at": agg["last_mention"] if agg else None,
            "mention_count": agg["mention_count"] if agg else 0,
        },
    }


@app.get("/query/entity/{entity_type}")
async def query_entity_detail(request: Request, entity_type: str, value: Optional[str] = None, entity: Optional[str] = None):
    """Phase 1.7 — single entity detail with backlinks (QUERY-PARAM form).

    Returns: {ok: bool, entity: {type, value, insight_links: [...], principle_links: [...],
                                  first_mention_at, last_mention_at, mention_count}}.

    Value is a query param (?value=...) to support slash-containing values like
    path:/hooks/rtk-rewrite. Used by /api/objects/entity/{type}?value=<enc>.
    """
    # Accept value from ?value= (new) or legacy ?entity= param
    entity_value = value or entity or ""
    t0 = time.perf_counter()
    result = await asyncio.get_event_loop().run_in_executor(
        None, _query_entity_detail_sync, entity_type, entity_value
    )
    _log_request(request, f"/query/entity/{entity_type}?value={entity_value}", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result, status_code=200 if result.get("ok") else 404)


@app.get("/query/session/{session_id}")
async def query_session(request: Request, session_id: str):
    """Phase 1.7 — single session_meta row + aggregates from related tables.

    Returns: {ok: bool, session: {... session_meta cols + aggregates}, error?}.
    Aggregates: insights_saved_count, recalls_count, recall_hits_total,
    recall_misses_total, tokens_in_total, tokens_out_total.
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        row = conn.execute(
            "SELECT session_uuid, project, cwd, started_at, last_seen_at, source "
            "FROM session_meta WHERE session_uuid = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"session_id '{session_id}' not in session_meta"}

        session = dict(row)

        # Per-session aggregates (counts; cheap because session_id is indexed)
        session["insights_saved_count"] = conn.execute(
            "SELECT COUNT(*) FROM insights WHERE session_id = ?", (session_id,),
        ).fetchone()[0]
        session["recalls_count"] = conn.execute(
            "SELECT COUNT(*) FROM recall_events WHERE session_id = ?", (session_id,),
        ).fetchone()[0]

        # token_ledger aggregate (per Phase 17: hits/misses/in/out)
        tk = conn.execute(
            "SELECT SUM(tokens_in)        AS tokens_in_total, "
            "       SUM(tokens_out)       AS tokens_out_total, "
            "       SUM(recall_hits)      AS recall_hits_total, "
            "       SUM(recall_misses)    AS recall_misses_total, "
            "       SUM(novel_saves)      AS novel_saves_total, "
            "       SUM(repeated_errors)  AS repeated_errors_total "
            "FROM token_ledger WHERE session_id = ?", (session_id,),
        ).fetchone()
        if tk:
            for k in ("tokens_in_total", "tokens_out_total", "recall_hits_total",
                      "recall_misses_total", "novel_saves_total", "repeated_errors_total"):
                session[k] = tk[k] or 0

        return {"ok": True, "session": session}

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, f"/query/session/{session_id}", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result, status_code=200 if result.get("ok") else 404)


class OperatorActionBody(BaseModel):
    """Phase 7 — generic operator action wrapper.

    The dashboard calls this for any audit-logged mutation:
      mark_fp        — clear_suspect on a pair
      supersede      — manual supersede(loser_id, winner_id, reason)
      promote        — promote_insight(id) → principle
      arena_verdict  — run arena strategy on a pair
      decay_drop     — verify_insight(id, status='stale') to decay
      drift_resolve  — update_insight or supersede w/ reason='drift'
    All actions are logged to operator_audit_log before/after execution.
    """
    action: str
    target_class: Optional[str] = None
    target_id: Optional[str] = None
    payload: dict = {}
    note: Optional[str] = None
    actor: str = "operator"
    session_id: Optional[str] = None


def _audit_log(actor: str, action: str, target_class: Optional[str],
               target_id: Optional[str], payload: dict, result: dict,
               note: Optional[str], session_id: Optional[str]) -> int:
    """Insert one row into operator_audit_log. Returns audit row id."""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO operator_audit_log "
        "(actor, action, target_class, target_id, payload, result, note, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            actor, action, target_class, target_id,
            json.dumps(payload)[:4000],
            json.dumps(result)[:4000],
            (note or "")[:500],
            session_id,
        ),
    )
    conn.commit()
    return cur.lastrowid


@app.post("/operator/action")
async def operator_action(body: OperatorActionBody, request: Request):
    """Phase 7 — execute + audit a generic operator action.

    Dispatches to underlying daemon methods based on `action`.  Always
    logs to operator_audit_log regardless of success/failure so the
    audit trail captures attempts too.

    Returns {ok, audit_id, action, result}
    """
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()

    def _do() -> dict:
        action = body.action
        payload = body.payload or {}

        try:
            if action == "mark_fp":
                # clear suspect flag between a pair (a_id, b_id)
                a_id = int(payload.get("a_id", 0))
                b_id = int(payload.get("b_id", 0))
                if not a_id or not b_id:
                    return {"ok": False, "error": "mark_fp requires a_id + b_id"}
                # WS3a single-write-path: same helper as /clear_suspect (also
                # NULLs suspect_detected_at + stamps updated_at — the old inline
                # copy left the detection timestamp dangling).
                conn = get_db()
                try:
                    cleared, noop, not_found = _clear_suspect_ids(conn, [a_id, b_id])
                    conn.commit()
                    return {"ok": True, "cleared": cleared, "noop": noop,
                            "not_found": not_found}
                finally:
                    conn.close()

            elif action == "supersede":
                loser_id = int(payload.get("loser_id", 0))
                winner_id = int(payload.get("winner_id", 0))
                reason = payload.get("reason") or body.note or "operator decision"
                if not loser_id or not winner_id:
                    return {"ok": False, "error": "supersede requires loser_id + winner_id"}
                # WS3a single-write-path: same helper as /supersede (validation,
                # canonical ISO timestamps, arena_events audit row, provenance).
                conn = get_db()
                try:
                    result = _do_supersede(
                        conn, loser_id, winner_id, reason,
                        provenance="operator",
                        session_id=body.session_id,
                    )
                    if result.get("ok"):
                        conn.commit()
                    return result
                finally:
                    conn.close()

            elif action == "promote":
                iid = int(payload.get("insight_id", 0))
                content = payload.get("content")
                if not iid:
                    return {"ok": False, "error": "promote requires insight_id"}
                # WS3a: single write path — same helper as /promote_insight
                # (audit row, tags/provenance carry-over, scoring seed constant).
                conn = get_db()
                try:
                    result = _do_promote_insight(
                        conn, iid, actor=body.actor or "operator",
                        content=content, session_id=payload.get("session_id"),
                    )
                    if result.get("ok"):
                        conn.commit()
                    return result
                finally:
                    conn.close()

            elif action == "decay_drop":
                iid = int(payload.get("insight_id", 0))
                if not iid:
                    return {"ok": False, "error": "decay_drop requires insight_id"}
                # Mark insight stale by dropping confidence (scoring SSOT delta)
                conn = get_db()
                try:
                    row = conn.execute("SELECT confidence FROM insights WHERE id=?", (iid,)).fetchone()
                    if not row:
                        return {"ok": False, "error": f"insight {iid} not found"}
                    new_conf = max(0.0, (row["confidence"] or 0.5) - scoring.VERIFY_INSIGHT_DOWN)
                    conn.execute(
                        "UPDATE insights SET confidence=?, verified_at=?, updated_at=? WHERE id=?",
                        (new_conf, _utcnow_iso(), _utcnow_iso(), iid),
                    )
                    conn.commit()
                    return {"ok": True, "insight_id": iid, "new_confidence": round(new_conf, 2)}
                finally:
                    conn.close()

            elif action == "drift_resolve":
                # Resolve a drift: either supersede or update content
                iid = int(payload.get("insight_id", 0))
                mode = payload.get("mode", "update")  # 'update' | 'supersede'
                if not iid:
                    return {"ok": False, "error": "drift_resolve requires insight_id"}
                conn = get_db()
                try:
                    if mode == "supersede":
                        winner_id = int(payload.get("winner_id", 0))
                        if not winner_id:
                            return {"ok": False, "error": "supersede mode requires winner_id"}
                        # WS3a single-write-path: same helper as /supersede.
                        result = _do_supersede(
                            conn, iid, winner_id, body.note or "drift resolved",
                            provenance="drift",
                            session_id=body.session_id,
                        )
                        if not result.get("ok"):
                            return result
                    else:
                        new_content = payload.get("new_content", "")
                        if not new_content:
                            return {"ok": False, "error": "update mode requires new_content"}
                        new_content = new_content[:2000]
                        conn.execute(
                            "UPDATE insights SET content=?, updated_at=? WHERE id=?",
                            (new_content, _utcnow_iso(), iid),
                        )
                        # Re-enrich — same single enrichment path as
                        # /update_insight and /save_insight: fresh embedding,
                        # entity_links (stale ones deleted first), falsifier.
                        try:
                            proj_row = conn.execute(
                                "SELECT project FROM insights WHERE id = ?", (iid,)
                            ).fetchone()
                            conn.execute("DELETE FROM entity_links WHERE insight_id = ?", (iid,))
                            conn.commit()
                            _enrich_insight(conn, iid, new_content,
                                            proj_row["project"] if proj_row else None)
                        except Exception as e:
                            logger.warning("drift_resolve re-enrichment failed for #%s: %s", iid, e)
                    conn.commit()
                    return {"ok": True, "insight_id": iid, "mode": mode}
                finally:
                    conn.close()

            else:
                return {"ok": False, "error": f"unknown action: {action}"}

        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    result = await loop.run_in_executor(None, _do)
    # Log every attempt, success or failure.
    audit_id = await loop.run_in_executor(
        None, _audit_log,
        body.actor, body.action, body.target_class, body.target_id,
        body.payload or {}, result, body.note, body.session_id,
    )

    _log_request(request, "/operator/action", (time.perf_counter() - t0) * 1000,
                 action=body.action)

    # v2.6 SSE — broadcast operator_action
    await _sse_publish("operator_action", {
        "audit_id": audit_id,
        "action": body.action,
        "actor": body.actor,
        "target_class": body.target_class or "",
        "target_id": str(body.target_id) if body.target_id else "",
        "ok": result.get("ok", False),
    })

    return JSONResponse(content={
        "ok": result.get("ok", False),
        "audit_id": audit_id,
        "action": body.action,
        "result": result,
    })


@app.get("/operator/audit_log")
async def operator_audit_log(
    request: Request,
    limit: int = 50,
    action: Optional[str] = None,
    target_class: Optional[str] = None,
    target_id: Optional[str] = None,
):
    """Phase 7 — read operator_audit_log.

    Filters (all optional):
      action       — single action name (e.g. 'supersede')
      target_class — single class
      target_id    — exact target id

    Returns {ok, count, rows: [...]}
    """
    limit = min(max(int(limit), 1), 500)

    def _do():
        conn = get_db()
        where = []
        args: list = []
        if action:
            where.append("action = ?")
            args.append(action)
        if target_class:
            where.append("target_class = ?")
            args.append(target_class)
        if target_id:
            where.append("target_id = ?")
            args.append(target_id)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT id, created_at, actor, action, target_class, target_id, "
            f"       payload, result, note, session_id "
            f"FROM operator_audit_log {clause} "
            f"ORDER BY id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Parse JSON columns for easier UI rendering
            for k in ("payload", "result"):
                try:
                    d[k] = json.loads(d[k]) if d[k] else None
                except Exception:
                    pass
            out.append(d)
        return out

    rows = await asyncio.get_event_loop().run_in_executor(None, _do)
    return JSONResponse(content={"ok": True, "count": len(rows), "rows": rows})


@app.get("/search")
async def search_fanout(request: Request, q: str, limit: int = 8):
    """Phase v2.5 — global search fan-out for the Command Palette.

    Searches across insights, principles, sessions, entities, recall_events,
    and arena_events in parallel.  Returns a flat ranked list of matches with
    `kind`, `id`, `title`, `subtitle` per row — the palette renders these as
    one unified picker.

    Numeric input is treated as a direct ID lookup across {insight, principle,
    session, recall, arena, supersede, contradiction, epic} — the palette gets
    a "go to insight #N" shortcut row as the top result if `q` is numeric.

    Query params:
      q     — search string (required)
      limit — per-category cap, default 8

    Returns:
      {ok, q, results: [{kind, id, title, subtitle, score?}], total}
    """
    t0 = time.perf_counter()
    q = q.strip()
    if not q:
        return JSONResponse(content={"ok": True, "q": "", "results": [], "total": 0})

    def _do():
        conn = get_db()
        results: list[dict] = []

        # Numeric → ID shortcut row(s).  Surface insight + principle by that
        # exact id at the top.
        if q.isdigit():
            qid = int(q)
            try:
                ins = conn.execute(
                    "SELECT id, substr(content,1,80) AS preview, project, type FROM insights WHERE id=?",
                    (qid,),
                ).fetchone()
                if ins:
                    results.append({
                        "kind": "insight",
                        "id": ins["id"],
                        "title": f"insight #{ins['id']}",
                        "subtitle": f"{ins['type']} · {ins['project']} · {ins['preview']}",
                        "score": 1.0,
                    })
            except Exception:
                pass
            try:
                pr = conn.execute(
                    "SELECT id, substr(content,1,80) AS preview, project FROM principles WHERE id=?",
                    (qid,),
                ).fetchone()
                if pr:
                    results.append({
                        "kind": "principle",
                        "id": pr["id"],
                        "title": f"principle #{pr['id']}",
                        "subtitle": f"{pr['project']} · {pr['preview']}",
                        "score": 1.0,
                    })
            except Exception:
                pass

        # Insights — FTS5 if available, fall back to LIKE
        try:
            rows = conn.execute(
                "SELECT i.id, substr(i.content,1,80) AS preview, i.project, i.type, "
                "       bm25(insights_fts) AS rank "
                "FROM insights_fts JOIN insights i ON i.id = insights_fts.rowid "
                "WHERE insights_fts MATCH ? AND i.superseded_by IS NULL "
                "ORDER BY rank LIMIT ?",
                (q, limit),
            ).fetchall()
        except Exception:
            rows = conn.execute(
                "SELECT id, substr(content,1,80) AS preview, project, type "
                "FROM insights WHERE content LIKE ? AND superseded_by IS NULL "
                "ORDER BY confidence DESC LIMIT ?",
                (f"%{q}%", limit),
            ).fetchall()
        for r in rows:
            results.append({
                "kind": "insight",
                "id": r["id"],
                "title": f"insight #{r['id']}: {r['preview']}",
                "subtitle": f"{r['type']} · {r['project']}",
            })

        # Principles
        try:
            rows = conn.execute(
                "SELECT id, substr(content,1,80) AS preview, project "
                "FROM principles WHERE content LIKE ? ORDER BY confidence DESC LIMIT ?",
                (f"%{q}%", limit),
            ).fetchall()
            for r in rows:
                results.append({
                    "kind": "principle",
                    "id": r["id"],
                    "title": f"principle #{r['id']}: {r['preview']}",
                    "subtitle": f"{r['project']}",
                })
        except Exception:
            pass

        # Sessions — by id, role, or task_summary substring
        try:
            rows = conn.execute(
                "SELECT DISTINCT session_id, role, MAX(created_at) AS latest_ts, "
                "       project, task_summary "
                "FROM token_ledger WHERE session_id IS NOT NULL AND session_id != '' "
                "  AND (session_id LIKE ? OR role LIKE ? OR task_summary LIKE ?) "
                "GROUP BY session_id ORDER BY latest_ts DESC LIMIT ?",
                (f"%{q}%", f"%{q}%", f"%{q}%", limit),
            ).fetchall()
            for r in rows:
                short = (r["session_id"] or "")[:12]
                results.append({
                    "kind": "session",
                    "id": r["session_id"],
                    "title": f"session {short}…",
                    "subtitle": f"{r['role'] or '?'} · {r['project'] or '?'} · {(r['task_summary'] or '')[:60]}",
                })
        except Exception:
            pass

        # Entities (port/ip/file/service/etc.) — fuzzy match across types
        try:
            rows = conn.execute(
                "SELECT DISTINCT entity_type, entity AS entity_value, COUNT(*) AS mentions "
                "FROM entity_links WHERE entity LIKE ? "
                "GROUP BY entity_type, entity ORDER BY mentions DESC LIMIT ?",
                (f"%{q}%", limit),
            ).fetchall()
            for r in rows:
                results.append({
                    "kind": "entity",
                    "id": f"{r['entity_type']}/{r['entity_value']}",
                    "title": f"{r['entity_type']}: {r['entity_value']}",
                    "subtitle": f"{r['mentions']} mention{'s' if r['mentions'] != 1 else ''}",
                })
        except Exception:
            pass

        # Epics — by tag substring
        try:
            rows = conn.execute(
                "SELECT epic_tag, COUNT(*) AS count FROM insights "
                "WHERE epic_tag IS NOT NULL AND epic_tag != '' AND epic_tag LIKE ? "
                "GROUP BY epic_tag ORDER BY count DESC LIMIT ?",
                (f"%{q}%", limit),
            ).fetchall()
            for r in rows:
                results.append({
                    "kind": "epic",
                    "id": r["epic_tag"],
                    "title": f"epic: {r['epic_tag']}",
                    "subtitle": f"{r['count']} insight{'s' if r['count'] != 1 else ''}",
                })
        except Exception:
            pass

        return {"ok": True, "q": q, "results": results, "total": len(results)}

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, "/search", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/operator/diff_since")
async def operator_diff_since(request: Request, since: str, project: Optional[str] = None):
    """Phase v2.4 — "while you were away" diff aggregator.

    Given an ISO timestamp `since`, returns deltas across the operator-visible
    surfaces.  Powers the home-page DiffSinceCard that surfaces new
    contradictions, decay candidates, arena verdicts, principles, session ends,
    and operator actions since the operator's last visit.

    Query params:
      since   — ISO timestamp (e.g. 2026-05-27T22:00:00Z) — required
      project — optional filter scope

    Returns:
      {ok, since, deltas: {
        contradictions: {count, samples: [...]},
        decay:          {count, samples: [...]},
        arena_verdicts: {count, samples: [...]},
        principles:     {count, samples: [...]},
        session_ends:   {count, samples: [...]},
        operator_actions: {count, samples: [...]},
      }}
    """
    t0 = time.perf_counter()

    # Validate ISO format. Reject obviously invalid input to fail loudly.
    # Also normalize: SQLite's `datetime('now')` produces 'YYYY-MM-DD HH:MM:SS'
    # (space separator) — if the operator passes the T-form, string-compare
    # against stored values fails (space=0x20 < T=0x54).  Normalize to space.
    try:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
        since_normalized = parsed.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return JSONResponse(
            content={"ok": False, "error": f"invalid `since` ISO timestamp: {since}"},
            status_code=400,
        )
    since = since_normalized

    def _do():
        conn = get_db()
        proj_clause = "AND project = ?" if project else ""
        proj_args: tuple = (project,) if project else ()

        # ── New contradictions (insights flagged suspect_of since `since`) ──
        contrad_rows = conn.execute(
            f"SELECT id AS suspect_id, suspect_of AS contradicts_id, "
            f"       substr(content, 1, 80) AS preview, suspect_reason, project "
            f"FROM insights "
            f"WHERE suspect_of IS NOT NULL "
            f"  AND suspect_detected_at IS NOT NULL "
            f"  AND suspect_detected_at >= ? {proj_clause} "
            f"ORDER BY suspect_detected_at DESC LIMIT 10",
            (since, *proj_args),
        ).fetchall()
        contrad_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM insights "
            f"WHERE suspect_of IS NOT NULL "
            f"  AND suspect_detected_at IS NOT NULL "
            f"  AND suspect_detected_at >= ? {proj_clause}",
            (since, *proj_args),
        ).fetchone()["c"]

        # ── New decay candidates (insights that crossed into decay window) ──
        # An insight becomes a decay candidate once it satisfies: superseded_by
        # IS NULL AND conf<0.5 AND age>30d AND no recall in 60d.  Approximate
        # "newly entered decay" as insights whose verified_at >= since AND
        # confidence < 0.5 — these are recent confidence drops.
        decay_rows = conn.execute(
            f"SELECT id, substr(content, 1, 80) AS preview, project, confidence "
            f"FROM insights "
            f"WHERE superseded_by IS NULL "
            f"  AND verified_at IS NOT NULL AND verified_at >= ? "
            f"  AND confidence < 0.5 {proj_clause} "
            f"ORDER BY verified_at DESC LIMIT 10",
            (since, *proj_args),
        ).fetchall()
        decay_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM insights "
            f"WHERE superseded_by IS NULL "
            f"  AND verified_at IS NOT NULL AND verified_at >= ? "
            f"  AND confidence < 0.5 {proj_clause}",
            (since, *proj_args),
        ).fetchone()["c"]

        # ── New arena verdicts since ─────────────────────────────────────────
        # arena_events uses `ts` (not created_at)
        arena_rows = conn.execute(
            f"SELECT id, ts AS created_at, strategy, verdict, project "
            f"FROM arena_events WHERE ts >= ? {proj_clause} "
            f"ORDER BY id DESC LIMIT 10",
            (since, *proj_args),
        ).fetchall()
        arena_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM arena_events WHERE ts >= ? {proj_clause}",
            (since, *proj_args),
        ).fetchone()["c"]

        # ── New principles since ─────────────────────────────────────────────
        principle_rows = conn.execute(
            f"SELECT id, created_at, substr(content, 1, 80) AS preview, project "
            f"FROM principles WHERE created_at >= ? {proj_clause} "
            f"ORDER BY id DESC LIMIT 10",
            (since, *proj_args),
        ).fetchall()
        principle_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM principles WHERE created_at >= ? {proj_clause}",
            (since, *proj_args),
        ).fetchone()["c"]

        # ── Session ends (token_ledger entries with new session_id) ──────────
        # token_ledger row written at session end — its created_at IS the
        # session-end timestamp.
        sess_rows = conn.execute(
            f"SELECT session_id, created_at, tokens_in, tokens_out, "
            f"       recall_hits, recall_misses, task_summary, project "
            f"FROM token_ledger "
            f"WHERE created_at >= ? AND session_id IS NOT NULL AND session_id != '' "
            f"  {proj_clause} ORDER BY id DESC LIMIT 10",
            (since, *proj_args),
        ).fetchall()
        sess_count = conn.execute(
            f"SELECT COUNT(DISTINCT session_id) AS c FROM token_ledger "
            f"WHERE created_at >= ? AND session_id IS NOT NULL AND session_id != '' {proj_clause}",
            (since, *proj_args),
        ).fetchone()["c"]

        # ── Operator actions since (audit log) ───────────────────────────────
        action_rows = conn.execute(
            "SELECT id, created_at, actor, action, target_class, target_id "
            "FROM operator_audit_log WHERE created_at >= ? "
            "ORDER BY id DESC LIMIT 10",
            (since,),
        ).fetchall()
        action_count = conn.execute(
            "SELECT COUNT(*) AS c FROM operator_audit_log WHERE created_at >= ?",
            (since,),
        ).fetchone()["c"]

        return {
            "ok": True,
            "since": since,
            "project": project,
            "deltas": {
                "contradictions":   {"count": contrad_count,   "samples": [dict(r) for r in contrad_rows]},
                "decay":            {"count": decay_count,     "samples": [dict(r) for r in decay_rows]},
                "arena_verdicts":   {"count": arena_count,     "samples": [dict(r) for r in arena_rows]},
                "principles":       {"count": principle_count, "samples": [dict(r) for r in principle_rows]},
                "session_ends":     {"count": sess_count,      "samples": [dict(r) for r in sess_rows]},
                "operator_actions": {"count": action_count,    "samples": [dict(r) for r in action_rows]},
            },
        }

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, "/operator/diff_since", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/operator/queues")
async def operator_queues(request: Request, project: Optional[str] = None):
    """Phase 7 — aggregate operator work queues at a glance.

    Returns counts + small samples for:
      contradictions  — audit_contradictions queue
      decay           — high-age + zero-recall + low-confidence
      drift           — placeholder for now (needs pattern-match)
      recent_actions  — last 5 operator_audit_log entries

    Used by the WorkflowsPage as the home screen.
    """
    def _do():
        conn = get_db()
        # Contradictions
        proj_clause = "AND i.project = ?" if project else ""
        proj_args: tuple = (project,) if project else ()
        contradiction_rows = conn.execute(
            f"SELECT i.id AS suspect_id, i.suspect_of AS contradicts_id, "
            f"       substr(i.content, 1, 80) AS suspect_preview, "
            f"       i.suspect_reason, i.project "
            f"FROM insights i WHERE i.suspect_of IS NOT NULL "
            f"AND i.superseded_by IS NULL {proj_clause} "
            f"ORDER BY i.id DESC LIMIT 10",
            proj_args,
        ).fetchall()
        contradiction_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM insights i "
            f"WHERE i.suspect_of IS NOT NULL AND i.superseded_by IS NULL {proj_clause}",
            proj_args,
        ).fetchone()["c"]

        # Decay candidates (same query as /query/roi)
        decay_rows = conn.execute(
            f"SELECT id, type, substr(content, 1, 80) AS preview, project, confidence, "
            f"       julianday('now') - julianday(created_at) AS age_days "
            f"FROM insights "
            f"WHERE superseded_by IS NULL AND confidence < 0.5 "
            f"  AND (last_recalled_at IS NULL OR "
            f"       julianday('now') - julianday(last_recalled_at) > 60) "
            f"  AND julianday('now') - julianday(created_at) > 30 "
            f"  {('AND project = ?' if project else '')} "
            f"ORDER BY age_days DESC LIMIT 10",
            proj_args,
        ).fetchall()
        decay_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM insights "
            f"WHERE superseded_by IS NULL AND confidence < 0.5 "
            f"  AND (last_recalled_at IS NULL OR "
            f"       julianday('now') - julianday(last_recalled_at) > 60) "
            f"  AND julianday('now') - julianday(created_at) > 30 "
            f"  {('AND project = ?' if project else '')}",
            proj_args,
        ).fetchone()["c"]

        # Recent actions
        recent_rows = conn.execute(
            "SELECT id, created_at, actor, action, target_class, target_id "
            "FROM operator_audit_log ORDER BY id DESC LIMIT 5"
        ).fetchall()

        decay_out = []
        for r in decay_rows:
            d = dict(r)
            d["age_days"] = round(d.get("age_days") or 0, 1)
            decay_out.append(d)

        return {
            "ok": True,
            "project": project,
            "contradictions": {
                "count": contradiction_count,
                "samples": [dict(r) for r in contradiction_rows],
            },
            "decay": {
                "count": decay_count,
                "samples": decay_out,
            },
            "recent_actions": [dict(r) for r in recent_rows],
        }

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    return JSONResponse(content=result)


# ── Shared edge builder (Phase 6.2) ─────────────────────────────────────────
#
# _emit_node_edges: single source of truth for all 6 edge kinds.
# Called by BOTH /query/graph (focal/BFS) and /query/graph_global (structural
# backbone).  Both endpoints pass their local _add_node/_add_edge closures so
# node/edge deduplication remains in the caller.
#
# Parameters:
#   conn       — sqlite3 connection (row_factory = sqlite3.Row)
#   kls        — node class: "insight" | "principle" | "entity" | "epic" | "session"
#   id_        — raw id string (numeric str for insight/principle; "type/val" for
#                entity; tag str for epic; session_uuid for session)
#   add_node   — closure: (kls, id_, label, **extras) → node_key  str
#   add_edge   — closure: (from_key, to_key, etype, label="") → None
#   frontier   — optional list; when provided (BFS mode), newly discovered
#                neighbour (kls, id_, hop+1) tuples are appended so the caller
#                can continue BFS.  When None (global mode), only edges/nodes
#                among the existing set are emitted (no BFS expansion).
#   hop        — current BFS hop depth (used only when frontier is not None)
#   want       — optional set of edge kind strings to emit; None = emit all 6.

def _emit_node_edges(
    conn,
    kls: str,
    id_: str,
    add_node,
    add_edge,
    *,
    frontier=None,
    hop: int = 0,
    want=None,
):
    """Emit all edges for one node.  Returns the node's key (str) or None if
    the node could not be loaded."""

    def _want(kind: str) -> bool:
        return want is None or kind in want

    def _push(k, i, h):
        if frontier is not None:
            frontier.append((k, i, h))

    if kls == "insight":
        try:
            iid = int(id_)
        except ValueError:
            return None
        row = conn.execute(
            "SELECT id, type, content, project, confidence, "
            "       superseded_by, suspect_of, epic_tag, session_id "
            "FROM insights WHERE id = ?",
            (iid,),
        ).fetchone()
        if not row:
            return None
        row = dict(row)
        label = (row.get("content") or f"insight #{iid}")[:80]
        this_key = add_node(
            "insight", str(iid), label,
            project=row.get("project"),
            confidence=row.get("confidence"),
            insight_type=row.get("type"),
        )

        # ── supersede (bidirectional) ─────────────────────────────────────
        if _want("supersede"):
            # this insight as LOSER — winner is row["superseded_by"]
            if row.get("superseded_by"):
                wid = int(row["superseded_by"])
                wrow = conn.execute(
                    "SELECT id, content, project, confidence FROM insights WHERE id = ?",
                    (wid,),
                ).fetchone()
                if wrow:
                    wrow = dict(wrow)
                    wkey = add_node(
                        "insight", str(wid),
                        (wrow.get("content") or f"insight #{wid}")[:80],
                        project=wrow.get("project"),
                        confidence=wrow.get("confidence"),
                    )
                    add_edge(this_key, wkey, "supersede", "superseded_by")
                    _push("insight", str(wid), hop + 1)

            # this insight as WINNER — find its losers
            for loser_row in conn.execute(
                "SELECT id, content, project, confidence FROM insights "
                "WHERE superseded_by = ?",
                (iid,),
            ).fetchall():
                loser_row = dict(loser_row)
                lid = loser_row["id"]
                lkey = add_node(
                    "insight", str(lid),
                    (loser_row.get("content") or f"insight #{lid}")[:80],
                    project=loser_row.get("project"),
                    confidence=loser_row.get("confidence"),
                )
                add_edge(lkey, this_key, "supersede", "superseded_by")
                _push("insight", str(lid), hop + 1)

        # ── contradiction (suspect_of) ────────────────────────────────────
        if _want("contradiction") and row.get("suspect_of"):
            sid = int(row["suspect_of"])
            srow = conn.execute(
                "SELECT id, content, project, confidence FROM insights WHERE id = ?",
                (sid,),
            ).fetchone()
            if srow:
                srow = dict(srow)
                skey = add_node(
                    "insight", str(sid),
                    (srow.get("content") or f"insight #{sid}")[:80],
                    project=srow.get("project"),
                    confidence=srow.get("confidence"),
                )
                add_edge(this_key, skey, "contradiction", "suspect_of")
                _push("insight", str(sid), hop + 1)

        # ── epic membership ───────────────────────────────────────────────
        if _want("epic_member") and row.get("epic_tag"):
            etag = row["epic_tag"]
            ekey = add_node("epic", etag, f"epic: {etag}")
            add_edge(this_key, ekey, "epic_member")
            # siblings in same epic (up to 5)
            for sib in conn.execute(
                "SELECT id, content, project, confidence FROM insights "
                "WHERE epic_tag = ? AND id != ? "
                "AND superseded_by IS NULL LIMIT 5",
                (etag, iid),
            ).fetchall():
                sibrow = dict(sib)
                skey = add_node(
                    "insight", str(sibrow["id"]),
                    (sibrow.get("content") or "")[:80],
                    project=sibrow.get("project"),
                    confidence=sibrow.get("confidence"),
                )
                add_edge(skey, ekey, "epic_member")

        # ── entity links ──────────────────────────────────────────────────
        if _want("entity_link"):
            for elink in conn.execute(
                "SELECT entity_type, entity FROM entity_links "
                "WHERE insight_id = ? LIMIT 5",
                (iid,),
            ).fetchall():
                elink = dict(elink)
                etype = elink["entity_type"]
                eval_ = elink["entity"]
                e_key = add_node("entity", f"{etype}/{eval_}", f"{etype}:{eval_}")
                add_edge(this_key, e_key, "entity_link", etype)
                _push("entity", f"{etype}/{eval_}", hop + 1)

        # ── session membership ────────────────────────────────────────────
        if _want("session_member") and row.get("session_id"):
            sess_id = row["session_id"]
            sess_label = sess_id[:12] + ("…" if len(sess_id) > 12 else "")
            skey = add_node("session", sess_id, f"session {sess_label}")
            add_edge(this_key, skey, "session_member")

        # ── promoted_to principle ─────────────────────────────────────────
        if _want("promoted_to"):
            promo_rows = conn.execute(
                "SELECT id, content, confidence FROM principles "
                "WHERE ',' || source_insights || ',' LIKE ?",
                (f"%,{iid},%",),
            ).fetchall()
            for pr in promo_rows:
                pdic = dict(pr)
                pkey = add_node(
                    "principle", str(pdic["id"]),
                    (pdic.get("content") or "")[:80],
                    confidence=pdic.get("confidence"),
                )
                add_edge(this_key, pkey, "promoted_to", "promoted")

        return this_key

    elif kls == "principle":
        try:
            pid = int(id_)
        except ValueError:
            return None
        prow = conn.execute(
            "SELECT id, content, confidence, project, source_insights "
            "FROM principles WHERE id = ?",
            (pid,),
        ).fetchone()
        if not prow:
            return None
        prow = dict(prow)
        label = (prow.get("content") or f"principle #{pid}")[:80]
        this_key = add_node(
            "principle", str(pid), label,
            project=prow.get("project"),
            confidence=prow.get("confidence"),
        )

        # ── source insights → promoted_to ─────────────────────────────────
        if _want("promoted_to") and prow.get("source_insights"):
            src_ids = [s.strip() for s in str(prow["source_insights"]).split(",") if s.strip()]
            for sid in src_ids[:10]:
                try:
                    siid = int(sid)
                except ValueError:
                    continue
                srow = conn.execute(
                    "SELECT id, content, project, confidence FROM insights WHERE id = ?",
                    (siid,),
                ).fetchone()
                if srow:
                    srow = dict(srow)
                    skey = add_node(
                        "insight", str(siid),
                        (srow.get("content") or "")[:80],
                        project=srow.get("project"),
                        confidence=srow.get("confidence"),
                    )
                    add_edge(skey, this_key, "promoted_to", "source")
                    _push("insight", str(siid), hop + 1)

        return this_key

    elif kls == "entity":
        if "/" not in id_:
            return None
        etype, eval_ = id_.split("/", 1)
        this_key = add_node("entity", id_, f"{etype}:{eval_}")

        if frontier is not None:  # BFS mode only: expand to member insights
            for r in conn.execute(
                "SELECT i.id, i.content, i.project, i.confidence "
                "FROM entity_links el JOIN insights i ON i.id = el.insight_id "
                "WHERE el.entity_type = ? AND el.entity = ? LIMIT 10",
                (etype, eval_),
            ).fetchall():
                irow = dict(r)
                ikey = add_node(
                    "insight", str(irow["id"]),
                    (irow.get("content") or "")[:80],
                    project=irow.get("project"),
                    confidence=irow.get("confidence"),
                )
                add_edge(ikey, this_key, "entity_link", etype)

        return this_key

    elif kls == "epic":
        this_key = add_node("epic", id_, f"epic: {id_}")

        if frontier is not None:  # BFS mode only
            for r in conn.execute(
                "SELECT id, content, project, confidence FROM insights "
                "WHERE epic_tag = ? AND superseded_by IS NULL LIMIT 50",
                (id_,),
            ).fetchall():
                irow = dict(r)
                ikey = add_node(
                    "insight", str(irow["id"]),
                    (irow.get("content") or "")[:80],
                    project=irow.get("project"),
                    confidence=irow.get("confidence"),
                )
                add_edge(ikey, this_key, "epic_member")

        return this_key

    elif kls == "session":
        this_key = add_node("session", id_, f"session {id_[:12]}…")

        if frontier is not None:  # BFS mode only
            for r in conn.execute(
                "SELECT id, content, project, confidence FROM insights "
                "WHERE session_id = ? LIMIT 50",
                (id_,),
            ).fetchall():
                irow = dict(r)
                ikey = add_node(
                    "insight", str(irow["id"]),
                    (irow.get("content") or "")[:80],
                    project=irow.get("project"),
                    confidence=irow.get("confidence"),
                )
                add_edge(ikey, this_key, "session_member")

        return this_key

    return None


@app.get("/query/graph")
async def query_graph(
    request: Request,
    focus_class: str,
    focus_id: str,
    depth: int = 2,
    max_nodes: int = 60,
):
    """Phase 6.1 — knowledge graph around a focal object.

    BFS expand from focal node up to `depth` hops via these edges:
      - insights.superseded_by   → supersede edge
      - insights.suspect_of      → contradiction edge
      - insights.epic_tag        → epic membership edge
      - entity_links             → insight↔entity, principle↔entity

    Returns:
      nodes: [{id, type, label, project?, confidence?}]
      edges: [{from, to, type, label?}]
      focal: {class, id}
      meta:  {nodes_returned, edges_returned, depth, hit_cap}

    node id format: "{class}:{id}" (e.g. "insight:2453", "entity:port/8786")
    """
    t0 = time.perf_counter()
    depth = min(max(int(depth), 1), 3)
    max_nodes = min(max(int(max_nodes), 5), 200)

    def _do():
        conn = get_db()
        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        seen_edges: set[tuple] = set()
        frontier: list[tuple[str, str, int]] = [(focus_class, focus_id, 0)]
        visited: set[tuple[str, str]] = set()

        def _add_node(kls: str, id_: str, label: str, **extras) -> str:
            key = f"{kls}:{id_}"
            if key not in nodes:
                nodes[key] = {
                    "id": key,
                    "type": kls,
                    "label": label[:80],
                    "url": f"/o/{kls}/{id_}",
                    **extras,
                }
            return key

        def _add_edge(from_key: str, to_key: str, etype: str, label: str = "") -> None:
            tup = (from_key, to_key, etype)
            if tup in seen_edges:
                return
            if from_key == to_key:
                return
            seen_edges.add(tup)
            edges.append({"from": from_key, "to": to_key, "type": etype, "label": label})

        def _load_insight(iid: int) -> Optional[dict]:
            row = conn.execute(
                "SELECT id, type, content, project, confidence, "
                "       superseded_by, suspect_of, epic_tag, session_id "
                "FROM insights WHERE id = ?",
                (iid,),
            ).fetchone()
            return dict(row) if row else None

        def _load_principle(pid: int) -> Optional[dict]:
            row = conn.execute(
                "SELECT id, content, confidence, project, source_insights "
                "FROM principles WHERE id = ?",
                (pid,),
            ).fetchone()
            return dict(row) if row else None

        while frontier and len(nodes) < max_nodes:
            kls, id_, hop = frontier.pop(0)
            if (kls, id_) in visited:
                continue
            visited.add((kls, id_))

            # Pass frontier only when we haven't reached the depth limit yet,
            # so newly discovered neighbours get enqueued for expansion.
            next_frontier = frontier if hop < depth else None
            _emit_node_edges(
                conn, kls, id_,
                _add_node, _add_edge,
                frontier=next_frontier,
                hop=hop,
            )

        return {
            "ok": True,
            "focal": {"class": focus_class, "id": focus_id},
            "nodes": list(nodes.values()),
            "edges": edges,
            "meta": {
                "nodes_returned": len(nodes),
                "edges_returned": len(edges),
                "depth": depth,
                "hit_cap": len(nodes) >= max_nodes,
            },
        }

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, "/query/graph", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/query/graph_global")
async def query_graph_global(
    request: Request,
    project: Optional[str] = None,
    type: Optional[str] = None,
    min_confidence: Optional[float] = None,
    since: Optional[str] = None,
    tag: Optional[str] = None,
    entity_type: Optional[str] = None,
    epic_tag: Optional[str] = None,
    role: Optional[str] = None,
    include_supersedes: bool = True,
    include_contradictions: bool = True,
    include_entities: bool = True,
    include_principles: bool = True,
    include_epics: bool = True,
    max_nodes: int = 800,
    view: str = "overview",
):
    """v4 — Global knowledge-graph view with enterprise filters.

    Unlike /query/graph (which BFS-expands from a focal node), this endpoint
    returns the FULL graph snapshot subject to filter constraints.  The
    structural skeleton (all supersede/contradiction/promoted_to/session/epic
    edges) is NEVER truncated regardless of max_nodes — those nodes are seeded
    first.  max_nodes controls only the top-up of additional degree-ranked
    insights and hub entities beyond the structural floor.  Default 800 keeps
    the overview render-friendly (structural floor ~532 for the infra DB plus a
    hub-entity tail); the elided tail is reported honestly in meta.

    view (Phase: graph-display-overhaul):
      overview (default) — the knowledge-evolution BACKBONE only:
          * principles + supersede / contradiction / promoted_to chains and
            their insight endpoints (complete structural skeleton);
          * sessions & epics rendered as COLLAPSED cluster nodes — ONE node per
            session/epic carrying `collapsed: true` + `member_count`, with the
            individual session_member / epic_member edges OMITTED (this removes
            the highest-cardinality clutter; expand a cluster on-demand via the
            focal endpoint focus_class=session|epic);
          * entity nodes OFF (no entity_link nodes/edges).
        Target ≤ ~250 nodes — render-friendly for the canvas force graph.
      full — the complete behaviour: all structural edges + hub entities + all
        session_member / epic_member member edges (the legacy default).

    Filters (all optional, AND-combined):
      project          — scope to one project
      type             — insight type (gotcha, pattern, architecture, …)
      min_confidence   — confidence >= N
      since            — created_at >= ISO ts
      tag              — substring match in tags column
      entity_type      — only include entities of this type
      epic_tag         — exact epic match
      role             — coordinator | subagent | operator
      include_*        — toggle edge categories
      max_nodes        — soft cap; structural floor always emitted regardless

    Returns same envelope as /query/graph with extra meta.filters_applied.
    """
    t0 = time.perf_counter()
    max_nodes = max(int(max_nodes), 5)
    view = (view or "overview").lower()
    if view not in ("overview", "full"):
        view = "overview"
    overview = view == "overview"
    # In overview, entities are forced OFF regardless of the include_entities
    # flag (the backbone view excludes the entity layer by design).
    if overview:
        include_entities = False

    def _do():  # noqa: C901 (complexity OK — single responsibility)
        conn = get_db()
        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        seen_edges: set[tuple] = set()

        def _add_node(kls: str, id_: str, label: str, **extras) -> str:
            key = f"{kls}:{id_}"
            if key not in nodes:
                nodes[key] = {"id": key, "type": kls,
                              "label": label[:120] if label else "", **extras}
            return key

        def _add_edge(src: str, dst: str, etype: str, label: str = ""):
            if src == dst:
                return
            sig = (src, dst, etype)
            if sig in seen_edges:
                return
            seen_edges.add(sig)
            edges.append({"from": src, "to": dst, "type": etype, "label": label})

        # ── Build filter WHERE clause (same for insights + totals count) ──
        where: list[str] = ["status = 'active' OR status IS NULL"]
        args: list = []
        if project:
            where.append("project = ?")
            args.append(project)
        if type:
            where.append("type = ?")
            args.append(type)
        if min_confidence is not None:
            where.append("confidence >= ?")
            args.append(float(min_confidence))
        if since:
            where.append("created_at >= ?")
            args.append(since)
        if tag:
            where.append("tags LIKE ?")
            args.append(f"%{tag}%")
        if epic_tag:
            where.append("epic_tag = ?")
            args.append(epic_tag)
        if role:
            where.append("role = ?")
            args.append(role)
        where_sql = " AND ".join(where)

        # Structural-scope clause: same filters as where_sql but WITHOUT the
        # status predicate.  Supersede/contradiction chains are structural
        # history — a stale/superseded endpoint (e.g. a loser insight) must
        # still be seeded so its edge appears.  Excluding stale rows here was
        # the root cause of supersede 29/30 (the all-stale pair loser=70 →
        # winner=129 was dropped because loser 70 was status='stale').
        struct_where: list[str] = []
        struct_args: list = []
        if project:
            struct_where.append("project = ?")
            struct_args.append(project)
        if type:
            struct_where.append("type = ?")
            struct_args.append(type)
        if min_confidence is not None:
            struct_where.append("confidence >= ?")
            struct_args.append(float(min_confidence))
        if since:
            struct_where.append("created_at >= ?")
            struct_args.append(since)
        if tag:
            struct_where.append("tags LIKE ?")
            struct_args.append(f"%{tag}%")
        if role:
            struct_where.append("role = ?")
            struct_args.append(role)
        struct_where_sql = (" AND ".join(struct_where)) if struct_where else "1=1"

        # ── Count totals for honest elision metadata ──────────────────────
        total_insights = conn.execute(
            f"SELECT COUNT(*) FROM insights WHERE {where_sql}", args
        ).fetchone()[0]
        total_principles = conn.execute("SELECT COUNT(*) FROM principles").fetchone()[0]
        total_sessions = conn.execute(
            f"SELECT COUNT(DISTINCT session_id) FROM insights "
            f"WHERE session_id IS NOT NULL AND {where_sql}", args
        ).fetchone()[0]
        total_epics = conn.execute(
            f"SELECT COUNT(DISTINCT epic_tag) FROM insights "
            f"WHERE epic_tag IS NOT NULL AND {where_sql}", args
        ).fetchone()[0]

        # ── Build want-set from include_* flags ──────────────────────────
        # entity_link is handled separately (hub-only filter in step 4).
        # In overview mode, session_member / epic_member edges are SUPPRESSED:
        # sessions/epics become collapsed cluster nodes (one node each) and the
        # high-cardinality membership edges are hidden behind the cluster.  The
        # member edges (and the per-member insight seeding) are reachable via
        # cluster expand-on-demand (focal focus_class=session|epic).
        want: set[str] = {"promoted_to"}
        if not overview:
            want.add("session_member")
            want.add("epic_member")
        if include_supersedes:
            want.add("supersede")
        if include_contradictions:
            want.add("contradiction")

        # Cluster bookkeeping (overview only): count member edges hidden behind
        # each collapsed cluster node so the UI can disclose the elision.
        collapsed_session_count = 0
        collapsed_epic_count = 0
        collapsed_member_edges_hidden = 0

        # ── Step 1: Seed ALL structural endpoints FIRST (never truncate) ──
        # Plan principle #4: "structural skeleton is NEVER truncated."
        # We pull every insight that participates in ANY structural edge —
        # supersede (winner + loser), contradiction (suspect + target),
        # session members, epic members — plus all principles (promoted_to).
        # This guarantees the full supersede/contradiction/promoted_to/
        # session_member/epic_member edge sets appear in global.

        def _seed_insight(iid: int) -> None:
            """Add a single insight node by id (no-op if already present)."""
            key = f"insight:{iid}"
            if key in nodes:
                return
            r = conn.execute(
                "SELECT id, substr(content,1,90) AS preview, project, type, confidence "
                "FROM insights WHERE id = ?",
                (iid,),
            ).fetchone()
            if r:
                r = dict(r)
                _add_node(
                    "insight", str(r["id"]), r["preview"],
                    project=r.get("project"),
                    confidence=r.get("confidence"),
                    insight_type=r.get("type"),
                )

        # Supersede: both winner and loser.  NO status filter (struct_where_sql)
        # — a superseded loser is status='stale'/'superseded' by definition, and
        # excluding it would drop the very edge we want to show.  Both endpoints
        # are seeded UPFRONT so the emission pass finds them in the snapshot.
        if include_supersedes:
            for row in conn.execute(
                f"SELECT id, superseded_by FROM insights "
                f"WHERE superseded_by IS NOT NULL AND {struct_where_sql}",
                struct_args,
            ).fetchall():
                _seed_insight(row["id"])
                _seed_insight(row["superseded_by"])

        # Contradiction: both suspect and target.  NO status filter.
        if include_contradictions:
            for row in conn.execute(
                f"SELECT id, suspect_of FROM insights "
                f"WHERE suspect_of IS NOT NULL AND {struct_where_sql}",
                struct_args,
            ).fetchall():
                _seed_insight(row["id"])
                _seed_insight(row["suspect_of"])

        # All principles (promoted_to) + their source insights
        if include_principles:
            p_where: list[str] = []
            p_args: list = []
            if project:
                p_where.append("project = ?")
                p_args.append(project)
            if min_confidence is not None:
                p_where.append("confidence >= ?")
                p_args.append(float(min_confidence))
            p_sql = ("WHERE " + " AND ".join(p_where)) if p_where else ""
            for pr in conn.execute(
                f"SELECT id, substr(content,1,90) AS preview, project, "
                f"       confidence, source_insights "
                f"FROM principles {p_sql} ORDER BY confidence DESC",
                p_args,
            ).fetchall():
                pr = dict(pr)
                _add_node(
                    "principle", str(pr["id"]), pr["preview"],
                    project=pr.get("project"),
                    confidence=pr.get("confidence"),
                )
                if pr.get("source_insights"):
                    for sid in str(pr["source_insights"]).split(","):
                        sid = sid.strip()
                        if sid.isdigit():
                            _seed_insight(int(sid))

        # All sessions (distinct, subject to filter).
        #   overview: emit ONE collapsed cluster node per session carrying
        #             member_count + collapsed=true; do NOT seed members nor
        #             emit member edges (expand on-demand via focal).
        #   full:     emit the session node + every member insight (member
        #             edges are emitted later in the structural drain).
        for sr in conn.execute(
            f"SELECT session_id, COUNT(*) AS member_count FROM insights "
            f"WHERE session_id IS NOT NULL AND {where_sql} "
            f"GROUP BY session_id",
            args,
        ).fetchall():
            sess_id = sr["session_id"]
            member_count = sr["member_count"]
            short = sess_id[:12] + ("…" if len(sess_id) > 12 else "")
            if overview:
                _add_node(
                    "session", sess_id, f"session {short}",
                    collapsed=True, member_count=member_count,
                )
                collapsed_session_count += 1
                collapsed_member_edges_hidden += member_count
            else:
                _add_node("session", sess_id, f"session {short}")
                for r in conn.execute(
                    f"SELECT id, substr(content,1,90) AS preview, project, type, confidence "
                    f"FROM insights WHERE session_id = ? AND {where_sql}",
                    [sess_id] + args,
                ).fetchall():
                    r = dict(r)
                    _add_node(
                        "insight", str(r["id"]), r["preview"],
                        project=r.get("project"),
                        confidence=r.get("confidence"),
                        insight_type=r.get("type"),
                    )

        # All epics (distinct, subject to filter).  Same overview/full split.
        if include_epics:
            for er in conn.execute(
                f"SELECT epic_tag, COUNT(*) AS member_count FROM insights "
                f"WHERE epic_tag IS NOT NULL AND epic_tag != '' AND {where_sql} "
                f"GROUP BY epic_tag ORDER BY epic_tag",
                args,
            ).fetchall():
                etag = er["epic_tag"]
                member_count = er["member_count"]
                if overview:
                    _add_node(
                        "epic", etag, f"epic: {etag}",
                        collapsed=True, member_count=member_count,
                    )
                    collapsed_epic_count += 1
                    collapsed_member_edges_hidden += member_count
                else:
                    _add_node("epic", etag, f"epic: {etag}")
                    for r in conn.execute(
                        f"SELECT id, substr(content,1,90) AS preview, project, type, confidence "
                        f"FROM insights WHERE epic_tag = ? AND {where_sql}",
                        [etag] + args,
                    ).fetchall():
                        r = dict(r)
                        _add_node(
                            "insight", str(r["id"]), r["preview"],
                            project=r.get("project"),
                            confidence=r.get("confidence"),
                            insight_type=r.get("type"),
                        )

        # ── Emit all structural edges for the seeded nodes ────────────────
        # WORKLIST DRAIN (not a one-shot snapshot): _emit_node_edges can ADD
        # new structural endpoints DURING emission (e.g. a winner discovered
        # while emitting its loser, then that winner's OTHER loser).  A snapshot
        # taken before emission would miss those — this was the second cause of
        # the supersede 29/30 gap.  We process a worklist and enqueue any newly
        # added insight/principle node until the structural closure is complete.
        emitted: set[str] = set()
        worklist: list[str] = [
            k for k in nodes
            if k.split(":", 1)[0] in ("insight", "principle")
        ]
        while worklist:
            key = worklist.pop()
            if key in emitted:
                continue
            emitted.add(key)
            parts = key.split(":", 1)
            if len(parts) != 2:
                continue
            kls_k, id_k = parts
            if kls_k not in ("insight", "principle"):
                continue
            before = set(nodes.keys())
            _emit_node_edges(
                conn, kls_k, id_k,
                _add_node, _add_edge,
                frontier=None,   # global mode — no BFS expansion
                hop=0,
                want=want,
            )
            # Enqueue any newly added structural nodes for their own emission.
            for new_key in set(nodes.keys()) - before:
                if (new_key not in emitted
                        and new_key.split(":", 1)[0] in ("insight", "principle")):
                    worklist.append(new_key)

        # Record how many nodes we have after fully seeding the structural skeleton.
        structural_node_count = len(nodes)

        # ── Step 2: Top-up with degree-ranked insights (fill remaining budget) ─
        # The structural skeleton is ALWAYS complete regardless of max_nodes.
        # The effective node budget is the structural floor PLUS whatever
        # max_nodes allows on top (never less than the floor — max_nodes only
        # governs the non-structural tail).  We split the headroom between
        # top-up insights and hub entities.
        hub_entity_budget = 0 if overview else 250
        headroom = max(0, max_nodes - structural_node_count)
        if overview:
            # Overview = backbone only.  No degree-ranked insight top-up and no
            # hub entities — the structural skeleton plus collapsed clusters IS
            # the view.  This keeps the node count at the structural floor
            # (target ≤ ~250) and removes the entity layer entirely.
            topup_budget = 0
        else:
            # Give insight top-up the headroom not reserved for hub entities, but
            # always allow at least a small insight top-up so hub-entity edges
            # have member insights to attach to even when max_nodes is tight.
            topup_budget = max(headroom - hub_entity_budget, min(headroom, 50))

        if topup_budget > 0:
            ranked_rows = conn.execute(
                f"""
                SELECT i.id,
                       substr(i.content, 1, 90) AS preview,
                       i.project, i.type, i.confidence, i.session_id,
                       COALESCE(el.link_count, 0)
                         + CASE WHEN i.superseded_by IS NOT NULL THEN 1 ELSE 0 END
                         + CASE WHEN i.suspect_of    IS NOT NULL THEN 1 ELSE 0 END
                         + CASE WHEN i.session_id    IS NOT NULL THEN 1 ELSE 0 END
                         + CASE WHEN i.epic_tag      IS NOT NULL THEN 1 ELSE 0 END
                         AS degree
                FROM insights i
                LEFT JOIN (
                    SELECT insight_id, COUNT(*) AS link_count
                    FROM entity_links GROUP BY insight_id
                ) el ON el.insight_id = i.id
                WHERE {where_sql}
                ORDER BY degree DESC, i.confidence DESC
                """,
                args,
            ).fetchall()
            added = 0
            for r in ranked_rows:
                if added >= topup_budget:
                    break
                r = dict(r)
                key = f"insight:{r['id']}"
                if key not in nodes:
                    _add_node(
                        "insight", str(r["id"]), r["preview"],
                        project=r.get("project"),
                        confidence=r.get("confidence"),
                        insight_type=r.get("type"),
                    )
                    # Emit structural edges for this newly added insight too
                    _emit_node_edges(
                        conn, "insight", str(r["id"]),
                        _add_node, _add_edge,
                        frontier=None,
                        hop=0,
                        want=want,
                    )
                    added += 1

        # nodes_total estimate (before hub entities are appended)
        _nodes_total_estimate = (total_insights + total_principles
                                 + total_sessions + total_epics)

        if not nodes:
            return {
                "ok": True,
                "nodes": [], "edges": [], "focal": None,
                "meta": {
                    "nodes_returned": 0, "edges_returned": 0,
                    "nodes_total": _nodes_total_estimate,
                    "edges_total": 0,
                    "global": True,
                    "view": view,
                    "collapsed_clusters": {
                        "sessions": collapsed_session_count,
                        "epics": collapsed_epic_count,
                        "member_edges_hidden": collapsed_member_edges_hidden,
                    },
                    "filters_applied": {
                        "project": project, "type": type,
                        "min_confidence": min_confidence, "since": since,
                        "tag": tag, "entity_type": entity_type,
                        "epic_tag": epic_tag, "role": role,
                        "view": view,
                    },
                },
            }

        # ── Step 3: Hub entity nodes (degree ≥ 2 among all insight nodes) ─
        # Singletons excluded; hubs ranked by degree; capped at hub_entity_budget.
        if include_entities:
            sel_iids = [int(k.split(":")[1]) for k in nodes if k.startswith("insight:")]
            if sel_iids:
                placeholders = ",".join("?" * len(sel_iids))
                et_where = f"insight_id IN ({placeholders})"
                et_args = list(sel_iids)
                if entity_type:
                    et_where += " AND entity_type = ?"
                    et_args.append(entity_type)
                hub_rows = conn.execute(
                    f"SELECT entity_type, entity AS entity_value, "
                    f"       COUNT(DISTINCT insight_id) AS degree "
                    f"FROM entity_links "
                    f"WHERE {et_where} "
                    f"GROUP BY entity_type, entity_value "
                    f"HAVING degree >= 2 "
                    f"ORDER BY degree DESC LIMIT ?",
                    et_args + [hub_entity_budget],
                ).fetchall()
                for hr in hub_rows:
                    hr = dict(hr)
                    ent_id = f"{hr['entity_type']}/{hr['entity_value']}"
                    ent_key = _add_node(
                        "entity", ent_id, hr["entity_value"],
                        entity_type=hr["entity_type"],
                    )
                    for elink in conn.execute(
                        f"SELECT insight_id FROM entity_links "
                        f"WHERE entity_type = ? AND entity = ? "
                        f"AND insight_id IN ({placeholders})",
                        [hr["entity_type"], hr["entity_value"]] + sel_iids,
                    ).fetchall():
                        src_key = f"insight:{elink['insight_id']}"
                        if src_key in nodes:
                            _add_edge(src_key, ent_key, "entity_link", hr["entity_type"])

        # ── Step 5: Count totals for honest elision metadata ─────────────
        # Count total possible structural edges (for meta reporting)
        total_supersede_edges = conn.execute(
            f"SELECT COUNT(*) FROM insights "
            f"WHERE superseded_by IS NOT NULL AND {where_sql}", args
        ).fetchone()[0]
        total_contradiction_edges = conn.execute(
            f"SELECT COUNT(*) FROM insights "
            f"WHERE suspect_of IS NOT NULL AND {where_sql}", args
        ).fetchone()[0]
        total_promoted_edges = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT source_insights FROM principles WHERE source_insights IS NOT NULL"
            ") sub"
        ).fetchone()[0]
        # entity_links count among selected insights for a more accurate total
        _sel_iids_for_count = [
            int(k.split(":")[1]) for k in nodes if k.startswith("insight:")
        ]
        if _sel_iids_for_count and include_entities:
            _ph = ",".join("?" * len(_sel_iids_for_count))
            _et_clause = f"insight_id IN ({_ph})"
            if entity_type:
                _et_clause += " AND entity_type = ?"
                _sel_iids_for_count.append(entity_type)
            total_entity_edges = conn.execute(
                f"SELECT COUNT(*) FROM entity_links WHERE {_et_clause}",
                _sel_iids_for_count,
            ).fetchone()[0]
        else:
            total_entity_edges = 0
        total_edges_est = max(
            len(edges),
            total_supersede_edges
            + total_contradiction_edges
            + total_promoted_edges
            + total_entity_edges,
        )

        # Per-kind edge counts in result
        edge_kinds: dict[str, int] = {}
        for e in edges:
            edge_kinds[e["type"]] = edge_kinds.get(e["type"], 0) + 1

        return {
            "ok": True,
            "nodes": list(nodes.values()),
            "edges": edges,
            "focal": None,
            "meta": {
                "nodes_returned": len(nodes),
                # Clamp so nodes_total is never < nodes_returned (structural
                # expansion can add nodes beyond the pre-expansion estimate).
                "nodes_total": max(len(nodes), _nodes_total_estimate),
                "edges_returned": len(edges),
                "edges_total": total_edges_est,
                "hit_cap": len(nodes) >= max_nodes,
                "edge_kinds": edge_kinds,
                "global": True,
                "view": view,
                # Cluster disclosure (overview only): how many session/epic
                # cluster nodes are collapsed and how many member edges are
                # hidden behind them.  In full view these are all 0 (members
                # are materialized as individual edges).
                "collapsed_clusters": {
                    "sessions": collapsed_session_count,
                    "epics": collapsed_epic_count,
                    "member_edges_hidden": collapsed_member_edges_hidden,
                },
                "filters_applied": {
                    "project": project, "type": type,
                    "min_confidence": min_confidence, "since": since,
                    "tag": tag, "entity_type": entity_type,
                    "epic_tag": epic_tag, "role": role,
                    "include_supersedes": include_supersedes,
                    "include_contradictions": include_contradictions,
                    "include_entities": include_entities,
                    "include_principles": include_principles,
                    "include_epics": include_epics,
                    "view": view,
                },
            },
        }

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, "/query/graph_global", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/query/roi")
async def query_roi(request: Request, days: int = 30, project: Optional[str] = None):
    """Phase 5.1 — weekly ROI rollup.

    Aggregates token_ledger Phase 17 counters into:
      - daily series (hits/misses/repeated_errors/novel_saves/tokens)
      - per-role breakdown
      - per-epic breakdown
      - decay candidates (insights with high age + zero recalls + low confidence)
    """
    t0 = time.perf_counter()
    days = min(max(int(days), 1), 180)

    def _do():
        conn = get_db()
        proj_clause = "AND project = ?" if project else ""
        proj_args: tuple = (project,) if project else ()

        # ── Daily series ────────────────────────────────────────────────────
        # COUNT DISTINCT session_id (ignoring NULL/empty) — Phase 17 made
        # session_id mandatory but historical rows have NULL/empty. Counting
        # rows directly inflated session counts ~10× per day. The correct
        # number is unique sessions, not unique ledger rows.
        daily_q = f"""
            SELECT date(created_at) AS day,
                   COUNT(DISTINCT NULLIF(session_id, '')) AS sessions,
                   COUNT(*) AS ledger_rows,
                   COALESCE(SUM(tokens_in), 0) AS tokens_in,
                   COALESCE(SUM(tokens_out), 0) AS tokens_out,
                   COALESCE(SUM(recall_hits), 0) AS hits,
                   COALESCE(SUM(recall_misses), 0) AS misses,
                   COALESCE(SUM(repeated_errors), 0) AS errors,
                   COALESCE(SUM(novel_saves), 0) AS novel
            FROM token_ledger
            WHERE date(created_at) >= date('now', '-{days} days') {proj_clause}
            GROUP BY day ORDER BY day ASC
        """
        daily_rows = [dict(r) for r in conn.execute(daily_q, proj_args).fetchall()]

        # ── Per-role rollup ─────────────────────────────────────────────────
        # Bug B2 fix (2026-05-29): old query did token_ledger LEFT JOIN insights
        # ON i.session_id=tl.session_id — a many-to-one JOIN that fan-out
        # inflated every SUM (→ 119,293,870,843 garbage tokens).
        # Fix: use token_ledger.role directly (populated by /ingest/session_tokens
        # and the backfill endpoint).  One row per session, no fan-out.
        role_q = f"""
            SELECT COALESCE(NULLIF(role, ''), '(unknown)') AS role,
                   COUNT(*) AS sessions,
                   COALESCE(SUM(recall_hits), 0) AS hits,
                   COALESCE(SUM(recall_misses), 0) AS misses,
                   COALESCE(SUM(novel_saves), 0) AS novel,
                   COALESCE(SUM(repeated_errors), 0) AS errors,
                   COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens
            FROM token_ledger
            WHERE date(created_at) >= date('now', '-{days} days') {('AND project = ?' if project else '')}
            GROUP BY COALESCE(NULLIF(role, ''), '(unknown)')
            ORDER BY sessions DESC LIMIT 10
        """
        role_rows = [dict(r) for r in conn.execute(role_q, proj_args).fetchall()]

        # ── Per-epic rollup ─────────────────────────────────────────────────
        # Bug C fix (2026-05-29): old query COUNT(DISTINCT i.session_id) returned
        # 0 because insights.session_id was mostly NULL.
        # Fix: join token_ledger via session_id to count real sessions per epic.
        # session_meta is the authoritative session→project map; we use it to
        # attribute sessions to epics via the insights.session_id foreign key.
        # Historical insights with NULL session_id contribute to insight count
        # but not to the session count — documented as best-effort heuristic.
        epic_q = f"""
            SELECT i.epic_tag,
                   COUNT(DISTINCT tl.session_id) AS sessions,
                   COUNT(i.id) AS insights_saved,
                   AVG(i.confidence) AS avg_confidence
            FROM insights i
            LEFT JOIN token_ledger tl ON tl.session_id = i.session_id
                AND tl.session_id IS NOT NULL AND tl.session_id != ''
            WHERE i.epic_tag IS NOT NULL AND i.epic_tag != ''
              AND date(i.created_at) >= date('now', '-{days} days')
              {('AND i.project = ?' if project else '')}
            GROUP BY i.epic_tag ORDER BY insights_saved DESC LIMIT 15
        """
        epic_rows = [dict(r) for r in conn.execute(epic_q, proj_args).fetchall()]

        # ── Decay candidates ───────────────────────────────────────────────
        decay_q = f"""
            SELECT id, type, content, project, confidence,
                   last_recalled_at, created_at,
                   julianday('now') - julianday(created_at) AS age_days
            FROM insights
            WHERE superseded_by IS NULL
              AND confidence < 0.5
              AND (last_recalled_at IS NULL OR
                   julianday('now') - julianday(last_recalled_at) > 60)
              AND julianday('now') - julianday(created_at) > 30
              {('AND project = ?' if project else '')}
            ORDER BY age_days DESC LIMIT 25
        """
        decay_rows = [dict(r) for r in conn.execute(decay_q, proj_args).fetchall()]
        # truncate content to 120 chars for payload size
        for r in decay_rows:
            r["content"] = (r.get("content") or "")[:120]
            r["age_days"] = round(r.get("age_days") or 0, 1)

        # ── Summary ─────────────────────────────────────────────────────────
        total_hits = sum(d["hits"] for d in daily_rows)
        total_misses = sum(d["misses"] for d in daily_rows)
        total_novel = sum(d["novel"] for d in daily_rows)
        total_errors = sum(d["errors"] for d in daily_rows)
        total_tokens = sum(d["tokens_in"] + d["tokens_out"] for d in daily_rows)

        # ── USD cost: MODEL-AWARE (WS4) ─────────────────────────────────────
        # Previously hardcoded $3/$15 per 1M for all rows. token_ledger has a
        # `model` column; price each model group at its canonical rate from
        # packages/pricing/pricing.py. Rows with unknown/NULL model are priced
        # at the default model (claude-fable-5) and counted honestly in
        # priced_with_default_model_rows (no silent assumption).
        model_q = f"""
            SELECT COALESCE(NULLIF(model, ''), '') AS model,
                   COALESCE(SUM(tokens_in), 0) AS tokens_in,
                   COALESCE(SUM(tokens_out), 0) AS tokens_out,
                   COUNT(*) AS rows
            FROM token_ledger
            WHERE date(created_at) >= date('now', '-{days} days') {proj_clause}
            GROUP BY model
        """
        model_rows = [dict(r) for r in conn.execute(model_q, proj_args).fetchall()]
        total_cost_usd = 0.0
        priced_with_default_model_rows = 0
        per_model_cost = []
        for mr in model_rows:
            model_name = mr["model"]
            if model_name:
                rates = _get_model_rates(model_name)
            else:
                rates = _get_model_rates(_DEFAULT_COST_MODEL)
                priced_with_default_model_rows += mr["rows"]
            cost = (
                mr["tokens_in"] * rates["input"]
                + mr["tokens_out"] * rates["output"]
            ) / 1_000_000
            total_cost_usd += cost
            per_model_cost.append({
                "model": model_name or f"(unknown→{_DEFAULT_COST_MODEL})",
                "tokens_in": mr["tokens_in"],
                "tokens_out": mr["tokens_out"],
                "rows": mr["rows"],
                "cost_usd": round(cost, 4),
            })
        hit_rate_pct = (total_hits / (total_hits + total_misses) * 100) if (total_hits + total_misses) > 0 else 0
        cost_per_hit = (total_cost_usd / total_hits) if total_hits > 0 else 0

        # Top-level distinct session count (not sum of daily — a session can
        # span multiple days and would otherwise be double-counted)
        sess_q = (
            "SELECT COUNT(DISTINCT NULLIF(session_id, '')) AS s, "
            "       COUNT(*) AS rows FROM token_ledger "
            f"WHERE date(created_at) >= date('now', '-{days} days') {proj_clause}"
        )
        sess_row = conn.execute(sess_q, proj_args).fetchone()
        distinct_sessions = sess_row["s"] or 0
        ledger_rows = sess_row["rows"] or 0

        return {
            "ok": True,
            "days": days,
            "project": project,
            "summary": {
                "sessions_total": distinct_sessions,
                "ledger_rows_total": ledger_rows,
                "tokens_total": total_tokens,
                "cost_usd_total": round(total_cost_usd, 4),
                "cost_basis": "model-aware (packages/pricing); unknown-model rows priced at " + _DEFAULT_COST_MODEL,
                "priced_with_default_model_rows": priced_with_default_model_rows,
                "recall_hits_total": total_hits,
                "recall_misses_total": total_misses,
                "hit_rate_pct": round(hit_rate_pct, 1),
                "repeated_errors_total": total_errors,
                "novel_saves_total": total_novel,
                "cost_per_recall_hit_usd": round(cost_per_hit, 6),
            },
            "cost_by_model": per_model_cost,
            "daily": daily_rows,
            "by_role": role_rows,
            "by_epic": epic_rows,
            "decay_candidates": decay_rows,
        }

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, "/query/roi", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/query/slo")
async def query_slo(request: Request):
    """Phase 5.2 — SLO posture snapshot (WS4 honest-metrics revision).

    Returns:
      recall_p99_ms — computed from the PERSISTENT recall_timings table over the
                      report window (falls back to the in-memory ring buffer only
                      when the table is empty; `source` states which).
      days_active   — activity PRESENCE (distinct days with a token_ledger row),
                      NOT an uptime percentage. `(days_active/30)*100` was a
                      projection and has been removed.
      self_reported_accuracy_pct — kept, but relabeled: it is derived from
                      SELF-REPORTED counters (repeated_errors, recall_hits/misses
                      supplied by agent sessions via add_token_record), so it
                      carries basis:"self-reported by agent sessions".
      hit_rate_pct — recall_hits / (hits + misses) from token_ledger (also
                      self-reported).
    """
    t0 = time.perf_counter()
    window_days = 30

    def _do():
        def _p(arr, pct):
            if not arr:
                return 0.0
            idx = max(0, min(len(arr) - 1, int(len(arr) * pct / 100)))
            return arr[idx]

        conn = get_db()

        # p99 from the persistent recall_timings table over the report window.
        # Fall back to the in-memory ring buffer only when the table is absent
        # or empty (fresh restart before any recall). `source` records which.
        p_source = "recall_timings"
        table_ms: list[float] = []
        try:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=window_days)
            ).isoformat()
            table_ms = sorted(
                r[0] for r in conn.execute(
                    "SELECT duration_ms FROM recall_timings WHERE ts >= ?",
                    (cutoff,),
                ).fetchall()
                if r[0] is not None
            )
        except Exception:
            table_ms = []

        if table_ms:
            all_ms = table_ms
        else:
            all_ms = sorted([e["elapsed_ms"] for e in _recall_slow_log])
            p_source = "ring_buffer"

        recall_p99_ms = round(_p(all_ms, 99), 1)
        recall_p95_ms = round(_p(all_ms, 95), 1)
        recall_p50_ms = round(_p(all_ms, 50), 1)

        # Hit rate + accuracy from last 30d token_ledger (self-reported counters)
        hits_row = conn.execute(
            "SELECT COALESCE(SUM(recall_hits), 0) AS hits, "
            "       COALESCE(SUM(recall_misses), 0) AS misses, "
            "       COALESCE(SUM(repeated_errors), 0) AS errors "
            "FROM token_ledger WHERE date(created_at) >= date('now', '-30 days')"
        ).fetchone()
        h = hits_row["hits"]
        m = hits_row["misses"]
        e = hits_row["errors"]
        hit_rate = (h / (h + m) * 100) if (h + m) > 0 else 0
        # Clamp to [0, 100]: repeated_errors can exceed hits+misses (e.g. errors
        # logged in sessions with zero recalls), which previously produced a
        # nonsensical NEGATIVE accuracy percentage on the SLO card.
        accuracy = max(0.0, min(100.0, (1 - e / max(h + m, 1)) * 100))

        # Activity presence: distinct days with a token_ledger row in last 30.
        # NOT a percentage — presence is a measurement, uptime% was a projection.
        days_active = conn.execute(
            "SELECT COUNT(DISTINCT date(created_at)) AS d FROM token_ledger "
            "WHERE date(created_at) >= date('now', '-30 days')"
        ).fetchone()["d"]

        return {
            "ok": True,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "slis": [
                {
                    "name": "recall_p99_ms",
                    "value": recall_p99_ms,
                    "target": 500,
                    "unit": "ms",
                    "comparator": "lt",
                    "status": "OK" if recall_p99_ms < 500 else "BREACH",
                    "extras": {
                        "p50_ms": recall_p50_ms,
                        "p95_ms": recall_p95_ms,
                        "source": p_source,
                        "sample_count": len(all_ms),
                    },
                },
                {
                    "name": "hit_rate_pct",
                    "value": round(hit_rate, 1),
                    "target": 80,
                    "unit": "%",
                    "comparator": "gte",
                    "status": "OK" if hit_rate >= 80 else "BREACH",
                    "extras": {"hits": h, "misses": m},
                },
                {
                    "name": "self_reported_accuracy_pct",
                    "value": round(accuracy, 1),
                    "target": 95,
                    "unit": "%",
                    "comparator": "gte",
                    "status": "OK" if accuracy >= 95 else "BREACH",
                    "basis": "self-reported by agent sessions",
                    "extras": {"repeated_errors": e},
                },
                {
                    "name": "days_active",
                    "value": days_active,
                    "unit": "days",
                    "note": "activity presence, not uptime",
                    "extras": {"window_days": window_days},
                },
            ],
        }

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, "/query/slo", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/query/session/{session_id}/recalls")
async def query_session_recalls(request: Request, session_id: str):
    """Phase 3.1 — all recall_events rows for a session (for timeline).

    Returns: {ok, rows: [{id, ts, query, project, role, epic_tag, hits_returned}]}
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        # NOTE: recall_events stores time in `recalled_at`, no `project` column.
        # We JOIN insights to surface project context when present.
        rows = conn.execute(
            "SELECT re.id, re.recalled_at AS ts, re.query, "
            "       COALESCE(i.project, p.project) AS project, "
            "       re.role, re.epic_tag, re.hit_rank "
            "FROM recall_events re "
            "LEFT JOIN insights i ON i.id = re.insight_id "
            "LEFT JOIN principles p ON p.id = re.principle_id "
            "WHERE re.session_id = ? ORDER BY re.id ASC LIMIT 500",
            (session_id,),
        ).fetchall()
        return {"ok": True, "rows": [dict(r) for r in rows]}

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, f"/query/session/{session_id}/recalls", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/query/session/{session_id}/insights")
async def query_session_insights(request: Request, session_id: str):
    """Phase 3.1 — all insights saved during a session (for timeline).

    Returns: {ok, rows: [{id, created_at, type, content, project, role, epic_tag, confidence}]}
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        # superseded_by is an INTEGER in engine.db (references insights.id)
        rows = conn.execute(
            "SELECT id, created_at, type, content, project, role, epic_tag, confidence "
            "FROM insights WHERE session_id = ? AND superseded_by IS NULL "
            "ORDER BY id ASC LIMIT 200",
            (session_id,),
        ).fetchall()
        return {"ok": True, "rows": [dict(r) for r in rows]}

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, f"/query/session/{session_id}/insights", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/query/session/{session_id}/arena")
async def query_session_arena(request: Request, session_id: str):
    """Phase 3.1 — arena_events for a session (for timeline).

    Returns: {ok, rows: [{id, created_at, strategy, verdict, role, epic_tag}]}
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        # arena_events uses `ts` not `created_at`
        rows = conn.execute(
            "SELECT id, ts AS created_at, strategy, verdict, role, epic_tag "
            "FROM arena_events WHERE session_id = ? ORDER BY id ASC LIMIT 200",
            (session_id,),
        ).fetchall()
        return {"ok": True, "rows": [dict(r) for r in rows]}

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, f"/query/session/{session_id}/arena", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/query/session/{session_id}/supersedes")
async def query_session_supersedes(request: Request, session_id: str):
    """Phase 3.1 — insights superseded during a session (for timeline).

    Uses the superseding insight's session_id to attribute the operation.
    Returns: {ok, rows: [{loser_id, winner_id, reason, created_at}]}
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        # superseded_by is INTEGER (FK to insights.id, set on the LOSER)
        # superseded_at is TEXT on the loser insight. We find all insights this
        # session marked as a superseder (the winner) by finding the row that
        # another insight points superseded_by at.
        # Simpler: find losers whose superseded_at is known + winner via superseded_by.
        rows = conn.execute(
            "SELECT l.id AS loser_id, l.superseded_by AS winner_id, "
            "       l.supersede_reason AS reason, l.superseded_at AS created_at "
            "FROM insights l "
            "WHERE l.superseded_by IS NOT NULL "
            "  AND EXISTS (SELECT 1 FROM insights w WHERE w.id = l.superseded_by "
            "              AND (w.session_id = ? OR l.session_id = ?)) "
            "ORDER BY l.id ASC LIMIT 100",
            (session_id, session_id),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "winner_id": r["winner_id"],
                "loser_id": r["loser_id"],
                "reason": r["reason"] or "superseded",
                "created_at": r["created_at"] or "",
            })
        return {"ok": True, "rows": out}

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, f"/query/session/{session_id}/supersedes", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/query/session/{session_id}/token_record")
async def query_session_token_record(request: Request, session_id: str):
    """Phase 3.1 — token_ledger entry for a session (for timeline).

    Returns: {ok, row: {id, created_at, tokens_in, tokens_out, recall_hits,
                        recall_misses, novel_saves, repeated_errors, task_summary, model}}
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        row = conn.execute(
            "SELECT id, created_at, tokens_in, tokens_out, "
            "       recall_hits, recall_misses, novel_saves, repeated_errors, "
            "       task_summary, model "
            "FROM token_ledger WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return {"ok": True, "row": dict(row) if row else {}}

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, f"/query/session/{session_id}/token_record", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.get("/query/principles")
async def query_principles(
    request: Request,
    q: str = "",
    project: str = "",
    limit: int = 200,
    offset: int = 0,
):
    """P2.2 — Paginated principles query with optional search."""
    t0 = time.perf_counter()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    def _do():
        conn = get_db()
        where_clauses: list[str] = []
        params: list = []

        if project:
            where_clauses.append("project = ?")
            params.append(project)
        if q:
            where_clauses.append("content LIKE ?")
            params.append(f"%{q}%")

        # where_clauses reference the principles table; prefix with p. for the join.
        pwhere = [c.replace("project", "p.project") for c in where_clauses]
        pwhere = [c.replace("content", "p.content") for c in pwhere]
        where_sql = ("WHERE " + " AND ".join(pwhere)) if pwhere else ""
        total = conn.execute(
            f"SELECT COUNT(*) FROM principles p {where_sql}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT p.id, p.content, p.project, p.confidence, p.created_at, p.updated_at,
                   p.grounded_at, p.volatility_class, p.grounding_due,
                   f.last_result AS falsifier_result, f.kind AS falsifier_kind
            FROM principles p
            LEFT JOIN falsifiers f
                   ON f.claim_kind = 'principle' AND f.claim_id = p.id
            {where_sql}
            ORDER BY p.confidence DESC, p.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        out_rows = []
        for r in rows:
            d = dict(r)
            d["liveness"] = _liveness_stamp(
                d.pop("grounded_at", None), d.pop("volatility_class", None),
                d.pop("grounding_due", 0), d.pop("falsifier_result", None),
                d.pop("falsifier_kind", None),
            )
            out_rows.append(d)
        conn.close()
        return {"rows": out_rows, "total": total, "limit": limit, "offset": offset}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/query/principles", (time.perf_counter() - t0) * 1000)
    return {"ok": True, **result}


@app.get("/principles/export")
async def principles_export(
    request: Request,
    project: str = "",
    compile_eligible: bool = False,
):
    """E1 — the crag-distill back-edge export (docs/architecture.md rev 2/5).

    Contract is defined by the CONSUMER — crag src/distill/fetch-principles.js
    + render.js (mirrored by crag test/fixtures/mock-memory-mcp.js):
      {"principles": [{id, text, confidence, claim_health, ...}], "as_of": iso}
    Field mapping: engine `content` -> `text` (verbatim — crag only places
    text, never rephrases). `claim_health` is a FLAT STRING; render.js's
    eligibility gate accepts only 'fresh'/'passing' and OMITS everything else
    (retirement by omission). We report the claim_rollup verdict honestly:
    a principle with no linked claims exports claim_health='unverified' and
    therefore does NOT compile — that is the evidence gate working, not a bug.
    No `scope` column exists yet; render.js fail-safes missing scope to the
    project layer, which is the correct conservative default.

    `compile_eligible=true` filters server-side to fresh-only (what distill
    asks for); default false returns all active for observability callers.
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        try:
            where = "WHERE superseded_by IS NULL"
            params: list = []
            if project:
                where += " AND project = ?"
                params.append(project)
            rows = conn.execute(
                f"""SELECT id, content, project, confidence, tags
                    FROM principles {where} ORDER BY id""",
                params,
            ).fetchall()
            out = []
            for r in rows:
                health = "unverified"
                if _CLAIM_LAYER:
                    try:
                        roll = _claim_layer.claim_rollup(conn, "principle", r["id"])
                        health = roll.get("verdict") or "unverified"
                    except Exception:
                        health = "unverified"
                if compile_eligible and health not in ("fresh", "passing"):
                    continue
                out.append({
                    "id": r["id"],
                    "text": r["content"],
                    "project": r["project"],
                    "confidence": r["confidence"],
                    "tags": r["tags"],
                    "claim_health": health,
                })
            return out
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    principles = await loop.run_in_executor(None, _do)
    _log_request(request, "/principles/export", (time.perf_counter() - t0) * 1000)
    return {
        "ok": True,
        "principles": principles,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/query/principles/{principle_id}")
async def query_principle_detail(request: Request, principle_id: int):
    """P2.2 — Single principle detail."""
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        row = conn.execute(
            "SELECT id, content, project, confidence, created_at, updated_at "
            "FROM principles WHERE id = ?",
            (principle_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, f"/query/principles/{principle_id}", (time.perf_counter() - t0) * 1000)
    if result is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Principle {principle_id} not found"},
        )
    return {"ok": True, "principle": result}


@app.get("/query/entities")
async def query_entities(
    request: Request,
    prefix: str = "",
    entity_type: str = "",
    limit: int = 100,
    offset: int = 0,
):
    """P2.2 — Paginated entity links query."""
    t0 = time.perf_counter()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    def _do():
        conn = get_db()
        where_clauses = ["1=1"]
        params: list = []
        if prefix:
            where_clauses.append("el.entity LIKE ?")
            params.append(f"{prefix}%")
        if entity_type:
            where_clauses.append("el.entity_type = ?")
            params.append(entity_type)
        where_sql = " AND ".join(where_clauses)

        total = conn.execute(
            f"""SELECT COUNT(DISTINCT el.entity || '|' || el.entity_type)
                FROM entity_links el WHERE {where_sql}""",
            params,
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT el.entity, el.entity_type,
                   COUNT(*) AS link_count,
                   GROUP_CONCAT(DISTINCT COALESCE(i.project, '')) AS projects
            FROM entity_links el
            LEFT JOIN insights i ON el.insight_id = i.id
            WHERE {where_sql}
            GROUP BY el.entity, el.entity_type
            ORDER BY link_count DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        conn.close()

        def _norm(row: dict) -> dict:
            raw = row.get("projects") or ""
            if isinstance(raw, str):
                row = {**row, "projects": [p for p in raw.split(",") if p]}
            return row

        return {
            "rows": [_norm(dict(r)) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/query/entities", (time.perf_counter() - t0) * 1000)
    return {"ok": True, **result}


@app.get("/query/task_clusters")
async def query_task_clusters(
    request: Request,
    limit: int = 100,
    offset: int = 0,
):
    """P2.2 — Paginated task clusters query."""
    t0 = time.perf_counter()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    def _do():
        conn = get_db()
        total = conn.execute(
            "SELECT COUNT(DISTINCT task_type || '/' || COALESCE(project,'')) FROM task_clusters"
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT task_type, project, COUNT(*) AS count
            FROM task_clusters
            GROUP BY task_type, project
            ORDER BY count DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        conn.close()
        return {"rows": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/query/task_clusters", (time.perf_counter() - t0) * 1000)
    return {"ok": True, **result}


@app.get("/query/recall_events")
async def query_recall_events(
    request: Request,
    limit: int = 100,
    offset: int = 0,
):
    """P2.2 — Recent recall events, paginated."""
    t0 = time.perf_counter()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    def _do():
        conn = get_db()
        # Phase A.4: count only canonical rows (hit_rank = 0) — rank>0 rows are
        # analytics filler (one per insight returned) and must not be shown.
        total = conn.execute(
            "SELECT COUNT(*) FROM recall_events WHERE hit_rank = 0"
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT id, substr(query, 1, 120) AS query, session_id,
                   (SELECT COUNT(*) FROM recall_events r2
                    WHERE r2.fingerprint = re.fingerprint AND re.fingerprint IS NOT NULL) AS hits,
                   recalled_at AS created_at
            FROM recall_events re
            WHERE re.hit_rank = 0
            ORDER BY recalled_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        conn.close()
        return {"rows": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/query/recall_events", (time.perf_counter() - t0) * 1000)
    return {"ok": True, **result}


@app.get("/query/recall/{recall_id}")
async def query_recall(request: Request, recall_id: int):
    """Phase 1.7 — single recall_events row by id."""
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        row = conn.execute(
            """SELECT id, insight_id, principle_id, session_id, query, hit_rank,
                      recalled_at, fingerprint, role, epic_tag
               FROM recall_events WHERE id = ?""",
            (recall_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"recall_events id {recall_id} not found"}
        return {"ok": True, "recall": dict(row)}

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, f"/query/recall/{recall_id}", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result, status_code=200 if result.get("ok") else 404)


@app.get("/query/arena/{arena_id}")
async def query_arena(request: Request, arena_id: int):
    """Phase 1.7 — single arena_events verdict row by id."""
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        row = conn.execute(
            """SELECT id, ts, project, input_insight_ids, winner_insight_id, strategy,
                      rationale, merged_insight_id, verdict, role, epic_tag, session_id
               FROM arena_events WHERE id = ?""",
            (arena_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"arena_events id {arena_id} not found"}
        return {"ok": True, "arena": dict(row)}

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, f"/query/arena/{arena_id}", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result, status_code=200 if result.get("ok") else 404)


@app.get("/query/contradiction/{insight_id}")
async def query_contradiction(request: Request, insight_id: int):
    """Phase 1.7 — single suspect_of pair: the suspect insight + the one it contradicts."""
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        suspect = conn.execute(
            """SELECT id, content, type, project, confidence, suspect_of,
                      suspect_reason, suspect_score, suspect_detected_at,
                      role, epic_tag, session_id, created_at
               FROM insights WHERE id = ? AND suspect_of IS NOT NULL""",
            (insight_id,),
        ).fetchone()
        if not suspect:
            return {"ok": False, "error": f"insight {insight_id} has no active suspect_of edge"}
        contradicts_id = suspect["suspect_of"]
        contradicts = conn.execute(
            """SELECT id, content, type, project, confidence, role, epic_tag, session_id, created_at
               FROM insights WHERE id = ?""",
            (contradicts_id,),
        ).fetchone()
        return {
            "ok": True,
            "suspect": dict(suspect),
            "contradicts": dict(contradicts) if contradicts else None,
        }

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, f"/query/contradiction/{insight_id}", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result, status_code=200 if result.get("ok") else 404)


@app.get("/query/epic/{epic_tag}")
async def query_epic(request: Request, epic_tag: str):
    """Phase 1.7 — epic_tag rollup: all insights + principles + arena verdicts
    sharing this epic_tag, plus session list and counters."""
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        insights = conn.execute(
            """SELECT id, substr(content, 1, 120) AS snippet, type, project,
                      confidence, role, created_at, session_id
               FROM insights WHERE epic_tag = ? AND status = 'active'
               ORDER BY created_at DESC LIMIT 100""",
            (epic_tag,),
        ).fetchall()
        principles = conn.execute(
            """SELECT id, substr(content, 1, 120) AS snippet, project, confidence,
                      role, created_at, session_id
               FROM principles WHERE epic_tag = ? AND superseded_by IS NULL
               ORDER BY created_at DESC LIMIT 50""",
            (epic_tag,),
        ).fetchall()
        arena_rows = conn.execute(
            """SELECT id, ts, project, strategy, verdict, winner_insight_id, role, session_id
               FROM arena_events WHERE epic_tag = ?
               ORDER BY ts DESC LIMIT 50""",
            (epic_tag,),
        ).fetchall()
        sessions = conn.execute(
            """SELECT DISTINCT session_id FROM insights
               WHERE epic_tag = ? AND session_id IS NOT NULL""",
            (epic_tag,),
        ).fetchall()

        if not insights and not principles and not arena_rows:
            return {"ok": False, "error": f"no records found with epic_tag '{epic_tag}'"}

        return {
            "ok": True,
            "epic": {
                "epic_tag": epic_tag,
                "insights_count": len(insights),
                "principles_count": len(principles),
                "arena_verdicts_count": len(arena_rows),
                "sessions_count": len(sessions),
                "insights": [dict(r) for r in insights],
                "principles": [dict(r) for r in principles],
                "arena_verdicts": [dict(r) for r in arena_rows],
                "sessions": [r["session_id"] for r in sessions],
            },
        }

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    _log_request(request, f"/query/epic/{epic_tag}", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result, status_code=200 if result.get("ok") else 404)


@app.get("/query/contradictions")
async def query_contradictions(
    request: Request,
    limit: int = 100,
    offset: int = 0,
):
    """P2.2 — Recent contradiction events, paginated."""
    t0 = time.perf_counter()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    def _do():
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM contradiction_events").fetchone()[0]
        rows = conn.execute(
            """
            SELECT ce.id AS new_id, ce.old_id AS suspect_of_id,
                   ce.new_kind, ce.old_kind, ce.cosine_sim AS score,
                   ce.haiku_response AS model, ce.detected_at AS decided_at,
                   substr(COALESCE(i_new.content, p_new.content, ''), 1, 200) AS new_content,
                   substr(COALESCE(i_old.content, p_old.content, ''), 1, 200) AS old_content
            FROM contradiction_events ce
            LEFT JOIN insights  i_new ON ce.new_kind = 'insight'   AND ce.new_id = i_new.id
            LEFT JOIN principles p_new ON ce.new_kind = 'principle' AND ce.new_id = p_new.id
            LEFT JOIN insights  i_old ON ce.old_kind = 'insight'   AND ce.old_id = i_old.id
            LEFT JOIN principles p_old ON ce.old_kind = 'principle' AND ce.old_id = p_old.id
            ORDER BY ce.detected_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        conn.close()
        return {"rows": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/query/contradictions", (time.perf_counter() - t0) * 1000)
    return {"ok": True, **result}


@app.get("/query/broadcasts")
async def query_broadcasts(
    request: Request,
    limit: int = 100,
    offset: int = 0,
):
    """P2.2 — Recent broadcast events, paginated."""
    t0 = time.perf_counter()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    def _do():
        import json as _json
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM broadcast_events").fetchone()[0]
        rows = conn.execute(
            """
            SELECT id, kind, payload,
                   emitted_at AS ts, subscriber_count
            FROM broadcast_events
            ORDER BY emitted_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        conn.close()

        # Phase A.4: server-side parse. broadcast_events.payload stores the
        # full broadcaster envelope: {"kind": k, "ts": t, "payload": {...}}.
        # Return a structured `inner` dict instead of raw JSON-in-JSON string.
        result_rows = []
        for r in rows:
            row = dict(r)
            raw = row.pop("payload", None) or ""
            inner: dict = {}
            try:
                envelope = _json.loads(raw)
                # The broadcaster wraps: {"kind": ..., "ts": ..., "payload": {…}}
                inner = envelope.get("payload") or {}
            except Exception:
                pass
            row["inner"] = inner
            result_rows.append(row)

        return {"rows": result_rows, "total": total, "limit": limit, "offset": offset}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/query/broadcasts", (time.perf_counter() - t0) * 1000)
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Phase 15: promote_insight / update_insight / token_record
# (Exposes CLI-only ops to the MCP server via daemon HTTP)
# ---------------------------------------------------------------------------

@app.post("/promote_insight")
async def promote_insight(body: PromoteInsightBody, request: Request):
    """Promote a single insight to a principle at confidence 0.9.
    Mirrors engine-cli.py promote-insight but accessible via MCP daemon call.
    Sets insights.promoted_to = <new principle id>.
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        try:
            # WS2 T1 — shared promote logic (same path as auto-promote).
            res = _do_promote_insight(
                conn, body.insight_id, actor="operator", content=body.content,
                role=body.role, epic_tag=body.epic_tag, session_id=body.session_id,
            )
            if res.get("ok"):
                conn.commit()
                # Grounding v2: importance rose — re-author the principle's recipe.
                principle_id = res.get("auto_promoted") or res.get("principle_id")
                if _GROUNDING_V2 and principle_id and _table_exists(conn, "grounding_jobs"):
                    try:
                        _gv2_enqueue_job(conn, "principle", principle_id, "author", priority=3)
                        conn.commit()
                    except Exception:
                        pass
            return res
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/promote_insight", (time.perf_counter() - t0) * 1000)
    return result


@app.post("/update_insight")
async def update_insight(body: UpdateInsightBody, request: Request):
    """Patch an existing insight's content / tags / source_file in place.
    Does NOT create a new insight — use when refining a known record.
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        sets = []
        params: dict = {}
        for field, val in [("content", body.content), ("tags", body.tags), ("source_file", body.source_file)]:
            if val is not None:
                sets.append(f"{field} = :{field}")
                params[field] = val
        if not sets:
            conn.close()
            return {"ok": False, "error": "no fields to update — supply at least one of: content, tags, source_file"}
        sets.append("updated_at = :updated_at")
        params["updated_at"] = _utcnow_iso()
        params["id"] = body.id
        cur = conn.execute(f"UPDATE insights SET {', '.join(sets)} WHERE id = :id", params)
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            return {"ok": False, "error": f"insight {body.id} not found"}
        # Re-enrich on content change — parity with /update_principle and with
        # the MCP `update` contract ("re-embeds on content change"). Without
        # this the row keeps the OLD content's embedding (semantic recall
        # drifts), stale entity_links (recall_by_entity + falsifier derivation
        # anchor on entities the content no longer mentions), and a falsifier
        # derived from the old claim. Stale links are deleted first because
        # _enrich_insight's INSERT OR IGNORE alone never removes them.
        reenriched = False
        if body.content is not None:
            try:
                proj_row = conn.execute(
                    "SELECT project FROM insights WHERE id = ?", (body.id,)
                ).fetchone()
                conn.execute("DELETE FROM entity_links WHERE insight_id = ?", (body.id,))
                conn.commit()
                _enrich_insight(conn, body.id, body.content,
                                proj_row["project"] if proj_row else None)
                reenriched = True
            except Exception as e:
                logger.warning("update_insight re-enrichment failed for #%s: %s", body.id, e)
        conn.close()
        out = {"ok": True, "id": body.id, "reenriched": reenriched}
        # Every mutation auditable (doctrine) — parity with /update_principle.
        try:
            _audit_log("agent", "update_insight", "insight", str(body.id),
                       {k: (v[:120] if isinstance(v, str) else v) for k, v in params.items() if k != "id"},
                       out, None, None)
        except Exception:
            pass
        return out

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/update_insight", (time.perf_counter() - t0) * 1000)
    return result


@app.post("/update_principle")
async def update_principle(body: UpdatePrincipleBody, request: Request):
    """Patch a principle's content / confidence / tags IN PLACE; re-embeds when content changes.

    Closes the self-consistency gap discovered 2026-05-28: insights could be edited
    (/update_insight) and superseded, but principles — the highest-trust layer, loaded
    FIRST at pre-start — had no correction path at all (only /update embedding existed).
    A drifted principle (stale topology, renamed service) could not be fixed by the agent.
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        row = conn.execute("SELECT id, content FROM principles WHERE id = ?", (body.id,)).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": f"principle {body.id} not found"}
        sets = []
        params: dict = {}
        for field, val in [("content", body.content), ("confidence", body.confidence), ("tags", body.tags)]:
            if val is not None:
                sets.append(f"{field} = :{field}")
                params[field] = val
        if not sets:
            conn.close()
            return {"ok": False, "error": "no fields to update — supply at least one of: content, confidence, tags"}
        sets.append("updated_at = :updated_at")
        params["updated_at"] = _utcnow_iso()
        params["id"] = body.id
        conn.execute(f"UPDATE principles SET {', '.join(sets)} WHERE id = :id", params)
        conn.commit()
        # Re-embed on content change so recall_principle semantic match stays accurate.
        reembedded = False
        if body.content is not None:
            try:
                emb_bytes = _embed_one(body.content)
                conn.execute("UPDATE principles SET embedding = ? WHERE id = ?", (emb_bytes, body.id))
                conn.commit()
                reembedded = True
            except Exception as e:
                logger.warning("Failed to re-embed principle %s after update: %s", body.id, e)
        conn.close()
        out = {"ok": True, "id": body.id, "reembedded": reembedded}
        try:
            _audit_log("agent", "update_principle", "principle", str(body.id),
                       {k: (v[:120] if isinstance(v, str) else v) for k, v in params.items() if k != "id"},
                       out, None, None)
        except Exception:
            pass
        return out

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/update_principle", (time.perf_counter() - t0) * 1000)
    if isinstance(result, dict) and result.get("ok"):
        await _sse_publish("principle_updated", {"principle_id": body.id})
    return JSONResponse(content=result)


@app.post("/supersede_principle")
async def supersede_principle(body: SupersedePrincipleBody, request: Request):
    """Mark loser principle superseded by winner principle.

    Schema columns (superseded_by/at/reason) added in migration 013; recall_principle
    already filters WHERE superseded_by IS NULL, so the loser drops out of recall but
    stays queryable by ID for audit. Use when a NEWER principle fully replaces an older one.
    """
    t0 = time.perf_counter()

    def _do():
        if body.loser_id == body.winner_id:
            return {"ok": False, "error": "loser_id and winner_id must differ"}
        conn = get_db()
        for pid, label in [(body.loser_id, "loser"), (body.winner_id, "winner")]:
            if conn.execute("SELECT 1 FROM principles WHERE id = ?", (pid,)).fetchone() is None:
                conn.close()
                return {"ok": False, "error": f"{label} principle #{pid} not found"}
        now = _utcnow_iso()
        conn.execute(
            """UPDATE principles SET superseded_by = ?, superseded_at = ?,
                                     supersede_reason = ?, updated_at = ?
               WHERE id = ?""",
            (body.winner_id, now, f"manual:{body.reason or 'manual'}", now, body.loser_id),
        )
        conn.commit()
        conn.close()
        out = {"ok": True, "superseded": body.loser_id, "by": body.winner_id, "reason": body.reason}
        try:
            _audit_log("agent", "supersede_principle", "principle", str(body.loser_id),
                       {"loser_id": body.loser_id, "winner_id": body.winner_id, "reason": body.reason or "manual"},
                       out, None, body.session_id)
        except Exception:
            pass
        return out

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/supersede_principle", (time.perf_counter() - t0) * 1000)
    if isinstance(result, dict) and result.get("ok"):
        await _sse_publish("principle_superseded", {"loser_id": body.loser_id, "winner_id": body.winner_id})
    return JSONResponse(content=result)


@app.post("/verify_principle")
async def verify_principle(body: VerifyPrincipleBody, request: Request):
    """Confirm (+0.05) or contradict (-0.1) a principle's confidence. Gentler deltas than
    verify_insight because principles are curated high-trust facts. Clamps to [0.0, 1.0]."""
    t0 = time.perf_counter()

    def _do():
        if body.status not in ("verified", "stale"):
            return {"ok": False, "error": "status must be 'verified' or 'stale'"}
        conn = get_db()
        row = conn.execute("SELECT id, confidence FROM principles WHERE id = ?", (body.id,)).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": f"principle {body.id} not found"}
        conf = row["confidence"] if row["confidence"] is not None else scoring.PROMOTE_SEED_CONFIDENCE
        new_conf = (min(1.0, conf + scoring.VERIFY_PRINCIPLE_UP) if body.status == "verified"
                    else max(0.0, conf - scoring.VERIFY_PRINCIPLE_DOWN))
        conn.execute("UPDATE principles SET confidence = ?, updated_at = ? WHERE id = ?",
                     (new_conf, _utcnow_iso(), body.id))
        conn.commit()
        conn.close()
        out = {"ok": True, "id": body.id, "new_confidence": round(new_conf, 3), "status": body.status}
        try:
            _audit_log("agent", "verify_principle", "principle", str(body.id),
                       {"status": body.status}, out, None, None)
        except Exception:
            pass
        return out

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/verify_principle", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.post("/token_record")
async def token_record(body: TokenRecordBody, request: Request):
    """Append or update one row in token_ledger per session_id.

    Migration 022 added UNIQUE(session_id) so we switch from INSERT OR IGNORE
    to UPSERT (ON CONFLICT DO UPDATE).  This ensures the MCP add_token_record
    call always reflects the latest session totals rather than being silently
    dropped by the old INSERT OR IGNORE.
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        conn.execute(
            """INSERT INTO token_ledger
               (session_id, project, task_summary, tokens_in, tokens_out,
                cache_hits, cache_misses, rtk_savings_pct, headroom_savings_pct,
                wall_time_sec, model, cache_read_tokens, cache_write_tokens, fresh_input_tokens,
                recall_hits, recall_misses, repeated_errors, novel_saves, role)
               VALUES
               (:session_id, :project, :task_summary, :tokens_in, :tokens_out,
                :cache_hits, :cache_misses, :rtk_savings_pct, :headroom_savings_pct,
                :wall_time_sec, :model, :cache_read_tokens, :cache_write_tokens, :fresh_input_tokens,
                :recall_hits, :recall_misses, :repeated_errors, :novel_saves, :role)
               ON CONFLICT(session_id) DO UPDATE SET
                 project               = COALESCE(excluded.project, token_ledger.project),
                 task_summary          = COALESCE(excluded.task_summary, token_ledger.task_summary),
                 tokens_in             = excluded.tokens_in,
                 tokens_out            = excluded.tokens_out,
                 cache_hits            = excluded.cache_hits,
                 cache_misses          = excluded.cache_misses,
                 rtk_savings_pct       = excluded.rtk_savings_pct,
                 headroom_savings_pct  = excluded.headroom_savings_pct,
                 wall_time_sec         = excluded.wall_time_sec,
                 model                 = COALESCE(excluded.model, token_ledger.model),
                 cache_read_tokens     = excluded.cache_read_tokens,
                 cache_write_tokens    = excluded.cache_write_tokens,
                 fresh_input_tokens    = excluded.fresh_input_tokens,
                 recall_hits           = excluded.recall_hits,
                 recall_misses         = excluded.recall_misses,
                 repeated_errors       = excluded.repeated_errors,
                 novel_saves           = excluded.novel_saves,
                 role                  = COALESCE(excluded.role, token_ledger.role)
               """,
            {
                "session_id": body.session_id,
                "project": body.project,
                "task_summary": body.task_summary,
                "tokens_in": body.tokens_in,
                "tokens_out": body.tokens_out,
                "cache_hits": body.cache_hits,
                "cache_misses": body.cache_misses,
                "rtk_savings_pct": body.rtk_savings_pct,
                "headroom_savings_pct": body.headroom_savings_pct,
                "wall_time_sec": body.wall_time_sec,
                "model": body.model,
                "cache_read_tokens": body.cache_read_tokens,
                "cache_write_tokens": body.cache_write_tokens,
                "fresh_input_tokens": body.fresh_input_tokens,
                "recall_hits": body.recall_hits,
                "recall_misses": body.recall_misses,
                "repeated_errors": body.repeated_errors,
                "novel_saves": body.novel_saves,
                "role": body.role,  # from TokenRecordBody.role (COALESCE keeps existing if None)
            },
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM token_ledger WHERE session_id = ?", (body.session_id,)
        ).fetchone()
        row_id = row[0] if row else conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return {"ok": True, "id": row_id}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/token_record", (time.perf_counter() - t0) * 1000)
    return result


# ---------------------------------------------------------------------------
# Transcript-based session token ingest (migration 022)
# ---------------------------------------------------------------------------

class SessionTokensBody(BaseModel):
    """Body for POST /ingest/session_tokens."""
    session_id: str
    project: str
    transcript_path: Optional[str] = None   # explicit path; resolved from UUID if omitted
    task_summary: Optional[str] = None
    # Phase 17 counters (from MCP caller or left at 0 if ingested from transcript only)
    recall_hits: Optional[int] = 0
    recall_misses: Optional[int] = 0
    repeated_errors: Optional[int] = 0
    novel_saves: Optional[int] = 0


def _derive_role_for_session(conn, session_id: str) -> Optional[str]:
    """Return the majority role for a session, defaulting to 'coordinator'.

    Looks at insights.role + recall_events joined to insights. NULL rows are
    ignored. When no explicit role is found the session defaults to
    'coordinator': /ingest/session_tokens is only ever called by the Stop hook
    of a top-level Claude Code session (subagents run as subprocesses and never
    fire the Stop hook), so an unclassified session IS the coordinator — far
    more truthful than the old '(unknown)' bucket that swallowed every session.
    """
    rows = conn.execute(
        """
        SELECT role, COUNT(*) AS cnt
        FROM (
            SELECT role FROM insights
            WHERE session_id = ? AND role IS NOT NULL AND role != ''
            UNION ALL
            SELECT i.role FROM recall_events re
            JOIN insights i ON re.insight_id = i.id
            WHERE re.session_id = ? AND i.role IS NOT NULL AND i.role != ''
        )
        GROUP BY role ORDER BY cnt DESC LIMIT 1
        """,
        (session_id, session_id),
    ).fetchone()
    return rows["role"] if rows and rows["role"] else "coordinator"


@app.post("/ingest/session_tokens")
async def ingest_session_tokens(body: SessionTokensBody, request: Request):
    """Parse the session transcript and UPSERT one authoritative token_ledger row.

    This endpoint is called:
      - By the Stop hook (thin curl POST) at session end.
      - By the backfill admin endpoint for historical sessions.

    The transcript path is resolved from session_uuid under ~/.claude/projects/
    if not explicitly provided.  Role is derived from action data for
    this session (insights.role majority vote).  All token columns are
    populated from the transcript (authoritative Anthropic-reported values).

    Returns:
        {ok, session_id, tokens_in, tokens_out, cache_read_tokens,
         cache_write_tokens, fresh_input_tokens, model, role,
         transcript_found: bool, upserted: bool}
    """
    t0 = time.perf_counter()
    from transcript_tokens import parse_transcript, resolve_transcript

    def _do():
        # --- resolve transcript ---
        if body.transcript_path:
            t_path = Path(body.transcript_path)
        else:
            t_path = resolve_transcript(body.session_id)

        transcript_found = t_path is not None and t_path.exists() if t_path else False

        if transcript_found:
            tok = parse_transcript(t_path)
        else:
            tok = {
                "fresh_input_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "model": None,
            }

        conn = get_db()
        role = _derive_role_for_session(conn, body.session_id)

        conn.execute(
            """INSERT INTO token_ledger
               (session_id, project, task_summary,
                tokens_in, tokens_out,
                fresh_input_tokens, cache_read_tokens, cache_write_tokens,
                model, role,
                recall_hits, recall_misses, repeated_errors, novel_saves,
                cache_hits, cache_misses, rtk_savings_pct, headroom_savings_pct, wall_time_sec)
               VALUES
               (:session_id, :project, :task_summary,
                :tokens_in, :tokens_out,
                :fresh_input_tokens, :cache_read_tokens, :cache_write_tokens,
                :model, :role,
                :recall_hits, :recall_misses, :repeated_errors, :novel_saves,
                0, 0, 0, 0, 0)
               ON CONFLICT(session_id) DO UPDATE SET
                 tokens_in             = excluded.tokens_in,
                 tokens_out            = excluded.tokens_out,
                 fresh_input_tokens    = excluded.fresh_input_tokens,
                 cache_read_tokens     = excluded.cache_read_tokens,
                 cache_write_tokens    = excluded.cache_write_tokens,
                 model                 = COALESCE(excluded.model, token_ledger.model),
                 role                  = COALESCE(excluded.role, token_ledger.role),
                 project               = COALESCE(excluded.project, token_ledger.project),
                 task_summary          = COALESCE(excluded.task_summary, token_ledger.task_summary),
                 recall_hits           = CASE WHEN excluded.recall_hits > 0
                                             THEN excluded.recall_hits
                                             ELSE token_ledger.recall_hits END,
                 recall_misses         = CASE WHEN excluded.recall_misses > 0
                                             THEN excluded.recall_misses
                                             ELSE token_ledger.recall_misses END,
                 repeated_errors       = CASE WHEN excluded.repeated_errors > 0
                                             THEN excluded.repeated_errors
                                             ELSE token_ledger.repeated_errors END,
                 novel_saves           = CASE WHEN excluded.novel_saves > 0
                                             THEN excluded.novel_saves
                                             ELSE token_ledger.novel_saves END
               """,
            {
                "session_id": body.session_id,
                "project": body.project,
                "task_summary": body.task_summary or "auto-ingested from transcript",
                "tokens_in": tok["tokens_in"],
                "tokens_out": tok["tokens_out"],
                "fresh_input_tokens": tok["fresh_input_tokens"],
                "cache_read_tokens": tok["cache_read_tokens"],
                "cache_write_tokens": tok["cache_write_tokens"],
                "model": tok.get("model"),
                "role": role,
                "recall_hits": body.recall_hits or 0,
                "recall_misses": body.recall_misses or 0,
                "repeated_errors": body.repeated_errors or 0,
                "novel_saves": body.novel_saves or 0,
            },
        )
        conn.commit()
        conn.close()

        return {
            "ok": True,
            "session_id": body.session_id,
            "tokens_in": tok["tokens_in"],
            "tokens_out": tok["tokens_out"],
            "cache_read_tokens": tok["cache_read_tokens"],
            "cache_write_tokens": tok["cache_write_tokens"],
            "fresh_input_tokens": tok["fresh_input_tokens"],
            "model": tok.get("model"),
            "role": role,
            "transcript_found": transcript_found,
            "upserted": True,
        }

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/ingest/session_tokens", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


class SessionStateBody(BaseModel):
    """Body for POST /ingest/session_state (migration 029).

    Auto-captured facts about a session's WORK (as opposed to its tokens),
    posted by a Stop-hook step that computes a git diff against the baseline
    recorded at SessionStart. Mirrors /ingest/session_tokens: idempotent
    UPSERT keyed by session_id, safe to call multiple times per session
    (every Stop fire overwrites with the latest git delta).
    """
    session_id: str
    project: str
    git_branch: Optional[str] = None
    commits_count: Optional[int] = None
    files_changed_count: Optional[int] = None
    wall_time_sec: Optional[int] = None


@app.post("/ingest/session_state")
async def ingest_session_state(body: SessionStateBody, request: Request):
    """UPSERT the auto-captured half of a session's `sessions` row.

    This endpoint NEVER touches narrative columns (accomplished, decisions,
    problems, next_steps, raw_markdown, files_changed) — those belong
    exclusively to the session_diary(add) writer (see /lifecycle/session/add).
    Two independent writers, two disjoint column sets, one row per
    session_uuid: this is what makes the row safe to fill in from either
    direction without a race clobbering the other's data.

    Returns:
        {ok, session_id, row_id, upserted: bool}
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        try:
            today = _utcnow_iso()[:10]
            conn.execute(
                """INSERT INTO sessions
                   (project, date, session_uuid, git_branch, commits_count,
                    files_changed_count, wall_time_sec, auto_captured_at)
                   VALUES
                   (:project, :date, :session_uuid, :git_branch, :commits_count,
                    :files_changed_count, :wall_time_sec, :auto_captured_at)
                   ON CONFLICT(session_uuid) WHERE session_uuid IS NOT NULL DO UPDATE SET
                     project              = COALESCE(excluded.project, sessions.project),
                     git_branch           = COALESCE(excluded.git_branch, sessions.git_branch),
                     commits_count        = COALESCE(excluded.commits_count, sessions.commits_count),
                     files_changed_count  = COALESCE(excluded.files_changed_count, sessions.files_changed_count),
                     wall_time_sec        = COALESCE(excluded.wall_time_sec, sessions.wall_time_sec),
                     auto_captured_at     = excluded.auto_captured_at
                """,
                {
                    "project": body.project,
                    "date": today,
                    "session_uuid": body.session_id,
                    "git_branch": body.git_branch,
                    "commits_count": body.commits_count,
                    "files_changed_count": body.files_changed_count,
                    "wall_time_sec": body.wall_time_sec,
                    "auto_captured_at": _utcnow_iso(),
                },
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM sessions WHERE session_uuid = ?", (body.session_id,)
            ).fetchone()
            return {"ok": True, "session_id": body.session_id,
                     "row_id": row["id"] if row else None, "upserted": True}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/ingest/session_state", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.post("/admin/backfill_token_ledger")
async def backfill_token_ledger(request: Request, project: Optional[str] = None):
    """Scan all ~/.claude/projects transcripts and UPSERT one token_ledger row each.

    This is the one-shot repair for historical sessions that had no token data
    (or garbage data from the old Headroom-counter-diff hook).

    Query params:
        project  — optional project filter (only sessions whose session_meta.project
                   matches will be updated); omit to process all.

    Returns:
        {ok, sessions_processed, sessions_with_transcripts, sessions_skipped,
         sample_errors: [...]}
    """
    t0 = time.perf_counter()
    from transcript_tokens import iter_all_transcripts, parse_transcript

    def _do():
        conn = get_db()

        processed = 0
        with_transcripts = 0
        skipped = 0
        errors: list = []

        for session_uuid, t_path in iter_all_transcripts():
            try:
                # Check session_meta for project attribution
                meta = conn.execute(
                    "SELECT project FROM session_meta WHERE session_uuid = ?",
                    (session_uuid,),
                ).fetchone()

                session_project = meta["project"] if meta else "unknown"

                if project and session_project != project:
                    skipped += 1
                    continue

                tok = parse_transcript(t_path)
                role = _derive_role_for_session(conn, session_uuid)

                conn.execute(
                    """INSERT INTO token_ledger
                       (session_id, project,
                        tokens_in, tokens_out,
                        fresh_input_tokens, cache_read_tokens, cache_write_tokens,
                        model, role,
                        task_summary, cache_hits, cache_misses,
                        rtk_savings_pct, headroom_savings_pct, wall_time_sec,
                        recall_hits, recall_misses, repeated_errors, novel_saves)
                       VALUES
                       (:session_id, :project,
                        :tokens_in, :tokens_out,
                        :fresh_input_tokens, :cache_read_tokens, :cache_write_tokens,
                        :model, :role,
                        'backfill', 0, 0, 0, 0, 0, 0, 0, 0, 0)
                       ON CONFLICT(session_id) DO UPDATE SET
                         tokens_in          = excluded.tokens_in,
                         tokens_out         = excluded.tokens_out,
                         fresh_input_tokens = excluded.fresh_input_tokens,
                         cache_read_tokens  = excluded.cache_read_tokens,
                         cache_write_tokens = excluded.cache_write_tokens,
                         model              = COALESCE(excluded.model, token_ledger.model),
                         role               = COALESCE(excluded.role, token_ledger.role),
                         project            = COALESCE(excluded.project, token_ledger.project)
                    """,
                    {
                        "session_id": session_uuid,
                        "project": session_project,
                        "tokens_in": tok["tokens_in"],
                        "tokens_out": tok["tokens_out"],
                        "fresh_input_tokens": tok["fresh_input_tokens"],
                        "cache_read_tokens": tok["cache_read_tokens"],
                        "cache_write_tokens": tok["cache_write_tokens"],
                        "model": tok.get("model"),
                        "role": role,
                    },
                )
                processed += 1
                if tok["tokens_in"] > 0 or tok["tokens_out"] > 0:
                    with_transcripts += 1

            except Exception as e:
                errors.append({"session_id": session_uuid, "error": str(e)[:200]})
                if len(errors) >= 10:
                    break  # stop collecting errors after 10

        conn.commit()
        conn.close()

        return {
            "ok": True,
            "sessions_processed": processed,
            "sessions_with_transcripts": with_transcripts,
            "sessions_skipped": skipped,
            "sample_errors": errors,
        }

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/admin/backfill_token_ledger", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


@app.post("/admin/backfill_session_uuid")
async def backfill_session_uuid(request: Request):
    """Best-effort link historical `sessions` date-rows to `session_meta.session_uuid`.

    Migration 029 gives `sessions` a session_uuid column, but the 212
    pre-existing rows were written before that column existed — they are
    date-keyed (project, date), not uuid-keyed, and there is no reliable
    join key. This endpoint links ONLY the unambiguous case: exactly one
    `sessions` row (session_uuid IS NULL) and exactly one `session_meta` row
    share (project, date) with no other candidate on either side. Every
    other case (0 candidates, or 2+ candidates on either side — e.g. the
    exact fragmentation bug this migration fixes, multiple diary rows for
    one real session) is left unlinked and counted separately. Doctrine:
    best-effort with an honest 'ambiguous/unlinked' bucket, never guess a
    fabricated 1:1 match (ref insight #2191 — the sibling token_events
    backfill made the same call for the same reason).

    Returns:
        {ok, linked, ambiguous_multiple_sessions_rows, ambiguous_multiple_meta_rows,
         no_candidate, sample_ambiguous: [...]}
    """
    t0 = time.perf_counter()

    def _do():
        conn = get_db()
        try:
            unlinked = conn.execute(
                "SELECT id, project, date FROM sessions WHERE session_uuid IS NULL"
            ).fetchall()

            linked = 0
            ambiguous_multiple_sessions_rows = 0
            ambiguous_multiple_meta_rows = 0
            no_candidate = 0
            sample_ambiguous: list = []

            for row in unlinked:
                # How many OTHER unlinked sessions rows share this (project, date)?
                sibling_count = conn.execute(
                    """SELECT COUNT(*) FROM sessions
                       WHERE session_uuid IS NULL AND project = ? AND date = ?""",
                    (row["project"], row["date"]),
                ).fetchone()[0]
                if sibling_count > 1:
                    ambiguous_multiple_sessions_rows += 1
                    if len(sample_ambiguous) < 10:
                        sample_ambiguous.append(
                            {"sessions_id": row["id"], "project": row["project"],
                             "date": row["date"], "reason": "multiple_sessions_rows_same_day"}
                        )
                    continue

                # Candidate session_meta rows for this (project, date), excluding
                # any session_uuid already linked to a different sessions row.
                candidates = conn.execute(
                    """SELECT session_uuid FROM session_meta
                       WHERE project = ?
                         AND (substr(started_at, 1, 10) = ? OR substr(last_seen_at, 1, 10) = ?)
                         AND session_uuid NOT IN (
                             SELECT session_uuid FROM sessions WHERE session_uuid IS NOT NULL
                         )""",
                    (row["project"], row["date"], row["date"]),
                ).fetchall()

                if len(candidates) == 0:
                    no_candidate += 1
                    continue
                if len(candidates) > 1:
                    ambiguous_multiple_meta_rows += 1
                    if len(sample_ambiguous) < 10:
                        sample_ambiguous.append(
                            {"sessions_id": row["id"], "project": row["project"],
                             "date": row["date"], "reason": "multiple_session_meta_candidates"}
                        )
                    continue

                # Exactly one candidate on both sides — safe to link.
                conn.execute(
                    "UPDATE sessions SET session_uuid = ? WHERE id = ?",
                    (candidates[0]["session_uuid"], row["id"]),
                )
                linked += 1

            conn.commit()
            return {
                "ok": True,
                "linked": linked,
                "ambiguous_multiple_sessions_rows": ambiguous_multiple_sessions_rows,
                "ambiguous_multiple_meta_rows": ambiguous_multiple_meta_rows,
                "no_candidate": no_candidate,
                "sample_ambiguous": sample_ambiguous,
            }
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do)
    _log_request(request, "/admin/backfill_session_uuid", (time.perf_counter() - t0) * 1000)
    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# Insights session_id backfill (Bug C heuristic, migration 022)
# ---------------------------------------------------------------------------


# ===========================================================================
# C2 — Operator-lifecycle endpoints (session diary, context, events, cost)
# ===========================================================================

# ── Pydantic bodies ─────────────────────────────────────────────────────────

class SessionAddBody(BaseModel):
    project: str
    date: str = ""
    accomplished: str = ""
    files_changed: str = ""
    commits: str = ""
    decisions: str = ""
    problems: str = ""
    next_steps: str = ""
    duration: str = ""
    raw_markdown: str = ""
    # Migration 029: when the caller knows the Claude session_uuid, pass it so
    # this call ENRICHES the one canonical `sessions` row for that session
    # (UPSERT) instead of creating a new fragmented date-row. Omit for
    # backward-compatible manual/non-Claude-Code callers — legacy bare-INSERT
    # behavior is unchanged when session_uuid is absent.
    session_uuid: Optional[str] = None


class ContextSetBody(BaseModel):
    project: str
    status: Optional[str] = None
    current_branch: Optional[str] = None
    last_session_date: Optional[str] = None
    architecture_decisions: Optional[str] = None
    known_issues: Optional[str] = None
    backlog: Optional[str] = None


class EventAddBody(BaseModel):
    project: Optional[str] = None
    source: str
    event_type: str
    summary: str
    payload: str = ""
    priority: str = "normal"
    expires_at: Optional[str] = None


class EventClaimBody(BaseModel):
    id: int
    claimed_by: str = "unknown"


class EventCompleteBody(BaseModel):
    id: int
    status: str  # completed | failed
    result: str = ""


class EventBulkExpireBody(BaseModel):
    project: Optional[str] = None
    priority: Optional[str] = None


# ── Session diary ────────────────────────────────────────────────────────────

@app.post("/lifecycle/session/add")
async def lifecycle_session_add(body: SessionAddBody):
    """Append (or enrich) a session diary entry. Returns {ok, id}.

    Migration 029: when body.session_uuid is set, this UPSERTs the ONE
    canonical `sessions` row for that session_uuid instead of always
    inserting a new date-row — the fix for the fragmentation where a single
    real Claude session accumulated multiple `sessions` rows (observed: 5
    rows for one session_uuid on 2026-07-05, unjoinable to session_meta).
    Auto-captured columns (git_branch, commits_count, files_changed_count,
    wall_time_sec, auto_captured_at — written by /ingest/session_state) are
    NEVER touched here; this writer owns only the narrative columns.
    Text fields use CASE-WHEN-non-empty instead of COALESCE so an
    intentionally-omitted field on a later enrichment call does not blank out
    narrative a prior call already wrote (COALESCE alone can't distinguish
    "" from an unset value once both sides are non-NULL strings).
    """
    def _do():
        conn = get_db()
        try:
            # Root-cause fix (migration 029): the documented session_diary(add)
            # calling convention (.claude/rules/diary.md) never passes
            # session_uuid — every existing call site and future skill/rule
            # would otherwise keep hitting the legacy fragmenting INSERT.
            # Instead of requiring every caller to learn its own session UUID
            # (not exposed to the agent's tool-calling context — no
            # CLAUDE_SESSION_ID env var reaches Bash or MCP subprocesses),
            # auto-resolve it server-side from session_meta: the most
            # recently active session for this project, bounded by a
            # staleness window so a long-idle stale session_meta row for the
            # same project is never guessed as the match (doctrine:
            # best-effort, never fabricate a link).
            session_uuid = body.session_uuid
            if not session_uuid:
                max_age_min = int(os.environ.get("CRAG_ANCHOR_SESSION_LINK_MAX_AGE_MIN", "240"))
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_min)).isoformat()
                resolved = conn.execute(
                    """SELECT session_uuid FROM session_meta
                       WHERE project = ? AND last_seen_at >= ?
                       ORDER BY last_seen_at DESC LIMIT 1""",
                    (body.project, cutoff),
                ).fetchone()
                if resolved:
                    session_uuid = resolved["session_uuid"]

            if session_uuid:
                conn.execute(
                    """INSERT INTO sessions
                       (project, date, session_uuid, accomplished, files_changed, commits,
                        decisions, problems, next_steps, duration, raw_markdown,
                        narrative_updated_at)
                       VALUES
                       (:project, :date, :session_uuid, :accomplished, :files_changed, :commits,
                        :decisions, :problems, :next_steps, :duration, :raw_markdown,
                        :narrative_updated_at)
                       ON CONFLICT(session_uuid) WHERE session_uuid IS NOT NULL DO UPDATE SET
                         project      = CASE WHEN excluded.project != '' THEN excluded.project ELSE sessions.project END,
                         date         = CASE WHEN excluded.date != '' THEN excluded.date ELSE sessions.date END,
                         accomplished = CASE WHEN excluded.accomplished != '' THEN excluded.accomplished ELSE sessions.accomplished END,
                         files_changed= CASE WHEN excluded.files_changed != '' THEN excluded.files_changed ELSE sessions.files_changed END,
                         commits      = CASE WHEN excluded.commits != '' THEN excluded.commits ELSE sessions.commits END,
                         decisions    = CASE WHEN excluded.decisions != '' THEN excluded.decisions ELSE sessions.decisions END,
                         problems     = CASE WHEN excluded.problems != '' THEN excluded.problems ELSE sessions.problems END,
                         next_steps   = CASE WHEN excluded.next_steps != '' THEN excluded.next_steps ELSE sessions.next_steps END,
                         duration     = CASE WHEN excluded.duration != '' THEN excluded.duration ELSE sessions.duration END,
                         raw_markdown = CASE WHEN excluded.raw_markdown != '' THEN excluded.raw_markdown ELSE sessions.raw_markdown END,
                         narrative_updated_at = excluded.narrative_updated_at
                    """,
                    {
                        "project": body.project,
                        "date": body.date or _utcnow_iso()[:10],
                        "session_uuid": session_uuid,
                        "accomplished": body.accomplished,
                        "files_changed": body.files_changed,
                        "commits": body.commits,
                        "decisions": body.decisions,
                        "problems": body.problems,
                        "next_steps": body.next_steps,
                        "duration": body.duration,
                        "raw_markdown": body.raw_markdown,
                        "narrative_updated_at": _utcnow_iso(),
                    },
                )
                conn.commit()
                row = conn.execute(
                    "SELECT id FROM sessions WHERE session_uuid = ?", (session_uuid,)
                ).fetchone()
                return {"ok": True, "id": row["id"] if row else None}

            # Legacy path (no session_uuid): unchanged bare-INSERT behavior.
            conn.execute(
                """INSERT INTO sessions (project, date, accomplished, files_changed, commits,
                   decisions, problems, next_steps, duration, raw_markdown)
                   VALUES (:project, :date, :accomplished, :files_changed, :commits,
                   :decisions, :problems, :next_steps, :duration, :raw_markdown)""",
                {
                    "project": body.project,
                    "date": body.date or _utcnow_iso()[:10],
                    "accomplished": body.accomplished,
                    "files_changed": body.files_changed,
                    "commits": body.commits,
                    "decisions": body.decisions,
                    "problems": body.problems,
                    "next_steps": body.next_steps,
                    "duration": body.duration,
                    "raw_markdown": body.raw_markdown,
                },
            )
            conn.commit()
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return {"ok": True, "id": row_id}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


@app.get("/lifecycle/session/get")
async def lifecycle_session_get(project: str, limit: int = 5):
    """Get recent session diary entries for a project. Returns {sessions, count}."""
    def _do():
        conn = get_db()
        try:
            rows = conn.execute(
                """SELECT id, project, date, accomplished, files_changed, commits,
                   decisions, problems, next_steps, duration
                   FROM sessions WHERE project = ? ORDER BY date DESC LIMIT ?""",
                (project, max(1, min(limit, 50))),
            ).fetchall()
            return {"sessions": [dict(r) for r in rows], "count": len(rows)}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


# ── Project context ──────────────────────────────────────────────────────────

@app.get("/lifecycle/context/get")
async def lifecycle_context_get(project: str):
    """Get project context. Returns the row or {project, status:'no context saved'}."""
    def _do():
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM project_context WHERE project = ?", (project,)
            ).fetchone()
            if row:
                return dict(row)
            return {"project": project, "status": "no context saved"}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


@app.post("/lifecycle/context/set")
async def lifecycle_context_set(body: ContextSetBody):
    """Upsert project context. Returns {ok, project}."""
    def _do():
        conn = get_db()
        try:
            conn.execute(
                """INSERT INTO project_context (project, status, current_branch,
                   last_session_date, architecture_decisions, known_issues, backlog, updated_at)
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
                    "project": body.project,
                    "status": body.status,
                    "current_branch": body.current_branch,
                    "last_session_date": body.last_session_date,
                    "architecture_decisions": body.architecture_decisions,
                    "known_issues": body.known_issues,
                    "backlog": body.backlog,
                    "updated_at": _utcnow_iso(),
                },
            )
            conn.commit()
            return {"ok": True, "project": body.project}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


# ── Events ────────────────────────────────────────────────────────────────────

@app.post("/lifecycle/events/add")
async def lifecycle_events_add(body: EventAddBody):
    """Enqueue a pending event. Returns {ok, id}."""
    if body.priority not in ("critical", "high", "normal", "low"):
        return JSONResponse(status_code=422, content={"ok": False, "error": "priority must be critical|high|normal|low"})

    def _do():
        conn = get_db()
        try:
            conn.execute(
                """INSERT INTO pending_events (project, source, event_type, summary,
                   payload, priority, expires_at)
                   VALUES (:project, :source, :event_type, :summary,
                   :payload, :priority, :expires_at)""",
                {
                    "project": body.project,
                    "source": body.source,
                    "event_type": body.event_type,
                    "summary": body.summary,
                    "payload": body.payload,
                    "priority": body.priority,
                    "expires_at": body.expires_at,
                },
            )
            conn.commit()
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return {"ok": True, "id": row_id}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


@app.get("/lifecycle/events/list")
async def lifecycle_events_list(
    project: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 20,
):
    """List pending events ordered by priority then creation. Returns {events, count}."""
    def _do():
        conn = get_db()
        try:
            # Expire stale events first
            conn.execute(
                "UPDATE pending_events SET status='expired' WHERE status='pending'"
                " AND expires_at IS NOT NULL AND expires_at < ?",
                (_utcnow_iso(),),
            )
            conn.commit()
            conditions = ["status = 'pending'"]
            params: list = []
            if project:
                conditions.append("(project = ? OR project IS NULL)")
                params.append(project)
            if priority:
                conditions.append("priority = ?")
                params.append(priority)
            rows = conn.execute(
                f"SELECT * FROM pending_events WHERE {' AND '.join(conditions)}"
                " ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1"
                " WHEN 'normal' THEN 2 ELSE 3 END, created_at ASC LIMIT ?",
                params + [max(1, min(limit, 200))],
            ).fetchall()
            return {"events": [dict(r) for r in rows], "count": len(rows)}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


@app.post("/lifecycle/events/claim")
async def lifecycle_events_claim(body: EventClaimBody):
    """Claim a pending event. Returns {ok, id} or {ok:False, error}."""
    def _do():
        conn = get_db()
        try:
            cur = conn.execute(
                "UPDATE pending_events SET status='claimed', claimed_by=?, claimed_at=?"
                " WHERE id=? AND status='pending'",
                (body.claimed_by, _utcnow_iso(), body.id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return {"ok": False, "error": "Event not found or already claimed"}
            return {"ok": True, "id": body.id}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


@app.post("/lifecycle/events/complete")
async def lifecycle_events_complete(body: EventCompleteBody):
    """Mark an event completed or failed. Returns {ok, id, status}."""
    if body.status not in ("completed", "failed"):
        return JSONResponse(status_code=422, content={"ok": False, "error": "status must be completed|failed"})

    def _do():
        conn = get_db()
        try:
            cur = conn.execute(
                "UPDATE pending_events SET status=?, result=?, completed_at=? WHERE id=?",
                (body.status, body.result, _utcnow_iso(), body.id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return {"ok": False, "error": f"Event not found: {body.id}"}
            return {"ok": True, "id": body.id, "status": body.status}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


@app.post("/lifecycle/events/bulk_expire")
async def lifecycle_events_bulk_expire(body: EventBulkExpireBody):
    """Expire all pending events matching optional project/priority. Returns {ok, expired}."""
    def _do():
        conn = get_db()
        try:
            filters = ["status = 'pending'"]
            params: list = [_utcnow_iso()]
            if body.project:
                filters.append("project = ?")
                params.append(body.project)
            if body.priority:
                filters.append("priority = ?")
                params.append(body.priority)
            cur = conn.execute(
                f"UPDATE pending_events SET status='expired', completed_at=? WHERE {' AND '.join(filters)}",
                params,
            )
            conn.commit()
            return {"ok": True, "expired": cur.rowcount}
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


# ── Cost report ───────────────────────────────────────────────────────────────

@app.get("/lifecycle/cost_report")
async def lifecycle_cost_report(project: Optional[str] = None, days: int = 7):
    """Token/cost report: totals, per-project breakdown, 7-day trend.
    Returns {totals, by_project, trend}."""
    def _do():
        conn = get_db()
        try:
            conditions: list[str] = []
            params: list = []
            if project:
                conditions.append("project = ?")
                params.append(project)
            if days:
                conditions.append("julianday('now') - julianday(created_at) <= ?")
                params.append(days)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            totals_row = conn.execute(
                f"""SELECT COUNT(*) as sessions, SUM(tokens_in) as total_in,
                    SUM(tokens_out) as total_out, SUM(cache_hits) as total_cache_hits,
                    SUM(wall_time_sec) as total_wall_sec,
                    AVG(rtk_savings_pct) as avg_rtk, AVG(headroom_savings_pct) as avg_headroom
                    FROM token_ledger {where}""",
                params,
            ).fetchone()

            by_project_rows = conn.execute(
                f"""SELECT project, COUNT(*) as sessions, SUM(tokens_in) as tokens_in,
                    SUM(tokens_out) as tokens_out, SUM(cache_hits) as cache_hits,
                    AVG(rtk_savings_pct) as avg_rtk, AVG(headroom_savings_pct) as avg_headroom
                    FROM token_ledger {where} GROUP BY project ORDER BY tokens_in DESC""",
                params,
            ).fetchall()

            trend_rows = conn.execute(
                f"""SELECT date(created_at) as day, SUM(tokens_in) as tokens_in,
                    SUM(tokens_out) as tokens_out, COUNT(*) as sessions
                    FROM token_ledger {where}
                    GROUP BY date(created_at) ORDER BY day DESC LIMIT 7""",
                params,
            ).fetchall()

            return {
                "totals": dict(totals_row) if totals_row else {},
                "by_project": [dict(r) for r in by_project_rows],
                "trend": [dict(r) for r in trend_rows],
            }
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


# ===========================================================================
# C3 — Coordinator brief endpoint
# ===========================================================================

@app.get("/brief")
async def coordinator_brief(project: str):
    """ONE-call pre-flight brief for a coordinator agent.

    Returns:
      principles      — up to 10 principles for the project (≤ 120 chars each)
      pending_events  — CRITICAL + HIGH pending events (≤ 5)
      grounding_flags — count of claims in the drift queue for this project
      last_session    — most recent session accomplished field (≤ 120 chars)
      token_nudge     — last 7-day token total for the project
      grounding_jobs  — pending/running grounding v2 job count (0 if v2 disabled)

    Target < 200 ms (single DB connection, no LLM calls).
    """
    def _do():
        conn = get_db()
        try:
            # Principles (top 10 by confidence, no status column in schema)
            principle_rows = conn.execute(
                """SELECT content FROM principles
                   WHERE (project = ? OR project IS NULL)
                   AND (superseded_by IS NULL)
                   ORDER BY confidence DESC LIMIT 10""",
                (project,),
            ).fetchall()
            principles = [r["content"][:120] for r in principle_rows]

            # Pending events (critical + high only, max 5)
            conn.execute(
                "UPDATE pending_events SET status='expired' WHERE status='pending'"
                " AND expires_at IS NOT NULL AND expires_at < ?",
                (_utcnow_iso(),),
            )
            event_rows = conn.execute(
                """SELECT id, priority, event_type, summary, created_at FROM pending_events
                   WHERE status='pending' AND priority IN ('critical','high')
                   AND (project = ? OR project IS NULL)
                   ORDER BY CASE priority WHEN 'critical' THEN 0 ELSE 1 END,
                   created_at ASC LIMIT 5""",
                (project,),
            ).fetchall()
            pending_events = [dict(r) for r in event_rows]

            # Grounding drift flags — open entries in grounding_queue for this project
            grounding_flags = 0
            if _table_exists(conn, "grounding_queue"):
                row = conn.execute(
                    """SELECT COUNT(*) FROM grounding_queue gq
                       LEFT JOIN insights i
                         ON gq.claim_kind='insight' AND i.id=gq.claim_id
                       WHERE gq.status='open'
                       AND (i.project IS NULL OR i.project = ?)""",
                    (project,),
                ).fetchone()
                grounding_flags = row[0] if row else 0

            # Last session
            session_row = conn.execute(
                "SELECT date, accomplished FROM sessions WHERE project=? ORDER BY date DESC LIMIT 1",
                (project,),
            ).fetchone()
            last_session = None
            if session_row and session_row["accomplished"]:
                last_session = f"{session_row['date']}: {session_row['accomplished']}"[:120]

            # Token nudge (last 7d total_in for this project)
            token_row = conn.execute(
                """SELECT SUM(tokens_in) as total FROM token_ledger
                   WHERE project=? AND julianday('now') - julianday(created_at) <= 7""",
                (project,),
            ).fetchone()
            token_nudge = token_row["total"] if token_row and token_row["total"] else 0

            # Grounding v2 job queue depth
            grounding_jobs = 0
            if _GROUNDING_V2 and _table_exists(conn, "grounding_jobs"):
                job_row = conn.execute(
                    "SELECT COUNT(*) FROM grounding_jobs WHERE status IN ('pending','running')"
                ).fetchone()
                grounding_jobs = job_row[0] if job_row else 0

            conn.commit()  # persist the expires we wrote above
            return {
                "project": project,
                "principles": principles,
                "pending_events": pending_events,
                "grounding_flags": grounding_flags,
                "last_session": last_session,
                "token_nudge": token_nudge,
                "grounding_jobs": grounding_jobs,
            }
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


# ===========================================================================
# D — Self-describing surface: /llms.txt + /guide
# ===========================================================================

def _load_capabilities():
    """Import capabilities module. Cached after first load."""
    if not hasattr(_load_capabilities, "_mod"):
        import importlib.util as _ilu
        _cap_path = DB_DIR / "capabilities.py"
        _spec = _ilu.spec_from_file_location("capabilities", _cap_path)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _load_capabilities._mod = _mod
    return _load_capabilities._mod


@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt():
    """Machine-readable memory surface document. Suitable for LLM context injection."""
    cap = _load_capabilities()
    return PlainTextResponse(cap.render_llms_txt(), media_type="text/plain; charset=utf-8")


@app.get("/guide")
async def guide():
    """Structured JSON guide to all crag-anchor tools and endpoints."""
    cap = _load_capabilities()
    return JSONResponse(content=cap.render_guide())


# ===========================================================================
# Graph v2 (migration 027) — traversal endpoints
# ===========================================================================

@app.get("/graph/siblings")
async def graph_siblings(claim_kind: str, claim_id: int, limit: int = 20):
    """Return claims that share ≥1 canonical entity with the given claim.

    Ranked by number of shared canonical entities (descending).
    claim_kind: 'insight' | 'principle'
    """
    def _do():
        conn = get_db()
        try:
            # Find canonical entity IDs linked to the query claim.
            if claim_kind == "insight":
                anchor_rows = conn.execute(
                    """SELECT DISTINCT el.canonical_entity_id
                       FROM entity_links el
                       WHERE el.insight_id = ? AND el.canonical_entity_id IS NOT NULL""",
                    (claim_id,),
                ).fetchall()
            elif claim_kind == "principle":
                anchor_rows = conn.execute(
                    """SELECT DISTINCT el.canonical_entity_id
                       FROM entity_links el
                       WHERE el.principle_id = ? AND el.canonical_entity_id IS NOT NULL""",
                    (claim_id,),
                ).fetchall()
            else:
                return {"error": "claim_kind must be 'insight' or 'principle'"}

            anchor_ids = [r["canonical_entity_id"] for r in anchor_rows]
            if not anchor_ids:
                return {"claim_kind": claim_kind, "claim_id": claim_id,
                        "siblings": [], "anchor_entities": 0}

            placeholders = ",".join("?" * len(anchor_ids))

            # Insights sharing at least one canonical entity (excluding self)
            insight_siblings = conn.execute(
                f"""SELECT el2.insight_id AS sibling_id, 'insight' AS sibling_kind,
                           COUNT(*) AS shared_count
                    FROM entity_links el2
                    WHERE el2.canonical_entity_id IN ({placeholders})
                      AND el2.insight_id IS NOT NULL
                      AND NOT (el2.insight_id = ? AND 'insight' = ?)
                    GROUP BY el2.insight_id
                    ORDER BY shared_count DESC
                    LIMIT ?""",
                [*anchor_ids, claim_id, claim_kind, limit],
            ).fetchall()

            # Principles sharing at least one canonical entity (excluding self)
            principle_siblings = conn.execute(
                f"""SELECT el2.principle_id AS sibling_id, 'principle' AS sibling_kind,
                           COUNT(*) AS shared_count
                    FROM entity_links el2
                    WHERE el2.canonical_entity_id IN ({placeholders})
                      AND el2.principle_id IS NOT NULL
                      AND NOT (el2.principle_id = ? AND 'principle' = ?)
                    GROUP BY el2.principle_id
                    ORDER BY shared_count DESC
                    LIMIT ?""",
                [*anchor_ids, claim_id, claim_kind, limit],
            ).fetchall()

            siblings = sorted(
                [dict(r) for r in insight_siblings] + [dict(r) for r in principle_siblings],
                key=lambda x: x["shared_count"],
                reverse=True,
            )[:limit]

            return {
                "claim_kind": claim_kind,
                "claim_id": claim_id,
                "anchor_entities": len(anchor_ids),
                "siblings": siblings,
            }
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


@app.get("/graph/neighbors")
async def graph_neighbors(entity: str, entity_type: str, limit: int = 20):
    """Return canonical entity info + typed relations + linked claims count.

    Resolves the raw entity value to its canonical form first.
    """
    def _do():
        conn = get_db()
        try:
            norm = _normalize_entity(entity_type, entity)
            canonical = norm["canonical"]

            # Resolve to entity_canonical row
            ec_row = conn.execute(
                """SELECT * FROM entity_canonical
                   WHERE entity_type = ? AND (canonical = ? OR raw_value = ?)""",
                (entity_type, canonical, entity),
            ).fetchone()

            if not ec_row:
                return {"entity": entity, "entity_type": entity_type,
                        "canonical": canonical, "found": False, "neighbors": []}

            ec_id = ec_row["id"]

            # Resolve the full alias family: entity_canonical allows several rows
            # with distinct raw_value but the SAME canonical (UNIQUE is on
            # (entity_type, raw_value), not canonical). entity_relations /
            # entity_links may have been seeded against ANY alias id, so a
            # single ec_id lookup silently misses relations attached to a
            # sibling alias row. Query all ids sharing this canonical instead.
            alias_rows = conn.execute(
                "SELECT id FROM entity_canonical WHERE entity_type = ? AND canonical = ?",
                (entity_type, ec_row["canonical"]),
            ).fetchall()
            alias_ids = [r["id"] for r in alias_rows] or [ec_id]
            alias_placeholders = ",".join("?" * len(alias_ids))

            # Typed relations where this entity is entity_a or entity_b
            relations_out = conn.execute(
                f"""SELECT er.relation_type,
                          ecb.entity_type AS target_type,
                          ecb.canonical   AS target_canonical,
                          ecb.id          AS target_id
                   FROM entity_relations er
                   JOIN entity_canonical ecb ON ecb.id = er.entity_b_id
                   WHERE er.entity_a_id IN ({alias_placeholders})
                   LIMIT ?""",
                (*alias_ids, limit),
            ).fetchall()

            relations_in = conn.execute(
                f"""SELECT er.relation_type,
                          eca.entity_type AS source_type,
                          eca.canonical   AS source_canonical,
                          eca.id          AS source_id
                   FROM entity_relations er
                   JOIN entity_canonical eca ON eca.id = er.entity_a_id
                   WHERE er.entity_b_id IN ({alias_placeholders})
                   LIMIT ?""",
                (*alias_ids, limit),
            ).fetchall()

            # Count claims linked to this canonical entity (any alias id)
            claims_count = conn.execute(
                f"""SELECT COUNT(DISTINCT insight_id) + COUNT(DISTINCT principle_id)
                   FROM entity_links WHERE canonical_entity_id IN ({alias_placeholders})""",
                alias_ids,
            ).fetchone()[0]

            return {
                "entity": entity,
                "entity_type": entity_type,
                "canonical": canonical,
                "found": True,
                "canonical_entity_id": ec_id,
                "claims_count": claims_count,
                "relations_out": [dict(r) for r in relations_out],
                "relations_in": [dict(r) for r in relations_in],
            }
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


@app.get("/graph/impact")
async def graph_impact(entity: str, entity_type: str):
    """Return all claims linked to the entity + 1-hop entity neighbors and their claims.

    'Impact zone' of an entity: what would be affected if this entity changed.
    """
    def _do():
        conn = get_db()
        try:
            norm = _normalize_entity(entity_type, entity)
            canonical = norm["canonical"]

            ec_row = conn.execute(
                """SELECT id, canonical FROM entity_canonical
                   WHERE entity_type = ? AND (canonical = ? OR raw_value = ?)""",
                (entity_type, canonical, entity),
            ).fetchone()

            if not ec_row:
                return {"entity": entity, "entity_type": entity_type,
                        "canonical": canonical, "found": False, "impact": []}

            ec_id = ec_row["id"]

            # Resolve the full alias family sharing this canonical value (see
            # /graph/neighbors for why: entity_relations may be seeded against
            # a sibling alias id, not the one this lookup happens to match).
            alias_rows = conn.execute(
                "SELECT id FROM entity_canonical WHERE entity_type = ? AND canonical = ?",
                (entity_type, ec_row["canonical"]),
            ).fetchall()
            alias_ids = [r["id"] for r in alias_rows] or [ec_id]
            alias_placeholders = ",".join("?" * len(alias_ids))

            # 1-hop neighbor entity IDs via entity_relations
            neighbor_ids = set(alias_ids)  # include self (+ aliases)
            hop1_rows = conn.execute(
                f"""SELECT entity_b_id AS nb FROM entity_relations
                    WHERE entity_a_id IN ({alias_placeholders})
                    UNION
                    SELECT entity_a_id AS nb FROM entity_relations
                    WHERE entity_b_id IN ({alias_placeholders})""",
                (*alias_ids, *alias_ids),
            ).fetchall()
            for r in hop1_rows:
                neighbor_ids.add(r["nb"])

            placeholders = ",".join("?" * len(neighbor_ids))
            nb_list = list(neighbor_ids)

            # All insight IDs linked to any of these canonical entities
            insight_ids = conn.execute(
                f"""SELECT DISTINCT insight_id FROM entity_links
                    WHERE canonical_entity_id IN ({placeholders})
                      AND insight_id IS NOT NULL""",
                nb_list,
            ).fetchall()

            # All principle IDs linked to any of these canonical entities
            principle_ids = conn.execute(
                f"""SELECT DISTINCT principle_id FROM entity_links
                    WHERE canonical_entity_id IN ({placeholders})
                      AND principle_id IS NOT NULL""",
                nb_list,
            ).fetchall()

            return {
                "entity": entity,
                "entity_type": entity_type,
                "canonical": canonical,
                "found": True,
                "canonical_entity_id": ec_id,
                # neighbor_ids seeds with the full alias family (self), not just
                # one id, so exclude all of them — not a hardcoded 1 — to get
                # the true hop-1 count.
                "hop1_neighbors": len(neighbor_ids) - len(alias_ids),
                "impacted_insights": [r["insight_id"] for r in insight_ids],
                "impacted_principles": [r["principle_id"] for r in principle_ids],
            }
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    return JSONResponse(content=await loop.run_in_executor(None, _do))


# ---------------------------------------------------------------------------
# Insights session_id backfill — placeholder kept for migration 022 compatibility
# (actual backfill code removed; endpoint /admin/backfill_token_ledger above)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Embedded console — the operator console is a static Vite build served BY this
# daemon so the engine + console are ONE deployable unit (one release updates
# both). Mounted at /console with SPA fallback: any client-side route
# (/console/claims, /console/review, ...) that is not a real file returns
# index.html so the router can take over.
#
# Fail-soft: if apps/console/dist is absent (console not built), /console
# returns a JSON hint instead of a 500 — the daemon stays fully functional as
# an API without the UI.
# ---------------------------------------------------------------------------

_CONSOLE_DIST = Path(__file__).resolve().parents[1] / "console" / "dist"


def _mount_console() -> None:
    from starlette.responses import FileResponse
    from starlette.staticfiles import StaticFiles

    if not (_CONSOLE_DIST / "index.html").is_file():
        @app.get("/console")
        @app.get("/console/{path:path}")
        async def _console_missing(path: str = ""):
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "console not built",
                    "hint": "npm run build in apps/console",
                },
            )
        logger.info("console dist absent — /console serves build hint (fail-soft)")
        return

    # Config-driven frame policy for embedding. When
    # CRAG_ANCHOR_CONSOLE_FRAME_ANCESTORS is set (space-separated origins), the
    # console mount emits a CSP frame-ancestors header so those hosts may iframe
    # it (e.g. "https://app.crag.sh"). Unset => no header, browser same-origin
    # default applies. Scoped to the console static mount only — the API is
    # untouched.
    _frame_ancestors = os.environ.get("CRAG_ANCHOR_CONSOLE_FRAME_ANCESTORS", "").strip()

    class _SpaStaticFiles(StaticFiles):
        """StaticFiles that falls back to index.html for unmatched paths so the
        client-side router owns deep links (/console/claims etc.), and stamps the
        optional frame-ancestors CSP so the console can be embedded off-origin."""

        def _apply_frame_policy(self, response):
            if _frame_ancestors:
                response.headers["Content-Security-Policy"] = (
                    f"frame-ancestors {_frame_ancestors}"
                )
            return response

        async def get_response(self, path, scope):
            try:
                return self._apply_frame_policy(await super().get_response(path, scope))
            except Exception:
                # 404 from a missing file (client route) -> serve the SPA shell.
                return self._apply_frame_policy(
                    FileResponse(_CONSOLE_DIST / "index.html")
                )

    app.mount("/console", _SpaStaticFiles(directory=str(_CONSOLE_DIST), html=True),
              name="console")
    logger.info(
        "console mounted at /console from %s (frame-ancestors=%s)",
        _CONSOLE_DIST,
        _frame_ancestors or "unset",
    )


_mount_console()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_config=None)
