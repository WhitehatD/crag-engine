# coding: utf-8
"""stack.toml [capture] accessor — mirrors db/grounding_config.py's pattern
exactly (env var wins > stack.toml > repo-relative default). Every key is
OPTIONAL; delete the [capture] section and the runner keeps working against
these defaults. Nothing about *where* the transcript glob lives, *where* the
watermark state lives, or the anti-storm rate limits is hardcoded outside
this seam.

House style: pure module, reads stack.toml once (cached), no I/O beyond
that. `reload_config()` forces a re-read (tests only).
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_THIS_DIR = Path(__file__).resolve().parent          # db/capture/
_DB_DIR = _THIS_DIR.parent                             # db/
if str(_DB_DIR) not in sys.path:
    sys.path.insert(0, str(_DB_DIR))

import engine_paths  # noqa: E402

_STACK_TOML = Path(os.environ.get("CRAG_ANCHOR_STACK_TOML", str(_DB_DIR / "stack.toml")))


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# Default transcript glob: every project's persisted session JSONL, Windows
# and POSIX home both handled via expanduser. This is the rev-8/rev-9
# "guarded last-resort adapter" surface — Claude Code's on-disk log.
_DEFAULT_TRANSCRIPT_GLOB = str(Path("~/.claude/projects/**/*.jsonl").expanduser())


@dataclass(frozen=True)
class CaptureConfig:
    transcript_glob: str          # glob (recursive) for Claude Code JSONL logs
    watermark_store: str          # sqlite file tracking per-file byte offsets
    daemon_url: str               # base URL for POST /capture/event
    max_spans_per_poll: int       # anti-storm: cap spans returned per adapter.poll()
    max_candidates_per_session_run: int  # anti-storm: cap emitted candidates/session/run
    dedup_similarity: float       # embedding cosine >= this against corpus => noop
    extract_model_fallback: str   # used if [models].extract is absent (mirrors claim_layer)
    daemon_task_enabled: bool     # REV 6/8: run the tailer loop as a daemon lifespan task
    daemon_task_interval_sec: float  # seconds between in-process capture scans
    event_token: str              # rev-9 §9.2 shared secret for POST /capture/event ("" = fail-open)
    auth_token_file: str          # rev-9 §9.2 path to a gitignored token file ("" = disabled)


def _load_toml() -> dict:
    if not _STACK_TOML.exists():
        return {}
    try:
        with _STACK_TOML.open("rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _section(doc: dict, dotted: str) -> dict:
    node = doc
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _resolve(env_name: str, toml_val, default) -> str:
    raw = os.environ.get(env_name)
    if raw is not None and raw != "":
        return raw
    if toml_val is not None and toml_val != "":
        return str(toml_val)
    return str(default)


def _build_config() -> CaptureConfig:
    doc = _load_toml()
    cap = _section(doc, "capture")
    paths = engine_paths.get_paths()

    transcript_glob = _resolve(
        "CRAG_ANCHOR_CAPTURE_TRANSCRIPT_GLOB", cap.get("transcript_glob"), _DEFAULT_TRANSCRIPT_GLOB
    )
    default_watermark = str(paths.home / "db" / "capture-state.db")
    watermark_store = _resolve(
        "CRAG_ANCHOR_CAPTURE_WATERMARK_STORE", cap.get("watermark_store"), default_watermark
    )
    daemon_url = _resolve("CRAG_ANCHOR_CAPTURE_DAEMON_URL", cap.get("daemon_url"), paths.daemon_url)

    max_spans = _resolve("CRAG_ANCHOR_CAPTURE_MAX_SPANS_PER_POLL", cap.get("max_spans_per_poll"), "50")
    max_cand = _resolve(
        "CRAG_ANCHOR_CAPTURE_MAX_CANDIDATES_PER_SESSION_RUN",
        cap.get("max_candidates_per_session_run"), "20",
    )
    dedup_sim = _resolve("CRAG_ANCHOR_CAPTURE_DEDUP_SIMILARITY", cap.get("dedup_similarity"), "0.92")
    extract_fallback = _resolve(
        "CRAG_ANCHOR_CAPTURE_EXTRACT_MODEL_FALLBACK",
        cap.get("extract_model_fallback"), "claude-haiku-4-5-20251001",
    )
    # REV 6/8: in-process daemon capture task. Default ENABLED — "the loop
    # runs around the agent" is the whole point; an operator can opt out with
    # daemon_task_enabled=false or CRAG_ANCHOR_CAPTURE_DAEMON_TASK_ENABLED=0.
    daemon_task_enabled = _resolve(
        "CRAG_ANCHOR_CAPTURE_DAEMON_TASK_ENABLED", cap.get("daemon_task_enabled"), "true"
    )
    daemon_task_interval = _resolve(
        "CRAG_ANCHOR_CAPTURE_DAEMON_TASK_INTERVAL_SEC", cap.get("daemon_task_interval_sec"), "120"
    )
    # rev-9 §9.2: shared secret for POST /capture/event. Env wins; "" = fail-open
    # (loopback-only, one-time unauthenticated warning) to preserve the current
    # single-user local deployment.
    event_token = _resolve("CRAG_ANCHOR_CAPTURE_TOKEN", cap.get("event_token"), "")
    # rev-9 §9.2: alternatively, the token can live in a gitignored FILE (path
    # given here). The file's contents (whitespace-stripped) take precedence
    # over an inline event_token when the file exists and is non-empty — a
    # secret-in-a-file is the recommended production posture (never committed,
    # rotatable on disk without editing stack.toml).
    auth_token_file = _resolve("CRAG_ANCHOR_CAPTURE_AUTH_TOKEN_FILE", cap.get("auth_token_file"), "")

    try:
        max_spans_i = int(max_spans)
    except (TypeError, ValueError):
        max_spans_i = 50
    try:
        max_cand_i = int(max_cand)
    except (TypeError, ValueError):
        max_cand_i = 20
    try:
        dedup_sim_f = float(dedup_sim)
    except (TypeError, ValueError):
        dedup_sim_f = 0.92
    try:
        daemon_task_interval_f = float(daemon_task_interval)
    except (TypeError, ValueError):
        daemon_task_interval_f = 120.0

    return CaptureConfig(
        transcript_glob=transcript_glob,
        watermark_store=watermark_store,
        daemon_url=daemon_url,
        max_spans_per_poll=max_spans_i,
        max_candidates_per_session_run=max_cand_i,
        dedup_similarity=dedup_sim_f,
        extract_model_fallback=extract_fallback,
        daemon_task_enabled=_bool(daemon_task_enabled),
        daemon_task_interval_sec=daemon_task_interval_f,
        event_token=str(event_token or ""),
        auth_token_file=str(auth_token_file or ""),
    )


def effective_event_token(cfg: Optional[CaptureConfig] = None) -> str:
    """The token to enforce on /capture/event, resolving the file-vs-inline
    precedence (rev-9 §9.2): a non-empty auth_token_file wins over an inline
    event_token. Read fresh from disk each call so on-disk rotation takes effect
    without a daemon restart. "" means auth is disabled (fail-open)."""
    if cfg is None:
        cfg = get_config()
    token_file = getattr(cfg, "auth_token_file", "") or ""
    if token_file:
        try:
            val = Path(token_file).read_text(encoding="utf-8").strip()
            if val:
                return val
        except Exception:
            pass  # unreadable/missing file -> fall through to inline token
    return str(getattr(cfg, "event_token", "") or "")


_cached: Optional[CaptureConfig] = None


def get_config() -> CaptureConfig:
    global _cached
    if _cached is None:
        _cached = _build_config()
    return _cached


def reload_config() -> CaptureConfig:
    """Force a re-read of stack.toml + env. Tests only."""
    global _cached
    _cached = _build_config()
    return _cached
