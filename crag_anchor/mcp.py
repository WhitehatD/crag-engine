# coding: utf-8
"""Console-entry shim: `crag-anchor-mcp` -> apps/mcp/mcp-server.py."""
from __future__ import annotations

from . import run_script


def main() -> None:
    run_script("apps", "mcp", "mcp-server.py")
