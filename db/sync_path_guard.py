# coding: utf-8
"""Sync-folder corruption guard (docs/architecture.md REV 4 item 2).

A SQLite DB living under a file-sync service (Dropbox / OneDrive / Google
Drive / iCloud / Syncthing / a generic `.sync` folder) is a documented
corruption class: the sync client rewrites the file's inode / copies the
`-wal` and `-shm` sidecars out from under an open connection, producing
"database disk image is malformed". See https://sqlite.org/howtocorrupt
(section on network/sync filesystems).

Pure module, no I/O beyond a path resolution the caller passes in. The
daemon calls `check_db_path(DB_PATH)` at lifespan begin and refuses to start
(RuntimeError) unless the operator sets CRAG_ANCHOR_ALLOW_SYNC_PATH=1, which
downgrades the refusal to a loud warning.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# Case-insensitive path-SEGMENT patterns. We match against individual path
# segments (split on both separators) so "my_dropbox_backups" as a full
# filename does NOT trip, but a real ".../Dropbox/..." segment does. Syncthing
# marker folders (`.stfolder`, `.stversions`) and the generic `.sync` marker
# are matched too.
_SYNC_SEGMENT_PATTERNS = [
    r"^dropbox$",
    r"^onedrive.*$",              # "OneDrive", "OneDrive - Contoso"
    r"^google ?drive$",           # "Google Drive", "GoogleDrive"
    r"^gdrive$",
    r"^my ?drive$",               # Google Drive for Desktop virtual root
    r"^icloud.*$",                # "iCloud", "iCloudDrive", "iCloud Drive"
    r"^com~apple~clouddocs$",     # macOS iCloud Drive on-disk segment
    r"^\.sync$",
    r"^\.stfolder$",              # Syncthing marker
    r"^\.stversions$",            # Syncthing versions
    r"^syncthing$",
]

_SYNC_RE = re.compile("|".join(_SYNC_SEGMENT_PATTERNS), re.IGNORECASE)

_ALLOW_ENV = "CRAG_ANCHOR_ALLOW_SYNC_PATH"


def _segments(path: Path) -> list[str]:
    """Return normalized path segments (drive/anchor stripped), splitting on
    both separators so a value like 'C:\\Users\\me\\Dropbox\\db' yields
    ['Users', 'me', 'Dropbox', 'db'] regardless of the running OS."""
    raw = str(path)
    parts: list[str] = []
    for chunk in re.split(r"[\\/]+", raw):
        seg = chunk.strip()
        if not seg or seg.endswith(":"):   # drop empty + drive-letter anchors
            continue
        parts.append(seg)
    return parts


def detect_unc_prefix(path) -> Optional[str]:
    """Return a marker string if `path` is a UNC / network-share path
    (Windows \\\\server\\share or POSIX //server/share), else None. SQLite over
    a network filesystem is a documented corruption class independent of any
    sync client (advisory-lock semantics differ over the wire). We inspect the
    RAW leading separators — a resolved Path can collapse a leading '//' — so
    the caller should pass the raw configured path for this probe."""
    raw = str(path)
    norm = raw.replace("\\", "/")
    if norm.startswith("//"):
        # //server/share/... — the marker is the server component when present.
        rest = norm.lstrip("/")
        server = rest.split("/", 1)[0] if rest else ""
        return f"\\\\{server}" if server else "\\\\<network>"
    return None


def detect_sync_segment(path) -> Optional[str]:
    """Return the offending path segment if `path` falls under a known
    sync-service folder OR is a UNC/network-share path, else None. Pure —
    resolves the path lexically (no disk access required; caller may pass an
    already-resolved absolute path). The UNC probe runs on the raw string
    BEFORE Path normalization can collapse a leading '//'.
    """
    unc = detect_unc_prefix(path)
    if unc is not None:
        return unc
    try:
        p = Path(path)
    except TypeError:
        return None
    for seg in _segments(p):
        if _SYNC_RE.match(seg):
            return seg
    return None


def check_db_path(path, *, logger=None) -> Optional[str]:
    """Enforce the guard for a DB path.

    - If the path is NOT under a sync folder: return None (all good).
    - If it IS and CRAG_ANCHOR_ALLOW_SYNC_PATH is unset/false: raise RuntimeError.
    - If it IS and the escape hatch is set: log a loud warning and return the
      offending segment (does NOT raise).

    `logger` is optional (a logging.Logger). When absent, the warning path is
    silent to the caller's logs but still returns the segment.
    """
    # UNC/network probe must see the RAW string — Path.resolve() collapses a
    # leading '//'. Run it before any normalization.
    seg = detect_unc_prefix(path)
    if seg is None:
        try:
            resolved = Path(path).resolve()
        except Exception:
            resolved = Path(path)
        seg = detect_sync_segment(resolved)
    else:
        resolved = Path(path)
    if seg is None:
        return None

    allow = str(os.environ.get(_ALLOW_ENV, "")).strip().lower() in ("1", "true", "yes", "on")
    msg = (
        f"Engine DB path resolves under a file-sync folder segment "
        f"'{seg}' ({resolved}). File-sync services (Dropbox/OneDrive/Google "
        f"Drive/iCloud/Syncthing) rewrite WAL/inode state under open SQLite "
        f"connections — a documented corruption class "
        f"(https://sqlite.org/howtocorrupt). Move the DB off the synced tree, "
        f"or set {_ALLOW_ENV}=1 to override at your own risk."
    )
    if allow:
        if logger is not None:
            logger.warning("SYNC-PATH OVERRIDE (%s=1): %s", _ALLOW_ENV, msg)
        return seg
    raise RuntimeError(msg)
