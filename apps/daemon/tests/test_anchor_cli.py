# coding: utf-8
"""Lifecycle-CLI contract suite — `crag-anchor` subcommands (insight #3636).

Origin: until 2026-07-18 the `crag-anchor` entry point was a bare runpy shim;
`--help` BOOTED A DAEMON on the configured port and every subcommand the crag
CLI spawns (up --detach / down / mcp / --version probe) fell through to a
foreground boot. Name-level rename verification and unit tests both missed it
because no test exercised the installed entry point end-to-end. This suite IS
that missing cross-piece gate: it drives the real contract the crag CLI's
src/commands/memory.js depends on, via subprocess, against a throwaway port+DB.

Run: python apps/daemon/tests/test_anchor_cli.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PORT = 8798  # clear of 8786 (live), 8787/8788 (proxies), 9797 (watchdog)
URL = f"http://127.0.0.1:{PORT}"

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  [{'v' if ok else 'x'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def run_cli(args: list[str], env: dict, timeout: int = 30) -> subprocess.CompletedProcess:
    """Invoke the entry point exactly as a console script would."""
    code = (
        "import sys; sys.argv=['crag-anchor']+sys.argv[1:]; "
        "from crag_anchor.daemon import main; main()"
    )
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(REPO),
    )


def health(timeout: float = 2.0):
    try:
        with urllib.request.urlopen(f"{URL}/health", timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anchor-cli-"))
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(REPO),
        CRAG_ANCHOR_DB_PATH=str(tmp / "cli-test.db"),
        CRAG_ANCHOR_DAEMON_PORT=str(PORT),
        CRAG_ANCHOR_LOG_DIR=str(tmp / "logs"),
        PYTHONIOENCODING="utf-8",
    )
    env.pop("CRAG_ANCHOR_MODULES", None)  # no overlays in this suite

    print("=== crag-anchor lifecycle-CLI suite ===")

    print("test_fast_commands:")
    r = run_cli(["--help"], env, timeout=20)
    check("T_HELP_exit0", r.returncode == 0, f"rc={r.returncode} err={r.stderr[:200]}")
    check("T_HELP_prints_usage", "usage:" in r.stdout and "up [--detach]" in r.stdout)
    check("T_HELP_does_not_boot", health() is None, "a daemon appeared on the test port!")

    r = run_cli(["--version"], env, timeout=20)
    check("T_VERSION_exit0", r.returncode == 0, f"rc={r.returncode} err={r.stderr[:200]}")
    check("T_VERSION_output", bool(r.stdout.strip()), "empty version output")
    check("T_VERSION_no_daemon_noise", "uvicorn" not in r.stdout.lower())

    r = run_cli(["frobnicate"], env, timeout=20)
    check("T_UNKNOWN_exit2", r.returncode == 2, f"rc={r.returncode}")
    check("T_UNKNOWN_usage_on_stderr", "usage:" in r.stderr)

    r = run_cli(["up", "--detach", "--bogus"], env, timeout=20)
    check("T_UP_BADFLAG_exit2", r.returncode == 2, f"rc={r.returncode}")

    print("test_lifecycle_roundtrip:")
    r = run_cli(["down"], env, timeout=20)
    check("T_DOWN_idle_exit0", r.returncode == 0, f"rc={r.returncode} out={r.stdout[:200]}")
    check("T_DOWN_idle_message", "not running" in r.stdout)

    r = run_cli(["up", "--detach"], env, timeout=45)
    check("T_UP_exit0", r.returncode == 0, f"rc={r.returncode} err={r.stderr[:300]}")
    check("T_UP_returns_immediately", "starting (pid" in r.stdout, r.stdout[:200])
    pidfile = tmp / "logs" / "crag-anchor.pid"
    check("T_UP_pidfile", pidfile.exists())

    status = None
    for _ in range(30):  # up to 60s — embeddings may cold-load
        time.sleep(2)
        status = health()
        if status is not None:
            break
    check("T_UP_daemon_responds", status is not None, "no HTTP response within 60s")
    if status is not None:
        with urllib.request.urlopen(f"{URL}/stats", timeout=5) as resp:
            stats = json.load(resp)
        check("T_UP_serves_fresh_db", stats.get("insight_counts", {}).get("total", -1) == 0,
              f"stats={str(stats)[:120]}")

    r = run_cli(["up", "--detach"], env, timeout=20)
    check("T_UP_idempotent", r.returncode == 0 and "already running" in r.stdout,
          f"rc={r.returncode} out={r.stdout[:200]}")

    r = run_cli(["logs", "-n", "5"], env, timeout=20)
    check("T_LOGS_exit0", r.returncode == 0, f"rc={r.returncode} out={r.stdout[:200]}")
    check("T_LOGS_content", bool(r.stdout.strip()))

    r = run_cli(["down"], env, timeout=30)
    check("T_DOWN_exit0", r.returncode == 0, f"rc={r.returncode} out={r.stdout[:300]}")
    check("T_DOWN_stopped_message", "stopped" in r.stdout, r.stdout[:200])
    check("T_DOWN_port_freed", health() is None, "daemon still responding after down")
    check("T_DOWN_pidfile_gone", not pidfile.exists())

    print("=" * 50)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"PASS {passed}  FAIL {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
