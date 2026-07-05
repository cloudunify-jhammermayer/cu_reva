"""GET /api/v1/value-reports."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_redis, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers


@pytest.fixture()
def client_db():
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:",
        github_app_id=1,
        github_webhook_secret="x",
        github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_redis] = lambda: None
    yield TestClient(app), db
    app.dependency_overrides.clear()


def test_latest_value_report_404_when_empty(client_db) -> None:
    client, _db = client_db

    response = client.get("/api/v1/value-reports/latest")

    assert response.status_code == 404


def test_list_and_latest_value_reports(client_db) -> None:
    client, db = client_db
    writers.upsert_value_report(
        db,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        "may",
        {"reviews": 0},
    )
    writers.upsert_value_report(
        db,
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        "june",
        {"reviews": 1},
    )

    listing = client.get("/api/v1/value-reports").json()
    latest = client.get("/api/v1/value-reports/latest").json()

    assert listing["total"] == 2
    assert [item["content_md"] for item in listing["items"]] == ["june", "may"]
    assert latest["content_md"] == "june"
    assert latest["stats"] == {"reviews": 1}

