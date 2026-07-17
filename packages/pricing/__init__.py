# coding: utf-8
"""
packages.pricing — Anthropic API pricing, re-exported from pricing.py.

Import from here, not from apps.dashboard.app.pricing:

    from packages.pricing import get_rates, PRICING, SUBSCRIPTION_USD_PER_MONTH

"""
from .pricing import PRICING, PRICES_AS_OF, SUBSCRIPTION_USD_PER_MONTH, get_rates

__all__ = ["PRICING", "PRICES_AS_OF", "SUBSCRIPTION_USD_PER_MONTH", "get_rates"]
