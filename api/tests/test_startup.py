"""Tests for API startup behaviour."""

from __future__ import annotations

import structlog.testing

from app.main import warn_if_no_api_key
from app.settings import Settings


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        database_url="sqlite:///:memory:",
        github_app_id=1,
        github_webhook_secret="x",
        github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def test_warn_if_no_api_key_logs_warning():
    settings = _make_settings(api_key="")
    with structlog.testing.capture_logs() as logs:
        warn_if_no_api_key(settings)
    assert any(
        log.get("log_level") == "warning" and "REVA_API_KEY" in log.get("detail", "")
        for log in logs
    )


def test_no_warning_when_api_key_set():
    settings = _make_settings(api_key="secret")
    with structlog.testing.capture_logs() as logs:
        warn_if_no_api_key(settings)
    assert not any("REVA_API_KEY" in log.get("detail", "") for log in logs)
