"""Tests for the timesheet wording review endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url


PAYLOAD = {
    "request_id": "req-1",
    "flagged_words": ["stupid"],
    "lines": [
        {
            "line_id": 1,
            "project_name": "ACME",
            "task_name": "Reports",
            "user_name": "Jo",
            "user_role": "developer",
            "description": "fixed stupid bug",
        }
    ],
}


def _payload(n: int = 1, **overrides):
    lines = [
        {
            **PAYLOAD["lines"][0],
            "line_id": i,
            "description": f"fixed stupid bug {i}",
        }
        for i in range(1, n + 1)
    ]
    payload = {**PAYLOAD, "lines": lines}
    payload.update(overrides)
    return payload


@dataclass
class FakeJob:
    id: str


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
    key = tc.post("/api/v1/odoo-instances", json={
        "name": "test",
        "callback_url": "",
        "callback_api_key": "",
    }).json()["api_key"]
    yield tc, db, queue, {"Authorization": f"Bearer {key}"}
    app.state.rq_queue = prev_queue
    app.dependency_overrides.clear()


def test_submit_enqueues_timesheet_review(client_db_queue):
    client, _, queue, headers = client_db_queue

    r = client.post("/api/v1/timesheet-review", json=_payload(250), headers=headers)

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    func_path, params, kwargs = queue.enqueued[0]
    assert func_path == "worker.timesheet_tasks.run_timesheet_review"
    assert params["request_id"] == "req-1"
    assert params["run_id"] == body["run_id"]
    assert kwargs.get("retry") is not None
    assert kwargs["failure_ttl"] == 7 * 24 * 3600
    assert kwargs["job_timeout"] == 600


def test_large_batch_sets_dynamic_timeout(client_db_queue):
    client, _, queue, headers = client_db_queue

    r = client.post("/api/v1/timesheet-review", json=_payload(650), headers=headers)

    assert r.status_code == 202
    _, _, kwargs = queue.enqueued[0]
    assert kwargs["job_timeout"] == 840


def test_duplicate_submit_dedups_to_one_job(client_db_queue):
    client, _, queue, headers = client_db_queue

    r1 = client.post("/api/v1/timesheet-review", json=PAYLOAD, headers=headers)
    r2 = client.post("/api/v1/timesheet-review", json=PAYLOAD, headers=headers)

    assert r1.json()["run_id"] == r2.json()["run_id"]
    assert len(queue.enqueued) == 1


def test_stale_pending_is_superseded(client_db_queue):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from reva.db import writers
    from reva.db.models import TimesheetReviewRun

    client, db, queue, headers = client_db_queue
    first = client.post("/api/v1/timesheet-review", json=PAYLOAD, headers=headers).json()
    stale = datetime.now(timezone.utc) - timedelta(minutes=61)
    with db.session() as s:
        s.execute(
            update(TimesheetReviewRun)
            .where(TimesheetReviewRun.id == first["run_id"])
            .values(created_at=stale)
        )

    second = client.post("/api/v1/timesheet-review", json=PAYLOAD, headers=headers).json()

    assert second["run_id"] != first["run_id"]
    assert len(queue.enqueued) == 2
    assert writers.get_timesheet_run(db, first["run_id"])["status"] == "failed"


def test_master_can_read_timesheet_review(client_db_queue):
    client, _, _, headers = client_db_queue
    run_id = client.post("/api/v1/timesheet-review", json=PAYLOAD, headers=headers).json()["run_id"]

    detail = client.get(f"/api/v1/timesheet-review/{run_id}").json()
    listing = client.get("/api/v1/timesheet-reviews").json()

    assert detail["request_id"] == "req-1"
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == run_id


def test_invalid_line_role_is_422(client_db_queue):
    client, _, queue, headers = client_db_queue
    payload = {**PAYLOAD, "lines": [{**PAYLOAD["lines"][0], "user_role": "admin"}]}

    r = client.post("/api/v1/timesheet-review", json=payload, headers=headers)

    assert r.status_code == 422
    assert queue.enqueued == []


def test_flagged_word_limits_are_enforced(client_db_queue):
    client, _, queue, headers = client_db_queue
    payload = {**PAYLOAD, "flagged_words": ["x" * 101]}

    r = client.post("/api/v1/timesheet-review", json=payload, headers=headers)

    assert r.status_code == 422
    assert queue.enqueued == []
