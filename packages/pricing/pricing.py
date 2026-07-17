# coding: utf-8
"""Anthropic API rates — canonical pricing table for cost estimation.

Update this table when your provider publishes new prices. Cost calculations
(token ledger reporting, grounding-cost accounting) reference this — no rates
hardcoded elsewhere. A model absent from the table yields a NULL estimate
rather than a fabricated number.

Rates are in USD per 1M tokens.
"""
from __future__ import annotations

PRICING: dict[str, dict[str, float]] = {
    # Claude Fable 5 — $10/$50 per MTok. Mythos-class; top tier (2026-06-09)
    "claude-fable-5": {
        "input": 10.0,
        "output": 50.0,
        "cache_read": 1.00,
        "cache_write_5m": 12.5,
        "cache_write_1h": 20.0,
    },
    # Claude Opus 4.8 — $5/$25 per MTok (same as 4.5–4.7)
    "claude-opus-4-8": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
    },
    "claude-opus-4-8-1m": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
    },
    # Claude Opus 4.7 — corrected from $15/$75 (was Opus 4.1 rates)
    "claude-opus-4-7": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
    },
    "claude-opus-4-7-1m": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
    },
    # Claude Opus 4.6 — corrected from $15/$75
    "claude-opus-4-6": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
    },
    # Claude Opus 4.5
    "claude-opus-4-5": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
    },
    # Claude Opus 4.1 — correctly $15/$75
    "claude-opus-4-1": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.50,
        "cache_write_5m": 18.75,
        "cache_write_1h": 30.0,
    },
    # Legacy short names seen in token_ledger (collector used short names)
    "opus-4": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
    },
    "claude-sonnet-4-5": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
    },
    "claude-haiku-4-5-20251001": {
        "input": 1.0,
        "output": 5.0,
        "cache_read": 0.10,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.0,
    },
    # Fallback for unknown model strings — assume Sonnet rates
    "_unknown": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
    },
}

PRICES_AS_OF = "2026-06-09"
SUBSCRIPTION_USD_PER_MONTH = 200.0


def get_rates(model: str) -> dict[str, float]:
    """Return pricing rates for *model*, falling back to _unknown.

    Keys returned: input, output, cache_read, cache_write_5m, cache_write_1h.
    All values are USD per 1M tokens.
    """
    if not model:
        return PRICING["_unknown"]
    # Exact match first
    if model in PRICING:
        return PRICING[model]
    # Prefix/substring match (handles "claude-opus-4-8[1m]" style)
    model_lower = model.lower()
    for key in PRICING:
        if key.startswith("_"):
            continue
        if key in model_lower or model_lower.startswith(key):
            return PRICING[key]
    return PRICING["_unknown"]
