"""transcript_tokens.py — Pure transcript-based token parser for crag engine daemon.

Replaces the broken Headroom-counter-diff approach with authoritative
per-session token sums read directly from Claude Code JSONL transcripts.

Each assistant message in the JSONL carries:
  message.usage.input_tokens                  — fresh (non-cached) input
  message.usage.cache_read_input_tokens       — cache read
  message.usage.cache_creation_input_tokens   — cache write
  message.usage.output_tokens                 — output

Derivations (Anthropic billing categories are DISTINCT — do NOT conflate):
  tokens_in  = sum(input_tokens)            — fresh (uncached) input only
  tokens_out = sum(output_tokens)
  cache_read_tokens / cache_write_tokens are tracked SEPARATELY (different
  cost tiers: read ~10%, write ~125% of base). Summing cache_read into
  tokens_in inflates the headline by 100-1000x on long cached sessions
  (the same context is re-read every turn), so it is intentionally excluded.

The file is streamed line-by-line so multi-thousand-line transcripts never
require loading the whole file into memory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Optional

# Default location of Claude Code project transcript dirs (platform-independent
# string; callers on Windows should pass a native Path explicitly).
_DEFAULT_PROJECTS_DIR: Optional[Path] = None


def _get_default_projects_dir() -> Path:
    """Return ~/.claude/projects, expanding ~ to the real home dir."""
    return Path.home() / ".claude" / "projects"


def parse_transcript(path: "str | Path") -> dict:
    """Parse a Claude Code session JSONL transcript and return token sums.

    Streams the file line-by-line — safe for large files (>4000 lines).
    Skips malformed lines and lines without message.usage silently.

    Returns:
        {
          "fresh_input_tokens":  int,   # sum of input_tokens across assistant msgs
          "cache_read_tokens":   int,   # sum of cache_read_input_tokens
          "cache_write_tokens":  int,   # sum of cache_creation_input_tokens
          "output_tokens":       int,   # sum of output_tokens
          "tokens_in":           int,   # = fresh_input_tokens (Anthropic input_tokens; cache excluded)
          "tokens_out":          int,   # output_tokens
          "assistant_msgs":      int,   # count of assistant messages with usage
          "model":               str | None,  # last model seen in assistant msgs
        }

    Never raises — returns all-zero dict on missing / empty / unreadable file.
    """
    path = Path(path)
    fresh = 0
    cache_read = 0
    cache_write = 0
    output = 0
    msgs = 0
    model: Optional[str] = None

    if not path.exists():
        return _zero_result()

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if not isinstance(obj, dict):
                    continue
                if obj.get("type") != "assistant":
                    continue

                message = obj.get("message")
                if not isinstance(message, dict):
                    continue

                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue

                # Sum — guard against None values (some fields are optional)
                fresh      += int(usage.get("input_tokens") or 0)
                cache_read += int(usage.get("cache_read_input_tokens") or 0)
                cache_write += int(usage.get("cache_creation_input_tokens") or 0)
                output     += int(usage.get("output_tokens") or 0)
                msgs       += 1

                m = message.get("model")
                if m:
                    model = m  # keep last seen model

    except OSError:
        return _zero_result()

    return {
        "fresh_input_tokens": fresh,
        "cache_read_tokens":  cache_read,
        "cache_write_tokens": cache_write,
        "output_tokens":      output,
        "tokens_in":          fresh,
        "tokens_out":         output,
        "assistant_msgs":     msgs,
        "model":              model,
    }


def _zero_result() -> dict:
    return {
        "fresh_input_tokens": 0,
        "cache_read_tokens":  0,
        "cache_write_tokens": 0,
        "output_tokens":      0,
        "tokens_in":          0,
        "tokens_out":         0,
        "assistant_msgs":     0,
        "model":              None,
    }


def resolve_transcript(
    session_uuid: str,
    projects_dir: "Optional[Path | str]" = None,
) -> Optional[Path]:
    """Find the JSONL transcript for a given session_uuid.

    Globs ``<projects_dir>/*/<session_uuid>.jsonl`` and returns the first
    match, or None if not found.

    Args:
        session_uuid:  The UUID from session_meta (matches the filename stem).
        projects_dir:  Override the default ``~/.claude/projects`` root.
                       Useful in tests.

    Returns:
        Path to the JSONL file, or None.
    """
    if projects_dir is None:
        base = _get_default_projects_dir()
    else:
        base = Path(projects_dir)

    if not base.exists():
        return None

    target_name = f"{session_uuid}.jsonl"
    for candidate in base.iterdir():
        if not candidate.is_dir():
            continue
        p = candidate / target_name
        if p.exists():
            return p

    return None


def iter_all_transcripts(
    projects_dir: "Optional[Path | str]" = None,
) -> Iterator[tuple[str, Path]]:
    """Yield (session_uuid, path) for every transcript under projects_dir.

    Skips files whose stem does not look like a UUID (e.g. non-JSONL or
    metadata files).  Does NOT read or parse the files — caller does that.

    Args:
        projects_dir: Override the default ``~/.claude/projects`` root.

    Yields:
        (session_uuid, path) tuples — one per ``.jsonl`` file found.
    """
    if projects_dir is None:
        base = _get_default_projects_dir()
    else:
        base = Path(projects_dir)

    if not base.exists():
        return

    for project_dir in sorted(base.iterdir()):
        if not project_dir.is_dir():
            continue
        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            stem = jsonl_file.stem
            # Minimal UUID-ish check: 36 chars with hyphens at right positions
            # Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
            if (
                len(stem) == 36
                and stem[8] == "-"
                and stem[13] == "-"
                and stem[18] == "-"
                and stem[23] == "-"
            ):
                yield stem, jsonl_file
