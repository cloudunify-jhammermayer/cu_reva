"""Scheduler settings tests."""

from __future__ import annotations


def test_ops_events_retention_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    from scheduler.settings import Settings

    assert Settings.from_env().ops_events_retention_days == 30

    monkeypatch.setenv("REVA_OPS_EVENTS_RETENTION_DAYS", "7")
    assert Settings.from_env().ops_events_retention_days == 7
