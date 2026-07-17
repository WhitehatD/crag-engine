"""Entity normalization — Graph v2 (migration 027).

Pure functions only: no DB I/O, no imports beyond stdlib.
Each per-type normalizer returns::

    {"canonical": str, "reject": bool, "reason": str | None}

Public API
----------
normalize(entity_type, raw_value) -> dict
    Route to the per-type normalizer. Unknown types pass through unchanged
    (canonical == raw_value, reject=False) so callers never crash on new types.

REJECT means "do not insert into entity_canonical and do not set
canonical_entity_id on the entity_links row". The raw entity_links row is
kept (append-only doctrine); only the canonical tier is guarded.
"""
from __future__ import annotations

import os
import re

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_LEADING_ZEROS_RE = re.compile(r"^0+(\d+)$")

# Known repo roots — single-segment paths under these are legitimate.
# Override via CRAG_ENGINE_KNOWN_ROOTS (comma-separated) for your workspace layout.
_KNOWN_ROOTS = {
    r.strip() for r in os.environ.get(
        "CRAG_ENGINE_KNOWN_ROOTS", "/src,/projects,/workspace,/repos"
    ).split(",") if r.strip()
}

# URL-fragment-ish short paths to reject (single or double segment, non-repo).
_JUNK_PATH_SEGMENTS_RE = re.compile(
    r"^/?(?:main|api|governance|src|docs|lib|dist|build|out|tmp|var|etc|opt|usr|bin|sbin)/?$",
    re.I,
)

# Reverse-package / dotted-namespace — reject as domain false-positive.
# Real reverse-package names START with a TLD-ish token (com.example.foo,
# org.springframework.boot); real domains END with a TLD (api.github.com).
# Anchor on the FIRST label only — matching "any short first label" (the old
# regex) wrongly rejected 36.2% of real 3+-label domains (api.github.com,
# git.example.com, cdn.assets.example.org, api.eu.example.net).
_REVERSE_PKG_TLD_TOKENS = frozenset({
    "com", "org", "net", "io", "dev", "edu", "gov", "co",
})


# ---------------------------------------------------------------------------
# Per-type normalizers
# ---------------------------------------------------------------------------

def _normalize_port(raw: str) -> dict:
    """Strip leading zeros; reject if out of valid port range 1-65535."""
    m = _LEADING_ZEROS_RE.match(raw.strip())
    canonical = m.group(1) if m else raw.strip()
    try:
        port_num = int(canonical)
    except ValueError:
        return {"canonical": raw, "reject": True, "reason": "non-numeric port"}
    if not (1 <= port_num <= 65535):
        return {"canonical": canonical, "reject": True,
                "reason": f"port {port_num} out of range 1-65535"}
    return {"canonical": canonical, "reject": False, "reason": None}


def _normalize_ip(raw: str) -> dict:
    """Validate IP octets; no transformation needed (already dotted-decimal)."""
    v = raw.strip()
    parts = v.split(".")
    if len(parts) != 4:
        return {"canonical": v, "reject": True, "reason": "not 4-octet IP"}
    try:
        if not all(0 <= int(o) <= 255 for o in parts):
            return {"canonical": v, "reject": True, "reason": "octet out of range"}
    except ValueError:
        return {"canonical": v, "reject": True, "reason": "non-numeric octet"}
    return {"canonical": v, "reject": False, "reason": None}


def _normalize_domain(raw: str) -> dict:
    """Lowercase; reject reverse-package patterns.

    TLD-anchored: reverse-package names put the TLD-ish token FIRST
    (com.example.foo); real domains put it LAST (api.github.com). Only
    reject when there are 3+ labels AND the first label is a known
    TLD-ish token — this leaves real subdomains (api.*, git.*, cdn.*,
    2-6 char labels) untouched regardless of label count.
    """
    v = raw.strip().lower()
    labels = v.split(".")
    if len(labels) >= 3 and labels[0] in _REVERSE_PKG_TLD_TOKENS:
        return {"canonical": v, "reject": True,
                "reason": "reverse-package namespace, not a domain"}
    return {"canonical": v, "reject": False, "reason": None}


def _normalize_path(raw: str) -> dict:
    """Collapse drive prefix, normalise separators, reject junk fragments.

    Rules (from PART E audit):
    - Strip Windows drive letter prefix (D:/ → /).
    - Normalise backslashes to forward slashes.
    - Reject if fewer than 2 non-empty segments (e.g. /main, /api).
    - Reject well-known junk single-segment paths.
    - Deduplicate duplicate separators.
    """
    v = raw.strip()
    # Strip drive prefix: D:/foo → /foo
    if _DRIVE_RE.match(v):
        v = "/" + _DRIVE_RE.sub("", v)
    # Normalise separators
    v = v.replace("\\", "/")
    # Collapse duplicate slashes
    v = re.sub(r"/+", "/", v)
    # Drop trailing slash (unless root)
    if len(v) > 1:
        v = v.rstrip("/")

    segments = [s for s in v.split("/") if s]

    # Fewer than 2 segments → only allow known repos
    if len(segments) == 0:
        return {"canonical": v, "reject": True, "reason": "empty path"}
    if len(segments) == 1:
        # Single segment: only pass if it matches a known repo root
        candidate = "/" + segments[0]
        if candidate not in _KNOWN_ROOTS:
            return {"canonical": v, "reject": True,
                    "reason": f"single-segment path not in known roots: {segments[0]}"}
    # Check for well-known junk top-level segments
    if _JUNK_PATH_SEGMENTS_RE.match("/" + segments[0]):
        return {"canonical": v, "reject": True,
                "reason": f"URL-fragment-ish junk path: /{segments[0]}"}

    return {"canonical": v, "reject": False, "reason": None}


def _normalize_service(raw: str) -> dict:
    """Lowercase only; services are short known names."""
    return {"canonical": raw.strip().lower(), "reject": False, "reason": None}


def _normalize_file(raw: str) -> dict:
    """Extract basename; keep as-is (filenames are already short)."""
    v = raw.strip().replace("\\", "/")
    # basename only
    v = v.rsplit("/", 1)[-1]
    return {"canonical": v, "reject": False, "reason": None}


def _normalize_classname(raw: str) -> dict:
    """Strip leading/trailing whitespace; keep case (Java/Python conventions)."""
    return {"canonical": raw.strip(), "reject": False, "reason": None}


def _normalize_env_var(raw: str) -> dict:
    """Uppercase; strip whitespace."""
    return {"canonical": raw.strip().upper(), "reject": False, "reason": None}


_NORMALIZERS = {
    "port": _normalize_port,
    "ip": _normalize_ip,
    "domain": _normalize_domain,
    "path": _normalize_path,
    "service": _normalize_service,
    "file": _normalize_file,
    "classname": _normalize_classname,
    "env_var": _normalize_env_var,
}


def normalize(entity_type: str, raw_value: str) -> dict:
    """Normalize *raw_value* for the given *entity_type*.

    Returns::
        {
            "canonical": str,   # normalized form
            "reject":    bool,  # True → do not insert into entity_canonical
            "reason":    str | None,  # rejection reason or None
        }

    Unknown entity_types pass through unchanged (reject=False).
    """
    fn = _NORMALIZERS.get(entity_type)
    if fn is None:
        return {"canonical": raw_value, "reject": False, "reason": None}
    return fn(raw_value)
