"""Tests for the ticket-analysis endpoints, focused on attachment intake.

Ticket-analysis is text-first; an optional .docx/.pdf/.txt attachment may be
sent alongside `text` (extracted and folded in by the worker). Unsupported or
malformed attachments are rejected at accept time (422) so Odoo shows the error.
"""

from __future__ import annotations

import base64
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
    key = tc.post("/api/v1/odoo-instances", json={
        "name": "test", "callback_url": "", "callback_api_key": "",
    }).json()["api_key"]
    yield tc, db, queue, {"Authorization": f"Bearer {key}"}
    app.state.rq_queue = prev_queue
    app.dependency_overrides.clear()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_text_only_request_still_accepted(client_db_queue):
    client, _, queue, headers = client_db_queue
    r = client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers)
    assert r.status_code == 202
    _, params, _ = queue.enqueued[0]
    assert params["text"] == BASE_PAYLOAD["text"]
    assert params["attachment"] is None


def test_txt_attachment_accepted_and_passed_to_job(client_db_queue):
    client, _, queue, headers = client_db_queue
    payload = {
        **BASE_PAYLOAD,
        "attachment": {"filename": "notes.txt", "content_base64": _b64(b"extra detail")},
    }
    r = client.post("/api/v1/ticket-analysis", json=payload, headers=headers)
    assert r.status_code == 202
    _, params, _ = queue.enqueued[0]
    assert params["attachment"]["filename"] == "notes.txt"


def test_pdf_attachment_accepted(client_db_queue):
    client, _, _, headers = client_db_queue
    payload = {
        **BASE_PAYLOAD,
        "attachment": {"filename": "spec.pdf", "content_base64": _b64(b"%PDF-1.4\nminimal")},
    }
    assert client.post("/api/v1/ticket-analysis", json=payload, headers=headers).status_code == 202


def test_unsupported_attachment_type_is_422(client_db_queue):
    client, _, queue, headers = client_db_queue
    payload = {
        **BASE_PAYLOAD,
        "attachment": {"filename": "photo.png", "content_base64": _b64(b"\x89PNG\r\n")},
    }
    r = client.post("/api/v1/ticket-analysis", json=payload, headers=headers)
    assert r.status_code == 422
    assert "attachment" in r.json()["detail"]
    assert queue.enqueued == []


def test_attachment_invalid_base64_is_422(client_db_queue):
    client, _, queue, headers = client_db_queue
    payload = {
        **BASE_PAYLOAD,
        "attachment": {"filename": "notes.txt", "content_base64": "%%%not base64%%%"},
    }
    r = client.post("/api/v1/ticket-analysis", json=payload, headers=headers)
    assert r.status_code == 422
    assert queue.enqueued == []
