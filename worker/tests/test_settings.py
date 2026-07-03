"""Tests for env parsing in worker.settings."""

from __future__ import annotations

from worker.settings import _verify_findings_default_from_env


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
