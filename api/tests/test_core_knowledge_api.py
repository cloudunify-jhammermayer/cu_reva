"""Instance odoo_version + dashboard core-knowledge status."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url
from reva.db.models import CoreKnowledgeVersion


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
    settings = Settings(
        database_url="sqlite:///:memory:",
        github_app_id=1,
        github_webhook_secret="x",
        github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    previous_queue = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = type("Q", (), {"connection": None})()
    yield TestClient(app), db
    app.state.rq_queue = previous_queue
    app.dependency_overrides.clear()


def test_instance_version_patch_and_list(client_db):
    client, _ = client_db
    instance_id = client.post("/api/v1/odoo-instances", json={
        "name": "acme",
        "callback_url": "",
        "callback_api_key": "",
    }).json()["id"]

    assert client.patch(
        f"/api/v1/odoo-instances/{instance_id}",
        json={"odoo_version": "19.0"},
    ).status_code == 200
    instance = next(
        item for item in client.get("/api/v1/odoo-instances").json()["items"]
        if item["id"] == instance_id
    )
    assert instance["odoo_version"] == "19.0"


def test_dashboard_core_knowledge_status(client_db):
    client, db = client_db
    with db.session() as s:
        s.add(CoreKnowledgeVersion(
            odoo_version="19.0",
            modules=625,
            models=2500,
            fields=16000,
            sections=9000,
        ))
    body = client.get("/api/v1/metrics/dashboard").json()
    assert body["core_knowledge"][0]["odoo_version"] == "19.0"
    assert body["core_knowledge"][0]["modules"] == 625


def test_dashboard_core_knowledge_absent_when_not_loaded(client_db):
    client, _ = client_db
    assert client.get("/api/v1/metrics/dashboard").json()["core_knowledge"] == []
