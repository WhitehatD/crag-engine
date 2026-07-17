#!/usr/bin/env python3
"""Phase 16-D — Auto-clear known-FP-pattern suspect flags.

Runs as a daily scheduled task (cron / Task Scheduler). Walks the open contradiction
queue (/audit_contradictions), applies the same heuristics Phase 16-C uses
for new-flag suppression (provenance, temporal cohort), and calls
/clear_suspect for matches. Leaves genuinely-uncertain pairs alone for
manual operator triage.

Heuristics (in order, cheapest first):
  1. provenance — A in B.source_insights (or vice versa), or A.promoted_to == B.id
  2. temporal-cohort — both created within 2h of each other
  3. same-source-file — both reference same source_file path (OPT-IN, off by default)

Logs to the engine logs dir, optionally sends an ntfy summary (set NTFY_URL).

Idempotent: re-running has no effect if no new FPs match the patterns.

Usage:
  python auto-clear-fp-patterns.py [--dry-run] [--project X] [--max N] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# CRAG_ENGINE_DB_PATH / CRAG_ENGINE_LOG_DIR defaults resolve through the shared
# accessor db/engine_paths.py (env → stack.toml → today's default). The explicit
# env vars still win (preserved below); when unset the
# accessor supplies db_path / log_dir instead of the old hardcoded literals.
# This file lives in apps/cron/; the repo root is two parents up.
_CRON_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_CRON_REPO_ROOT / "db"))
try:
    from engine_paths import get_paths as _get_engine_paths  # noqa: E402
    _BP = _get_engine_paths()
except Exception:
    _BP = None

DAEMON_URL = os.environ.get("CRAG_ENGINE_DAEMON_URL", "http://127.0.0.1:8786")
if os.environ.get("CRAG_ENGINE_DB_PATH"):
    ENGINE_DB = Path(os.environ["CRAG_ENGINE_DB_PATH"])
elif _BP is not None:
    ENGINE_DB = _BP.db_path
else:
    ENGINE_DB = Path(__file__).resolve().parent.parent.parent / "db" / "engine.db"
TEMPORAL_COHORT_SECONDS = int(os.environ.get("CRAG_ENGINE_CONTRA_TEMPORAL_COHORT_SECONDS", "7200"))
SAME_SOURCE_FILE = os.environ.get("CRAG_ENGINE_CONTRA_SKIP_SAME_SOURCE", "0") == "1"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "crag-engine")
NTFY_URL = os.environ.get("NTFY_URL", "")  # empty = notifications disabled

if os.environ.get("CRAG_ENGINE_LOG_DIR"):
    LOG_DIR = Path(os.environ["CRAG_ENGINE_LOG_DIR"])
elif _BP is not None:
    LOG_DIR = _BP.log_dir
else:
    LOG_DIR = _CRON_REPO_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "auto-clear-fp.log"


def _setup_logging(verbose: bool) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("auto-clear-fp")


def _http_json(method: str, path: str, body: Optional[dict] = None) -> dict:
    url = f"{DAEMON_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "status": exc.code, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _parse_iso(ts: Optional[str]) -> Optional[float]:
    """Parse either historical TEXT timestamp format to epoch seconds:
    legacy SQLite 'YYYY-MM-DD HH:MM:SS' (naive-UTC) or canonical offset-aware
    ISO-T. The old strptime pair had no %z, so '+00:00'-suffixed values raised
    ValueError → None → the temporal-cohort FP check failed OPEN (2026-07-02
    audit finding)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace(" ", "T").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _load_principle_source_map() -> dict[int, set[int]]:
    """WS2 T4b — read-only map: principle_id -> {source insight ids}.

    principles.source_insights is a comma-separated list of insight ids (e.g.
    '2079,2083'). Two insights that BOTH seed the same principle (or that share a
    promotion lineage) are provenance-linked and their contradiction flag is a
    FALSE POSITIVE. Pre-WS2 this map was never consulted, so provenance FPs
    involving source_insights never cleared (auto-clear reported 0). Read-only
    (mode=ro) — never mutates the live DB.
    """
    out: dict[int, set[int]] = {}
    if not ENGINE_DB.exists():
        return out
    try:
        conn = sqlite3.connect(f"file:{ENGINE_DB}?mode=ro", uri=True)
        try:
            for pid, src in conn.execute(
                "SELECT id, source_insights FROM principles WHERE source_insights IS NOT NULL"
            ):
                ids = {int(x) for x in str(src).replace(" ", "").split(",") if x.strip().isdigit()}
                if ids:
                    out[pid] = ids
        finally:
            conn.close()
    except Exception:
        pass  # best-effort; falls back to promoted_to-only provenance
    return out


# Loaded once per run in main(); global so _in_provenance_chain stays pure-ish.
_PRINCIPLE_SOURCE_MAP: dict[int, set[int]] = {}


def _in_provenance_chain(a: dict, b: dict) -> bool:
    """A and B linked via promoted_to / source_insights (WS2 T4b — both directions).

    Cleared as FP when:
      1. A.promoted_to == B.id  OR  B.promoted_to == A.id  (direct promotion), OR
      2. A.promoted_to == B.promoted_to (both promoted into the same principle), OR
      3. A.id and B.id BOTH appear in some principle's source_insights list
         (co-distilled — the previously-skipped case).
    """
    a_id, b_id = a.get("id"), b.get("id")
    a_promoted = a.get("promoted_to")
    b_promoted = b.get("promoted_to")
    # 1 — direct promotion, either direction
    if a_promoted and a_promoted == b_id:
        return True
    if b_promoted and b_promoted == a_id:
        return True
    # 2 — both promoted into the same principle
    if a_promoted and b_promoted and a_promoted == b_promoted:
        return True
    # 3 — co-listed in a principle's source_insights (the skipped case)
    if a_id is not None and b_id is not None:
        for src_ids in _PRINCIPLE_SOURCE_MAP.values():
            if a_id in src_ids and b_id in src_ids:
                return True
        # promoted_to points at a principle whose source_insights contains the other
        if a_promoted and b_id in _PRINCIPLE_SOURCE_MAP.get(a_promoted, set()):
            return True
        if b_promoted and a_id in _PRINCIPLE_SOURCE_MAP.get(b_promoted, set()):
            return True
    return False


def _within_temporal_cohort(a: dict, b: dict, max_seconds: int) -> bool:
    t_a = _parse_iso(a.get("created_at"))
    t_b = _parse_iso(b.get("created_at"))
    if t_a is None or t_b is None:
        return False
    return abs(t_a - t_b) <= max_seconds


def _same_source_file(a: dict, b: dict) -> bool:
    sa = (a.get("source_file") or "").strip()
    sb = (b.get("source_file") or "").strip()
    return bool(sa) and sa == sb


def _classify_pair(a: dict, b: dict) -> Optional[str]:
    """Returns the FP-pattern name if matched, else None."""
    if _in_provenance_chain(a, b):
        return "provenance"
    if TEMPORAL_COHORT_SECONDS > 0 and _within_temporal_cohort(a, b, TEMPORAL_COHORT_SECONDS):
        return "temporal-cohort"
    if SAME_SOURCE_FILE and _same_source_file(a, b):
        return "same-source-file"
    return None


def _send_ntfy(message: str, priority: str = "default") -> None:
    if not NTFY_TOPIC or not NTFY_URL:
        return
    url = f"{NTFY_URL.rstrip('/')}/{NTFY_TOPIC}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={"Priority": priority, "Title": "crag-engine FP-sweep"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass  # ntfy is best-effort


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Don't actually clear, just report")
    p.add_argument("--project", help="Limit to one project")
    p.add_argument("--max", type=int, default=200, help="Max pairs to evaluate this run")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--no-ntfy", action="store_true", help="Skip ntfy notification")
    args = p.parse_args()

    log = _setup_logging(args.verbose)
    t0 = time.time()
    log.info("Phase 16-D auto-clear started (dry_run=%s, project=%s)",
             args.dry_run, args.project or "all")

    # WS2 T4b — load the principle source_insights map once (read-only) so
    # co-distilled provenance FPs actually clear.
    global _PRINCIPLE_SOURCE_MAP
    _PRINCIPLE_SOURCE_MAP = _load_principle_source_map()
    log.info("Loaded %d principle source-insight chains (provenance T4b)",
             len(_PRINCIPLE_SOURCE_MAP))

    # Step 1 — fetch open contradictions
    path = "/audit_contradictions"
    if args.project:
        path += f"?project={args.project}"
    audit = _http_json("GET", path)
    if not audit.get("ok"):
        log.error("audit_contradictions failed: %s", audit.get("error"))
        return 1

    contradictions = audit.get("contradictions", [])[: args.max]
    log.info("Open contradictions: %d (limited to %d)", audit.get("count", 0), len(contradictions))

    # Step 2 — classify each pair via heuristics
    stats = {"provenance": 0, "temporal-cohort": 0, "same-source-file": 0,
             "uncertain": 0, "errors": 0, "cleared_ids": []}

    for row in contradictions:
        loser_id = row.get("id")
        winner_id = row.get("suspect_of")
        if not loser_id or not winner_id:
            continue

        # Fetch both sides. WS2 T4b bugfix: /insight/{id} returns a WRAPPED
        # envelope {"ok":true,"insight":{...}} — the pre-WS2 code treated the
        # envelope AS the row, so promoted_to / created_at / source_file were
        # ALWAYS None and every heuristic silently returned False (→ cleared 0).
        a_env = _http_json("GET", f"/insight/{loser_id}")
        b_env = _http_json("GET", f"/insight/{winner_id}")
        if a_env.get("ok") is False or b_env.get("ok") is False:
            stats["errors"] += 1
            log.debug("Skipped pair %d<->%d: fetch failed", loser_id, winner_id)
            continue
        a = a_env.get("insight") or a_env  # unwrap; tolerate un-wrapped mocks in tests
        b = b_env.get("insight") or b_env
        if "id" not in a:
            a["id"] = loser_id
        if "id" not in b:
            b["id"] = winner_id

        pattern = _classify_pair(a, b)
        if pattern is None:
            stats["uncertain"] += 1
            continue

        stats[pattern] += 1
        log.info("Pair %d<->%d matches FP pattern: %s", loser_id, winner_id, pattern)

        if args.dry_run:
            continue

        # Step 3 — clear suspect flag
        result = _http_json("POST", "/clear_suspect", {
            "id": loser_id,
            "reason": f"auto-fp-sweep:{pattern}",
        })
        if result.get("ok"):
            stats["cleared_ids"].extend(result.get("cleared", []))
        else:
            stats["errors"] += 1
            log.warning("clear_suspect failed for %d: %s", loser_id, result.get("error"))

    # Step 4 — summary
    dur = time.time() - t0
    summary = (
        f"FP-sweep ({'DRY' if args.dry_run else 'LIVE'}): "
        f"{len(stats['cleared_ids'])} cleared, "
        f"{stats['provenance']} provenance + "
        f"{stats['temporal-cohort']} temporal + "
        f"{stats['same-source-file']} same-file matches | "
        f"{stats['uncertain']} uncertain (kept for triage) | "
        f"{stats['errors']} errors | "
        f"{dur:.1f}s"
    )
    log.info(summary)

    if not args.no_ntfy and (stats["cleared_ids"] or stats["errors"]):
        priority = "high" if stats["errors"] else "default"
        _send_ntfy(summary, priority=priority)

    return 0 if stats["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
