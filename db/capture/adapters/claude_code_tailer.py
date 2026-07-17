# coding: utf-8
"""Claude Code JSONL tailer — the highest-fidelity Claude Code adapter (docs/
closed-loop.md REV 8/9/11), ONE adapter implementing ports.CaptureAdapter,
never the universal mechanism (the gateway adapter, adapters/gateway.py, is
the harness-agnostic base layer; this is a provider IMPROVEMENT).

Reads the PERSISTED transcript (~/.claude/projects/**/<uuid>.jsonl, path via
config, NEVER hardcoded — see db/capture/config.py transcript_glob) —
compaction-proof and crash-proof by construction:

  - Compaction affects the agent's live CONTEXT, not the on-disk transcript;
    claudex appends every turn to the JSONL as it completes, so the durable
    record survives compaction untouched (rev 8).
  - A per-file BYTE-OFFSET watermark (db/capture/state.py) makes the tailer
    resumable across restarts/crashes: re-running after a crash mid-session
    picks up exactly where it left off — nothing is re-emitted, nothing is
    silently dropped.
  - A span is only considered COMPLETE (and the watermark advanced past it)
    once a LATER 'user' turn closes it. A trailing, still-being-written
    assistant turn is deliberately left un-watermarked, so a crash mid-turn
    just means that turn is re-read (harmlessly — never re-EMITTED, span_id
    dedup in state.py handles that) once something eventually closes it.
    `poll(force_close_tail=True)` — the PreCompact/SessionEnd low-latency
    nudge path (rev 8: hooks are nudges, never the correctness mechanism) —
    treats EOF itself as a boundary so the final in-flight span is captured
    without waiting for the next user turn.

JSONL shape (verified against live transcripts, 2026-07-17):
  each line: {"type": "user"|"assistant"|"system"|"mode"|..., "message":
  {"role", "content": str|[{type:"text"|"thinking"|"tool_use"|"tool_result",
  ...}]}, "sessionId", "uuid", "timestamp", "cwd", ...}
Only 'user' and 'assistant' lines carry conversation content; everything
else (mode/system/attribution-snapshot/file-history-snapshot/summary) is
adapter bookkeeping and is skipped (advanced-over, never spanned).
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crag-engine-capture")

_THIS_DIR = Path(__file__).resolve().parent            # db/capture/adapters/
_CAPTURE_DIR = _THIS_DIR.parent                          # db/capture/
if str(_CAPTURE_DIR) not in sys.path:
    sys.path.insert(0, str(_CAPTURE_DIR))

from ports import CaptureEvent, CaptureSpan  # noqa: E402
import state as capture_state  # noqa: E402

NAME = "claude_code_tailer"


def _iter_lines_from_offset(path: str, start_byte: int):
    """Yield (line_text, end_byte_of_this_line) for every COMPLETE line from
    start_byte onward. A trailing partial line (no newline yet — the writer
    is mid-flush) is NOT yielded, so the caller never treats incomplete
    bytes as data."""
    with open(path, "rb") as f:
        f.seek(start_byte)
        buf = b""
        pos = start_byte
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                pos += len(line) + 1
                yield line.decode("utf-8", errors="replace"), pos


def _extract_text_blocks(content) -> tuple:
    """Return (text, tool_calls, tool_results) from a message.content value
    that is either a raw string or a list of typed content blocks."""
    if isinstance(content, str):
        return content, [], []
    if not isinstance(content, list):
        return "", [], []

    texts: list = []
    tool_calls: list = []
    tool_results: list = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if t:
                texts.append(str(t))
        elif btype == "thinking":
            # Internal reasoning — not observable conversation content;
            # skipping keeps spans focused on what actually happened.
            continue
        elif btype == "tool_use":
            tool_calls.append({"name": block.get("name"), "input": block.get("input")})
        elif btype == "tool_result":
            rc = block.get("content")
            if isinstance(rc, str):
                rtext = rc
            elif isinstance(rc, list):
                rtext = " ".join(
                    str(b.get("text", "")) for b in rc if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                rtext = ""
            tool_results.append({"tool_use_id": block.get("tool_use_id"), "content": rtext[:4000]})
    return "\n".join(texts), tool_calls, tool_results


def _agent_id_for_path(path: str) -> tuple:
    """Best-effort agent_id/parent_agent_id from the file's location:
    '.../<session-uuid>/subagents/agent-<id>.jsonl' -> (agent_id, session-uuid);
    a top-level '<session-uuid>.jsonl' -> (None, None) — the main session."""
    p = Path(path)
    if p.parent.name == "subagents":
        session_dir = p.parent.parent.name
        agent_id = p.stem
        return agent_id, session_dir
    return None, None


def _parse_line(raw: str) -> Optional[dict]:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _is_user_turn(obj: dict) -> bool:
    return obj.get("type") == "user" and (obj.get("message") or {}).get("role") == "user"


def _discover_files(transcript_glob: str) -> list:
    try:
        files = glob.glob(transcript_glob, recursive=True)
    except Exception as exc:
        logger.warning("claude_code_tailer: glob failed for %r (fail-soft): %s", transcript_glob, exc)
        return []
    return sorted(f for f in files if os.path.isfile(f))


class ClaudeCodeTailer:
    """CaptureAdapter implementation. Owns its own watermark via
    db/capture/state.py (keyed by absolute file path)."""

    name = NAME

    def __init__(self, transcript_glob: Optional[str] = None, watermark_db: Optional[str] = None):
        import config as capture_config
        cfg = capture_config.get_config()
        self.transcript_glob = transcript_glob or cfg.transcript_glob
        self.watermark_db = watermark_db or cfg.watermark_store

    def poll(self, max_spans: int = 50, force_close_tail: bool = False,
             persist_watermark: bool = True) -> list:
        """Scan every matching JSONL file, tail from its watermark, and
        return newly-COMPLETE CaptureSpans (capped at max_spans total across
        all files this call — anti-storm at the adapter layer too, mirroring
        REV 6's rate-limit ethos). `force_close_tail=True` is the PreCompact/
        SessionEnd low-latency nudge: treats EOF as a closing boundary so
        the final in-flight span is captured immediately rather than
        waiting for the next user turn. `persist_watermark=False` makes the
        poll SIDE-EFFECT-FREE (dry-run preview): spans are returned but the
        on-disk watermark is not advanced, so a subsequent real run re-sees
        exactly the same spans."""
        spans: list = []
        for path in _discover_files(self.transcript_glob):
            if len(spans) >= max_spans:
                break
            try:
                spans.extend(self._poll_file(path, max_spans - len(spans),
                                             force_close_tail, persist_watermark))
            except Exception as exc:
                # One bad/locked/rotated file must never sink the whole poll.
                logger.warning("claude_code_tailer: poll_file failed for %s (skipped): %s", path, exc)
                continue
        return spans

    def _poll_file(self, path: str, budget: int, force_close_tail: bool,
                   persist_watermark: bool = True) -> list:
        try:
            size_now = os.path.getsize(path)
        except OSError:
            return []
        start = capture_state.get_watermark(self.watermark_db, path)
        if start > size_now:
            # File shrank/rotated underneath us — safest fail-soft recovery
            # is to restart from 0 rather than crash or wedge forever.
            logger.warning("claude_code_tailer: %s shrank (%d -> %d); resetting watermark", path, start, size_now)
            start = 0
        if start >= size_now:
            return []  # nothing new

        agent_id, parent_agent_id = _agent_id_for_path(path)

        spans: list = []
        pending_events: list = []       # events accumulating for the open span
        pending_project: Optional[str] = None
        prev_end_byte = start           # byte offset just before the CURRENT line
        watermark = start               # byte offset of the last CLOSED span's boundary
        turn_counter = 0

        def _close_span(boundary_byte: int) -> None:
            nonlocal pending_events, pending_project, watermark
            if not pending_events:
                return
            last = pending_events[-1]
            span_id_src = f"{path}:{last.get('uuid') or boundary_byte}"
            span_id = hashlib.sha1(span_id_src.encode("utf-8")).hexdigest()
            session = last.get("sessionId") or Path(path).stem
            evs = [
                CaptureEvent(
                    session=session, turn=i, role=e["role"], content=e["content"],
                    tool_calls=e["tool_calls"], tool_results=e["tool_results"],
                    agent_id=agent_id, parent_agent_id=parent_agent_id,
                    ts=e.get("ts"), source=NAME,
                )
                for i, e in enumerate(pending_events)
            ]
            spans.append(CaptureSpan(session=session, events=evs, span_id=span_id, project=pending_project))
            pending_events = []
            watermark = boundary_byte

        for raw, end_byte in _iter_lines_from_offset(path, start):
            if len(spans) >= budget:
                # Stop consuming; watermark stays at the last CLOSED
                # boundary so the un-consumed remainder is re-read next poll.
                break

            raw_s = raw.strip()
            if not raw_s:
                prev_end_byte = end_byte
                continue
            obj = _parse_line(raw_s)
            if obj is None:
                prev_end_byte = end_byte
                continue

            etype = obj.get("type")
            if etype not in ("user", "assistant"):
                prev_end_byte = end_byte
                continue

            if _is_user_turn(obj):
                # A NEW user turn closes whatever span was open (boundary =
                # the byte offset right before THIS line began).
                _close_span(prev_end_byte)
                pending_project = obj.get("cwd") or pending_project

            message = obj.get("message") or {}
            role = message.get("role") or etype
            text, tool_calls, tool_results = _extract_text_blocks(message.get("content"))

            if text or tool_calls or tool_results:
                turn_counter += 1
                pending_events.append({
                    "role": role, "content": text, "tool_calls": tool_calls,
                    "tool_results": tool_results, "ts": obj.get("timestamp"),
                    "uuid": obj.get("uuid"), "sessionId": obj.get("sessionId"),
                })
            prev_end_byte = end_byte

        if force_close_tail and pending_events:
            _close_span(prev_end_byte)

        if persist_watermark:
            capture_state.set_watermark(self.watermark_db, path, watermark)
        return spans
