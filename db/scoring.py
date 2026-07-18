"""crag-anchor scoring & lifecycle constants — SINGLE SOURCE OF TRUTH (WS2 T6).

Every tunable that governs how the engine ranks, verifies, promotes, decays,
dedups, and grounds knowledge lived scattered across engine_daemon.py (3×
duplicated hybrid weights + inline formula strings), engine-cli.py (decay
factor/window), and contradiction.py (cosine/entail thresholds). This module
collects them so a change lands in exactly one place and the daemon, the
operator CLI, and the cron jobs all agree.

Honesty tags on every constant:
  measured              — value backed by logged/measured evidence in the engine.
  unvalidated-heuristic — value is a reasonable guess; NOT empirically tuned.

Do NOT add behaviour here. This is data only. Importers:
  - apps/daemon/engine_daemon.py  (path-inserts db/ — see its sys.path.insert)
  - db/engine-cli.py              (same dir)
  - db/contradiction.py          (re-exports its thresholds for reference)
  - db/lifecycle.py              (decay + resolvability share these)
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Hybrid recall ranking weights (with embeddings available)
#   hybrid = W_COSINE*cosine + W_FTS*fts + W_CONF*confidence
# Duplicated pre-WS2 at daemon ~385, ~2621, ~2735 plus 3 formula strings.
# ---------------------------------------------------------------------------
HYBRID_W_COSINE: float = 0.50  # unvalidated-heuristic — semantic weight
HYBRID_W_FTS: float = 0.35     # unvalidated-heuristic — lexical (BM25) weight
HYBRID_W_CONF: float = 0.15    # unvalidated-heuristic — trust prior weight
HYBRID_FORMULA: str = "0.50*cosine + 0.35*fts + 0.15*confidence"

# No-embedding fallback weights (model not loaded / row unembedded)
NOEMB_W_FTS: float = 0.65      # unvalidated-heuristic
NOEMB_W_CONF: float = 0.35     # unvalidated-heuristic
NOEMB_FORMULA: str = "0.65*fts + 0.35*confidence (no embeddings)"

# ---------------------------------------------------------------------------
# Verification confidence deltas
# ---------------------------------------------------------------------------
VERIFY_INSIGHT_UP: float = 0.10    # unvalidated-heuristic — +conf on 'verified'
VERIFY_INSIGHT_DOWN: float = 0.20  # unvalidated-heuristic — -conf on 'stale'
VERIFY_PRINCIPLE_UP: float = 0.05  # unvalidated-heuristic — gentler (curated)
VERIFY_PRINCIPLE_DOWN: float = 0.10  # unvalidated-heuristic
# Below this confidence a 'stale' verdict flips insight.status to 'stale'.
STALE_STATUS_FLOOR: float = 0.30   # unvalidated-heuristic

# ---------------------------------------------------------------------------
# Promotion / distillation
# ---------------------------------------------------------------------------
PROMOTE_SEED_CONFIDENCE: float = 0.90  # unvalidated-heuristic — manual/auto promote + distill seed

# Auto-promote gate (WS2 T1): the DOCUMENTED lifecycle, previously vaporware.
# A 'verified' bump that crosses ALL THREE thresholds auto-promotes to principle.
AUTO_PROMOTE_MIN_CONFIDENCE: float = 0.85  # unvalidated-heuristic
AUTO_PROMOTE_MIN_VERIFY_COUNT: int = 3     # unvalidated-heuristic
AUTO_PROMOTE_MIN_VERIFY_STREAK: int = 2    # unvalidated-heuristic

# ---------------------------------------------------------------------------
# Decay (WS2 T2): trust must be able to FALL. Weekly loop + CLI share db/lifecycle.
#   active insight, not recalled in DECAY_WINDOW_DAYS, not promoted →
#   confidence *= DECAY_FACTOR (floored at DECAY_FLOOR). Principles NOT decayed.
# ---------------------------------------------------------------------------
DECAY_FACTOR: float = 0.90        # unvalidated-heuristic — multiplicative shrink
DECAY_WINDOW_DAYS: int = 60       # unvalidated-heuristic — recall recency window
DECAY_FLOOR: float = 0.10         # unvalidated-heuristic — never decay below this
# Principle re-verify nudge window (CLI flags stale principles, does not decay them)
DECAY_PRINCIPLE_FLAG_DAYS: int = 90  # unvalidated-heuristic

# ---------------------------------------------------------------------------
# Dedup guard (save_insight cosine gate)
# ---------------------------------------------------------------------------
DEDUP_COSINE_THRESHOLD: float = 0.85   # unvalidated-heuristic — cosine dup cutoff
DEDUP_JACCARD_THRESHOLD: float = 0.72  # unvalidated-heuristic — shingle fallback

# ---------------------------------------------------------------------------
# Liveness multipliers (WS2 T3d): consume the Phase-25 grounding verdict in
# ranking. Transparent, post-score. fresh/aging/unverified pass through ×1.0.
# ---------------------------------------------------------------------------
LIVENESS_MULT_STALE: float = 0.75         # unvalidated-heuristic — actively disproved / drifted
LIVENESS_MULT_REVALIDATING: float = 0.90  # unvalidated-heuristic — flagged, truth unconfirmed
LIVENESS_MULT_DEFAULT: float = 1.00       # fresh | aging | unverified | none


def liveness_multiplier(verdict: str | None) -> float:
    """Map a Phase-25 liveness verdict to its ranking multiplier (T3d)."""
    if verdict == "stale":
        return LIVENESS_MULT_STALE
    if verdict == "revalidating":
        return LIVENESS_MULT_REVALIDATING
    return LIVENESS_MULT_DEFAULT


# ---------------------------------------------------------------------------
# Contradiction thresholds — re-exported from contradiction.py for reference /
# single-place discovery. contradiction.py remains the authority (env-overridable
# there); these mirror its NEW WS2 defaults so a reader finds all knobs here too.
# ---------------------------------------------------------------------------
CONTRA_COSINE_THRESHOLD: float = float(os.environ.get("CRAG_ANCHOR_CONTRA_COSINE", "0.70"))  # unvalidated-heuristic (WS2: 0.55→0.70)
CONTRA_ENTAIL_THRESHOLD: float = float(os.environ.get("CRAG_ANCHOR_CONTRA_ENTAIL", "0.90"))  # unvalidated-heuristic (WS2: 0.70→0.90)
