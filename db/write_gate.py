# coding: utf-8
"""Grounding v3 REV 3 — write-path governance.

"The same governance that gates code commits gates memory writes"
(docs/architecture.md, REV 3, operator). Before migration 031/032 there was NO
save-time gate at all beyond the near-dup guard: any string could enter the
corpus. This module adds the gates that run BEFORE anything is written:

    schema gate (size/type sanity)  -> HARD, routes to insights_staging
    secret-pattern scan on content  -> HARD, routes to insights_staging
    dedup + lifecycle classification -> ADVISORY, annotates the response

CRITICAL CONSTRAINT (read before touching provenance checks):
apps/daemon/tests/test_staging_removal.py::T_DIRECT is a DELIBERATE, already-
shipped operator decision (2026-07-04): low-provenance saves (no role, no
source_file, no session_id) MUST insert DIRECTLY into `insights`, not a
staging tier — the OLD `insights_staged` staging tier was removed because it
had a 4% graduation rate and 79 rows stuck pending forever. The REV 3 brief
asks for a "required provenance" schema-gate check; that requirement is
implemented here as an ADVISORY signal only (surfaced in the response, never
blocking) specifically to avoid resurrecting the exact anti-pattern the
operator already tore out. This is a deliberate, documented deviation from a
literal reading of the brief — see the docstring on `check_schema()`.

House style: pure functions, take an open sqlite3.Connection where needed,
never raise to the save-path caller (fail-soft; a write_gate bug must never
block a save). Timestamps via lifecycle._utcnow_iso().
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crag-anchor")

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from lifecycle import _utcnow_iso  # noqa: E402

# ---------------------------------------------------------------------------
# Schema gate constants
# ---------------------------------------------------------------------------

MIN_CONTENT_LEN = 3
MAX_CONTENT_LEN = 20_000  # generous sanity cap; no legitimate insight is this long

# Mirrors the MCP save_insight tool's `type` enum (server-side defense in
# depth — the MCP client already restricts this, but /save_insight is a raw
# HTTP endpoint any caller can hit directly).
VALID_TYPES = frozenset({
    "gotcha", "pattern", "architecture", "decision", "bug-fix",
    "tool", "feedback", "user-context", "project-context", "reference",
})

_INVALIDATION_RE = re.compile(r"\b(?:supersedes|replaces)\s+#(\d+)\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Content secret-pattern scan — live CREDENTIAL VALUES, not command shapes.
# (Distinct from grounding_queue_v2._SECRET_PATTERNS, which guards the SHAPE
# of a read-only shell command about to run. This guards insight TEXT about
# to be PERSISTED — the failure mode is a live credential value landing
# in the memory corpus.)
# ---------------------------------------------------------------------------
_CONTENT_SECRET_PATTERNS: tuple = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("stripe_live_key", re.compile(r"\bsk_live_[0-9a-zA-Z]{16,}\b")),
    ("stripe_live_key_alt", re.compile(r"\bpk_live_[0-9a-zA-Z]{16,}\b")),
    ("github_pat", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{20,}\b")),
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{20,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("generic_password_assign", re.compile(r"\bpassword\s*=\s*['\"]?\S{6,}", re.IGNORECASE)),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("bearer_token_inline", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
)


def scan_content_secrets(content: str) -> Optional[str]:
    """Return the matched pattern name if `content` looks like it embeds a
    live credential, else None. Fail-open on regex errors (never raises)."""
    if not content:
        return None
    try:
        for name, pattern in _CONTENT_SECRET_PATTERNS:
            if pattern.search(content):
                return name
    except Exception as exc:  # pragma: no cover — regexes are static/tested
        logger.warning("write_gate: secret scan raised (fail-open): %s", exc)
        return None
    return None


# ---------------------------------------------------------------------------
# Schema gate
# ---------------------------------------------------------------------------

@dataclass
class SchemaVerdict:
    ok: bool
    reason: Optional[str] = None            # set when ok=False (HARD failure)
    provenance_present: bool = True         # ADVISORY only, see module docstring
    advisories: list = field(default_factory=list)


def check_schema(content: str, type_: str, tags: Optional[str],
                 source_file: Optional[str], session_id: Optional[str],
                 role: Optional[str]) -> SchemaVerdict:
    """HARD gate: size caps + type constraint. Provenance (session_id /
    source_file / evidence-reference) is checked but recorded as ADVISORY
    only — see module docstring for why this deliberately does not block
    (T_DIRECT, 2026-07-04 staging-tier removal)."""
    content = content or ""
    if len(content.strip()) < MIN_CONTENT_LEN:
        return SchemaVerdict(ok=False, reason="schema_gate:content_too_short")
    if len(content) > MAX_CONTENT_LEN:
        return SchemaVerdict(
            ok=False,
            reason=f"schema_gate:content_too_long ({len(content)} > {MAX_CONTENT_LEN})",
        )
    if type_ and type_ not in VALID_TYPES:
        return SchemaVerdict(
            ok=False, reason=f"schema_gate:invalid_type ({type_!r} not in {sorted(VALID_TYPES)})"
        )

    advisories: list = []
    has_provenance = bool(
        (session_id and str(session_id).strip())
        or (source_file and str(source_file).strip())
        or (tags and "evidence:" in str(tags).lower())
    )
    if not has_provenance:
        advisories.append("no session_id/source_file/evidence-ref — accepted per "
                          "T_DIRECT (2026-07-04 staging-tier removal); provenance-poor")
    return SchemaVerdict(ok=True, provenance_present=has_provenance, advisories=advisories)


# ---------------------------------------------------------------------------
# Lifecycle resolver — TRACE-style classification of a candidate dup set.
# ADVISORY: annotates the response; does NOT change the existing dedup guard's
# insert/reject decision (that guard is unchanged, tested by T_DEDUP).
# ---------------------------------------------------------------------------

# Above this similarity, two claims are "the same assertion" (noop candidate).
# Below it but still a dedup-guard hit, they're "related" (update candidate).
NOOP_SIMILARITY_FLOOR = 0.97


def resolve_lifecycle(content: str, dup_candidates: list) -> dict:
    """Classify the incoming write against `dup_candidates` (the existing
    dedup guard's near-dup list, id/content/similarity dicts) into
    noop|update|supersede|new. `split` is NOT decided here — that is claim
    decomposition's job downstream (multi-assertion insights already split
    into claims at persist_claims time).

    Returns {"action": ..., "target_id": <id>|None}.
    """
    m = _INVALIDATION_RE.search(content or "")
    if m:
        return {"action": "supersede", "target_id": int(m.group(1))}

    if not dup_candidates:
        return {"action": "new", "target_id": None}

    top = max(dup_candidates, key=lambda c: c.get("similarity", 0))
    if top.get("similarity", 0) >= NOOP_SIMILARITY_FLOOR:
        return {"action": "noop", "target_id": top.get("id")}
    return {"action": "update", "target_id": top.get("id")}


# ---------------------------------------------------------------------------
# Staging writer — HARD-gate failures land here with a machine-readable
# reason (migration 032: insights_staging.reason). Never raises.
# ---------------------------------------------------------------------------

def route_to_staging(conn, content: str, type_: str, project: Optional[str],
                     reason: str, source: str = "gate_failure") -> Optional[int]:
    """Write a gate-rejected save to insights_staging. Returns the staging
    row id, or None on failure (fail-soft — a staging-write failure must
    never crash the save path; the caller already has the reject reason to
    report to the client even if this persistence step fails)."""
    try:
        now = _utcnow_iso()
        payload = json.dumps({"content": content, "type": type_})
        blob = source + reason + (content or "")[:500]
        dedup_key = hashlib.sha1(blob.encode("utf-8")).hexdigest()
        cur = conn.execute(
            "INSERT INTO insights_staging "
            "(source, project, payload, dedup_key, status, reason, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (source, project, payload, dedup_key, reason, now),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as exc:
        logger.warning("write_gate: route_to_staging failed (fail-soft): %s", exc)
        return None


def evaluate_hard_gates(content: str, type_: str, tags: Optional[str],
                        source_file: Optional[str], session_id: Optional[str],
                        role: Optional[str]) -> Optional[str]:
    """Run the HARD gates (schema + secret scan) in order. Returns a
    machine-readable reason string on FIRST failure, or None if both pass.
    Never raises — any internal error is treated as a pass (fail-open, same
    doctrine as the mechanical falsifier/contradiction detectors elsewhere
    in this codebase: a write_gate bug must never block a legitimate save)."""
    try:
        verdict = check_schema(content, type_, tags, source_file, session_id, role)
        if not verdict.ok:
            return verdict.reason
        secret_hit = scan_content_secrets(content)
        if secret_hit:
            return f"secret_scan:{secret_hit}"
    except Exception as exc:
        logger.warning("write_gate: evaluate_hard_gates raised (fail-open): %s", exc)
        return None
    return None
