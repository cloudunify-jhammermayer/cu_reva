"""Tests for reva.cost.estimate_cost model resolution (M2)."""

from __future__ import annotations

from reva.cost import PRICING, estimate_cost


def test_exact_model_uses_its_rates():
    # 1M output tokens at Opus's $25/M.
    assert estimate_cost("claude-opus-4-8", 0, 1_000_000) == 25.0


def test_dated_model_id_resolves_to_base_rates():
    """The Messages API echoes a dated id; it must price at Opus, not fall back
    to Sonnet ($15/M) — that undercounted Opus spend ~40%."""
    dated = estimate_cost("claude-opus-4-8-20260101", 0, 1_000_000)
    assert dated == 25.0


def test_vendor_prefixed_model_id_resolves():
    assert estimate_cost("us.anthropic.claude-opus-4-8", 0, 1_000_000) == 25.0


def test_sonnet_dated_id_stays_sonnet():
    assert estimate_cost("claude-sonnet-5-20260514", 0, 1_000_000) == 15.0


def test_unknown_model_falls_back_to_sonnet_46():
    fallback = estimate_cost("gpt-4", 0, 1_000_000)
    assert fallback == PRICING["claude-sonnet-4-6"]["output"] * 1_000_000


def test_haiku_45_prices_at_haiku_rates():
    # $1/M input, $5/M output.
    assert estimate_cost("claude-haiku-4-5", 1_000_000, 0) == 1.0
    assert estimate_cost("claude-haiku-4-5", 0, 1_000_000) == 5.0


def test_haiku_dated_id_resolves_to_haiku_rates():
    # The Messages API echoes the dated id (claude-haiku-4-5-20251001).
    assert estimate_cost("claude-haiku-4-5-20251001", 0, 1_000_000) == 5.0


def test_haiku_cache_rates():
    assert estimate_cost("claude-haiku-4-5", 0, 0, 1_000_000, 0) == 0.1
    assert estimate_cost("claude-haiku-4-5", 0, 0, 0, 1_000_000) == 1.25
