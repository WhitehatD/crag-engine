# coding: utf-8
"""Grounding LLM cost ledger + budget enforcement (migration 029 / Phase 1b,
insight #3339).

Append-only usage/cost trail for every grounding LLM call (author/adjudicate/
correction, any provider), plus the hard-budget-cap check that makes the
tradeoff of using the CLAUDE SUBSCRIPTION WEEKLY LIMIT (anthropic-oauth
provider) fail-safe: `budget_exceeded()` is checked once per job at the top
of `grounding_queue_v2.drain_one_job()` — if today's usage is over either cap
and `pause_on_budget=true`, the worker refuses to start a new LLM-calling job
rather than risk starving the operator's own Claude usage.

Public API
----------
record_call(conn, provider, model, stage, tokens_in, tokens_out,
            claim_kind=None, claim_id=None) -> None
    Append one row. Caller owns the transaction/commit (house style).

budget_status(conn, cfg=None) -> dict
    {calls_today, tokens_today, daily_budget_calls, daily_budget_tokens,
     calls_remaining, tokens_remaining, exceeded, pause_on_budget}

budget_exceeded(conn, cfg=None) -> bool
    True iff pause_on_budget is enabled AND either daily cap is exceeded.
    False (never blocks) when pause_on_budget=false, regardless of usage —
    that's the explicit opt-out for zero-cost/zero-quota-risk setups
    (ollama-local, or an operator who wants observability without the gate).

House style: pure functions take an open sqlite3.Connection (never open/
close/commit-across-a-boundary the caller doesn't already own — record_call
itself does not commit; callers already batch grounding_history/falsifier
writes into one commit per job, and this row joins that same transaction).
Timestamps: _utcnow_iso() only, never datetime('now').
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crag-anchor")

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from lifecycle import _utcnow_iso  # noqa: E402
import grounding_config  # noqa: E402

# ---------------------------------------------------------------------------
# Pricing table — $/million tokens. A model absent from this table gets
# est_cost_usd=NULL rather than a fabricated guess. Update when your provider
# publishes new rates. (packages/pricing/pricing.py is the fuller canonical
# table; this is a lean copy for the grounding-cost accounting path.)
# ---------------------------------------------------------------------------
_PRICING_PER_MTOK: dict = {
    "claude-fable-5":            {"input": 10.00, "output": 50.00},
    "claude-opus-4-8":           {"input": 5.00, "output": 25.00},
    "claude-opus-4-7":           {"input": 5.00, "output": 25.00},
    "claude-opus-4-6":           {"input": 5.00, "output": 25.00},
    # promo pricing runs through 2026-08-31, then $3/$15 (matches sonnet-4-6).
    "claude-sonnet-5":           {"input": 2.00, "output": 10.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-20250514":  {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}

_QUOTA_TYPE_BY_PROVIDER = {
    "anthropic-oauth": "weekly_subscription",
    "anthropic-api": "metered_api",
    "openai": "metered_api",
    "ollama-local": "local",
}


def _quota_type(provider: str) -> str:
    return _QUOTA_TYPE_BY_PROVIDER.get(provider, "metered_api")


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> Optional[float]:
    price = _PRICING_PER_MTOK.get(model)
    if price is None:
        return None
    return round(
        (tokens_in / 1_000_000.0) * price["input"]
        + (tokens_out / 1_000_000.0) * price["output"],
        6,
    )


def record_call(
    conn,
    provider: str,
    model: str,
    stage: str,
    tokens_in: int,
    tokens_out: int,
    claim_kind: Optional[str] = None,
    claim_id: Optional[int] = None,
) -> None:
    """Append one row to llm_cost_ledger. Never raises — a cost-recording
    failure must not fail the grounding job it's recording."""
    try:
        conn.execute(
            """
            INSERT INTO llm_cost_ledger
                (ts, provider, model, stage, claim_kind, claim_id,
                 tokens_in, tokens_out, est_cost_usd, quota_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utcnow_iso(), provider or "unknown", model or "unknown", stage,
                claim_kind, claim_id, int(tokens_in or 0), int(tokens_out or 0),
                _estimate_cost(model or "", tokens_in or 0, tokens_out or 0),
                _quota_type(provider or ""),
            ),
        )
    except Exception as exc:
        logger.warning("grounding_cost: record_call failed: %s", exc)


def _today_usage(conn) -> tuple:
    """Return (calls_today, tokens_today) for the current UTC date, derived
    from the ledger's own timestamps (substr(ts,1,10) = today's ISO date) —
    not wall-clock date.time() to keep this testable/deterministic against
    whatever _utcnow_iso() the caller's rows were stamped with."""
    today = _utcnow_iso()[:10]
    row = conn.execute(
        """
        SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens
        FROM llm_cost_ledger WHERE substr(ts, 1, 10) = ?
        """,
        (today,),
    ).fetchone()
    return (row["calls"], row["tokens"])


def budget_status(conn, cfg: Optional["grounding_config.GroundingLLMConfig"] = None) -> dict:
    cfg = cfg or grounding_config.get_config()
    calls_today, tokens_today = _today_usage(conn)
    calls_over = calls_today >= cfg.daily_budget_calls
    tokens_over = tokens_today >= cfg.daily_budget_tokens
    return {
        "calls_today": calls_today,
        "tokens_today": tokens_today,
        "daily_budget_calls": cfg.daily_budget_calls,
        "daily_budget_tokens": cfg.daily_budget_tokens,
        "calls_remaining": max(0, cfg.daily_budget_calls - calls_today),
        "tokens_remaining": max(0, cfg.daily_budget_tokens - tokens_today),
        "exceeded": bool(calls_over or tokens_over),
        "pause_on_budget": cfg.pause_on_budget,
    }


def budget_exceeded(conn, cfg: Optional["grounding_config.GroundingLLMConfig"] = None) -> bool:
    """True iff the worker should PAUSE before starting a new LLM-calling
    job: pause_on_budget is enabled AND today's usage is over either cap.
    False whenever pause_on_budget=false — an explicit operator opt-out, not
    a bug (e.g. a local/ollama setup with no quota to protect)."""
    cfg = cfg or grounding_config.get_config()
    if not cfg.pause_on_budget:
        return False
    status = budget_status(conn, cfg)
    return status["exceeded"]
