# coding: utf-8
"""Grounding v3 — the Claim Layer.

An insight is a NARRATIVE. This module decomposes it into ATOMIC CLAIMS,
canonicalizes+dedups them into a shared pool, types each claim with the closed
P1-P5 predicate taxonomy, and links parents to claims. Verification and rollup
run at CLAIM granularity elsewhere (grounding_queue_v2 verify executors +
claim_rollup below).

Pipeline (called from the daemon's async post-save path, after entity extraction):

    decompose_insight()  -> list[ClaimDraft]        (role 'decompose', fail-soft)
    classify_claim()     -> predicate_class          (rules first, LLM last)
    author_predicate()   -> predicate_spec           (P1/P4 via role model)
    canonicalize+link()  -> claims + *_claims rows    (persist_claims)

House style (db/lifecycle.py): pure functions take an open sqlite3.Connection /
client as args, never open/close connections themselves. Timestamps via
lifecycle._utcnow_iso(). NEVER SQLite datetime('now').

Fail-soft ethos: every stage degrades to a safe default and NEVER raises to the
caller (the save already returned 200). A decomposition failure yields ONE
summary claim; a classification failure yields P5 (terminal, never queued); an
authoring failure leaves predicate_spec NULL (the claim still exists + rolls up).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("crag-anchor")

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from lifecycle import _utcnow_iso  # noqa: E402
import entity_extract  # noqa: E402

# The subprocess write-guard is authoritative in grounding_queue_v2; we import
# its predicates so P1 authoring validates against the SAME sacred guard.
import grounding_queue_v2  # noqa: E402

# ---------------------------------------------------------------------------
# Predicate taxonomy — closed and total. Coverage is 100% by construction.
# ---------------------------------------------------------------------------
P1_MECHANICAL = "P1"   # read-only shell check {cmd, expect}
P2_DOCUMENTARY = "P2"  # source anchor {file, load_bearing_substrings|region_hash}
P3_TEMPORAL = "P3"     # event assertion checked vs local ground truth
P4_SEMANTIC = "P4"     # evidence-bundle recipe {sources[], question} -> LLM verdict
P5_AXIOMATIC = "P5"    # preference/decision/cost/history — terminal, never queued

PREDICATE_CLASSES = (P1_MECHANICAL, P2_DOCUMENTARY, P3_TEMPORAL, P4_SEMANTIC, P5_AXIOMATIC)

# P5 default re-confirm cadence (days). Tunable; preferences drift slowly.
P5_REVIEW_DAYS = 180

MAX_CLAIMS_PER_INSIGHT = 10
MIN_CLAIMS_PER_INSIGHT = 1

# Canonical dedup: cosine >= this AND same primary entity => LINK not insert.
CANONICAL_DEDUP_THRESHOLD = 0.92


@dataclass
class ClaimDraft:
    """A single atomic assertion produced by decomposition, before persistence."""
    text: str
    role: str = "supporting"            # core|supporting|context
    entities: list = field(default_factory=list)  # [{entity, entity_type}]
    predicate_class: Optional[str] = None
    predicate_spec: Optional[dict] = None
    review_after: Optional[str] = None


# ===========================================================================
# 1. DECOMPOSE  (role 'decompose', strict JSON, fail-soft to one summary claim)
# ===========================================================================

_DECOMPOSE_SYSTEM = (
    "You split a memory note into atomic, independently-checkable assertions. "
    "Return STRICT JSON: an array of 1-10 objects, each "
    '{"text": "<single assertion>", "role": "core|supporting|context"}. '
    "A 'core' claim is load-bearing (the note is wrong if it is wrong). "
    "'supporting' adds detail; 'context' is background/preference. "
    "Each text must be ONE falsifiable-or-axiomatic assertion, self-contained "
    "(resolve pronouns). Output ONLY the JSON array, no prose."
)


def _summary_claim(content: str) -> ClaimDraft:
    """The fail-soft floor: the whole insight as one claim (its first sentence
    or a 240-char summary), role 'core', entities extracted by rule."""
    text = (content or "").strip()
    # First sentence-ish, capped.
    m = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
    head = (m[0] if m else text)[:240].strip() or text[:240].strip()
    ents = [{"entity": e["entity"], "entity_type": e["entity_type"]}
            for e in entity_extract.extract_entities(head)]
    return ClaimDraft(text=head, role="core", entities=ents)


def _parse_decompose_json(raw: str) -> Optional[list]:
    """Extract the JSON array from an LLM completion, tolerating fences/prose."""
    if not raw:
        return None
    s = raw.strip()
    # Strip ```json fences.
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    # Grab the first [...] block if there is surrounding prose.
    if not s.startswith("["):
        start = s.find("[")
        end = s.rfind("]")
        if start != -1 and end != -1 and end > start:
            s = s[start:end + 1]
    try:
        data = json.loads(s)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    return data


def decompose_insight(content: str, llm: Any = None) -> list:
    """Decompose an insight narrative into 1-10 atomic ClaimDrafts.

    Uses the 'decompose' role model when `llm` is provided; fail-soft to a
    single summary claim on ANY failure (no LLM, malformed JSON, empty output,
    out-of-range count). Entities are re-extracted per claim by rule so the
    canonical dedup + blast-radius links are always populated.
    """
    content = (content or "").strip()
    if not content:
        return []

    if llm is None:
        return [_summary_claim(content)]

    try:
        import grounding_config
        cfg = grounding_config.get_config()
        model = get_role_model("decompose")
        resp = grounding_queue_v2.llm_client.call_with_retry(
            llm,
            model=model,
            max_tokens=cfg.author_max_tokens,
            messages=[{"role": "user", "content": _DECOMPOSE_SYSTEM + "\n\nNOTE:\n" + content}],
        )
        grounding_queue_v2.llm_client.record_usage(resp, model=model, provider=cfg.provider)
        text = resp.content[0].text if getattr(resp, "content", None) else ""
    except grounding_queue_v2.llm_client.TransientLLMError:
        # Transient — do not burn the insight into a permanent 1-claim shape.
        # The backfill/sweep can re-decompose later; for the live save path we
        # still want a claim NOW, so return the summary floor (idempotent: a
        # later real decomposition supersedes via canonical linking).
        return [_summary_claim(content)]
    except Exception as exc:
        logger.debug("claim_layer: decompose LLM call failed (fail-soft): %s", exc)
        return [_summary_claim(content)]

    data = _parse_decompose_json(text)
    if not data:
        return [_summary_claim(content)]

    drafts: list = []
    for obj in data[:MAX_CLAIMS_PER_INSIGHT]:
        if not isinstance(obj, dict):
            continue
        ctext = str(obj.get("text", "")).strip()
        if not ctext:
            continue
        role = str(obj.get("role", "supporting")).strip().lower()
        if role not in ("core", "supporting", "context"):
            role = "supporting"
        ents = [{"entity": e["entity"], "entity_type": e["entity_type"]}
                for e in entity_extract.extract_entities(ctext)]
        drafts.append(ClaimDraft(text=ctext[:1000], role=role, entities=ents))

    if not drafts:
        return [_summary_claim(content)]
    return drafts


# ===========================================================================
# 2. CLASSIFY  (rules FIRST — P2/P3/P5 — then LLM for the remainder)
# ===========================================================================

# P3 event-shaped: past-tense infra/action verbs, ship records, dates, PR/commit refs.
_P3_MARKERS = re.compile(
    r"\b(shipped|deployed|merged|committed|pushed|released|migrated|rotated|"
    r"restarted|reverted|fixed in|landed|created (?:pr|the pr)|pr #?\d+|"
    r"commit [0-9a-f]{7}|on 20\d\d-\d\d-\d\d|as of 20\d\d)\b",
    re.I,
)

# P5 axiomatic: preference / decision / cost / history / operator-choice.
_P5_MARKERS = re.compile(
    r"\b(prefer|preference|we decided|decision:|chose|opt(?:ed)? for|"
    r"policy|convention|always use|never use|by design|operator (?:wants|prefers|decided)|"
    r"doctrine|rule of thumb|going forward|from now on|as a matter of)\b",
    re.I,
)

# Feedback/user-correction shape is also axiomatic (a preference expressed as a lesson).
_FEEDBACK_MARKERS = re.compile(
    r"\b(user (?:said|corrected|wants|asked)|don'?t do|stop doing|"
    r"lesson learned|the (?:right|correct) way is)\b",
    re.I,
)

# General-practice / craft-meta shape: imperative advice with NO documentary
# subject. These are P5 (axiomatic terminal) even when the parent insight has a
# source_file — the advice is not ABOUT that file's current content, so
# anchoring it to the file is a mis-classification (defect #3 in insight #3589:
# "when parallel agents collide use a separate branch" tagged P2 with a
# FormsPublicClient.tsx anchor). Modal/imperative craft verbs with no file
# subject of their own => general lesson, not a documentary fact.
_GENERAL_PRACTICE_MARKERS = re.compile(
    r"\b(when(?:ever)? (?:you|an? |parallel|the|it|there)|"
    r"if you (?:need|want|must|have to)|"
    r"(?:you )?should (?:always|never|use|prefer|avoid|not)|"
    r"(?:you )?must (?:always|never|use|not)|"
    r"best practice|use \w+ (?:not|instead of)|instead of|rather than|"
    r"remember to|make sure (?:to|you)|be careful (?:to|not)|"
    r"the trick is|the pattern is|as a rule)\b",
    re.I,
)

# Load-bearing entity types for a P2 documentary anchor. If a P2-eligible claim
# yields NONE of these, its file predicate degrades to a hollow "does the file
# exist" check (defect #2) — so we downgrade such claims to P5 instead.
_P2_SUBSTRING_TYPES = ("service", "port", "ip", "domain", "classname", "env_var", "path")


def _has_file_subject(draft: ClaimDraft) -> bool:
    """True iff the claim itself carries a file entity — i.e. the claim is
    plausibly ABOUT a specific file's content, not merely inheriting the parent
    insight's source_file. Used to gate P2 for general-practice claims."""
    return any(e["entity_type"] == "file" for e in draft.entities)


def _p2_load_bearing_substrings(draft: ClaimDraft) -> list:
    """Extract the strongest non-file identifiers a P2 predicate can grep for.
    Empty => the P2 predicate would verify NOTHING about content."""
    return [e["entity"] for e in draft.entities
            if e["entity_type"] in _P2_SUBSTRING_TYPES][:5]


def classify_claim(draft: ClaimDraft, source_file: Optional[str] = None) -> str:
    """Return a predicate_class for a claim. RULES run before any LLM call.

    ORDERING (fixed 2026-07-17 per insight #3589 — per-CLAIM classification, not
    per-insight-source_file spray):

      P5 — preference/decision/cost/history/feedback OR a general craft-practice
           lesson with no documentary file subject (terminal, never queued).
      P3 — event-shaped: past-tense ship/deploy/commit/date assertion. Checked
           BEFORE P2 so a temporal fact ("on 2026-07-12 a migration corrupted
           prod") is not falsely tagged P2 against a current file it predates.
      P2 — documentary: the claim is genuinely about a file's CURRENT content
           AND yields non-empty load-bearing substrings; else downgrade to P5
           (a hollow file-exists check verifies nothing — defect #2).

    Anything the rules don't catch that HAS a checkable entity is P1 (mechanical
    probe) if the entity yields a non-'none' falsifier, else P4 (semantic
    evidence bundle). Guarantees a class for every claim (100% coverage).

    `source_file` is the PARENT insight's source_file (claims inherit it) —
    passed by persist_claims; the LLM is NOT consulted here.
    """
    text = draft.text or ""

    # P5 first: a preference/decision/feedback is axiomatic regardless of any
    # entity it mentions.
    if _P5_MARKERS.search(text) or _FEEDBACK_MARKERS.search(text):
        return P5_AXIOMATIC

    # General craft-practice lesson with no file subject of its own => P5. This
    # runs BEFORE the P2 source_file inheritance so imperative advice does not
    # get spuriously anchored to the parent insight's file (defect #3).
    if _GENERAL_PRACTICE_MARKERS.search(text) and not _has_file_subject(draft):
        return P5_AXIOMATIC

    # P3: temporal event assertion. BEFORE P2 — a past event cannot be verified
    # by grepping a current file (defect #1).
    if _P3_MARKERS.search(text):
        return P3_TEMPORAL

    # P2: documentary — the claim is about a file's content. Two entry points:
    #   (a) the claim carries its own file entity, or
    #   (b) it inherits the parent insight's source_file.
    # In EITHER case P2 is only valid if the claim yields non-empty load-bearing
    # substrings to grep for; otherwise the predicate degrades to a hollow
    # file-exists check that verifies nothing, so downgrade to P5 (defect #2).
    p2_eligible = _has_file_subject(draft) or bool(source_file and str(source_file).strip())
    if p2_eligible:
        if _p2_load_bearing_substrings(draft):
            return P2_DOCUMENTARY
        # No load-bearing content identifiers. If the claim still has a
        # mechanical entity, prefer a real probe (P1) below; else it's an
        # unverifiable-as-documentary narrative — terminal P5.
        for e in draft.entities:
            fal = entity_extract.falsifier_for(e["entity_type"], e["entity"])
            if fal.get("kind") not in (None, "none"):
                return P1_MECHANICAL
        return P5_AXIOMATIC

    # Remaining: entity-bearing -> P1 if a mechanical falsifier exists, else P4.
    for e in draft.entities:
        fal = entity_extract.falsifier_for(e["entity_type"], e["entity"])
        if fal.get("kind") not in (None, "none"):
            return P1_MECHANICAL

    # No mechanical anchor: semantic evidence-bundle claim.
    return P4_SEMANTIC


# ===========================================================================
# 3. PREDICATE AUTHORING  (P2/P3 template-derived; P1/P4 via role model)
# ===========================================================================

_P1_AUTHOR_SYSTEM = (
    "Author a READ-ONLY shell check that would FALSIFY the claim if it ran and "
    "the output did not match. Return STRICT JSON: "
    '{"cmd": "<single read-only bash command>", "expect": "<substring the '
    'output MUST contain for the claim to hold>", "safety_class": "read_only"}. '
    "The command MUST be read-only: no rm/mv/cp/tee, no >, no writes, no POST/"
    "PUT/DELETE, no secrets (no cat of .env/.credentials, no --token, no base64 "
    "-d). Output ONLY the JSON object."
)

_P4_AUTHOR_SYSTEM = (
    "Author an evidence-bundle recipe to semantically re-verify the claim. "
    'Return STRICT JSON: {"sources": ["<read-only bash cmd or file to consult>", '
    '...], "question": "<yes/no question a judge answers from the gathered '
    'evidence to decide if the claim still holds>"}. Sources must be read-only. '
    "Output ONLY the JSON object."
)


def _validate_p1_spec(spec: dict) -> Optional[dict]:
    """Validate a P1 predicate spec against the SACRED grounding_queue_v2 guard
    (_FORBIDDEN + _SECRET_PATTERNS). Returns the spec on pass, None on reject."""
    if not isinstance(spec, dict):
        return None
    cmd = str(spec.get("cmd", "")).strip()
    expect = str(spec.get("expect", "")).strip()
    if not cmd or not expect:
        return None
    # Reuse the authoritative read-only guard — do NOT reimplement or weaken it.
    if not grounding_queue_v2._is_read_only(cmd):
        logger.debug("claim_layer: P1 spec rejected by write/secret guard: %r", cmd[:120])
        return None
    # No-op stub class (echo/printf/true): always succeeds, verifies nothing.
    if _P1_NOOP_CMD_RE.match(cmd):
        logger.debug("claim_layer: P1 spec rejected as no-op stub: %r", cmd[:120])
        return None
    return {"cmd": cmd, "expect": expect, "safety_class": "read_only"}


# P4 sources must ground against LOCAL read-only evidence (repo grep, git,
# local file reads) — never external network fetches or doc-path guessing
# (defect #4 in insight #3589: insight #1953 authored P4 sources like
# `curl -s https://docs.docker.com/...` and `cat $(find /usr/share/doc/...)`
# which won't ground reliably — third-party uptime and path-guessing are not
# evidence about OUR claims). _is_read_only permits `curl https://...` (it only
# blocks `curl -o`), so we add an explicit external-network + path-guess gate.
_P4_EXTERNAL_SOURCE_RE = re.compile(
    r"\b(?:curl|wget|https?://|nslookup|dig|host|ping|nc|telnet|ssh)\b", re.I
)
# POSIX system-directory references — hallucination class (round 2, pilot
# finding 2026-07-17: 31% of authored P4 specs guessed /var/log/..., cat
# /etc/os-release, even invented /var/log/dex_requisitions.log). These paths
# are meaningless on the box that grounds the claim (Windows laptop) and were
# never evidence about OUR claims: reject them in ANY command shape (grep,
# cat, ls, find, tail — the round-1 regex only caught `find /usr...`).
_P4_PATHGUESS_RE = re.compile(
    r"(?:^|[\s='\"(])/(?:usr|etc|var|opt|root|home|srv|proc|sys)\b", re.I
)


def _p4_source_is_local(s: str) -> bool:
    """True iff a P4 source consults LOCAL repo/filesystem evidence only.
    Rejects external-network fetches and POSIX system-path guessing."""
    if _P4_EXTERNAL_SOURCE_RE.search(s):
        return False
    if _P4_PATHGUESS_RE.search(s):
        return False
    return True


# P1 no-op command class — the author LLM emits `echo '...'` / `printf`/`true`
# stubs when it cannot author a real probe (pilot: 2/50 P1 specs). A predicate
# whose command always succeeds verifies nothing; reject so the claim persists
# specless (rolls up 'unverified') instead of falsely 'fresh'.
_P1_NOOP_CMD_RE = re.compile(r"^\s*(?:(?:echo|printf|true)\b|:(?:\s|$))", re.I)


def _validate_p4_spec(spec: dict) -> Optional[dict]:
    if not isinstance(spec, dict):
        return None
    sources = spec.get("sources")
    question = str(spec.get("question", "")).strip()
    if not isinstance(sources, list) or not sources or not question:
        return None
    clean_sources = []
    for s in sources:
        s = str(s).strip()
        if not s:
            continue
        # Guard 1: the sacred read-only write/secret guard.
        if not grounding_queue_v2._is_read_only(s):
            continue
        # Guard 2: LOCAL-only — no external network or system-path guessing.
        if not _p4_source_is_local(s):
            logger.debug("claim_layer: P4 source rejected as non-local: %r", s[:120])
            continue
        clean_sources.append(s)
    if not clean_sources:
        return None
    return {"sources": clean_sources[:6], "question": question}


def _author_via_llm(system: str, claim_text: str, llm: Any, role: str) -> Optional[dict]:
    try:
        import grounding_config
        cfg = grounding_config.get_config()
        model = get_role_model(role)
        resp = grounding_queue_v2.llm_client.call_with_retry(
            llm,
            model=model,
            max_tokens=cfg.author_max_tokens,
            messages=[{"role": "user", "content": system + "\n\nCLAIM:\n" + claim_text}],
        )
        grounding_queue_v2.llm_client.record_usage(resp, model=model, provider=cfg.provider)
        text = resp.content[0].text if getattr(resp, "content", None) else ""
    except grounding_queue_v2.llm_client.TransientLLMError:
        raise
    except Exception as exc:
        logger.debug("claim_layer: author LLM call failed (fail-soft): %s", exc)
        return None
    return _parse_decompose_json_obj(text)


def _parse_decompose_json_obj(raw: str) -> Optional[dict]:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    if not s.startswith("{"):
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            s = s[start:end + 1]
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def author_predicate(draft: ClaimDraft, predicate_class: str, llm: Any = None,
                     source_file: Optional[str] = None) -> Optional[dict]:
    """Author the predicate_spec for a claim of the given class.

    P2/P3 are TEMPLATE-derived (zero LLM). P1/P4 are LLM-authored (role
    'author') and schema-validated + guard-checked; a validation failure
    returns None (the claim keeps its class but has no runnable predicate —
    it rolls up as 'unverified' rather than blocking). Re-raises
    TransientLLMError so the worker can requeue.
    """
    if predicate_class == P2_DOCUMENTARY:
        anchor = source_file
        if not anchor:
            files = [e["entity"] for e in draft.entities if e["entity_type"] == "file"]
            anchor = files[0] if files else None
        # Load-bearing substrings: the strongest non-file entities in the claim.
        subs = _p2_load_bearing_substrings(draft)
        # Defence-in-depth (defect #2): a P2 with an empty substring list is a
        # hollow file-exists check that verifies NOTHING about content. classify
        # already downgrades such claims to P5, but if one slips through, return
        # no runnable predicate rather than authoring the hollow check.
        if not subs:
            return None
        return {"file": anchor, "load_bearing_substrings": subs}

    if predicate_class == P3_TEMPORAL:
        return {"assertion": draft.text[:300], "check": "git_log_or_events"}

    if predicate_class == P5_AXIOMATIC:
        return None  # terminal; no runnable predicate

    if predicate_class == P1_MECHANICAL:
        if llm is None:
            # Fall back to the mechanical entity falsifier template.
            for e in draft.entities:
                fal = entity_extract.falsifier_for(e["entity_type"], e["entity"])
                if fal.get("kind") not in (None, "none") and fal.get("spec"):
                    return {"cmd": fal["spec"], "expect": "", "safety_class": "read_only",
                            "authored_by": "template"}
            return None
        spec = _author_via_llm(_P1_AUTHOR_SYSTEM, draft.text, llm, "author")
        return _validate_p1_spec(spec) if spec else None

    if predicate_class == P4_SEMANTIC:
        if llm is None:
            return None
        spec = _author_via_llm(_P4_AUTHOR_SYSTEM, draft.text, llm, "author")
        return _validate_p4_spec(spec) if spec else None

    return None


# ===========================================================================
# 4. CANONICALIZE + PERSIST
# ===========================================================================

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_claim_text(text: str) -> str:
    """Normalize a claim to its canonical form for exact-hash dedup: lowercase,
    strip punctuation, collapse whitespace. Deliberately lossy — near-dups that
    survive this go through the embedding-similarity gate."""
    t = (text or "").lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def canonical_key(text: str) -> str:
    return hashlib.sha1(normalize_claim_text(text).encode("utf-8")).hexdigest()


def _primary_entity(draft: ClaimDraft) -> tuple:
    """Pick the strongest entity as the dedup + blast-radius anchor, using the
    same priority order as the mechanical falsifier derivation."""
    by_type: dict = {}
    for e in draft.entities:
        by_type.setdefault(e["entity_type"], []).append(e["entity"])
    for etype in entity_extract.FALSIFIER_PRIORITY:
        vals = by_type.get(etype)
        if vals:
            return (vals[0], etype)
    return (None, None)


def _embed_claim(text: str) -> Optional[bytes]:
    try:
        import embed
        return embed.embed_text(text)
    except Exception as exc:
        logger.debug("claim_layer: claim embed failed (fail-soft): %s", exc)
        return None


def _embedding_model_version() -> Optional[str]:
    """REV 4 item 4 — the identifier of the embedding model producing vectors,
    used to stamp claim_embeddings rows so a model swap is detectable. Single
    source of truth: embed.EMBEDDING_MODEL (do not invent a name)."""
    try:
        import embed
        return getattr(embed, "EMBEDDING_MODEL", None)
    except Exception:
        return None


def find_canonical_match(conn, draft: ClaimDraft, key: str,
                         primary_entity: Optional[str],
                         emb_bytes: Optional[bytes]) -> Optional[int]:
    """Return the id of an existing ACTIVE claim this draft should LINK to
    instead of inserting, or None. Two paths:

      1. Exact canonical-key + same primary entity (cheap, deterministic).
      2. Embedding cosine >= CANONICAL_DEDUP_THRESHOLD AND same primary entity
         (the near-dup 'Palantir move').
    """
    # 1. Exact key + entity.
    row = conn.execute(
        "SELECT id FROM claims WHERE canonical_key=? AND COALESCE(primary_entity,'')=? "
        "AND status='active' LIMIT 1",
        (key, primary_entity or ""),
    ).fetchone()
    if row:
        return row["id"]

    # 2. Embedding near-dup, gated on SAME primary entity (conservative link).
    if emb_bytes is None or primary_entity is None:
        return None
    try:
        import numpy as np
        import embed as _embed
        q = np.frombuffer(emb_bytes, dtype="float32")
        cand = conn.execute(
            "SELECT c.id, ce.embedding FROM claims c "
            "JOIN claim_embeddings ce ON ce.claim_id=c.id "
            "WHERE c.status='active' AND c.primary_entity=? AND ce.embedding IS NOT NULL",
            (primary_entity,),
        ).fetchall()
        best_id, best_sim = None, 0.0
        for r in cand:
            v = np.frombuffer(r["embedding"], dtype="float32")
            if v.shape[0] != q.shape[0]:
                continue
            sim = _embed.cosine_sim(q, v)
            if sim > best_sim:
                best_sim, best_id = sim, r["id"]
        if best_id is not None and best_sim >= CANONICAL_DEDUP_THRESHOLD:
            return best_id
    except Exception as exc:
        logger.debug("claim_layer: near-dup scan failed (fail-soft): %s", exc)
    return None


def _link_parent(conn, parent_kind: str, parent_id: int, claim_id: int,
                 role: str, now: str) -> None:
    tbl = "insight_claims" if parent_kind == "insight" else "principle_claims"
    col = "insight_id" if parent_kind == "insight" else "principle_id"
    conn.execute(
        f"INSERT OR IGNORE INTO {tbl} ({col}, claim_id, role, weight, created_at) "
        f"VALUES (?, ?, ?, 1.0, ?)",
        (parent_id, claim_id, role, now),
    )


def persist_claims(conn, parent_kind: str, parent_id: int, drafts: list,
                   source_file: Optional[str] = None, llm: Any = None) -> dict:
    """Classify + author + canonicalize + persist a list of ClaimDrafts for a
    parent (insight|principle). Returns {inserted, linked, claim_ids}.

    Idempotent: re-running for the same parent LINKS to the same canonical
    claims (INSERT OR IGNORE on the parent-link table) and does not duplicate
    claim rows. Fail-soft per draft — one bad draft never sinks the batch.
    NEVER raises to the caller (save already returned 200).
    """
    now = _utcnow_iso()
    inserted, linked, claim_ids = 0, 0, []

    for draft in drafts:
        try:
            pclass = classify_claim(draft, source_file=source_file)
            draft.predicate_class = pclass

            # P5 gets a review_after; everything else leaves it NULL.
            review_after = None
            if pclass == P5_AXIOMATIC:
                from datetime import datetime, timedelta, timezone
                review_after = (datetime.now(timezone.utc) + timedelta(days=P5_REVIEW_DAYS)).isoformat()
            draft.review_after = review_after

            # Author predicate (may re-raise TransientLLMError; caught here so
            # the save path never fails — the claim persists specless).
            try:
                spec = author_predicate(draft, pclass, llm=llm, source_file=source_file)
            except grounding_queue_v2.llm_client.TransientLLMError:
                spec = None
            draft.predicate_spec = spec

            key = canonical_key(draft.text)
            primary_entity, primary_entity_type = _primary_entity(draft)
            emb_bytes = _embed_claim(draft.text)

            existing_id = find_canonical_match(conn, draft, key, primary_entity, emb_bytes)
            if existing_id is not None:
                _link_parent(conn, parent_kind, parent_id, existing_id, draft.role, now)
                linked += 1
                claim_ids.append(existing_id)
                continue

            cur = conn.execute(
                """INSERT INTO claims
                       (canonical_key, text, predicate_class, predicate_spec,
                        predicate_version, status, primary_entity, primary_entity_type,
                        review_after, grounding_due, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, 'active', ?, ?, ?, 0, ?, ?)""",
                (key, draft.text, pclass,
                 json.dumps(spec) if spec is not None else None,
                 primary_entity, primary_entity_type, review_after, now, now),
            )
            claim_id = cur.lastrowid
            if emb_bytes is not None:
                # REV 4 item 4 — stamp the producing model version. The live DB
                # won't have migration 034's column until the operator applies
                # it, so fall back to the pre-034 INSERT on OperationalError
                # (missing column). Never lose the embedding over the stamp.
                _emv = _embedding_model_version()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO claim_embeddings "
                        "(claim_id, embedding, created_at, embedding_model_version) "
                        "VALUES (?, ?, ?, ?)",
                        (claim_id, emb_bytes, now, _emv),
                    )
                except sqlite3.OperationalError:
                    conn.execute(
                        "INSERT OR REPLACE INTO claim_embeddings (claim_id, embedding, created_at) "
                        "VALUES (?, ?, ?)",
                        (claim_id, emb_bytes, now),
                    )
            # claim_entities join rows (blast-radius spine).
            for e in draft.entities:
                conn.execute(
                    "INSERT OR IGNORE INTO claim_entities (claim_id, entity, entity_type) "
                    "VALUES (?, ?, ?)",
                    (claim_id, e["entity"], e["entity_type"]),
                )
            _link_parent(conn, parent_kind, parent_id, claim_id, draft.role, now)
            inserted += 1
            claim_ids.append(claim_id)
        except sqlite3.IntegrityError as exc:
            # canonical race: find_canonical_match (embedding-based, thresholded)
            # missed, but idx_claims_canonical_active enforces exact-key
            # uniqueness on active claims, so the INSERT collided with a claim
            # that already exists. Recover the LINK instead of dropping it —
            # otherwise the parent (insight/principle) is orphaned from a claim
            # that demonstrably exists. Backfill-2026-07-17: principle decompose
            # surfaced this; it silently dropped principle_claims links.
            try:
                row = conn.execute(
                    "SELECT id FROM claims WHERE canonical_key = ? "
                    "AND status = 'active' LIMIT 1", (key,),
                ).fetchone()
                if row is not None:
                    existing_id = row[0]
                    _link_parent(conn, parent_kind, parent_id, existing_id, draft.role, now)
                    linked += 1
                    claim_ids.append(existing_id)
                    continue
            except Exception as recov_exc:
                logger.warning("claim_layer: canonical-collision recovery failed: %s", recov_exc)
            logger.warning("claim_layer: persist_claims draft integrity-skip: %s", exc)
            continue
        except Exception as exc:
            logger.warning("claim_layer: persist_claims draft failed (skipped): %s", exc)
            continue

    try:
        conn.commit()
    except Exception:
        pass
    return {"inserted": inserted, "linked": linked, "claim_ids": claim_ids}


def process_insight_claims(conn, insight_id: int, content: str,
                           source_file: Optional[str] = None, llm: Any = None) -> dict:
    """Top-level entry called from the daemon's async post-save path AFTER
    entity extraction. Decompose -> persist. Fail-soft; never raises.

    Idempotency guard (round 2, pilot finding 2026-07-17): two concurrent
    workers (backgrounded pilot + its orphaned foreground twin) both saw the
    same insight as un-decomposed and double-decomposed 23/50 insights —
    paraphrased LLM re-runs dodge canonical dedup, so the guard must be here,
    re-checked at process time, not only in the caller's upfront candidate
    list."""
    try:
        already = conn.execute(
            "SELECT COUNT(*) FROM insight_claims WHERE insight_id = ?",
            (insight_id,),
        ).fetchone()[0]
        if already:
            logger.info(
                "claim_layer: insight #%s already has %s claims — skipping re-decompose",
                insight_id, already,
            )
            return {"inserted": 0, "linked": 0, "claim_ids": [], "skipped": "already_decomposed"}
        drafts = decompose_insight(content, llm=llm)
        if not drafts:
            return {"inserted": 0, "linked": 0, "claim_ids": []}
        return persist_claims(conn, "insight", insight_id, drafts,
                              source_file=source_file, llm=llm)
    except Exception as exc:
        logger.warning("claim_layer: process_insight_claims failed for #%s: %s", insight_id, exc)
        return {"inserted": 0, "linked": 0, "claim_ids": []}


# ===========================================================================
# 5. ROLLUP — insight/principle liveness = worst(core claims) + fresh_fraction
# ===========================================================================

# Verdict ordering, worst -> best. worst-of-core picks the minimum.
_VERDICT_RANK = {"stale": 0, "revalidating": 1, "unverified": 2, "aging": 3, "fresh": 4}
# P5 axiomatic claims are terminal — they never count as stale in the rollup.
_AXIOMATIC_VERDICT = "axiomatic"


def _claim_verdict(claim_row: dict) -> str:
    """Derive a single claim's liveness verdict from its columns.
    P5 -> 'axiomatic' (terminal). Else map grounding_due/last_verdict/grounded_at."""
    if claim_row.get("predicate_class") == P5_AXIOMATIC:
        return _AXIOMATIC_VERDICT
    last = claim_row.get("last_verdict")
    if claim_row.get("grounding_due"):
        return "stale" if last == "fail" else "revalidating"
    if last == "pass" and claim_row.get("grounded_at"):
        return "fresh"
    if last == "fail":
        return "stale"
    return "unverified"


def claim_rollup(conn, parent_kind: str, parent_id: int) -> dict:
    """Roll a parent's claims up into a single liveness summary.

    Returns {verdict, fresh_fraction, claims_summary:{total,fresh,stale,axiomatic,
    unverified}}. `verdict` = worst of the CORE claims (non-core claims inform
    the fraction but do not gate the verdict), with P5 axiomatic claims excluded
    from worst-of (they are terminal, never stale). If a parent has no claims
    yet, verdict='unverified'.
    """
    tbl = "insight_claims" if parent_kind == "insight" else "principle_claims"
    col = "insight_id" if parent_kind == "insight" else "principle_id"
    rows = conn.execute(
        f"""SELECT c.predicate_class, c.grounding_due, c.grounded_at, c.last_verdict,
                   pc.role
            FROM {tbl} pc JOIN claims c ON c.id=pc.claim_id
            WHERE pc.{col}=? AND c.status='active'""",
        (parent_id,),
    ).fetchall()

    summary = {"total": 0, "fresh": 0, "stale": 0, "axiomatic": 0,
               "unverified": 0, "revalidating": 0, "aging": 0}
    core_verdicts: list = []
    fresh_or_axiom = 0

    for r in rows:
        rd = dict(r)
        v = _claim_verdict(rd)
        summary["total"] += 1
        summary[v] = summary.get(v, 0) + 1
        if v in ("fresh", _AXIOMATIC_VERDICT):
            fresh_or_axiom += 1
        if rd.get("role") == "core" and v != _AXIOMATIC_VERDICT:
            core_verdicts.append(v)

    if summary["total"] == 0:
        return {"verdict": "unverified", "fresh_fraction": 0.0, "claims_summary": summary}

    if core_verdicts:
        worst = min(core_verdicts, key=lambda v: _VERDICT_RANK.get(v, 2))
    else:
        # No non-axiomatic core claims: all-axiomatic parent is 'fresh' by
        # terminality; else fall back to the best available signal.
        worst = "fresh" if summary["total"] == summary["axiomatic"] else "unverified"

    fresh_fraction = round(fresh_or_axiom / summary["total"], 3)
    return {"verdict": worst, "fresh_fraction": fresh_fraction, "claims_summary": summary}


# ===========================================================================
# 6. MODEL ROLES — per-role model within the single configured provider.
#    Routing isolation is enforced in code (assert_no_interactive_proxy).
# ===========================================================================

# Interactive proxy ports the background grounding roles must NEVER route via.
_INTERACTIVE_PROXY_PORTS = (":8788", ":8787")

_ROLE_KEYS = ("decompose", "classify", "author", "verdict", "adjudicate")


def get_role_model(role: str) -> str:
    """Return the model name for a named role from stack.toml [models], falling
    back to the grounding primary model. Reads via grounding_config's raw TOML
    accessor so a missing [models] section degrades gracefully."""
    import grounding_config
    cfg = grounding_config.get_config()
    doc = grounding_config._load_toml()
    models = grounding_config._section(doc, "models")
    val = models.get(role) if isinstance(models, dict) else None
    if val:
        return str(val)
    # adjudicate/verdict default to escalation model; others to the primary.
    if role in ("adjudicate", "verdict"):
        return cfg.escalation_model or cfg.model
    return cfg.model


def get_role_provider() -> str:
    """The single active provider for all roles (model doctrine: provider-uniform)."""
    import grounding_config
    return grounding_config.get_config().provider


def assert_no_interactive_proxy(base_url: str, role: str = "") -> None:
    """Routing-isolation guard: background roles must never route through the
    interactive session's local proxy path (:8788 / :8787).
    Raises RuntimeError if the configured base_url points at either. Callers
    that build a grounding role client MUST call this first.

    EXCEPTION: anthropic-oauth's DEFAULT deployment historically routes grounding
    through :8788 (it has no metered key). The doctrine says the *v3 background
    roles* should use a direct api.anthropic.com base_url for oauth. This guard
    is enforced for the v3 claim-layer role clients specifically; it is not
    retroactively applied to the v2 author/adjudicate path (kept one release).
    """
    if not base_url:
        return
    for port in _INTERACTIVE_PROXY_PORTS:
        if port in base_url:
            raise RuntimeError(
                f"claim_layer: grounding role {role!r} base_url {base_url!r} routes "
                f"through the interactive proxy {port} — background roles must use a "
                f"direct lane (model doctrine: isolation is routing). "
                f"Set stack.toml [models].base_url to api.anthropic.com (oauth) or a "
                f"dedicated gateway."
            )


def get_role_base_url() -> str:
    """The base_url for background grounding role clients. Prefers stack.toml
    [models].base_url; for anthropic-oauth defaults to the DIRECT Anthropic API
    (never the :8788 proxy), per the model doctrine. Validated by
    assert_no_interactive_proxy before use."""
    import grounding_config
    cfg = grounding_config.get_config()
    doc = grounding_config._load_toml()
    models = grounding_config._section(doc, "models")
    explicit = models.get("base_url") if isinstance(models, dict) else None
    if explicit:
        return str(explicit)
    if cfg.provider == "anthropic-oauth":
        # Direct API — SDK default base_url. Return empty string sentinel so the
        # client factory omits base_url and the SDK uses api.anthropic.com.
        return ""
    return cfg.base_url


def get_role_client(role: str) -> Any:
    """Return an LLM client for a background grounding role, with routing
    isolation enforced. For anthropic-oauth this builds a DIRECT client
    (api.anthropic.com + oauth token), never the :8788 interactive proxy.

    Returns None (fail-open) if credentials/SDK are unavailable, same contract
    as llm_client.get_client(). Raises RuntimeError only if a misconfigured
    base_url would leak background traffic onto the interactive lane — that is a
    config bug the operator must fix, not a fail-open case.
    """
    base_url = get_role_base_url()
    assert_no_interactive_proxy(base_url, role)

    import grounding_config
    import llm_client
    cfg = grounding_config.get_config()

    if cfg.provider == "anthropic-oauth":
        # Build a direct-API oauth client (no proxy base_url).
        if not llm_client._HAVE_ANTHROPIC:
            return None
        token = llm_client._read_oauth_token()
        if not token:
            return None
        try:
            if base_url:
                return llm_client._anthropic_mod.Anthropic(auth_token=token, base_url=base_url)
            return llm_client._anthropic_mod.Anthropic(auth_token=token)
        except Exception as exc:
            logger.warning("claim_layer: role client init failed: %s", exc)
            return None

    # Other providers: reuse the shared factory (metered key / local endpoint —
    # not the interactive proxy by construction).
    return llm_client.get_client()
