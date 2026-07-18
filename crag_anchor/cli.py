# coding: utf-8
"""Console-entry shim: `crag-anchor-cli` -> db/engine-cli.py.

Note: crag-anchor-cli is OPERATOR tooling (migrate/backfill/decay/etc.), not part of
the agent's normal path. The entry point exists so operators get `crag-anchor-cli`
on PATH after `pip install -e .` instead of typing the full script path.
"""
from __future__ import annotations

from . import run_script


def main() -> None:
    run_script("db", "engine-cli.py")
