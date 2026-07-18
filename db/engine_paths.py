# coding: utf-8
"""crag-anchor path + bind resolver — single source of truth for filesystem
paths, the daemon bind (host/port), and any optional external source files.

MANDATE (mirrors db/grounding_config.py's config doctrine): nothing about
*where* the engine lives on disk or *what port* the daemon binds is hardcoded
scattered across the app scripts. Every path/bind lives here with a
resolution order:

    explicit env var  →  db/stack.toml value  →  repo-relative default

Env always wins. On a machine with ZERO config (no env, stack.toml absent or
without a [paths] section) every accessor returns a sane repo-relative
default — the repo root is derived from this file's location
(`db/engine_paths.py` → parent → crag-anchor root), so a fresh clone works
unchanged. Deleting stack.toml must not break anything.

House style: pure module, reads stack.toml once (cached), no I/O beyond that.
Mirrors grounding_config.py: `get_paths()` is cached; `reload_paths()` forces
a re-read (tests only — the daemon does not hot-reload; restart to pick up an
edit, same as every other daemon constant).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# db/engine_paths.py  →  db/  →  crag-anchor root
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT_DEFAULT = _THIS_DIR.parent
_STACK_TOML = Path(os.environ.get("CRAG_ANCHOR_STACK_TOML", str(_THIS_DIR / "stack.toml")))


# ---------------------------------------------------------------------------
# Optional external-source keys.
#
# An out-of-process integration may tail external log/db files (a watchdog
# log, an RTK-style history DB, a notification token file, ...). None of these
# have a portable default — they are entirely deployment-specific — so every
# key ships UNSET. Configure the ones you use via env
# (CRAG_ANCHOR_SOURCE_<KEY_UPPER>) or the [sources] block in db/stack.toml.
# Each source is INDIVIDUALLY optional and fail-soft: a source that is unset or
# missing on a given host is simply skipped, never fatal.
# ---------------------------------------------------------------------------
_SOURCE_DEFAULTS: dict[str, str] = {
    "connect_log": "",
    "ingest_forward_log": "",
    "watchdog_log": "",
    "router_log": "",
    "proxy_log": "",
    "history_db": "",
    "notify_token_file": "",
}


@dataclass(frozen=True)
class EnginePaths:
    # Core filesystem layout.
    home: Path            # crag-anchor root ("CRAG_ANCHOR_HOME"); db/, logs/ hang off this
    db_path: Path         # engine.db ("CRAG_ANCHOR_DB_PATH")
    log_dir: Path         # logs/ ("CRAG_ANCHOR_LOG_DIR")
    # Daemon bind.
    daemon_host: str      # "CRAG_ANCHOR_DAEMON_HOST" (default 127.0.0.1)
    daemon_port: int      # "CRAG_ANCHOR_DAEMON_PORT" (default 8786)
    # Optional external sources (each individually optional / fail-soft).
    sources: dict[str, Path] = field(default_factory=dict)

    @property
    def daemon_url(self) -> str:
        return f"http://{self.daemon_host}:{self.daemon_port}"

    def source(self, key: str) -> Optional[Path]:
        """Return an external source Path by key, or None if not configured."""
        return self.sources.get(key)


def _load_toml() -> dict:
    if not _STACK_TOML.exists():
        return {}
    try:
        with _STACK_TOML.open("rb") as f:
            return tomllib.load(f)
    except Exception:
        # Malformed stack.toml must not crash anything — fall back to
        # defaults + env, same as a missing file (grounding_config does this).
        return {}


def _section(doc: dict, dotted: str) -> dict:
    node = doc
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _resolve(env_name: str, toml_val, default) -> str:
    """Env wins → toml → default. Empty env string is treated as unset."""
    raw = os.environ.get(env_name)
    if raw is not None and raw != "":
        return raw
    if toml_val is not None and toml_val != "":
        return str(toml_val)
    return str(default)


def _build_paths() -> EnginePaths:
    doc = _load_toml()
    paths_toml = _section(doc, "paths")
    sources_toml = _section(doc, "sources")

    # ── home ────────────────────────────────────────────────────────────────
    home = Path(_resolve("CRAG_ANCHOR_HOME", paths_toml.get("home"), _REPO_ROOT_DEFAULT))

    # ── db_path ── default is <home>/db/engine.db, so it tracks a home override
    #    unless db_path is set explicitly.
    db_default = home / "db" / "engine.db"
    db_path = Path(_resolve("CRAG_ANCHOR_DB_PATH", paths_toml.get("db_path"), db_default))

    # ── log_dir ── default <home>/logs, tracks home unless overridden.
    log_default = home / "logs"
    log_dir = Path(_resolve("CRAG_ANCHOR_LOG_DIR", paths_toml.get("log_dir"), log_default))

    # ── daemon bind ───────────────────────────────────────────────────────────
    daemon_host = _resolve("CRAG_ANCHOR_DAEMON_HOST", paths_toml.get("daemon_host"), "127.0.0.1")
    port_raw = _resolve("CRAG_ANCHOR_DAEMON_PORT", paths_toml.get("daemon_port"), "8786")
    try:
        daemon_port = int(port_raw)
    except (TypeError, ValueError):
        daemon_port = 8786

    # ── external sources ── per-key env override:
    #    CRAG_ANCHOR_SOURCE_<KEY_UPPER>  (e.g. CRAG_ANCHOR_SOURCE_HISTORY_DB).
    #    Every default is empty; an unset source resolves to "" and is skipped.
    sources: dict[str, Path] = {}
    for key, default_val in _SOURCE_DEFAULTS.items():
        env_name = f"CRAG_ANCHOR_SOURCE_{key.upper()}"
        toml_val = sources_toml.get(key)
        resolved = _resolve(env_name, toml_val, default_val)
        if resolved:
            sources[key] = Path(resolved)

    return EnginePaths(
        home=home,
        db_path=db_path,
        log_dir=log_dir,
        daemon_host=daemon_host,
        daemon_port=daemon_port,
        sources=sources,
    )


_cached: Optional[EnginePaths] = None


def get_paths() -> EnginePaths:
    """Return the cached path config, building it on first call."""
    global _cached
    if _cached is None:
        _cached = _build_paths()
    return _cached


def reload_paths() -> EnginePaths:
    """Force a re-read of stack.toml + env. Tests only — the daemon does not
    hot-reload (restart to pick up a stack.toml edit)."""
    global _cached
    _cached = _build_paths()
    return _cached
