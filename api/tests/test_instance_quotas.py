"""Per-instance budget, per-instance rate limit, and quota PATCH."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url

BASE_PAYLOAD = {
    "ticket_id": 42,
    "model_name": "helpdesk.ticket",
    "field_name": "x_reva_analysis",
    "text": "The login page is broken.",
}


@dataclass
class FakeJob:
    id: str = "rq:job:fake-1"


@dataclass
class FakeQueue:
    enqueued: list[tuple] = field(default_factory=list)

    def enqueue(self, func_path, params, **kwargs):
        self.enqueued.append((func_path, params, kwargs))
        return FakeJob(id=f"rq:job:fake-{len(self.enqueued)}")


@pytest.fixture()
def client_db_queue(monkeypatch):
    from cryptography.fernet import Fernet

    from app import ratelimit

    ratelimit.reset()
    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
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
    queue = FakeQueue()
    prev_queue = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = queue
    tc = TestClient(app)
    created = tc.post("/api/v1/odoo-instances", json={
        "name": "test", "callback_url": "", "callback_api_key": "",
    }).json()
    yield tc, db, queue, created["id"], {"Authorization": f"Bearer {created['api_key']}"}
    app.state.rq_queue = prev_queue
    app.dependency_overrides.clear()
    ratelimit.reset()


def _burn_budget(db: Database, instance_id: int, cost: float) -> None:
    from reva.db.models import TicketAnalysis

    with db.session() as s:
        s.add(TicketAnalysis(
            odoo_instance_id=instance_id, ticket_id=9, model_name="m",
            field_name="f", input_text="t", status="completed",
            estimated_cost_usd=cost,
        ))


def test_patch_sets_and_clears_quota(client_db_queue):
    client, _, _, iid, _ = client_db_queue
    r = client.patch(
        f"/api/v1/odoo-instances/{iid}",
        json={"daily_budget_usd": 10, "rate_limit_per_minute": 30},
    )
    assert r.status_code == 200
    inst = next(i for i in client.get("/api/v1/odoo-instances").json()["items"]
                if i["id"] == iid)
    assert inst["daily_budget_usd"] == 10
    assert inst["rate_limit_per_minute"] == 30

    assert client.patch(
        f"/api/v1/odoo-instances/{iid}", json={"daily_budget_usd": None}
    ).status_code == 200
    inst = next(i for i in client.get("/api/v1/odoo-instances").json()["items"]
                if i["id"] == iid)
    assert inst["daily_budget_usd"] is None


def test_patch_rejects_negative_budget(client_db_queue):
    client, _, _, iid, _ = client_db_queue
    assert client.patch(
        f"/api/v1/odoo-instances/{iid}", json={"daily_budget_usd": -1}
    ).status_code == 422


def test_submit_429_when_over_budget(client_db_queue):
    client, db, queue, iid, headers = client_db_queue
    client.patch(f"/api/v1/odoo-instances/{iid}", json={"daily_budget_usd": 5})
    _burn_budget(db, iid, cost=6.0)
    r = client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers)
    assert r.status_code == 429
    assert "budget" in r.json()["detail"].lower()
    assert queue.enqueued == []


def test_submit_ok_under_budget(client_db_queue):
    client, db, queue, iid, headers = client_db_queue
    client.patch(f"/api/v1/odoo-instances/{iid}", json={"daily_budget_usd": 5})
    _burn_budget(db, iid, cost=1.0)
    r = client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers)
    assert r.status_code == 202
    assert len(queue.enqueued) == 1


def test_no_budget_means_unlimited(client_db_queue):
    client, db, queue, iid, headers = client_db_queue
    _burn_budget(db, iid, cost=1000.0)
    r = client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers)
    assert r.status_code == 202
    assert len(queue.enqueued) == 1


def test_instance_rate_limit_429(client_db_queue):
    client, _, _, iid, headers = client_db_queue
    client.patch(f"/api/v1/odoo-instances/{iid}", json={"rate_limit_per_minute": 2})
    assert client.post(
        "/api/v1/ticket-analysis", json={**BASE_PAYLOAD, "ticket_id": 1}, headers=headers
    ).status_code == 202
    assert client.post(
        "/api/v1/ticket-analysis", json={**BASE_PAYLOAD, "ticket_id": 2}, headers=headers
    ).status_code == 202
    r = client.post(
        "/api/v1/ticket-analysis", json={**BASE_PAYLOAD, "ticket_id": 3}, headers=headers
    )
    assert r.status_code == 429
