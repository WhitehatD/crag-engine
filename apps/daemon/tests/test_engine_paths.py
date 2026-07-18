#!/usr/bin/env python3
# coding: utf-8
"""engine_paths config accessor test suite.

Standalone (no pytest — mirrors test_grounding_llm_config.py style).

Covers the resolution contract of db/engine_paths.py:
  T_DEF   zero-config defaults == repo-relative paths + 127.0.0.1:8786
  T_ENV   env var wins over everything
  T_TOML  stack.toml values apply (middle tier) when env is unset
  T_PREC  env wins over TOML (precedence)
  T_SRC   external sources resolve (env + toml; unset by default)
  T_MAL   malformed / missing stack.toml falls back to defaults (never crashes)

Exit codes: 0 = all pass, 1 = any failure.

Run:
  python apps/daemon/tests/test_engine_paths.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_DIR = REPO_ROOT / "db"
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

import engine_paths  # noqa: E402

FAILURES: list[str] = []
PASSES: list[str] = []

# Environment keys the accessor reads — clear ALL of them for the default-path
# assertions so the real shell env can't skew the test.
_ALL_ENV = [
    "CRAG_ANCHOR_HOME", "CRAG_ANCHOR_DB_PATH", "CRAG_ANCHOR_LOG_DIR",
    "CRAG_ANCHOR_DAEMON_HOST", "CRAG_ANCHOR_DAEMON_PORT",
    "CRAG_ANCHOR_SOURCE_HISTORY_DB", "CRAG_ANCHOR_SOURCE_PROXY_LOG",
    "CRAG_ANCHOR_SOURCE_NOTIFY_TOKEN_FILE",
]
_CLEAR_ENV = {k: "" for k in _ALL_ENV}


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSES.append(name)
        print(f"  [v] {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [X] {name}  -- {detail}")


def _norm(p) -> str:
    return str(p).replace("\\", "/")


# ---------------------------------------------------------------------------
# T_DEF: zero-config defaults == repo-relative behavior
# ---------------------------------------------------------------------------
print("\n== T_DEF: zero-config defaults ==")

with mock.patch.dict(os.environ, _CLEAR_ENV, clear=False):
    with mock.patch.object(engine_paths, "_STACK_TOML", Path("/nonexistent/stack.toml")):
        bp = engine_paths._build_paths()

check("T_DEF_a: home == repo root",
      _norm(bp.home) == _norm(REPO_ROOT), f"home={bp.home} repo={REPO_ROOT}")
check("T_DEF_b: db_path == <root>/db/engine.db",
      _norm(bp.db_path) == _norm(REPO_ROOT / "db" / "engine.db"), f"db_path={bp.db_path}")
check("T_DEF_c: log_dir == <root>/logs",
      _norm(bp.log_dir) == _norm(REPO_ROOT / "logs"), f"log_dir={bp.log_dir}")
check("T_DEF_d: daemon_host default 127.0.0.1", bp.daemon_host == "127.0.0.1",
      f"host={bp.daemon_host}")
check("T_DEF_e: daemon_port default 8786", bp.daemon_port == 8786,
      f"port={bp.daemon_port}")
check("T_DEF_f: daemon_url composed", bp.daemon_url == "http://127.0.0.1:8786",
      f"url={bp.daemon_url}")
# External sources are UNSET by default (deployment-specific, fail-soft).
check("T_DEF_g: history source unset", bp.source("history_db") is None,
      f"history={bp.source('history_db')}")
check("T_DEF_h: notify token source unset", bp.source("notify_token_file") is None,
      f"notify={bp.source('notify_token_file')}")


# ---------------------------------------------------------------------------
# T_ENV: env var wins over everything
# ---------------------------------------------------------------------------
print("\n== T_ENV: env override wins ==")

env = {
    "CRAG_ANCHOR_HOME": "/srv/engine",
    "CRAG_ANCHOR_DB_PATH": "/data/custom.db",
    "CRAG_ANCHOR_LOG_DIR": "/var/log/engine",
    "CRAG_ANCHOR_DAEMON_HOST": "0.0.0.0",
    "CRAG_ANCHOR_DAEMON_PORT": "19999",
    "CRAG_ANCHOR_SOURCE_HISTORY_DB": "/tmp/history.db",
    "CRAG_ANCHOR_SOURCE_NOTIFY_TOKEN_FILE": "/etc/engine/notify.token",
}
with mock.patch.dict(os.environ, env, clear=False):
    with mock.patch.object(engine_paths, "_STACK_TOML", Path("/nonexistent/stack.toml")):
        bpe = engine_paths._build_paths()

check("T_ENV_a: home from env", _norm(bpe.home) == "/srv/engine", f"home={bpe.home}")
check("T_ENV_b: db_path from env", _norm(bpe.db_path) == "/data/custom.db", f"db={bpe.db_path}")
check("T_ENV_c: log_dir from env", _norm(bpe.log_dir) == "/var/log/engine", f"log={bpe.log_dir}")
check("T_ENV_d: host from env", bpe.daemon_host == "0.0.0.0", f"host={bpe.daemon_host}")
check("T_ENV_e: port from env", bpe.daemon_port == 19999, f"port={bpe.daemon_port}")
check("T_ENV_f: history source from env",
      _norm(bpe.source("history_db")) == "/tmp/history.db", f"history={bpe.source('history_db')}")
check("T_ENV_g: notify token from env",
      _norm(bpe.source("notify_token_file")) == "/etc/engine/notify.token",
      f"notify={bpe.source('notify_token_file')}")


# ---------------------------------------------------------------------------
# T_TOML: stack.toml middle tier (env unset)
# ---------------------------------------------------------------------------
print("\n== T_TOML: stack.toml values (middle tier) ==")

toml_content = b"""
[paths]
home = "/opt/engineroot"
daemon_port = 28786

[sources]
history_db = "/toml/history.db"
proxy_log = "/toml/proxy.log"
"""
with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as tf:
    tf.write(toml_content)
    tf.flush()
    toml_path = Path(tf.name)

try:
    with mock.patch.dict(os.environ, _CLEAR_ENV, clear=False):
        with mock.patch.object(engine_paths, "_STACK_TOML", toml_path):
            bpt = engine_paths._build_paths()
    check("T_TOML_a: home from toml", _norm(bpt.home) == "/opt/engineroot", f"home={bpt.home}")
    check("T_TOML_b: db_path tracks toml home",
          _norm(bpt.db_path) == "/opt/engineroot/db/engine.db", f"db={bpt.db_path}")
    check("T_TOML_c: port from toml", bpt.daemon_port == 28786, f"port={bpt.daemon_port}")
    check("T_TOML_d: history source from toml",
          _norm(bpt.source("history_db")) == "/toml/history.db", f"history={bpt.source('history_db')}")
    check("T_TOML_e: proxy source from toml",
          _norm(bpt.source("proxy_log")) == "/toml/proxy.log",
          f"proxy={bpt.source('proxy_log')}")
    # Un-overridden source stays unset (empty default -> skipped).
    check("T_TOML_f: un-overridden notify still unset",
          bpt.source("notify_token_file") is None,
          f"notify={bpt.source('notify_token_file')}")
finally:
    toml_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# T_PREC: env wins over toml
# ---------------------------------------------------------------------------
print("\n== T_PREC: env beats toml ==")

toml_content2 = b"""
[paths]
daemon_port = 28786
"""
with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as tf2:
    tf2.write(toml_content2)
    tf2.flush()
    toml_path2 = Path(tf2.name)

try:
    with mock.patch.dict(os.environ, {"CRAG_ANCHOR_DAEMON_PORT": "38786"}, clear=False):
        with mock.patch.object(engine_paths, "_STACK_TOML", toml_path2):
            bpp = engine_paths._build_paths()
    check("T_PREC_a: env port beats toml port", bpp.daemon_port == 38786,
          f"port={bpp.daemon_port} (toml said 28786)")
finally:
    toml_path2.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# T_MAL: malformed / missing stack.toml never crashes
# ---------------------------------------------------------------------------
print("\n== T_MAL: malformed toml falls back to defaults ==")

with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as tfm:
    tfm.write(b"this is not = valid toml [[[")
    tfm.flush()
    mal_path = Path(tfm.name)

try:
    with mock.patch.dict(os.environ, _CLEAR_ENV, clear=False):
        with mock.patch.object(engine_paths, "_STACK_TOML", mal_path):
            bpm = engine_paths._build_paths()
    check("T_MAL_a: malformed toml -> default port", bpm.daemon_port == 8786,
          f"port={bpm.daemon_port}")
    check("T_MAL_b: malformed toml -> default home",
          _norm(bpm.home) == _norm(REPO_ROOT), f"home={bpm.home}")
except Exception as exc:
    check("T_MAL_a: malformed toml handled without crash", False, str(exc))
finally:
    mal_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# T_CACHE: get_paths caches, reload_paths re-reads
# ---------------------------------------------------------------------------
print("\n== T_CACHE: caching + reload ==")

engine_paths._cached = None
c1 = engine_paths.get_paths()
c2 = engine_paths.get_paths()
check("T_CACHE_a: get_paths returns cached instance", c1 is c2)
c3 = engine_paths.reload_paths()
check("T_CACHE_b: reload_paths rebuilds", c3 is not c1)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*60}")
print(f"Results: {len(PASSES)} passed, {len(FAILURES)} failed")
if FAILURES:
    print("\nFAILURES:")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
else:
    print("All tests passed.")
    sys.exit(0)
