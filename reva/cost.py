"""Cost estimation for Claude API calls.

Rates reflect Anthropic public pricing for Sonnet 5, Sonnet 4.6, and Opus 4.8.
Verify against https://www.anthropic.com/pricing if billing accuracy matters.
Sonnet 5 uses its standard $3/$15 rate; the $2/$10 introductory rate (through
Aug 31, 2026) is intentionally not used so the budget cap estimates conservatively.
"""

from __future__ import annotations

# Per-token USD rates. (Per million tokens / 1_000_000.)
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {
        "input": 3.00 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "cache_read": 0.30 / 1_000_000,
        "cache_write_5m": 3.75 / 1_000_000,
    },
    "claude-sonnet-4-6": {
        "input": 3.00 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "cache_read": 0.30 / 1_000_000,
        "cache_write_5m": 3.75 / 1_000_000,
    },
    "claude-opus-4-8": {
        "input": 5.00 / 1_000_000,
        "output": 25.00 / 1_000_000,
        "cache_read": 0.50 / 1_000_000,
        "cache_write_5m": 6.25 / 1_000_000,
    },
}

_FALLBACK_KEY = "claude-sonnet-4-6"


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Return the estimated USD cost for one Claude call.

    An unknown/unpriced model silently falls back to Sonnet 4.6 rates
    (`_FALLBACK_KEY`), so the estimate stays non-zero but may be inaccurate for a
    model not in PRICING — keep the table current when adding models.
    """
    rates = PRICING.get(model, PRICING[_FALLBACK_KEY])
    total = (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + cache_read_tokens * rates["cache_read"]
        + cache_write_tokens * rates["cache_write_5m"]
    )
    return round(total, 6)
