"""Auth scoping: instance keys reach the create routes and the shared per-run
GET/requeue routes (scoped to their own rows); the master key is rejected on
create but works unscoped everywhere else."""

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


# --- shared gate: per-run GET/requeue (Odoo self-heal polls these) -----------


def _submit_analysis(client, key) -> int:
    r = client.post("/api/v1/ticket-analysis",
                    headers={"Authorization": f"Bearer {key}"}, json=PAYLOAD)
    assert r.status_code == 202
    return r.json()["analysis_id"]


def _mint_instance(client, name: str) -> str:
    r = client.post("/api/v1/odoo-instances",
                    headers={"Authorization": "Bearer master-secret"},
                    json={"name": name, "callback_url": "", "callback_api_key": ""})
    return r.json()["api_key"]


def test_instance_key_reads_own_analysis(ctx):
    client, key = ctx
    aid = _submit_analysis(client, key)
    r = client.get(f"/api/v1/ticket-analysis/{aid}",
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert r.json()["status"] == "pending"


def test_instance_key_gets_404_on_other_instances_analysis(ctx):
    client, key = ctx
    aid = _submit_analysis(client, key)
    other = _mint_instance(client, "OTHER")
    r = client.get(f"/api/v1/ticket-analysis/{aid}",
                   headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 404  # scoped: cross-instance ids are not probeable


def test_master_key_reads_any_analysis_unscoped(ctx):
    client, key = ctx
    aid = _submit_analysis(client, key)
    r = client.get(f"/api/v1/ticket-analysis/{aid}",
                   headers={"Authorization": "Bearer master-secret"})
    assert r.status_code == 200


def test_requeue_scoped_to_owning_instance(ctx):
    client, key = ctx
    aid = _submit_analysis(client, key)
    other = _mint_instance(client, "OTHER2")
    # Cross-instance: invisible (404). Own instance: visible — the fresh
    # pending row is not requeueable, so the guard answers 409, not 404.
    assert client.post(f"/api/v1/ticket-analysis/{aid}/requeue",
                       headers={"Authorization": f"Bearer {other}"}).status_code == 404
    assert client.post(f"/api/v1/ticket-analysis/{aid}/requeue",
                       headers={"Authorization": f"Bearer {key}"}).status_code == 409


def test_issue_run_get_and_requeue_scoped(ctx):
    from reva.db import writers
    from reva.types import TicketIssueJobParams

    client, key = ctx
    other = _mint_instance(client, "OTHER3")
    # Seed a run owned by instance 1 (the fixture's ACME) via the writer the
    # create route uses; the shared routes are what's under test here.
    db = app.dependency_overrides[get_db]()
    run_id = writers.record_ticket_issue_run_created(db, TicketIssueJobParams(
        run_id=0, odoo_instance_id=1, ticket_id=7, model_name="helpdesk.ticket",
        github_url="https://github.com/acme/widgets", name="T", description="d",
        analysis_html="", priority="1", ticket_url="https://odoo.example.com/7",
    ))
    own = {"Authorization": f"Bearer {key}"}
    theirs = {"Authorization": f"Bearer {other}"}
    assert client.get(f"/api/v1/create-issues/{run_id}", headers=own).status_code == 200
    assert client.get(f"/api/v1/create-issues/{run_id}", headers=theirs).status_code == 404
    assert client.post(f"/api/v1/create-issues/{run_id}/requeue",
                       headers=theirs).status_code == 404
    # Own requeue passes scoping and hits the status guard (pending → 409).
    assert client.post(f"/api/v1/create-issues/{run_id}/requeue",
                       headers=own).status_code == 409


def test_garbage_key_rejected_on_shared_routes(ctx):
    client, _ = ctx
    r = client.get("/api/v1/ticket-analysis/1",
                   headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
