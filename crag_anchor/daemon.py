# coding: utf-8
"""Console-entry shim: `crag-anchor` -> apps/daemon/engine_daemon.py."""
from __future__ import annotations

from . import run_script


def main() -> None:
    run_script("apps", "daemon", "engine_daemon.py")
