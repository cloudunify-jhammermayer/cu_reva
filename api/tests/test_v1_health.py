"""GET /api/v1/health — authenticated connection test.

Unlike the root /health (unauthenticated readiness probe), this verifies the
caller's credential: master REVA_API_KEY or per-instance Odoo key, reporting
which one matched. Auth posture mirrors require_api_key (fail-closed when
auth is required but unconfigured; open only in explicit dev mode).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url

_MASTER = "test-master-key"


def _settings(**overrides) -> Settings:
    return Settings(**{
        "database_url": "sqlite:///:memory:",
        "github_app_id": 1,
        "github_webhook_secret": "x",
        "github_private_key": "x",
        "redis_url": "redis://localhost:6379/0",
        **overrides,
    })


@pytest.fixture()
def client_db(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: _settings(api_key=_MASTER)
    yield TestClient(app), db
    app.dependency_overrides.clear()


@pytest.fixture()
def instance_key(client_db):
    client, _ = client_db
    return client.post(
        "/api/v1/odoo-instances",
        json={"name": "test", "callback_url": "", "callback_api_key": ""},
        headers={"Authorization": f"Bearer {_MASTER}"},
    ).json()["api_key"]


def test_master_key_ok(client_db):
    client, _ = client_db
    r = client.get("/api/v1/health", headers={"Authorization": f"Bearer {_MASTER}"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "authenticated_as": "master", "instance": None}


def test_instance_key_ok(client_db, instance_key):
    client, _ = client_db
    r = client.get("/api/v1/health", headers={"Authorization": f"Bearer {instance_key}"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "authenticated_as": "instance", "instance": "test"}


def test_wrong_key_is_401(client_db):
    client, _ = client_db
    r = client.get("/api/v1/health", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_missing_header_is_401(client_db):
    client, _ = client_db
    assert client.get("/api/v1/health").status_code == 401


def test_inactive_instance_key_is_401(client_db, instance_key):
    client, _ = client_db
    iid = client.get(
        "/api/v1/odoo-instances", headers={"Authorization": f"Bearer {_MASTER}"}
    ).json()["items"][0]["id"]
    client.patch(
        f"/api/v1/odoo-instances/{iid}", json={"active": False},
        headers={"Authorization": f"Bearer {_MASTER}"},
    )
    r = client.get("/api/v1/health", headers={"Authorization": f"Bearer {instance_key}"})
    assert r.status_code == 401


def test_fail_closed_when_required_but_unconfigured(client_db):
    client, _ = client_db
    app.dependency_overrides[get_settings] = lambda: _settings(
        api_key="", require_api_key=True
    )
    assert client.get("/api/v1/health").status_code == 503


def test_dev_mode_open_when_no_key_configured(client_db):
    client, _ = client_db
    app.dependency_overrides[get_settings] = lambda: _settings(api_key="")
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["authenticated_as"] == "unauthenticated"
