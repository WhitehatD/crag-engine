# coding: utf-8
"""Grounding v3 — claim-level contradiction detection.

The v2 detector ran at INSIGHT topicality and had a ~75% FP rate: topically
adjacent insights (same subsystem, different concern) flagged as contradictions.
v3 moves detection to the CLAIM level: two ATOMIC assertions about the SAME
canonical subject (same primary entity) that DISAGREE — negation flip or
value mismatch — with embedding antipodality as a tiebreaker.

Structural precision: comparing assertion-vs-assertion on the same subject means
"different concern about the same service" no longer flags — the claims have to
be ABOUT THE SAME THING and SAY OPPOSITE THINGS.

Enabled behind grounding_config.get_claims_config()['claim_contradiction_enabled']
(default False). The old insight-level detector stays live one release for
rollback. Writes to claim_contradictions (migration 031).

Fail-open: any error -> no flag (same doctrine as the v2 detector).
House style: pure-ish; takes an open sqlite3.Connection.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crag-anchor")

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from lifecycle import _utcnow_iso  # noqa: E402

# Negation markers whose presence/absence flipping between two same-subject
# claims signals disagreement.
_NEG = re.compile(
    r"\b(not|never|no|isn'?t|aren'?t|doesn'?t|don'?t|can'?t|won'?t|"
    r"without|disabled|removed|deprecated|broken|failed|wrong|incorrect|false)\b",
    re.I,
)

# Antonym-ish pairs (one side present in A, the other in B) that mark a value flip.
_ANTONYM_PAIRS = [
    ("enabled", "disabled"), ("on", "off"), ("up", "down"),
    ("present", "absent"), ("works", "broken"), ("pass", "fail"),
    ("true", "false"), ("allowed", "forbidden"), ("added", "removed"),
    ("open", "closed"), ("active", "inactive"), ("live", "dead"),
]

# Number/version tokens — a mismatch on the SAME entity subject is a value flip.
_NUM_RE = re.compile(r"\b(\d+(?:\.\d+)+|\d{2,5})\b")


def _negation_parity(a: str, b: str) -> bool:
    """True if exactly one of the two texts carries a negation (parity flip)."""
    return bool(_NEG.search(a)) != bool(_NEG.search(b))


def _antonym_flip(a: str, b: str) -> Optional[str]:
    la, lb = a.lower(), b.lower()
    for x, y in _ANTONYM_PAIRS:
        if (x in la and y in lb) or (y in la and x in lb):
            return f"{x}/{y}"
    return None


def _value_mismatch(a: str, b: str) -> Optional[str]:
    """Same-subject numeric/version disagreement: both mention numbers, and the
    number sets are disjoint (e.g. 'port 8786' vs 'port 8788')."""
    na = set(_NUM_RE.findall(a))
    nb = set(_NUM_RE.findall(b))
    if na and nb and na.isdisjoint(nb):
        # Only meaningful when the surrounding phrasing is parallel — cheap
        # guard: share >=2 non-number word tokens.
        wa = set(re.findall(r"[a-z]{3,}", a.lower()))
        wb = set(re.findall(r"[a-z]{3,}", b.lower()))
        if len(wa & wb) >= 2:
            return f"value:{sorted(na)}!={sorted(nb)}"
    return None


def _embedding_antipodal(conn, id_a: int, id_b: int, cosine_floor: float) -> Optional[float]:
    """Return cosine similarity if both claims are embedded, else None. High
    similarity + a negation/antonym flip is the strongest contradiction signal
    (they talk about the same thing in near-identical words but flip polarity)."""
    try:
        import numpy as np
        import embed
        ra = conn.execute("SELECT embedding FROM claim_embeddings WHERE claim_id=?", (id_a,)).fetchone()
        rb = conn.execute("SELECT embedding FROM claim_embeddings WHERE claim_id=?", (id_b,)).fetchone()
        if not ra or not rb or not ra[0] or not rb[0]:
            return None
        va = np.frombuffer(ra[0], dtype="float32")
        vb = np.frombuffer(rb[0], dtype="float32")
        if va.shape != vb.shape:
            return None
        return embed.cosine_sim(va, vb)
    except Exception:
        return None


def detect_for_claim(conn, claim_id: int) -> list:
    """Detect claim-level contradictions for a newly-persisted claim. Compares
    against ACTIVE claims sharing the SAME primary entity. Records open rows in
    claim_contradictions and returns the list of flagged pairs. Fail-open."""
    cfg = None
    try:
        import grounding_config
        cfg = grounding_config.get_claims_config()
    except Exception:
        cfg = {}
    if not cfg.get("claim_contradiction_enabled", False):
        return []
    cosine_floor = float(cfg.get("contradiction_cosine", 0.80))

    try:
        me = conn.execute(
            "SELECT id, text, primary_entity FROM claims WHERE id=? AND status='active'",
            (claim_id,),
        ).fetchone()
        if not me or not me["primary_entity"]:
            return []
        peers = conn.execute(
            "SELECT id, text FROM claims WHERE primary_entity=? AND status='active' AND id<>?",
            (me["primary_entity"], claim_id),
        ).fetchall()
    except Exception as exc:
        logger.debug("claim_contradiction: peer fetch failed: %s", exc)
        return []

    flagged = []
    now = _utcnow_iso()
    for peer in peers:
        a, b = me["text"], peer["text"]
        reason = None
        neg = _negation_parity(a, b)
        ant = _antonym_flip(a, b)
        val = _value_mismatch(a, b)

        # A polarity flip (negation parity or antonym) needs semantic proximity
        # to count — antipodality confirms they're about the same assertion.
        if neg or ant:
            sim = _embedding_antipodal(conn, me["id"], peer["id"], cosine_floor)
            if sim is None or sim >= cosine_floor:
                reason = f"polarity-flip ({'neg' if neg else ant}); cosine={sim}"
        elif val:
            reason = val  # value mismatch is self-evidencing on same entity

        if not reason:
            continue
        lo, hi = sorted((me["id"], peer["id"]))
        try:
            conn.execute(
                "INSERT OR IGNORE INTO claim_contradictions "
                "(claim_a_id, claim_b_id, reason, score, status, detected_at) "
                "VALUES (?, ?, ?, ?, 'open', ?)",
                (lo, hi, reason, 1.0, now),
            )
            flagged.append({"a": lo, "b": hi, "reason": reason})
        except Exception:
            pass
    if flagged:
        try:
            conn.commit()
        except Exception:
            pass
    return flagged
