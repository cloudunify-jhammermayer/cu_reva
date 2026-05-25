"""Cost estimation for Claude API calls.

Rates reflect Anthropic public pricing for Sonnet 4.6 and Opus 4.7 as of
May 2026. Verify against https://www.anthropic.com/pricing if billing
accuracy matters.
"""

from __future__ import annotations

# Per-token USD rates. (Per million tokens / 1_000_000.)
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.00 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "cache_read": 0.30 / 1_000_000,
        "cache_write_5m": 3.75 / 1_000_000,
    },
    "claude-opus-4-7": {
        "input": 15.00 / 1_000_000,
        "output": 75.00 / 1_000_000,
        "cache_read": 1.50 / 1_000_000,
        "cache_write_5m": 18.75 / 1_000_000,
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
    """Return the estimated USD cost for one Claude call."""
    rates = PRICING.get(model, PRICING[_FALLBACK_KEY])
    total = (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + cache_read_tokens * rates["cache_read"]
        + cache_write_tokens * rates["cache_write_5m"]
    )
    return round(total, 6)
