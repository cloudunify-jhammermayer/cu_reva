"""CRUD for /api/v1/odoo-instances (master-key, admin-only)."""

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
def client(monkeypatch):
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
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_create_returns_plaintext_key_once(client):
    r = client.post("/api/v1/odoo-instances", json={
        "name": "ACME", "callback_url": "https://odoo.acme/write-field",
        "callback_api_key": "outbound-secret",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["api_key"].startswith("reva_odoo_")
    assert body["name"] == "ACME"
    instance_id = body["id"]

    # GET never returns the secret.
    lst = client.get("/api/v1/odoo-instances").json()
    assert "api_key" not in lst["items"][0]
    assert lst["items"][0]["key_prefix"].startswith("reva_odoo_")
    assert "cost" in lst["items"][0]

    # Rotate mints a new key.
    rot = client.post(f"/api/v1/odoo-instances/{instance_id}/rotate-key")
    assert rot.status_code == 200
    assert rot.json()["api_key"] != body["api_key"]


def test_patch_toggles_active(client):
    iid = client.post("/api/v1/odoo-instances", json={
        "name": "ACME", "callback_url": "", "callback_api_key": "",
    }).json()["id"]
    r = client.patch(f"/api/v1/odoo-instances/{iid}", json={"active": False})
    assert r.status_code == 200
    assert client.get("/api/v1/odoo-instances").json()["items"][0]["active"] is False


def test_delete_removes_instance_and_detaches_history(client):
    from reva.db.models import ChangeNote, TicketAnalysis

    iid = client.post("/api/v1/odoo-instances", json={
        "name": "ACME", "callback_url": "", "callback_api_key": "",
    }).json()["id"]

    db = app.dependency_overrides[get_db]()
    with db.session() as s:
        s.add(TicketAnalysis(
            ticket_id=7, model_name="helpdesk.ticket", field_name="description",
            odoo_instance_id=iid, input_text="x",
        ))
        s.add(ChangeNote(
            repo_full_name="acme/repo", pr_number=1, ticket_id=7,
            odoo_instance_id=iid, model_name="helpdesk.ticket",
        ))

    r = client.delete(f"/api/v1/odoo-instances/{iid}")
    assert r.status_code == 200
    assert r.json() == {"id": iid, "deleted": True}
    assert client.get("/api/v1/odoo-instances").json()["total"] == 0

    with db.session() as s:
        analysis = s.query(TicketAnalysis).one()
        assert analysis.odoo_instance_id is None
        assert s.query(ChangeNote).count() == 0

    # Second delete: the row is gone.
    assert client.delete(f"/api/v1/odoo-instances/{iid}").status_code == 404


def test_create_requires_secret_key_when_outbound_set(client, monkeypatch):
    monkeypatch.delenv("REVA_SECRET_KEY", raising=False)
    r = client.post("/api/v1/odoo-instances", json={
        "name": "NoSecret", "callback_url": "https://x/write-field",
        "callback_api_key": "outbound-secret",
    })
    assert r.status_code == 400
