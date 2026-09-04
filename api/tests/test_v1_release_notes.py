"""Tests for the release-log lookup endpoints (spec 2026-09-04, R2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.routes.v1 import release_notes as release_notes_route
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import ReleaseNote

PAYLOAD = {
    "release_id": 3275,
    "name": "Lollipop",
    "date": "2026-09-30 00:00:00",
    "model_name": "project.task",
    "task_ids": [7595, 7620],
}


@dataclass
class FakeJob:
    id: str


@dataclass
class FakeQueue:
    enqueued: list[tuple] = field(default_factory=list)
    fail: bool = False

    def enqueue(self, func_path, params, **kwargs):
        if self.fail:
            raise RuntimeError("redis down")
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
        database_url="sqlite:///:memory:",
        github_app_id=1,
        github_webhook_secret="x",
        github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    queue = FakeQueue()
    prev_queue = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = queue
    tc = TestClient(app)
    created = tc.post("/api/v1/odoo-instances", json={
        "name": "wenatex",
        "callback_url": "",
        "callback_api_key": "",
    }).json()
    yield tc, db, queue, {"Authorization": f"Bearer {created['api_key']}"}, created["id"]
    app.state.rq_queue = prev_queue
    app.dependency_overrides.clear()


def test_submit_enqueues_lookup(client_db_queue):
    client, db, queue, headers, instance_id = client_db_queue

    r = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers)

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    func_path, params, kwargs = queue.enqueued[0]
    assert func_path == "worker.release_note_tasks.run_release_note"
    assert params == {
        "note_id": body["note_id"],
        "odoo_instance_id": instance_id,
        "release_id": 3275,
        "release_name": "Lollipop",
        "slug": "lollipop",
        "github_url": None,
    }
    assert kwargs["retry"] is not None
    assert kwargs["failure_ttl"] == 24 * 3600
    row = writers.get_release_note(db, body["note_id"])
    assert row["job_id"] == body["job_id"]
    assert row["status"] == "pending"


def test_github_url_is_passed_to_the_job(client_db_queue):
    client, _, queue, headers, _ = client_db_queue
    payload = {**PAYLOAD, "github_url": "https://github.com/acme/widgets"}

    r = client.post("/api/v1/release-note", json=payload, headers=headers)

    assert r.status_code == 202
    params = queue.enqueued[0][1]
    assert params["github_url"] == "https://github.com/acme/widgets"


def test_empty_github_url_is_none(client_db_queue):
    client, _, queue, headers, _ = client_db_queue
    payload = {**PAYLOAD, "github_url": ""}

    r = client.post("/api/v1/release-note", json=payload, headers=headers)

    assert r.status_code == 202
    params = queue.enqueued[0][1]
    assert params["github_url"] is None


def test_task_ids_optional_and_date_null(client_db_queue):
    client, _, queue, headers, _ = client_db_queue
    payload = {"release_id": 3277, "name": "Marsh Mallow", "date": None,
               "model_name": "project.task", "task_ids": []}

    r = client.post("/api/v1/release-note", json=payload, headers=headers)

    assert r.status_code == 202
    assert queue.enqueued[0][1]["slug"] == "marsh-mallow"


def test_blank_name_is_422(client_db_queue):
    client, _, queue, headers, _ = client_db_queue

    r = client.post("/api/v1/release-note", json={**PAYLOAD, "name": "   "}, headers=headers)

    assert r.status_code == 422
    assert queue.enqueued == []


@pytest.mark.parametrize(
    "name",
    ["../../victim/repo/contents/docs/secret", "a/b", ".hidden"],
)
def test_path_traversal_name_is_422(client_db_queue, name):
    client, _, queue, headers, _ = client_db_queue

    r = client.post("/api/v1/release-note", json={**PAYLOAD, "name": name}, headers=headers)

    assert r.status_code == 422
    assert queue.enqueued == []


def test_duplicate_request_echoes_pending_note_id(client_db_queue):
    client, db, queue, headers, _ = client_db_queue

    first = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers)
    second = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers)

    assert first.json()["note_id"] == second.json()["note_id"]
    assert len(queue.enqueued) == 1


def test_stale_pending_is_superseded(client_db_queue):
    client, db, queue, headers, _ = client_db_queue

    first = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers)
    note_id = first.json()["note_id"]
    with db.session() as s:
        row = s.get(ReleaseNote, note_id)
        row.created_at = datetime.now(timezone.utc) - timedelta(minutes=31)

    second = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers)

    assert second.json()["note_id"] != note_id
    first_row = writers.get_release_note(db, note_id)
    assert first_row["status"] == "failed"
    assert "stale pending lookup superseded" in first_row["error"]
    assert len(queue.enqueued) == 2


def test_dedup_race_echoes_pending_note_id(client_db_queue, monkeypatch):
    # Both requests' pre-check races the insert: neither sees the other's row
    # yet, so the second's insert hits the unique index and its except-branch
    # re-checks for real, finding the first request's now-committed row.
    client, db, queue, headers, _ = client_db_queue
    original = release_notes_route.writers.get_pending_release_note
    calls: list[int] = []

    def racy_get_pending(*args, **kwargs):
        calls.append(1)
        if len(calls) <= 2:
            return None
        return original(*args, **kwargs)

    monkeypatch.setattr(release_notes_route.writers, "get_pending_release_note", racy_get_pending)

    first = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers)
    second = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers)

    assert second.status_code == 202
    assert second.json()["note_id"] == first.json()["note_id"]
    assert len(queue.enqueued) == 1


def test_requires_instance_key(client_db_queue):
    client, *_ = client_db_queue
    assert client.post("/api/v1/release-note", json=PAYLOAD).status_code == 401


def test_queue_down_marks_row_failed_and_503(client_db_queue):
    client, _, queue, headers, _ = client_db_queue
    queue.fail = True

    r = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers)

    assert r.status_code == 503
    listing = client.get("/api/v1/release-notes").json()
    assert listing["total"] == 1
    assert listing["items"][0]["status"] == "failed"
    assert "enqueue failed" in listing["items"][0]["error"]


def test_master_lists_release_notes(client_db_queue):
    client, _, _, headers, _ = client_db_queue
    note_id = client.post("/api/v1/release-note", json=PAYLOAD, headers=headers).json()["note_id"]

    listing = client.get("/api/v1/release-notes").json()

    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["id"] == note_id
    assert (item["release_name"], item["slug"], item["status"]) == ("Lollipop", "lollipop", "pending")
    assert item["url"] is None and item["error"] is None
    assert client.get("/api/v1/release-notes?status=completed").json()["total"] == 0
