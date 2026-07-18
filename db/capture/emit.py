# coding: utf-8
"""Emit sanitized ExtractedCandidates to the daemon's /capture/event
receiver (docs/architecture.md REV 6/9). This is capture's ONLY write path —
it is CAPABILITY-SCOPED to staging: POSTing to /capture/event can only ever
land a row in `insights_staging` (quarantine), never in `insights` directly
(migration 031's endpoint contract) or in `principles`/governance. Promotion
out of quarantine is the Disposition Engine's job (db/disposition.py),
already merged and unchanged by this module.

Two anti-storm layers on top of the daemon's own dedup_key staging-dedup:
  1. Per-session-per-UTC-day emit budget (db/capture/state.py rate limit) —
     a chatty session cannot flood staging.
  2. Corpus near-dup check (embedding cosine >= dedup_similarity against
     ACTIVE insights) — "a lesson already known = noop" (REV 6). Best-
     effort + fail-open: if engine.db or the embedding model is unavailable,
     this layer is skipped and the daemon's dedup_key staging-dedup still
     applies.

House style: never raises to the caller; every failure returns a dict with
ok=False and a reason instead of propagating.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crag-anchor-capture")

_THIS_DIR = Path(__file__).resolve().parent           # db/capture/
_DB_DIR = _THIS_DIR.parent                              # db/
if str(_DB_DIR) not in sys.path:
    sys.path.insert(0, str(_DB_DIR))
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from extractor import ExtractedCandidate  # noqa: E402
import state as capture_state  # noqa: E402

HTTP_TIMEOUT_SEC = 10

# Category -> write_gate.VALID_TYPES mapping. Every category maps to an
# existing MCP save_insight type — capture never invents a new insight type.
_CATEGORY_TO_TYPE = {
    "correction": "feedback",
    "discovered-practice": "pattern",
    "anti-pattern": "gotcha",
    "craft-meta": "reference",
}

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


def _normalize(text: str) -> str:
    t = (text or "").lower()
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def dedup_key_for(candidate: ExtractedCandidate) -> str:
    blob = candidate.category + "|" + _normalize(candidate.text)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def build_payload(candidate: ExtractedCandidate) -> dict:
    type_ = _CATEGORY_TO_TYPE.get(candidate.category, "gotcha")
    tags = f"capture:{candidate.category},evidence:{candidate.evidence_ref}"
    payload = {
        "content": candidate.text,
        "type": type_,
        "tags": tags,
        "source_file": None,
    }
    if candidate.applicability:
        payload["applicability"] = candidate.applicability
    return payload


def is_known_lesson(engine_db_path: str, text: str, threshold: float) -> bool:
    """Best-effort near-dup check against ACTIVE insights' embeddings.
    Fail-open: returns False (not known — allow emission) on any error, so a
    dedup-layer failure never blocks a legitimate capture."""
    try:
        import embed
        import numpy as np
        q_bytes = embed.embed_text(text)
        q = np.frombuffer(q_bytes, dtype="float32")
        conn = sqlite3.connect(f"file:{engine_db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT embedding FROM insights WHERE status='active' AND embedding IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
        for (blob,) in rows:
            v = np.frombuffer(blob, dtype="float32")
            if v.shape[0] != q.shape[0]:
                continue
            if embed.cosine_sim(q, v) >= threshold:
                return True
        return False
    except Exception as exc:
        logger.debug("emit: is_known_lesson dedup check failed (fail-open): %s", exc)
        return False


def _resolve_event_token() -> str:
    """Resolve the shared secret to authenticate POST /capture/event (rev-9
    §9.2). Uses the same file-vs-inline precedence the daemon enforces
    (auth_token_file > CRAG_ANCHOR_CAPTURE_TOKEN > [capture].event_token) via
    config.effective_event_token; falls back to the raw env var if config is
    unavailable. "" means fail-open (daemon accepts unauthenticated)."""
    try:
        import config as capture_config
        if hasattr(capture_config, "effective_event_token"):
            return str(capture_config.effective_event_token() or "")
        return str(getattr(capture_config.get_config(), "event_token", "") or "")
    except Exception:
        import os
        return str(os.environ.get("CRAG_ANCHOR_CAPTURE_TOKEN", "") or "")


def post_capture_event(daemon_url: str, source: str, payload: dict,
                       project: Optional[str], dedup_key: str) -> dict:
    """POST to /capture/event. Never raises — network/HTTP errors return
    {"ok": False, "error": ...}. Sends X-Capture-Token when a shared secret
    is configured (rev-9 §9.2)."""
    body = json.dumps({
        "source": source, "payload": payload, "project": project, "dedup_key": dedup_key,
    }).encode("utf-8")
    url = daemon_url.rstrip("/") + "/capture/event"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    _token = _resolve_event_token()
    if _token:
        headers["X-Capture-Token"] = _token
    req = urllib.request.Request(
        url, data=body, method="POST", headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except urllib.error.URLError as exc:
        logger.warning("emit: POST %s failed (daemon unreachable?): %s", url, exc)
        return {"ok": False, "error": f"network: {exc}"}
    except Exception as exc:
        logger.warning("emit: POST %s failed: %s", url, exc)
        return {"ok": False, "error": str(exc)}


def emit_candidates(candidates: list, *, session: str, project: Optional[str],
                    daemon_url: str, watermark_db: str, rate_budget: int,
                    engine_db_path: Optional[str] = None, dedup_similarity: float = 0.92,
                    dry_run: bool = False) -> dict:
    """Emit each candidate through the anti-storm + dedup gates, then POST.
    Returns a summary dict. In dry_run mode nothing is POSTed or
    rate-limited — candidates are returned as-would-be-emitted for
    inspection only."""
    summary = {"emitted": 0, "rate_limited": 0, "known_lesson": 0, "failed": 0, "results": []}

    for cand in candidates:
        entry = {
            "category": cand.category, "text": cand.text,
            "evidence_ref": cand.evidence_ref, "applicability": cand.applicability,
        }
        if dry_run:
            entry["would_emit"] = True
            summary["results"].append(entry)
            summary["emitted"] += 1
            continue

        if engine_db_path and is_known_lesson(engine_db_path, cand.text, dedup_similarity):
            entry["skipped"] = "known_lesson"
            summary["results"].append(entry)
            summary["known_lesson"] += 1
            continue

        if not capture_state.rate_limit_check_and_increment(watermark_db, session, rate_budget):
            entry["skipped"] = "rate_limited"
            summary["results"].append(entry)
            summary["rate_limited"] += 1
            continue

        payload = build_payload(cand)
        dedup_key = dedup_key_for(cand)
        result = post_capture_event(daemon_url, cand.source, payload, project, dedup_key)
        entry["post_result"] = result
        if result.get("ok"):
            summary["emitted"] += 1
        else:
            summary["failed"] += 1
        summary["results"].append(entry)

    return summary
