# coding: utf-8
"""`crag-anchor` — daemon lifecycle CLI.

The crag CLI's `crag memory` surface and the `.crag/mcp.json` stdio wiring
consume this exact contract (crag repo, src/commands/memory.js):

    crag-anchor                 boot the daemon in the FOREGROUND
                                (Task Scheduler / systemd / docker entry)
    crag-anchor up              boot the daemon in the foreground (alias)
    crag-anchor up --detach     spawn the daemon detached, write a pidfile,
                                return immediately (callers poll /health)
    crag-anchor down            stop the detached daemon (pidfile)
    crag-anchor logs [-n N]     tail the detached daemon's log (default 60)
    crag-anchor mcp             run the stdio MCP server (same process,
                                equivalent to `crag-anchor-mcp`)
    crag-anchor --version       print the installed package version
    crag-anchor --help          this usage

History: until 2026-07-18 this shim ran the daemon unconditionally — even
`--help` booted a daemon on the configured port (insight #3636). The crag CLI
contract (spawnSync probes, detached lifecycle, stdio MCP) requires real
subcommands; bare invocation keeps the old foreground-boot behavior so
existing Task Scheduler / launchd / systemd / docker entries are unaffected.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import repo_root, run_script

USAGE = """crag-anchor — the verified-memory engine for crag (daemon lifecycle)

usage:
  crag-anchor                 boot the daemon (foreground)
  crag-anchor up [--detach]   boot the daemon (--detach: background + pidfile)
  crag-anchor down            stop the detached daemon
  crag-anchor logs [-n N]     tail the detached daemon's log (default 60 lines)
  crag-anchor mcp             run the stdio MCP server
  crag-anchor --version       print version
  crag-anchor --help          this help

config (env > db/stack.toml > default): CRAG_ANCHOR_DB_PATH,
CRAG_ANCHOR_DAEMON_HOST/PORT (default 127.0.0.1:8786), CRAG_ANCHOR_LOG_DIR,
CRAG_ANCHOR_HOME, CRAG_ANCHOR_MODULES (overlay modules, comma-separated)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _paths():
    """Resolve engine paths via db/engine_paths.py (env > stack.toml > default)."""
    root = repo_root()
    db_dir = str(root / "db")
    if db_dir not in sys.path:
        sys.path.insert(0, db_dir)
    import engine_paths  # repo script, importable only from db/

    return engine_paths.get_paths()


def _pidfile(paths) -> Path:
    return Path(paths.log_dir) / "crag-anchor.pid"


def _logfile(paths) -> Path:
    return Path(paths.log_dir) / "crag-anchor.log"


def _health_status(url: str, timeout: float = 2.0):
    """Return the HTTP status of GET <url>/health, or None if unreachable.

    Any HTTP response (200 healthy, 503 embeddings-degraded) means the daemon
    process is up and bound; only a connection failure means it is not.
    """
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:  # 503 degraded etc. — daemon IS up
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("crag-anchor")
    except Exception:
        return "0.0.0+checkout"


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def _cmd_up(detach: bool) -> int:
    paths = _paths()
    if _health_status(paths.daemon_url) is not None:
        print(f"crag-anchor is already running at {paths.daemon_url}")
        return 0
    if not detach:
        # Foreground boot — identical to the bare `crag-anchor` invocation.
        sys.argv = [sys.argv[0]]
        run_script("apps", "daemon", "engine_daemon.py")
        return 0

    import subprocess

    script = repo_root() / "apps" / "daemon" / "engine_daemon.py"
    log_dir = Path(paths.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = _logfile(paths)
    kwargs: dict = {}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW —
        # survives this console's exit, owns no window.
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    with logfile.open("ab") as lf:
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=lf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(repo_root()),
            **kwargs,
        )
    _pidfile(paths).write_text(str(proc.pid), encoding="ascii")
    print(
        f"crag-anchor starting (pid {proc.pid}) — {paths.daemon_url}\n"
        f"  log: {logfile}\n"
        f"  (poll {paths.daemon_url}/health; embeddings can take ~30s cold)"
    )
    return 0


def _cmd_down() -> int:
    paths = _paths()
    pidfile = _pidfile(paths)
    up_before = _health_status(paths.daemon_url) is not None
    if not pidfile.exists():
        if up_before:
            print(
                f"crag-anchor responds at {paths.daemon_url} but no pidfile at "
                f"{pidfile} — it was not started by `crag-anchor up --detach` "
                "(Task Scheduler / systemd instance?). Stop it with its own manager."
            )
            return 1
        print("crag-anchor is not running (no pidfile, daemon unreachable)")
        return 0
    try:
        pid = int(pidfile.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        pidfile.unlink(missing_ok=True)
        print(f"stale/unreadable pidfile removed: {pidfile}")
        return 1
    try:
        import signal

        os.kill(pid, signal.SIGTERM)  # Windows: TerminateProcess
    except (OSError, ProcessLookupError):
        pidfile.unlink(missing_ok=True)
        print(f"crag-anchor pid {pid} already gone — stale pidfile removed")
        return 0
    # Wait (bounded) for the port to actually free — that is the semantic
    # callers care about, and it needs no process-liveness API.
    for _ in range(10):
        time.sleep(0.5)
        if _health_status(paths.daemon_url, timeout=1.0) is None:
            break
    pidfile.unlink(missing_ok=True)
    still_up = _health_status(paths.daemon_url, timeout=1.0) is not None
    if still_up:
        print(
            f"killed pid {pid}, but {paths.daemon_url} still responds — "
            "another instance (Task Scheduler?) owns the port"
        )
        return 1
    print(f"crag-anchor stopped (pid {pid})")
    return 0


def _cmd_logs(argv: list[str]) -> int:
    n = 60
    if argv[:1] == ["-n"] and len(argv) > 1:
        try:
            n = max(1, int(argv[1]))
        except ValueError:
            print(USAGE, file=sys.stderr)
            return 2
    paths = _paths()
    logfile = _logfile(paths)
    if not logfile.exists():
        print(
            f"no log at {logfile} — `crag-anchor up --detach` writes it; "
            "foreground/Task-Scheduler instances log to their own stdout"
        )
        return 1
    lines = logfile.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-n:]:
        print(line)
    return 0


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def main() -> None:
    argv = sys.argv[1:]

    if not argv:
        # Bare invocation: foreground boot (Task Scheduler / docker compat).
        run_script("apps", "daemon", "engine_daemon.py")
        return

    cmd, rest = argv[0], argv[1:]

    if cmd in ("--help", "-h", "help"):
        print(USAGE)
        return
    if cmd == "--version":
        print(_version())
        return
    if cmd == "mcp":
        # Stdio MCP server in-process; strip argv so the server sees none.
        sys.argv = [sys.argv[0]]
        run_script("apps", "mcp", "mcp-server.py")
        return
    if cmd == "up":
        if rest not in ([], ["--detach"]):
            print(USAGE, file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(_cmd_up(detach=rest == ["--detach"]))
    if cmd == "down":
        raise SystemExit(_cmd_down())
    if cmd == "logs":
        raise SystemExit(_cmd_logs(rest))

    print(f"crag-anchor: unknown command {cmd!r}\n\n{USAGE}", file=sys.stderr)
    raise SystemExit(2)
