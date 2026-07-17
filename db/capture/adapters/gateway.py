# coding: utf-8
"""Gateway capture adapter — TODO STUB (docs/architecture.md REV 10/11).

This is the documented interface for the UNIVERSAL capture backbone: a
capture-aware, OpenAI-and-Anthropic-compatible model-API proxy (generalizing
the stack's existing model-router :8788 / Headroom :8787 pattern) that any
harness reaches by pointing OPENAI_BASE_URL / ANTHROPIC_BASE_URL at it.
Every current and future agent that merely makes LLM API calls (Codex CLI,
aider, raw LangGraph, OpenAI Agents SDK, ...) is captured with ZERO per-
harness integration, because capture happens at the API boundary every
harness already crosses.

NOT IMPLEMENTED in this increment — the Claude Code JSONL tailer
(claude_code_tailer.py) is the ONE adapter this workstream ships end-to-end,
per the brief ("ONE adapter, note the gateway adapter as a documented TODO
stub interface"). This file exists so:

  1. The `adapters/` package shape matches rev-11's ports-and-adapters
     structure NOW, before the gateway is built — a future PR implements
     GatewayCaptureAdapter against this exact shape without touching core/.
  2. `db/capture/run_capture.py` can import this module today (it currently
     no-ops if instantiated) so the runner's adapter-selection code doesn't
     need a second refactor when the gateway ships.

Planned shape (when implemented): a long-running HTTP proxy process observes
each proxied completion (prompt + response + tool calls) and appends
normalized CaptureEvents to an in-process or on-disk queue that
GatewayCaptureAdapter.poll() drains — the SAME ports.CaptureAdapter contract
the tailer implements, so run_capture.py treats both identically.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("crag-engine-capture")

NAME = "gateway"


class GatewayCaptureAdapter:
    """Stub implementing ports.CaptureAdapter's shape. `poll()` always
    returns an empty list until the gateway proxy itself is built — this is
    intentional (fail-soft: a runner that enables this adapter today simply
    gets zero spans from it, never a crash)."""

    name = NAME

    def __init__(self, *args, **kwargs):
        logger.debug("GatewayCaptureAdapter: stub, not yet implemented (docs/architecture.md REV 10/11)")

    def poll(self, max_spans: int = 50) -> list:
        return []
