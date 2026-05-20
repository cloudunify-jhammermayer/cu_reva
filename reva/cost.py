"""Cost estimation for Claude API calls.

Pricing values are placeholders based on the published Anthropic
Sonnet/Opus 4.x baseline ($3 / $15 per MTok input/output for Sonnet,
$15 / $75 for Opus). Cache reads are ~10% of input; 5-minute cache writes
are ~1.25x input. VERIFY against current Anthropic pricing before billing
treats these numbers as authoritative.
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
