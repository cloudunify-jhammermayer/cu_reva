"""Tests for GET /api/v1/change-notes (TUI Tickets tab feed)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers


def _settings(**kw) -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        github_app_id=1,
        github_webhook_secret="x",
        github_private_key="x",
        redis_url="redis://localhost:6379/0",
        **kw,
    )


@pytest.fixture()
def client_and_db():
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: _settings()
    yield TestClient(app), db
    app.dependency_overrides.clear()


def _note(db, pr, ticket, title="T") -> int:
    note_id, _ = writers.get_or_create_change_note(
        db, "acme/repo", pr, ticket, 1, "project.task",
        pr_title=title, pr_url=f"https://github.com/acme/repo/pull/{pr}",
    )
    return note_id


def test_empty(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/change-notes")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_two_notes_newest_first_with_all_fields(client_and_db):
    client, db = client_and_db
    first = _note(db, 1, 10, "first")
    second = _note(db, 2, 11, "second")
    writers.record_change_note_modules(db, second, ["cu_auth"], ["extra/sub"])
    writers.record_change_note_completed(db, second, "<p>secret text</p>", 0.25)

    body = client.get("/api/v1/change-notes").json()

    assert body["total"] == 2
    assert [i["id"] for i in body["items"]] == [second, first]
    top, bottom = body["items"]
    assert set(top) == {
        "id", "repo_full_name", "pr_number", "pr_title", "pr_url",
        "odoo_instance_id", "ticket_id", "model_name", "status", "source",
        "modules", "submodules", "error_message", "estimated_cost_usd",
        "created_at", "completed_at", "delivered_at",
    }
    assert "note_html" not in top
    assert top["modules"] == ["cu_auth"]
    assert top["submodules"] == ["extra/sub"]
    assert top["status"] == "completed"
    assert top["estimated_cost_usd"] == pytest.approx(0.25)
    assert top["ticket_id"] == 11 and top["model_name"] == "project.task"
    assert bottom["modules"] is None and bottom["submodules"] is None
    assert bottom["status"] == "pending"


def test_limit_and_offset_page(client_and_db):
    client, db = client_and_db
    ids = [_note(db, n, 10 + n) for n in range(1, 4)]  # oldest first
    newest_first = list(reversed(ids))

    page1 = client.get("/api/v1/change-notes?limit=2&offset=0").json()
    page2 = client.get("/api/v1/change-notes?limit=2&offset=2").json()

    assert page1["total"] == 3 and page2["total"] == 3
    assert [i["id"] for i in page1["items"]] == newest_first[:2]
    assert [i["id"] for i in page2["items"]] == newest_first[2:]


def test_requires_api_key(client_and_db):
    client, _ = client_and_db
    app.dependency_overrides[get_settings] = lambda: _settings(
        api_key="s3cret", require_api_key=True
    )
    assert client.get("/api/v1/change-notes").status_code == 401
    resp = client.get(
        "/api/v1/change-notes", headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401
    resp = client.get(
        "/api/v1/change-notes", headers={"Authorization": "Bearer s3cret"}
    )
    assert resp.status_code == 200
