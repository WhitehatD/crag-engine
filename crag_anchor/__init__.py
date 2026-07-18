# coding: utf-8
"""crag_anchor — thin console-entry-point shims for crag Anchor (WS-P, 2026-07-17).

The engine apps are deliberately single-file scripts with hyphenated names
(apps/daemon/engine_daemon.py, apps/mcp/mcp-server.py, db/engine-cli.py) —
they cannot be imported as modules and
we are NOT moving/renaming them (additive-only mandate; the live install runs
them by path today). This package exists solely so `pyproject.toml` can expose
proper console entry points:

    crag-anchor     -> crag_anchor.daemon:main
    crag-anchor-mcp -> crag_anchor.mcp:main
    crag-anchor-cli -> crag_anchor.cli:main

Each shim locates the script inside the repo checkout and executes it via
runpy with run_name="__main__", which is byte-for-byte equivalent to running
`python apps/daemon/engine_daemon.py`: the script's own `Path(__file__)` logic,
sys.path inserts, and `if __name__ == "__main__":` block all behave identically.

Why this matters (lesson #137/#76): MCP/daemon registration by absolute python
path broke silently for days when a path moved. A console script on PATH
(`claude mcp add --scope user crag-anchor crag-anchor-mcp`) eliminates that failure class.

Install mode: this package supports the *checkout* install (`pip install -e .`
from the repo root — the documented path for both bare-metal and the Docker
image). A bare wheel install without the repo tree cannot carry the app
scripts (they are not package modules), so run_script() fails loudly with the
fix instead of half-working.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

# Marker file used to validate that a candidate directory is the crag-anchor repo root.
_MARKER = ("apps", "daemon", "engine_daemon.py")


def repo_root() -> Path:
    """Return the crag-anchor repo root.

    Resolution: crag_anchor/ sits at the repo root in a checkout (editable
    install or direct PYTHONPATH use), so the parent of this package is the
    root. CRAG_ANCHOR_PKG_ROOT env overrides for exotic layouts.
    """
    import os

    override = os.environ.get("CRAG_ANCHOR_PKG_ROOT")
    candidates = []
    if override:
        candidates.append(Path(override))
    candidates.append(Path(__file__).resolve().parent.parent)

    for cand in candidates:
        marker = cand.joinpath(*_MARKER)
        if marker.exists():
            return cand

    raise RuntimeError(
        "crag_anchor: cannot locate the crag-anchor repo tree (looked for "
        f"{'/'.join(_MARKER)} next to the installed package). The crag-anchor "
        "must be installed FROM A CHECKOUT: `git clone <repo> && pip install -e .` "
        "— a bare wheel does not carry the app scripts. If your checkout lives "
        "elsewhere, set CRAG_ANCHOR_PKG_ROOT=/path/to/crag-anchor."
    )


def run_script(*rel_parts: str) -> None:
    """Execute a repo script exactly as `python <script>` would."""
    script = repo_root().joinpath(*rel_parts)
    if not script.exists():
        raise RuntimeError(f"crag_anchor: script not found: {script}")
    # Windows console-script exes inherit cp1252 on redirected stdio, which
    # explodes on the scripts' unicode output (→, ✓). Same fix the repo's test
    # runners apply to themselves; harmless on POSIX.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    # argv[0] should be the script path (argparse prog name, ps output, etc.)
    sys.argv[0] = str(script)
    runpy.run_path(str(script), run_name="__main__")
