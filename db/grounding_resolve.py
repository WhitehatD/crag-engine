# coding: utf-8
"""Grounding v2 -- autonomous resolution policy (migration 028 extension).

Closes the grounding loop: v2 (grounding_queue_v2.py + grounding_author.py)
authors falsifiers and adjudicates verdicts, but a verdict alone is detection,
not resolution -- something still has to decide what to DO about a pass/fail/
uncertain result. Before this module every flagged claim just sat in
grounding_queue waiting for a human (449 live, 2026-07-05). This module is the
graded policy that lets the daemon resolve the easy/safe cases itself while
routing everything else to a reviewable proposal instead of the raw queue.

Three responsibilities:

  1. Stale-falsifier detection (`falsifier_is_stale`) -- a flagged claim whose
     stored falsifier is a v1 existence-probe force-fit onto predicate-bearing
     content (or missing, or kind='none') gets re-authored via LLM instead of
     being probed with a falsifier that proves nothing.

  2. Judgment marking (`mark_judgment`) -- when grounding_author.author_recipe
     declares a claim mechanically_unverifiable, this persists that finding
     (falsifiers.tier='judgment' + insights/principles.grounding_mode='judgment')
     and auto-clears the flag. The claim STOPS being re-flagged for mechanical
     grounding -- doctrine still holds (nothing is destroyed, only the
     grounding classification changes).

  3. Graded resolution (`resolve_claim`) -- reads the latest reground verdict
     and decides one of:
       pass                              -> auto-verify + clear flag
       fail, insight, low-stakes, LLM
         drafts a confident correction    -> auto-update content + clear flag
       everything else (uncertain,
         principle, high-stakes,
         fail-without-confident-correction) -> resolution_proposals row,
                                               NO mutation, flag left open
                                               (dashboard-visible, non-blocking)
     Every path appends a job_type='resolve' grounding_history row -- full
     audit trail, nothing silent.

House style: pure-ish functions take an open sqlite3.Connection (never open,
close, or hold a transaction open across a function boundary the caller
doesn't already own). Timestamps: _utcnow_iso() only, never datetime('now').
sqlite3.Row: bracket access only, never .get().
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("crag-anchor")

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from lifecycle import _utcnow_iso  # noqa: E402
import scoring  # noqa: E402
from grounding_author import _redact_credential_shapes  # noqa: E402


def _tbl(claim_kind: str) -> str:
    return "insights" if claim_kind == "insight" else "principles"


# ---------------------------------------------------------------------------
# High-stakes heuristic
# ---------------------------------------------------------------------------

# Content matching any of these is NEVER auto-mutated, regardless of verdict --
# only escalated to a resolution_proposals row for human review. Deliberately
# broad (false positives just mean "ask a human", which is the safe direction).
#
# This regex list is a MECHANICAL BACKSTOP, not the primary gate -- an adversarial
# review (2026-07-05) found 10/10 dangerous-prose slips (destructive intent phrased
# without any of the original keywords, e.g. "wiping the boot disk") plus mechanical
# bugs where the pattern itself only matched a narrow inflection (plural "secrets"/
# "tokens"/"passwords"/"api keys" slipped past the singular-only patterns; the gerund
# "force-pushing" slipped past `\bforce.?push\b`). Both classes are fixed below, but
# the REAL fix is `_classify_stakes_llm` in resolve_claim's fail-path: an LLM second
# gate that catches prose no keyword list can enumerate. See that function's
# docstring for the two-gate policy this backstop feeds into.
_HIGH_STAKES_PATTERNS = [
    re.compile(r"\bbreathing cord\b", re.I),
    re.compile(r"\bnever\b", re.I),
    re.compile(r"\bmust not\b", re.I),
    re.compile(r"\bdo not\b", re.I),
    re.compile(r"\bdon't\b", re.I),
    re.compile(r"\bdestructive\b", re.I),
    re.compile(r"\bforce[-\s]?push(?:es|ed|ing)?\b", re.I),  # fix: gerund/plural ("force-pushing")
    re.compile(r"\brm -rf\b", re.I),
    re.compile(r"\bdrop\s+table\b", re.I),
    re.compile(r"\bcredentials?\b", re.I),
    re.compile(r"\bsecrets?\b", re.I),          # fix: plural ("secrets") now matches
    re.compile(r"\bpasswords?\b", re.I),        # fix: plural ("passwords") now matches
    re.compile(r"\bapi[-\s]?keys?\b", re.I),    # fix: plural + hyphenated ("api-keys") now matches
    re.compile(r"\btokens?\b", re.I),           # fix: plural ("tokens") now matches
    re.compile(r"\bproduction\b", re.I),
    re.compile(r"\blive db\b", re.I),
    re.compile(r"\bmerge to main\b", re.I),
    re.compile(r"\bpush to main\b", re.I),
    # --- added: adversarial-slip coverage (destructive verbs/nouns the original
    # list missed entirely; see the 10-slip corpus in test_grounding_autoresolve.py
    # T_STAKES) ---
    re.compile(r"\bwip(?:e|ed|es|ing)\b", re.I),               # "wiping the boot disk"
    re.compile(r"\beras(?:e|ed|es|ing)\b", re.I),
    re.compile(r"\bpurg(?:e|ed|es|ing)\b", re.I),              # "purging every row from ..."
    re.compile(r"\bdelete[-\s]?all\b", re.I),
    re.compile(r"\bdrop(?:s|ped|ping)?\b", re.I),              # standalone drop, not just "drop table"
    re.compile(r"\btruncat(?:e|ed|es|ing)\b", re.I),
    re.compile(r"\bworld[-\s]?(?:writable|readable)\b", re.I),  # "world-writable"
    re.compile(r"\bchmod\s+0?777\b", re.I),
    re.compile(r"\bpower(?:s|ed|ing)?\s+off\b", re.I),         # "powering off the primary database server"
    re.compile(r"\bturn(?:s|ed|ing)?\s+off\b", re.I),          # "turning off the perimeter firewall"
    re.compile(r"\bshut(?:s|ting)?\s+down\b", re.I),
    re.compile(r"\bfirewall\b", re.I),
    re.compile(r"\bdisabl(?:e|ed|es|ing)\b", re.I),
    re.compile(r"\bclear(?:s|ed|ing)?\s+(?:the\s+)?(?:audit|log)\b", re.I),  # "clearing the audit trail table"
    re.compile(r"\bunencrypted\b", re.I),
    re.compile(r"\bprivate\s+key\b", re.I),
]


def _is_high_stakes(content: str) -> bool:
    """True if content matches a safety-marker pattern -- see doctrine's 5th
    clause (nothing destroyed) and the subagent-verification governance file's
    catastrophic-failure-mode rationale. High-stakes claims are NEVER
    auto-mutated by resolve_claim, only escalated to a proposal.

    This is the MECHANICAL half of a two-gate policy -- see
    `_classify_stakes_llm` for the LLM half that catches dangerous prose no
    keyword list can enumerate."""
    if not content:
        return False
    return any(p.search(content) for p in _HIGH_STAKES_PATTERNS)


# ---------------------------------------------------------------------------
# LLM stakes gate (second, more important layer) -- fail-safe: unavailable,
# erroring, or ambiguous model output all resolve to "high" (proposal-only).
# ---------------------------------------------------------------------------

_STAKES_SYSTEM = """\
You are a safety gate for an autonomous memory-correction system. You will be \
shown a knowledge-base claim that a read-only verification check has proven \
FALSE. If this claim's content were relied on WHILE WRONG, could that \
plausibly cause data loss, a security/credential exposure, or production \
impact (e.g. a destructive command, a broken access control, exposed \
secrets, or a service outage)?

Answer with EXACTLY one word, nothing else: "high" or "low".

If you are not certain, answer "high" -- a human reviews either way, so it is \
always safe to answer "high". Only answer "low" when you are confident the \
claim is purely informational or cosmetic and acting on it while wrong could \
not plausibly cause harm.
"""

_STAKES_MAX_TOKENS = 8


def _classify_stakes_llm(claim_content: str, llm: Any) -> str:
    """Second stakes gate for the auto-correction (fail -> update-in-place)
    path. Returns 'low' ONLY when the model confidently says so; every other
    outcome -- llm is None, the call errors, or the reply is anything other
    than an unambiguous "low" -- returns 'high' (fail-safe: uncertainty always
    routes to a human proposal, never to auto-mutation).

    This is deliberately a SEPARATE call from `draft_correction` (rather than
    folding the question into that prompt) so the stakes verdict can gate
    whether `draft_correction` is even invoked -- a claim classified high-
    stakes here never reaches the LLM correction-drafting step at all."""
    if llm is None:
        return "high"

    import llm_client
    from llm_client import GROUNDING_MODEL

    try:
        resp = llm_client.call_with_retry(
            llm,
            model=GROUNDING_MODEL,
            max_tokens=_STAKES_MAX_TOKENS,
            system=_STAKES_SYSTEM,
            messages=[{"role": "user", "content": (claim_content or "").strip()}],
        )
        raw = resp.content[0].text.strip().lower() if resp.content else ""
    except llm_client.TransientLLMError:
        # Transient transport failure — NOT a stakes verdict. Propagate so the
        # resolve job is requeued rather than silently defaulting to 'high'
        # (which would write a proposal/decision off a failed call).
        raise
    except Exception as exc:
        logger.warning("grounding_resolve: stakes classification LLM call failed: %s", exc)
        return "high"

    if raw.startswith("low"):
        return "low"
    # Covers "high", empty, and any unparseable/ambiguous reply.
    return "high"


# ---------------------------------------------------------------------------
# Stale-falsifier detection (the 449-noise root cause)
# ---------------------------------------------------------------------------

def falsifier_is_stale(claim_content: str, fal_row: Optional[Any]) -> bool:
    """True if a claim's stored falsifier is junk and should be re-authored.

    Junk cases:
      - no falsifier row at all (never derived)
      - kind='none' (no falsifiable entity was ever found -- e.g. an
        operator-preference or meta claim)
      - authored_by in (None, 'mechanical') AND the CURRENT classify_tier()
        says this content is actually Tier-B (predicate-bearing) -- i.e. a v1
        existence-probe was force-fit onto content it can't meaningfully test
        (the "meta-principle tested by `find config.yaml`" failure mode).

    A falsifier already marked tier='judgment' (mechanically_unverifiable) is
    NOT stale -- it's a settled, durable classification; re-authoring it would
    just re-derive the same "can't check this" answer and burn an LLM call.
    """
    if fal_row is None:
        return True

    keys = fal_row.keys()
    tier = fal_row["tier"] if "tier" in keys else None
    authored_by = fal_row["authored_by"] if "authored_by" in keys else None
    fkind = fal_row["kind"] if "kind" in keys else None

    # Settled: mechanically-unverifiable judgment claim — re-authoring would
    # just re-derive "can't check this" and burn an LLM call.
    if tier == "judgment":
        return False

    # Already a v2 LLM-authored recipe. NOTE kind='none' is EXPECTED here — the
    # author path stores the recipe in the `recipe` column and leaves the legacy
    # mechanical `kind` as 'none', so kind alone must NOT flag a Tier-B recipe
    # as junk (this was the bug that re-authored every good v2 recipe).
    if tier == "B" and authored_by == "llm":
        return False

    # No falsifiable entity was ever found on a mechanical/legacy row → junk,
    # needs LLM authoring (judgment recipe or a real Tier-B check).
    if not fkind or fkind == "none":
        return True

    # A v1 mechanical existence probe force-fit onto predicate-bearing content
    # (the "meta-principle tested by `find config.yaml`" failure mode).
    if authored_by in (None, "mechanical"):
        try:
            from entity_extract import extract_entities
            import grounding_author
            entities = extract_entities(claim_content or "")
            current_tier = grounding_author.classify_tier(claim_content or "", entities)
        except Exception:
            return False
        if current_tier == "B":
            return True

    return False


# ---------------------------------------------------------------------------
# Judgment marking -- auto-clear + permanent exclusion
# ---------------------------------------------------------------------------

def mark_judgment(conn, claim_kind: str, claim_id: int, reason: str) -> None:
    """Persist judgment-only status: this claim cannot be mechanically
    verified (author_recipe declared it mechanically_unverifiable). Sets a
    durable marker so it STOPS being re-flagged for mechanical/reground work,
    and auto-clears the current flag. Doctrine: nothing destroyed -- content
    and confidence are untouched; only the grounding classification changes.
    Caller owns commit."""
    tbl = _tbl(claim_kind)
    now = _utcnow_iso()
    detail = f"judgment:{(reason or '')[:80]}"

    existing = conn.execute(
        "SELECT id FROM falsifiers WHERE claim_kind=? AND claim_id=?",
        (claim_kind, claim_id),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE falsifiers SET tier='judgment', updated_at=? WHERE id=?",
            (now, existing["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO falsifiers
                (claim_kind, claim_id, kind, derived, tier, authored_by, created_at, updated_at)
            VALUES (?, ?, 'none', 0, 'judgment', 'llm', ?, ?)
            """,
            (claim_kind, claim_id, now, now),
        )

    conn.execute(
        f"UPDATE {tbl} SET grounding_mode='judgment', grounding_due=0, "
        f"grounded_at=?, grounded_against=? WHERE id=?",
        (now, detail, claim_id),
    )
    conn.execute(
        "UPDATE grounding_queue SET status='resolved', resolved_at=?, resolved_by='worker', "
        "resolution='unverifiable-judgment' WHERE claim_kind=? AND claim_id=? AND status='open'",
        (now, claim_kind, claim_id),
    )
    logger.info(
        "grounding DECISION claim=%s#%s type=resolve status=judgment reason=%s",
        claim_kind, claim_id, (reason or "")[:120],
    )


# ---------------------------------------------------------------------------
# Proactive sweep -- funnels the existing flagged backlog into the job queue
# ---------------------------------------------------------------------------

def sweep_flagged_claims(conn, limit: int = 10) -> int:
    """Scan grounding_due=1 claims (both kinds) and enqueue the right job.

    Before this, the ONLY thing that enqueued author/reground jobs for a
    flagged claim was that claim happening to surface in a live /recall call
    with an aging/stale/unverified liveness verdict -- the "reground barely
    runs" symptom. This sweep proactively drains the existing flagged backlog,
    bounded per call (`limit` per claim_kind) to stay rate/cost bounded.

    Skips:
      - claims already marked grounding_mode='judgment' (permanently excluded)
      - claims with an open pending resolution_proposal (awaiting a human --
        do not re-loop LLM work while a decision is outstanding)

    Returns the number of jobs enqueued (0 if nothing new -- dedup index
    on grounding_jobs makes this idempotent to call every sweep tick).
    """
    # Deferred import: grounding_queue_v2 imports this module (mark_judgment,
    # resolve_claim) at call time, so importing it back at module load time
    # would create a load-order cycle. Both modules are fully loaded by the
    # time either function actually runs.
    from grounding_queue_v2 import enqueue_job

    enqueued = 0
    # 24h cooldown on claims whose last job FAILED: without it, a terminally-
    # failing claim is re-enqueued every sweep tick (job dedup is pending-only),
    # burning one LLM call per minute per claim forever. Cutoff built in Python
    # per principle #124 (SQLite datetime('now') is space-separated and breaks
    # lexicographic comparison against T-separated ISO timestamps).
    failed_cooldown_cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).isoformat()

    for claim_kind, tbl in (("insight", "insights"), ("principle", "principles")):
        # HEAD-OF-LINE STARVATION FIX (2026-07-06): the skip conditions MUST
        # live in the SQL, not the Python loop. Previously `ORDER BY id LIMIT n`
        # selected the same lowest-id head every tick and the loop then skipped
        # claims with pending proposals — with 9 of the head-10 awaiting review
        # and one poison-pill claim, the other ~410 flagged claims were NEVER
        # reached ("Calls today 0" while the backlog sat at 428). Excluding
        # skippable claims in SQL makes LIMIT a budget of ACTIONABLE claims,
        # so every tick reaches fresh backlog.
        rows = conn.execute(
            f"""
            SELECT t.id, t.content FROM {tbl} t
            WHERE t.grounding_due = 1
              AND (t.grounding_mode IS NULL OR t.grounding_mode != 'judgment')
              AND t.superseded_by IS NULL
              AND NOT EXISTS (
                    SELECT 1 FROM resolution_proposals rp
                    WHERE rp.claim_kind = ? AND rp.claim_id = t.id AND rp.status = 'pending')
              AND NOT EXISTS (
                    SELECT 1 FROM grounding_jobs gj
                    WHERE gj.claim_kind = ? AND gj.claim_id = t.id
                      AND gj.status IN ('pending', 'running'))
              AND NOT EXISTS (
                    SELECT 1 FROM grounding_jobs gf
                    WHERE gf.claim_kind = ? AND gf.claim_id = t.id
                      AND gf.status = 'failed'
                      AND COALESCE(gf.finished_at, gf.enqueued_at) >= ?)
            ORDER BY t.id
            LIMIT ?
            """,
            (claim_kind, claim_kind, claim_kind, failed_cooldown_cutoff, limit),
        ).fetchall()

        for r in rows:
            cid = r["id"]
            content = r["content"] or ""

            fal = conn.execute(
                "SELECT kind, tier, authored_by, recipe FROM falsifiers WHERE claim_kind=? AND claim_id=?",
                (claim_kind, cid),
            ).fetchone()

            if falsifier_is_stale(content, fal):
                job_type = "author"
            elif fal is not None and fal["recipe"]:
                job_type = "reground"
            else:
                job_type = "author"

            if enqueue_job(conn, claim_kind, cid, job_type, priority=2):
                enqueued += 1

    conn.commit()
    return enqueued


# ---------------------------------------------------------------------------
# Graded resolution -- the core policy
# ---------------------------------------------------------------------------

_CORRECTION_SYSTEM = """\
You are correcting a knowledge-base claim that a read-only verification check
has just proven FALSE. Given the original claim, the reasoning that refuted
it, and the evidence, write the CORRECTED claim: a single paragraph of plain
text, no markdown, no preamble, no meta-commentary -- just the corrected
factual statement reflecting current reality per the evidence.

If the evidence does not give you enough to write a confident, SPECIFIC
correction (e.g. it only shows the old claim is false but not what replaced
it), output exactly the single word: UNCERTAIN
"""

_CORRECTION_MAX_TOKENS = 4096  # fallback-only; primary source is grounding_config


def draft_correction(claim_content: str, reasoning: str, evidence: str, llm: Any) -> Optional[str]:
    """Ask the LLM to draft a corrected claim. Returns the corrected text, or
    None if the LLM is unavailable, declines (UNCERTAIN), errors, or the
    output fails a basic sanity check. Never raises."""
    if llm is None:
        return None

    import grounding_config
    import llm_client

    cfg = grounding_config.get_config()

    user_msg = (
        f"Original claim:\n{(claim_content or '').strip()}\n\n"
        f"Why it's now false:\n{(reasoning or '').strip()}\n\n"
        f"Evidence:\n{(evidence or '').strip()}\n"
    )

    try:
        resp = llm_client.call_with_retry(
            llm,
            model=cfg.model,
            max_tokens=cfg.correction_max_tokens,
            system=_CORRECTION_SYSTEM,
            # Omit temperature unless explicitly enabled — newer models 400 on it.
            **({"temperature": cfg.temperature} if cfg.send_temperature else {}),
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip() if resp.content else ""
        llm_client.record_usage(resp, model=cfg.model, provider=cfg.provider)
    except llm_client.TransientLLMError:
        # Transient transport failure — NOT a "no correction available" answer.
        # Propagate so the resolve job requeues instead of terminating.
        raise
    except Exception as exc:
        logger.warning("grounding_resolve: draft_correction LLM call failed: %s", exc)
        return None

    if not raw or raw.strip().upper().startswith("UNCERTAIN"):
        return None
    if raw.strip().lower() == (claim_content or "").strip().lower():
        return None
    # Sanity bounds -- reject degenerate outputs rather than trust them blindly.
    if len(raw) < 10 or len(raw) > 4000:
        return None
    return raw.strip()


def _apply_verify(conn, claim_kind: str, claim_id: int, now: str) -> Optional[float]:
    """Inline equivalent of /verify_insight or /verify_principle with
    status='verified', scoped to the resolve worker's already-open
    transaction (the HTTP endpoints own their own connection/commit, so this
    is a deliberate, minimal, same-semantics duplicate rather than a cross-
    module call into the daemon script). Auto-promote is intentionally NOT
    replicated here -- flagged for the coordinator as a deferred refinement.

    Returns the PRIOR confidence (before the bump), or None if the claim row
    no longer exists. The caller persists this into
    resolution_proposals.prior_confidence so /ground/resolutions/{id}/revert
    can restore the exact pre-bump trust score (safety-verifier FIX4)."""
    tbl = _tbl(claim_kind)
    row = conn.execute(f"SELECT confidence FROM {tbl} WHERE id=?", (claim_id,)).fetchone()
    if row is None:
        return None
    conf = row["confidence"] if row["confidence"] is not None else 0.5

    if claim_kind == "insight":
        new_conf = min(1.0, conf + scoring.VERIFY_INSIGHT_UP)
        conn.execute(
            """UPDATE insights SET confidence=?, verify_count=COALESCE(verify_count,0)+1,
                                    verify_streak=COALESCE(verify_streak,0)+1,
                                    status='active', verified_at=?, updated_at=?
               WHERE id=?""",
            (new_conf, now, now, claim_id),
        )
    else:
        new_conf = min(1.0, conf + scoring.VERIFY_PRINCIPLE_UP)
        conn.execute(
            "UPDATE principles SET confidence=?, updated_at=? WHERE id=?",
            (new_conf, now, claim_id),
        )
    return conf


def _insert_proposal(
    conn, claim_kind: str, claim_id: int, verdict: Optional[str], proposed_action: str,
    proposed_content: Optional[str], prior_content: Optional[str],
    reasoning: Optional[str], evidence: Optional[str], stakes: str,
    auto_applied: int, status: str, now: str,
    decided_at: Optional[str] = None, decided_by: Optional[str] = None,
    prior_confidence: Optional[float] = None,
) -> int:
    # FIX3 (credential redaction): proposed_content may carry fresh LLM output
    # (a drafted correction) which is durable, dashboard-visible text -- redact
    # any live-credential shape before it is ever written to this table, same
    # rationale as grounding_author's falsification_question/evidence redaction.
    if proposed_content:
        proposed_content = _redact_credential_shapes(proposed_content)
    cur = conn.execute(
        """
        INSERT INTO resolution_proposals
            (claim_kind, claim_id, verdict, proposed_action, proposed_content, prior_content,
             reasoning, evidence, stakes, auto_applied, status, created_at, decided_at, decided_by,
             prior_confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (claim_kind, claim_id, verdict, proposed_action, proposed_content, prior_content,
         reasoning, evidence, stakes, auto_applied, status, now, decided_at, decided_by,
         prior_confidence),
    )
    return cur.lastrowid


def resolve_claim(conn, claim_kind: str, claim_id: int, claim_content: str, llm: Any) -> Optional[str]:
    """Graded autonomous resolution for a claim that just completed a
    'reground' cycle. Reads the latest reground verdict from grounding_history
    and applies the decision table below. Returns an error string on failure
    (drain_one_job's _handle_* contract), or None on success.

    Decision table (post safety-verifier fixes -- see FIX1/FIX2/FIX3 below)
    --------------------------------------------------------------------
    verdict=pass, claim_kind=insight, stakes=low     -> auto-verify (confidence
                                                         bump), clear flag,
                                                         proposal row
                                                         status=auto-applied
                                                         (audit trail + revert
                                                         handle, prior_confidence
                                                         stored for revert)
    verdict=pass, claim_kind=principle OR stakes=high -> flag STILL clears
       (FIX1)                                           (claim confirmed true --
                                                         grounded_at is freshness,
                                                         not trust) but NO
                                                         confidence mutation;
                                                         resolution_proposals
                                                         row status=pending,
                                                         proposed_action=verify,
                                                         for a human to confirm
                                                         the trust bump
    verdict=fail, claim_kind=insight, stakes=low
      (mechanical regex AND LLM stakes gate both     -> auto-update content
       say low -- FIX2 two-gate policy),                 (update-in-place, NOT
      LLM drafts a confident correction                  supersede -- this is
                                                          drift correction, not
                                                          a genuine replacement),
                                                          corrected text is
                                                          credential-redacted
                                                          before any write
                                                          (FIX3), clear flag,
                                                          proposal row
                                                          status=auto-applied
                                                          (audit trail + revert
                                                          handle)
    verdict=fail, correction unavailable/declined,
      OR claim_kind=principle, OR mechanical-stakes=high,
      OR LLM stakes gate says high/unavailable/       -> resolution_proposals
      ambiguous (fail-safe -- FIX2)                       row status=pending,
                                                           proposed_action=update,
                                                           NO mutation, flag left
                                                           open (non-blocking)
    verdict=uncertain                                 -> resolution_proposals
                                                         row status=pending,
                                                         proposed_action=dismiss
                                                         (informational -- "no
                                                         confident auto verdict,
                                                         a human should read the
                                                         reasoning/evidence and
                                                         choose verify/update/
                                                         supersede/dismiss"),
                                                         NO mutation

    Rule of thumb (FIX1): auto-CONFIDENCE-mutation (the 'verify' action) is
    allowed ONLY for claim_kind='insight' AND stakes='low' -- on ANY verdict,
    not just fail. Auto-CONTENT-mutation (the 'update' action, i.e.
    draft_correction) additionally requires the LLM stakes gate to confirm
    'low' (FIX2) -- the mechanical regex in `_is_high_stakes` is a backstop,
    not the sole gate, because keyword lists cannot enumerate dangerous prose.

    Every branch appends a job_type='resolve' grounding_history row.
    """
    tbl = _tbl(claim_kind)

    hist = conn.execute(
        """
        SELECT verdict, reasoning, evidence FROM grounding_history
        WHERE claim_kind=? AND claim_id=? AND job_type='reground' AND verdict IS NOT NULL
        ORDER BY ts DESC LIMIT 1
        """,
        (claim_kind, claim_id),
    ).fetchone()
    if hist is None:
        return "no_reground_verdict: resolve job ran before any reground history existed"

    verdict = hist["verdict"]
    reasoning = hist["reasoning"] or ""
    evidence = hist["evidence"] or ""
    now = _utcnow_iso()
    stakes = "high" if (claim_kind == "principle" or _is_high_stakes(claim_content)) else "low"

    # Deferred import -- avoid a load-order cycle with grounding_queue_v2
    # (which imports this module lazily inside its own handlers).
    from grounding_queue_v2 import append_history

    if verdict == "pass":
        # FIX1: auto-confidence-mutation is allowed ONLY for a low-stakes
        # insight, on ANY verdict -- the pre-fix code called _apply_verify
        # unconditionally here with no stakes/claim_kind check at all (that
        # gate only existed on the fail path), so a `pass` on a principle
        # silently bumped confidence 0.9 -> 0.95 with zero human involvement.
        #
        # FIX2b (post-merge adversarial re-check, 2026-07-05): the mechanical
        # regex in `_is_high_stakes` is a keyword backstop and, same as on the
        # fail path, cannot enumerate every dangerous phrasing (probes found
        # "deleting the customer records permanently" and "rotate the AWS
        # access key" both slip past the list). Applying the LLM two-gate
        # policy ONLY on the fail path left the pass path -- which still
        # auto-mutates confidence -- checked by the mechanical gate alone.
        # Mirror the fail path here: when the mechanical gate says low for an
        # insight, ask the LLM gate too before treating it as auto-verify-ok.
        # Fail-safe unchanged: llm=None / error / ambiguous all resolve to
        # 'high' inside _classify_stakes_llm, so an unavailable LLM never
        # loosens this -- it only ever escalates a mechanical 'low' to 'high'.
        if claim_kind == "insight" and stakes == "low":
            llm_stakes = _classify_stakes_llm(claim_content, llm)
            if llm_stakes == "high":
                stakes = "high"
        auto_verify_ok = (claim_kind == "insight" and stakes == "low")

        # The flag clears either way: the falsifier CONFIRMED the claim, so
        # there is no live correctness problem to leave open. grounded_at is
        # a freshness stamp, not a trust score (same distinction the redaction
        # docstring draws) -- stamping it is safe even when the confidence
        # bump itself is deferred to a human below.
        conn.execute(
            f"UPDATE {tbl} SET grounded_at=?, grounding_due=0 WHERE id=?",
            (now, claim_id),
        )
        conn.execute(
            "UPDATE grounding_queue SET status='resolved', resolved_at=?, resolved_by='worker', "
            "resolution=? WHERE claim_kind=? AND claim_id=? AND status='open'",
            (now, "verified" if auto_verify_ok else "verified-pending-review", claim_kind, claim_id),
        )

        if auto_verify_ok:
            prior_conf = _apply_verify(conn, claim_kind, claim_id, now)
            _insert_proposal(
                conn, claim_kind, claim_id, verdict, "verify",
                proposed_content=None, prior_content=claim_content,
                reasoning=reasoning, evidence=evidence, stakes=stakes,
                auto_applied=1, status="auto-applied", now=now,
                decided_at=now, decided_by="worker-auto",
                prior_confidence=prior_conf,
            )
            append_history(conn, claim_kind, claim_id, "resolve", "pass",
                           "Auto-verified: falsifier recipe passed.", evidence, None)
        else:
            # Principle or high-stakes insight: NO confidence mutation. Write
            # a pending proposal so a human can approve the trust bump via
            # POST /ground/proposals/{id}/decide (which replays the same
            # 'verify' action _apply_verify performs, just human-gated).
            _insert_proposal(
                conn, claim_kind, claim_id, verdict, "verify",
                proposed_content=None, prior_content=claim_content,
                reasoning=reasoning, evidence=evidence, stakes=stakes,
                auto_applied=0, status="pending", now=now,
            )
            append_history(
                conn, claim_kind, claim_id, "resolve", "pass",
                f"Escalated to resolution_proposals (stakes={stakes}, claim_kind={claim_kind}): "
                "pass verdict confirmed the claim, but a confidence bump on a "
                "principle/high-stakes insight requires human confirmation.",
                evidence, None,
            )
        conn.commit()
        return None

    if verdict == "fail" and stakes == "low" and claim_kind == "insight":
        # FIX2 two-gate policy: `stakes == "low"` above is only the MECHANICAL
        # gate (regex backstop). Auto-correction additionally requires the LLM
        # gate to confirm 'low' -- fail-safe: llm=None, an error, or anything
        # but an unambiguous "low" reply all resolve to 'high', which falls
        # through to the escalation branch below exactly like a declined
        # correction would.
        llm_stakes = _classify_stakes_llm(claim_content, llm)
        if llm_stakes == "low":
            corrected = draft_correction(claim_content, reasoning, evidence, llm)
            if corrected:
                # FIX3: redact any credential-shaped substring the LLM echoed
                # back from the claim content BEFORE it is written anywhere --
                # this closes the same leak class as insight #2048 (see
                # grounding_author._redact_credential_shapes docstring).
                # _insert_proposal below redacts proposed_content again
                # defensively; redacting here also protects the actual
                # insights.content write.
                corrected = _redact_credential_shapes(corrected)
                conn.execute(
                    "UPDATE insights SET content=?, grounded_at=?, grounding_due=0, updated_at=? WHERE id=?",
                    (corrected, now, now, claim_id),
                )
                conn.execute(
                    "UPDATE grounding_queue SET status='resolved', resolved_at=?, resolved_by='worker', "
                    "resolution='auto-corrected' WHERE claim_kind=? AND claim_id=? AND status='open'",
                    (now, claim_kind, claim_id),
                )
                _insert_proposal(
                    conn, claim_kind, claim_id, verdict, "update",
                    proposed_content=corrected, prior_content=claim_content,
                    reasoning=reasoning, evidence=evidence, stakes=stakes,
                    auto_applied=1, status="auto-applied", now=now,
                    decided_at=now, decided_by="worker-auto",
                )
                append_history(conn, claim_kind, claim_id, "resolve", "fail",
                               f"Auto-corrected (low-stakes insight, LLM stakes gate=low): {reasoning[:200]}",
                               evidence, None)
                conn.commit()
                return None
            # No confident correction -- fall through to the escalation branch.
        else:
            # LLM gate overrode the mechanical 'low' -- record the escalation
            # as high-stakes so a human sees WHY this didn't auto-correct
            # despite passing the regex backstop.
            stakes = "high"

    # uncertain OR high-stakes OR principle OR fail-without-confident-correction
    proposed_action = "dismiss" if verdict == "uncertain" else "update"
    _insert_proposal(
        conn, claim_kind, claim_id, verdict, proposed_action,
        proposed_content=None, prior_content=claim_content,
        reasoning=reasoning, evidence=evidence, stakes=stakes,
        auto_applied=0, status="pending", now=now,
    )
    append_history(conn, claim_kind, claim_id, "resolve", verdict,
                   f"Escalated to resolution_proposals (stakes={stakes}): {reasoning[:200]}",
                   evidence, None)
    conn.commit()
    return None
