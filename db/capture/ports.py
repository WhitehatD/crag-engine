# coding: utf-8
"""ports/CaptureAdapter — the stable, harness-agnostic interface (docs/
closed-loop.md REV 9 §9.1 / REV 11).

Every attach surface (Claude Code JSONL tailer, a future gateway proxy, a
Cursor extension, an Agent-SDK middleware, ...) implements ONE interface:
`CaptureAdapter.poll()` returns newly-available `CaptureSpan`s made of
normalized `CaptureEvent`s. Nothing downstream (the extractor, the emitter,
the runner) imports a harness-specific module — they only ever see this
shape. This is what lets a gateway/Cursor/Codex adapter plug in later without
touching core/.

Universal event schema (REV 9 §9.1, OTel-GenAI-flavored):
    {session, turn, role, content, tool_calls, tool_results, agent_id,
     parent_agent_id, ts, source}

House style: dataclasses only, zero I/O in this module, zero provider
imports. Adapters live under db/capture/adapters/ and import this module,
never the reverse.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass
class CaptureEvent:
    """One normalized turn (or tool exchange) from any harness."""
    session: str                       # session/conversation id (opaque string)
    turn: int                          # monotonic turn index within the session
    role: str                          # "user" | "assistant" | "tool" | "system"
    content: str                       # the turn's text content (may be empty)
    tool_calls: list = field(default_factory=list)    # [{name, input}, ...]
    tool_results: list = field(default_factory=list)  # [{tool_use_id, content}, ...]
    agent_id: Optional[str] = None         # this turn's agent (main session or subagent id)
    parent_agent_id: Optional[str] = None  # None for the main session; parent for subagents
    ts: Optional[str] = None               # ISO-8601 UTC timestamp
    source: str = "unknown"                # adapter name, e.g. "claude_code_tailer"


@dataclass
class CaptureSpan:
    """A contiguous, extractable slice of a session — normally one user turn
    through the following assistant turn(s) up to (but not including) the
    next user turn, i.e. one "exchange". The extractor mines lessons per
    span, not per raw event, to keep the four-category taxonomy meaningful."""
    session: str
    events: list          # list[CaptureEvent], time-ordered
    span_id: str          # stable id for watermarking/dedup (e.g. "<file>:<line_end>")
    project: Optional[str] = None   # best-effort project slug/cwd, if derivable


@runtime_checkable
class CaptureAdapter(Protocol):
    """The stable port every harness-specific emitter implements.

    poll() is called repeatedly by the runner; it MUST be resumable (an
    adapter owns its own watermark/cursor) and MUST NOT raise on a harness
    quirk it doesn't understand — skip and continue (fail-soft, matches the
    rest of the write path's ethos: a capture bug must never crash the
    runner or corrupt state).
    """

    name: str

    def poll(self, max_spans: int = 50) -> list:
        """Return up to `max_spans` NEW CaptureSpans since the last poll.
        Must be idempotent-on-replay: calling poll() again without any new
        underlying data returns an empty list, never re-emits already-seen
        spans."""
        ...
