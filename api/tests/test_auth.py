"""Tests for /api/v1 bearer-token auth (TEST-2).

The other v1 tests run with auth disabled (no api_key). These pin the
require_api_key gate so a regression in compare_digest / router wiring fails.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url


@pytest.fixture()
def client():
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
        api_key="s3cret", require_api_key=True,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_missing_token_is_401(client):
    assert client.get("/api/v1/repos").status_code == 401


def test_wrong_token_is_401(client):
    r = client.get("/api/v1/repos", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_correct_token_is_200(client):
    r = client.get("/api/v1/repos", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
