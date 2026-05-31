"""Tests for the /api/v1 rate limiter."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url


@pytest.fixture()
def client_with_limit():
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
        rate_limit_per_minute=3,
    )
    import app.ratelimit as rl
    rl.reset()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()
    rl.reset()


def test_requests_within_limit_succeed(client_with_limit):
    for _ in range(3):
        assert client_with_limit.get("/api/v1/reviews").status_code == 200


def test_request_over_limit_returns_429(client_with_limit):
    for _ in range(3):
        client_with_limit.get("/api/v1/reviews")
    assert client_with_limit.get("/api/v1/reviews").status_code == 429


def test_limit_disabled_by_default(client_with_limit):
    """With rate_limit_per_minute=0 the limiter is a no-op."""
    import app.ratelimit as rl
    rl.reset()
    settings = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
        rate_limit_per_minute=0,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    for _ in range(10):
        assert client_with_limit.get("/api/v1/reviews").status_code == 200
