"""Tests for POST /api/v1/ticket-actuals (Odoo pushes timesheet totals on
ticket-done — the actuals side of the estimate-calibration loop)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers

PAYLOAD = {
    "ticket_id": 123,
    "model_name": "helpdesk.ticket",
    "actual_hours": 7.5,
    "timesheet_line_count": 4,
}


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
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    tc = TestClient(app)
    key = tc.post("/api/v1/odoo-instances", json={
        "name": "test", "callback_url": "", "callback_api_key": "",
    }).json()["api_key"]
    yield tc, db, {"Authorization": f"Bearer {key}"}
    app.dependency_overrides.clear()


def test_records_actuals_row(client_db):
    client, db, headers = client_db

    r = client.post("/api/v1/ticket-actuals", json=PAYLOAD, headers=headers)

    assert r.status_code == 200
    assert r.json() == {"status": "recorded"}
    row = writers.get_ticket_actuals(db, 1, 123, "helpdesk.ticket")
    assert row["actual_hours"] == 7.5
    assert row["timesheet_line_count"] == 4


def test_resend_replaces_totals_latest_wins(client_db):
    """A re-done ticket re-sends its totals — one row per (instance, ticket),
    the latest push wins."""
    client, db, headers = client_db

    client.post("/api/v1/ticket-actuals", json=PAYLOAD, headers=headers)
    r = client.post(
        "/api/v1/ticket-actuals",
        json={**PAYLOAD, "actual_hours": 9.0, "timesheet_line_count": 6},
        headers=headers,
    )

    assert r.status_code == 200
    row = writers.get_ticket_actuals(db, 1, 123, "helpdesk.ticket")
    assert row["actual_hours"] == 9.0
    assert row["timesheet_line_count"] == 6


def test_requires_instance_key(client_db):
    client, db, _ = client_db
    r = client.post("/api/v1/ticket-actuals", json=PAYLOAD)
    assert r.status_code == 401
    assert writers.get_ticket_actuals(db, 1, 123, "helpdesk.ticket") is None


def test_negative_hours_rejected(client_db):
    client, _, headers = client_db
    r = client.post(
        "/api/v1/ticket-actuals",
        json={**PAYLOAD, "actual_hours": -1.0},
        headers=headers,
    )
    assert r.status_code == 422


def test_line_count_optional(client_db):
    client, db, headers = client_db
    payload = {k: v for k, v in PAYLOAD.items() if k != "timesheet_line_count"}

    r = client.post("/api/v1/ticket-actuals", json=payload, headers=headers)

    assert r.status_code == 200
    row = writers.get_ticket_actuals(db, 1, 123, "helpdesk.ticket")
    assert row["actual_hours"] == 7.5
    assert row["timesheet_line_count"] is None
