# coding: utf-8
"""Phase 9 -- Contradiction detection via Haiku entailment.

Called from _do_save_insight + _do_distill after the row is inserted and
embedded.  Finds top-3 cosine-similar existing rows (same project, active)
above threshold 0.70.  For each, asks Haiku via the local proxy stack: 'does
NEW contradict OLD?'  Returns a 0..1 score.  Scores >= 0.90 flag the OLDER row
as suspect and record a contradiction_events row.

WS2 T4 raised both defaults (cosine 0.55→0.70, entail 0.70→0.90) to cut the
~75% false-positive rate at SOURCE: the FP-sweep cleared 0 in 7 days while the
queue grew 16→25, proving precision has to move upstream of the detector, not
downstream of it. Both remain env-overridable (CRAG_ANCHOR_CONTRA_COSINE /
CRAG_ANCHOR_CONTRA_ENTAIL).

Authentication architecture
---------------------------
The crag Anchor daemon does NOT use ANTHROPIC_API_KEY directly. Claude Code (claudex)
authenticates via OAuth against Claude.ai consumer accounts (token cached at
`~/.claude/.credentials.json` under `.claudeAiOauth.accessToken`). Every
claudex API call goes through the local proxy chain:
  client -> model-router (:8788) -> Headroom (:8787) -> api.anthropic.com
Headroom carries the OAuth-bearer auth and Anthropic accepts it via the
`anthropic-beta: oauth-2025-04-20` header (handled by the Anthropic SDK when
`auth_token=` is passed instead of `api_key=`).

This module mirrors that chain: it reads the OAuth access token from
`~/.claude/.credentials.json` and points the Anthropic SDK at
`http://localhost:8788` (the model-router). That way:
  - No separate API key required (no extra cost-source, no rotation work).
  - Calls inherit the same compression + caching the user's claudex traffic gets.
  - If claudex re-auths, the crag Anchor daemon picks up the new token on next call.

Conservative defaults (WS2 T4 — precision-at-source; raised to cut FP rate)
---------------------------------------------------------------------------
  COSINE_THRESHOLD = 0.70  -- lower => more Haiku calls / higher cost;
                               higher => misses real contradictions
                               (was 0.55; raised in WS2 to suppress topically-
                               adjacent non-contradictions before they flag)
  ENTAIL_THRESHOLD = 0.90  -- lower => false positives; higher => misses
                               (was 0.70; raised in WS2 — only near-certain
                               entailment reversals flag)
  MAX_NEIGHBORS    = 3     -- cap cost to ~3 Haiku calls per save
  Env overrides preserved: CRAG_ANCHOR_CONTRA_COSINE / CRAG_ANCHOR_CONTRA_ENTAIL.

Cost cap: 3 calls × ~50 tokens = ~150 tokens / save. Routed through Headroom
so cache hits + compression reduce real cost further.

Fail-open: if the OAuth token file is missing, expired, the proxy chain is
unreachable, or the Haiku call errors, the insight is saved normally; no
flagging occurs. A WARNING is logged.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Optional

import numpy as np

# Shared LLM client (Grounding v2 extraction — contradiction.py delegates here).
# _get_client() is kept as a thin alias so call-sites in this file remain unchanged.
from llm_client import get_client as _get_client  # noqa: F401  (used below)
from llm_client import GROUNDING_MODEL as HAIKU_MODEL  # re-export name callers expect

logger = logging.getLogger("crag-anchor")

# ---------------------------------------------------------------------------
# Tunable thresholds
# ---------------------------------------------------------------------------

# WS2 T4 — raised from 0.55/0.70 to cut FPs at source (env overrides preserved).
# WS2 T6 — sourced from db/scoring.py (single source of truth; scoring reads the
# same CRAG_ANCHOR_CONTRA_COSINE / CRAG_ANCHOR_CONTRA_ENTAIL env vars, so overrides still work).
try:
    from scoring import CONTRA_COSINE_THRESHOLD as COSINE_THRESHOLD  # noqa: F401
    from scoring import CONTRA_ENTAIL_THRESHOLD as ENTAIL_THRESHOLD  # noqa: F401
except ImportError:  # standalone use without db/ on sys.path — keep local fallback
    COSINE_THRESHOLD: float = float(os.environ.get("CRAG_ANCHOR_CONTRA_COSINE", "0.70"))
    ENTAIL_THRESHOLD: float = float(os.environ.get("CRAG_ANCHOR_CONTRA_ENTAIL", "0.90"))
MAX_NEIGHBORS: int = int(os.environ.get("CRAG_ANCHOR_CONTRA_NEIGHBORS", "3"))

# Phase 16-C — multi-stage FP filters (additive; set to 0/false to disable per filter).
# Default-on because every observed FP this audit (~75% of 33 flagged) would have been
# caught by one of these filters before the expensive Haiku call.
SKIP_PROVENANCE: bool = os.environ.get("CRAG_ANCHOR_CONTRA_SKIP_PROVENANCE", "1") == "1"
TEMPORAL_COHORT_SECONDS: int = int(
    os.environ.get("CRAG_ANCHOR_CONTRA_TEMPORAL_COHORT_SECONDS", "7200")  # 2 hours
)
SKIP_SAME_SOURCE_FILE: bool = os.environ.get("CRAG_ANCHOR_CONTRA_SKIP_SAME_SOURCE", "0") == "1"

HAIKU_MAX_TOKENS: int = 30  # binary classification: just a 0-100 integer


# ---------------------------------------------------------------------------
# Haiku entailment call
# ---------------------------------------------------------------------------

def _ask_haiku_entailment(new_text: str, old_text: str) -> tuple[float, str]:
    """Return (contradiction_score, raw_response).

    Score is 0.0..1.0 where 1.0 = definite contradiction.
    On any failure, returns (0.0, 'error: ...') so the caller can fail-open.
    """
    client = _get_client()
    if client is None:
        return (0.0, "no-client")

    prompt = (
        "Two engineering knowledge entries. Does the NEW entry CONTRADICT the OLD entry? "
        "Answer with a single integer 0-100: 0 means no contradiction, "
        "100 means direct contradiction. Just the number.\n\n"
        f"OLD: {old_text[:600]}\n\nNEW: {new_text[:600]}\n\nScore:"
    )
    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=HAIKU_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip() if resp.content else "0"
        # Extract the first contiguous run of digits
        digits_parts = "".join(ch if ch.isdigit() else " " for ch in raw).split()
        first = digits_parts[0] if digits_parts else "0"
        score = max(0, min(100, int(first))) / 100.0
        return (score, raw[:300])
    except Exception as exc:
        logger.warning("Phase 9: Haiku entailment call failed: %s", exc)
        return (0.0, f"error: {exc}")


# ---------------------------------------------------------------------------
# Phase 16-C — Pre-Haiku filters (multi-stage FP suppression)
# ---------------------------------------------------------------------------

def _in_provenance_chain(
    conn: sqlite3.Connection,
    new_kind: str,
    new_id: int,
    old_kind: str,
    old_id: int,
) -> bool:
    """True if NEW and OLD are linked via distill/promote provenance.

    Catches the false-positive class where a comprehensive consolidating insight
    gets flagged against the source insights it was distilled from. Examples
    observed in 2026-05-27 audit: 33 flagged → 25 were complementary (newer
    comprehensive insight + older fragments referenced via source_insights).

    Checks both directions: A in B's source_insights, OR B in A's source_insights.
    Also checks promoted_to chains (insight promoted to principle).
    """
    try:
        # Principle-side: source_insights is a comma-separated list of insight IDs.
        # Check if either side cites the other via promotion/distillation chain.
        if old_kind == "principle":
            row = conn.execute(
                "SELECT source_insights FROM principles WHERE id = ?", (old_id,)
            ).fetchone()
            if row and row["source_insights"]:
                ids = {s.strip() for s in str(row["source_insights"]).split(",")}
                if new_kind == "insight" and str(new_id) in ids:
                    return True

        if new_kind == "principle":
            row = conn.execute(
                "SELECT source_insights FROM principles WHERE id = ?", (new_id,)
            ).fetchone()
            if row and row["source_insights"]:
                ids = {s.strip() for s in str(row["source_insights"]).split(",")}
                if old_kind == "insight" and str(old_id) in ids:
                    return True

        # Insight-side: promoted_to points to a principle. If A.promoted_to ==
        # B.id OR vice versa, they're provenance-linked.
        if new_kind == "insight" and old_kind == "principle":
            row = conn.execute(
                "SELECT promoted_to FROM insights WHERE id = ?", (new_id,)
            ).fetchone()
            if row and row["promoted_to"] == old_id:
                return True
        if old_kind == "insight" and new_kind == "principle":
            row = conn.execute(
                "SELECT promoted_to FROM insights WHERE id = ?", (old_id,)
            ).fetchone()
            if row and row["promoted_to"] == new_id:
                return True
    except Exception as exc:
        # Schema may be older — fail open (don't skip, let Haiku decide)
        logger.debug("Phase 16-C provenance check failed (fail-open): %s", exc)
        return False

    return False


def _within_temporal_cohort(
    conn: sqlite3.Connection,
    new_kind: str,
    new_id: int,
    old_kind: str,
    old_id: int,
    max_seconds: int,
) -> bool:
    """True if both rows were created within `max_seconds` of each other.

    Catches the false-positive class where a refining-update sequence within a
    single session gets flagged as contradiction. Examples observed: a session
    discovers a bug, files a "BUG: X" insight, fixes it, files a "FIX: X
    implemented" insight 28 minutes later. These are TRUE complementary, not
    contradicting — but lexical overlap fools the cos+entail check.

    NOTE: this filter is bypassed for the case where a newer insight explicitly
    claims to "supersede" an older one (caller should arena/supersede manually).
    """
    if max_seconds <= 0:
        return False
    try:
        new_tbl = "insights" if new_kind == "insight" else "principles"
        old_tbl = "insights" if old_kind == "insight" else "principles"
        new_row = conn.execute(
            f"SELECT created_at FROM {new_tbl} WHERE id = ?", (new_id,)
        ).fetchone()
        old_row = conn.execute(
            f"SELECT created_at FROM {old_tbl} WHERE id = ?", (old_id,)
        ).fetchone()
        if not new_row or not old_row:
            return False
        # created_at is TEXT in one of two historical formats: legacy SQLite
        # 'YYYY-MM-DD HH:MM:SS' (space, naive-UTC) or the canonical offset-aware
        # ISO-T ('...T...+00:00', microseconds optional). fromisoformat handles
        # both once space→T and Z→+00:00 are normalized; naive values are
        # assumed UTC. The old strptime pair silently returned None on
        # offset/microsecond ISO values → the cohort check FAILED OPEN and this
        # FP suppression stopped working (2026-07-02 audit finding).
        from datetime import datetime as _dt, timezone as _tz
        def _parse(ts: str) -> Optional[float]:
            try:
                dt = _dt.fromisoformat(str(ts).replace(" ", "T").replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt.timestamp()
        t_new = _parse(new_row["created_at"])
        t_old = _parse(old_row["created_at"])
        if t_new is None or t_old is None:
            return False
        return abs(t_new - t_old) <= max_seconds
    except Exception as exc:
        logger.debug("Phase 16-C temporal-cohort check failed (fail-open): %s", exc)
        return False


def _same_source_file(
    conn: sqlite3.Connection,
    new_kind: str,
    new_id: int,
    old_kind: str,
    old_id: int,
) -> bool:
    """True if both rows reference the same source_file.

    Conservative filter: same source_file usually means complementary aspects
    of one component, not contradiction. OFF by default (set
    CRAG_ANCHOR_CONTRA_SKIP_SAME_SOURCE=1 to enable) because legitimate contradictions
    can still occur within one file (e.g., a refactor that inverts a claim).
    """
    try:
        new_tbl = "insights" if new_kind == "insight" else "principles"
        old_tbl = "insights" if old_kind == "insight" else "principles"
        # principles table may not have source_file
        try:
            new_row = conn.execute(
                f"SELECT source_file FROM {new_tbl} WHERE id = ?", (new_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        try:
            old_row = conn.execute(
                f"SELECT source_file FROM {old_tbl} WHERE id = ?", (old_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        if not new_row or not old_row:
            return False
        new_sf = (new_row["source_file"] or "").strip()
        old_sf = (old_row["source_file"] or "").strip()
        return bool(new_sf) and new_sf == old_sf
    except Exception as exc:
        logger.debug("Phase 16-C same-source-file check failed (fail-open): %s", exc)
        return False


def _should_skip_pair(
    conn: sqlite3.Connection,
    new_kind: str,
    new_id: int,
    old_kind: str,
    old_id: int,
) -> Optional[str]:
    """Run all Phase 16-C filters. Returns the skip reason if any matches, else None.

    Order matters: cheapest checks first. All filters are fail-open (return
    False on error) so the detector still runs if a filter errors.
    """
    if SKIP_PROVENANCE and _in_provenance_chain(conn, new_kind, new_id, old_kind, old_id):
        return "provenance"
    if TEMPORAL_COHORT_SECONDS > 0 and _within_temporal_cohort(
        conn, new_kind, new_id, old_kind, old_id, TEMPORAL_COHORT_SECONDS
    ):
        return "temporal-cohort"
    if SKIP_SAME_SOURCE_FILE and _same_source_file(conn, new_kind, new_id, old_kind, old_id):
        return "same-source-file"
    return None


# ---------------------------------------------------------------------------
# Main detection entry point
# ---------------------------------------------------------------------------

def detect_and_flag(
    conn: sqlite3.Connection,
    new_kind: str,          # 'insight' or 'principle'
    new_id: int,
    new_content: str,
    new_embedding: bytes,
    project: Optional[str],
) -> list[dict]:
    """Cosine-filter neighbours, run Haiku entailment, flag contradictions.

    Returns a list of dicts describing each flag applied (empty if none).
    Never raises — all failures degrade gracefully.
    """
    if not new_embedding:
        return []

    try:
        new_vec = np.frombuffer(new_embedding, dtype="float32")
    except Exception as exc:
        logger.warning("Phase 9: could not parse new_embedding: %s", exc)
        return []

    flagged: list[dict] = []

    for kind, table in [("insight", "insights"), ("principle", "principles")]:
        try:
            params: list = []
            if project:
                proj_clause = "(project = ? OR project IS NULL)"
                params.append(project)
            else:
                proj_clause = "1=1"

            # Active insights only; principles have no status column
            status_clause = "AND status='active'" if kind == "insight" else ""

            # Exclude self if scanning the same table as the new row
            exclude_clause = ""
            if kind == new_kind:
                exclude_clause = " AND id != ?"
                params.append(new_id)

            sql = (
                f"SELECT id, content, embedding FROM {table} "
                f"WHERE embedding IS NOT NULL AND {proj_clause} {status_clause}{exclude_clause}"
            )
            rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            logger.warning("Phase 9: contradiction scan query failed for %s: %s", table, exc)
            continue

        if not rows:
            continue

        # Build matrix and compute cosine similarities in one shot
        try:
            ids = [r["id"] for r in rows]
            contents = [r["content"] for r in rows]
            matrix = np.vstack(
                [np.frombuffer(r["embedding"], dtype="float32") for r in rows]
            )
        except Exception as exc:
            logger.warning("Phase 9: embedding matrix build failed for %s: %s", table, exc)
            continue

        sims = matrix @ new_vec  # shape: (N,)
        # Sort by descending similarity, keep top-MAX_NEIGHBORS above threshold
        indexed = sorted(
            enumerate(sims.tolist()), key=lambda x: -x[1]
        )
        top = [
            (ids[i], contents[i], s)
            for i, s in indexed[:MAX_NEIGHBORS]
            if s >= COSINE_THRESHOLD
        ]

        for old_id, old_content, cos_sim in top:
            # Phase 16-C — pre-Haiku FP filters. Skip pairs that match known
            # false-positive patterns (provenance chains, temporal cohorts).
            # This both reduces Haiku cost AND eliminates the FP-noise that
            # was polluting `audit_contradictions` (75% FP rate measured
            # 2026-05-27 — see insight #2424).
            skip_reason = _should_skip_pair(conn, new_kind, new_id, kind, old_id)
            if skip_reason:
                logger.info(
                    "Phase 16-C: skipped %s:%d ↔ %s:%d (cos=%.2f, reason=%s)",
                    new_kind, new_id, kind, old_id, cos_sim, skip_reason,
                )
                continue

            entail_score, raw_response = _ask_haiku_entailment(new_content, old_content)

            if entail_score < ENTAIL_THRESHOLD:
                continue  # not a contradiction — move on

            # Write the audit event FIRST. contradiction_events is the canonical
            # record — it has no FK back to insights/principles, so it accepts
            # any cross-kind reference (insight↔principle, principle↔insight).
            # Recall annotation falls back to this table when suspect_of is NULL.
            reason = (
                f"contradicted by {new_kind}:{new_id} "
                f"(cos={cos_sim:.2f}, entail={entail_score:.2f})"
            )
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO contradiction_events "
                    "(new_kind, new_id, old_kind, old_id, cosine_sim, entail_score, haiku_response) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (new_kind, new_id, kind, old_id, cos_sim, entail_score, raw_response),
                )
                conn.commit()
            except Exception as exc:
                logger.warning(
                    "Phase 9: contradiction_events write failed for %s:%d -> %s:%d: %s",
                    new_kind, new_id, kind, old_id, exc,
                )
                # Audit failed — skip the suspect_of denorm too and move on
                continue

            # Best-effort suspect_of denormalization (quick recall annotation).
            # SAME-KIND only — cross-kind hits the FK constraint declared in
            # migration 009 (insights.suspect_of REFERENCES insights(id),
            # principles.suspect_of REFERENCES principles(id)). For cross-kind
            # the recall code joins contradiction_events instead. This avoids
            # a heavy table-rebuild migration just to drop the FK.
            if kind == new_kind:
                try:
                    # Canonical offset-aware ISO-T — never SQLite datetime('now')
                    # (space-format, mis-sorts against ISO-T in lexical
                    # comparisons/orderings; same trap as the 2026-07-02
                    # supersede-burst watermark bug).
                    from datetime import datetime as _dtn, timezone as _tzn
                    conn.execute(
                        f"UPDATE {table} "
                        "SET suspect_of = ?, suspect_reason = ?, suspect_score = ?, "
                        "suspect_detected_at = ? "
                        "WHERE id = ?",
                        (new_id, reason, entail_score,
                         _dtn.now(_tzn.utc).isoformat(), old_id),
                    )
                    conn.commit()
                except Exception as exc:
                    logger.warning(
                        "Phase 9: suspect_of denorm failed for %s:%d (audit preserved): %s",
                        table, old_id, exc,
                    )

            logger.info(
                "Phase 9: flagged %s:%d as suspect of %s:%d "
                "(cos=%.2f entail=%.2f)",
                kind, old_id, new_kind, new_id, cos_sim, entail_score,
            )
            flagged.append({
                "old_kind": kind,
                "old_id": old_id,
                "cosine": round(cos_sim, 3),
                "entail": round(entail_score, 3),
            })

    return flagged
