"""Tests for env parsing in worker.settings."""

from __future__ import annotations

from worker.settings import _author_daily_budget_from_env, _verify_findings_default_from_env


def test_default_is_on(monkeypatch):
    monkeypatch.delenv("REVA_VERIFY_FINDINGS", raising=False)
    monkeypatch.delenv("REVA_VERIFY_HIGH_COST", raising=False)
    assert _verify_findings_default_from_env() is True


def test_new_var_wins(monkeypatch):
    monkeypatch.setenv("REVA_VERIFY_FINDINGS", "false")
    monkeypatch.setenv("REVA_VERIFY_HIGH_COST", "true")
    assert _verify_findings_default_from_env() is False


def test_legacy_var_honored_when_new_unset(monkeypatch):
    monkeypatch.delenv("REVA_VERIFY_FINDINGS", raising=False)
    monkeypatch.setenv("REVA_VERIFY_HIGH_COST", "false")
    assert _verify_findings_default_from_env() is False
    monkeypatch.setenv("REVA_VERIFY_HIGH_COST", "true")
    assert _verify_findings_default_from_env() is True


def test_author_budget_defaults_to_100(monkeypatch):
    monkeypatch.delenv("REVA_AUTHOR_DAILY_BUDGET_USD", raising=False)
    assert _author_daily_budget_from_env() == 100.0


def test_author_budget_zero_disables(monkeypatch):
    monkeypatch.setenv("REVA_AUTHOR_DAILY_BUDGET_USD", "0")
    assert _author_daily_budget_from_env() is None
    monkeypatch.setenv("REVA_AUTHOR_DAILY_BUDGET_USD", "-5")
    assert _author_daily_budget_from_env() is None


def test_author_budget_override(monkeypatch):
    monkeypatch.setenv("REVA_AUTHOR_DAILY_BUDGET_USD", "250")
    assert _author_daily_budget_from_env() == 250.0
