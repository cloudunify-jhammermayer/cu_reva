"""Auth scoping: instance keys reach only the two create routes; master key is
rejected there but works everywhere else."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url


@pytest.fixture()
def ctx(monkeypatch):
    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine_from_url(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
        api_key="master-secret", require_api_key=True,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings

    class FakeQueue:
        def __init__(self):
            self.enqueued = []

        def enqueue(self, func_path, params, **kwargs):
            self.enqueued.append((func_path, params, kwargs))
            class J:
                id = f"rq:job:{len(self.enqueued)}"
            return J()

    prev = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = FakeQueue()
    client = TestClient(app)
    # Mint an instance via the admin API (master key).
    h = {"Authorization": "Bearer master-secret"}
    key = client.post("/api/v1/odoo-instances", headers=h, json={
        "name": "ACME", "callback_url": "", "callback_api_key": "",
    }).json()["api_key"]
    yield client, key
    app.state.rq_queue = prev
    app.dependency_overrides.clear()


PAYLOAD = {"ticket_id": 7, "model_name": "helpdesk.ticket",
           "field_name": "x", "text": "hi"}


def test_instance_key_can_create(ctx):
    client, key = ctx
    r = client.post("/api/v1/ticket-analysis",
                    headers={"Authorization": f"Bearer {key}"}, json=PAYLOAD)
    assert r.status_code == 202


def test_master_key_rejected_on_create(ctx):
    client, _ = ctx
    r = client.post("/api/v1/ticket-analysis",
                    headers={"Authorization": "Bearer master-secret"}, json=PAYLOAD)
    assert r.status_code == 401


def test_instance_key_rejected_on_management(ctx):
    client, key = ctx
    r = client.get("/api/v1/odoo-instances", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401


def test_instance_key_rejected_on_read_routes(ctx):
    client, key = ctx
    assert client.get("/api/v1/repos",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert client.get("/api/v1/ticket-analyses",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_analysis_stamps_instance_id(ctx):
    client, key = ctx
    r = client.post("/api/v1/ticket-analysis",
                    headers={"Authorization": f"Bearer {key}"}, json=PAYLOAD)
    assert r.status_code == 202
    # The enqueued job params carry the resolved instance id.
    func_path, params, _ = app.state.rq_queue.enqueued[-1]
    assert params["odoo_instance_id"] >= 1
