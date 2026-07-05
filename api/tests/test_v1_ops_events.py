"""GET /api/v1/ops-events and dashboard degradation counter."""

from __future__ import annotations

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
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_redis] = lambda: None
    yield TestClient(app), db
    app.dependency_overrides.clear()


def _seed(db):
    writers.record_ops_event(
        db, "codegraph", "warning", "index_failed", {"repo": "acme/widgets"}
    )
    writers.record_ops_event(
        db, "odoo_callback", "error", "write_field_failed", {"analysis_id": 7}
    )


def test_list_all(client_db):
    client, db = client_db
    _seed(db)
    r = client.get("/api/v1/ops-events")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["items"][0]["event"] == "write_field_failed"
    assert body["items"][0]["detail"] == {"analysis_id": 7}


def test_filters(client_db):
    client, db = client_db
    _seed(db)
    assert client.get("/api/v1/ops-events?component=codegraph").json()["total"] == 1
    assert client.get("/api/v1/ops-events?severity=error").json()["total"] == 1
    assert client.get("/api/v1/ops-events?component=nope").json()["total"] == 0


def test_dashboard_degradations_counter(client_db):
    client, db = client_db
    _seed(db)
    r = client.get("/api/v1/metrics/dashboard")
    assert r.status_code == 200
    assert r.json()["degradations_24h"] == 2
