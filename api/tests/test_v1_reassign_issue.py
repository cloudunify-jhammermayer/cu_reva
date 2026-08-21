"""Tests for POST /api/v1/reassign-issue (spec 2026-08-20).

The load-bearing rule: this route NEVER returns 404. Odoo's Move-to wizard
reads 404/501 as "REVA has not shipped this yet" and commits the move with a
warning note that would be false.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_github_client, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent
from reva.types import TicketIssueJobParams

REPO = "https://github.com/acme/widgets"

PAYLOAD = {
    "number": 42,
    "repo": REPO,
    "from": {"ticket_id": 1234, "model_name": "project.task"},
    "to": {"ticket_id": 5678, "model_name": "helpdesk.ticket"},
}


@dataclass
class FakeQueue:
    enqueued: list = field(default_factory=list)

    def enqueue(self, func_path, params, **kwargs):
        self.enqueued.append((func_path, params, kwargs))
        return type("J", (), {"id": "rq:job:fake-1"})()


@dataclass
class FakeGitHub:
    installation_id: int = 99

    def get_repo_installation_id(self, owner: str, repo: str) -> int:
        return self.installation_id


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
    app.dependency_overrides[get_github_client] = lambda: FakeGitHub()
    prev = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = FakeQueue()
    tc = TestClient(app)
    key = tc.post("/api/v1/odoo-instances", json={
        "name": "test", "callback_url": "", "callback_api_key": "",
    }).json()["api_key"]
    # Resolve the id rather than assuming 1 — the seeds and union assertions
    # below are instance-scoped, and a wrong id fails as "no issues" rather
    # than as "wrong instance".
    from reva.db.models import OdooInstance
    with db.session() as s:
        instance_id = s.query(OdooInstance).one().id
    yield tc, db, instance_id, {"Authorization": f"Bearer {key}"}
    app.state.rq_queue = prev
    app.dependency_overrides.clear()


def _seed_issue(db: Database, instance_id: int, ticket_id: int = 1234,
                model_name: str = "project.task") -> None:
    """A completed run owning issue #42 on acme/widgets."""
    run_id = writers.record_ticket_issue_run_created(db, TicketIssueJobParams(
        run_id=0, odoo_instance_id=instance_id, ticket_id=ticket_id,
        model_name=model_name,
        github_url=REPO, name="Ticket name", description="d", analysis_html="",
        priority="1", ticket_url="https://odoo.example/web#id=1",
    ))
    writers.update_ticket_issue_progress(db, run_id, [
        {"title": "Issue 42", "number": 42,
         "url": "https://github.com/acme/widgets/issues/42", "state": "open"},
    ])


def _ops(db: Database) -> list[str]:
    with db.session() as s:
        return [e.event for e in s.query(OpsEvent).all()]


def test_move_is_accepted_and_redirects_the_union(client_db):
    client, db, iid, headers = client_db
    _seed_issue(db, iid)

    r = client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    assert r.status_code == 200
    assert r.json()["status"] == "reassigned"
    assert writers.get_ticket_issue_union(db, iid, 1234, "project.task") == []
    moved = writers.get_ticket_issue_union(db, iid, 5678, "helpdesk.ticket")
    assert [i["number"] for i in moved] == [42]


def test_repeating_the_same_move_is_a_noop_200(client_db):
    client, db, iid, headers = client_db
    _seed_issue(db, iid)
    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    r = client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    assert r.status_code == 200
    moved = writers.get_ticket_issue_union(db, iid, 5678, "helpdesk.ticket")
    assert [i["number"] for i in moved] == [42]


def test_stale_from_still_succeeds(client_db):
    """The Odoo wizard retries a move that already happened; `from` is advisory
    and must never 409."""
    client, db, iid, headers = client_db
    _seed_issue(db, iid)
    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    stale = {**PAYLOAD, "from": {"ticket_id": 9999, "model_name": "project.task"}}
    r = client.post("/api/v1/reassign-issue", json=stale, headers=headers)

    assert r.status_code == 200


def test_moving_back_to_the_natural_owner_clears_the_override(client_db):
    client, db, iid, headers = client_db
    _seed_issue(db, iid)
    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    back = {
        "number": 42, "repo": REPO,
        "from": {"ticket_id": 5678, "model_name": "helpdesk.ticket"},
        "to": {"ticket_id": 1234, "model_name": "project.task"},
    }
    r = client.post("/api/v1/reassign-issue", json=back, headers=headers)

    assert r.status_code == 200
    assert r.json()["status"] == "cleared"
    union = writers.get_ticket_issue_union(db, iid, 1234, "project.task")
    assert [i["number"] for i in union] == [42]


def test_unknown_issue_is_200_not_404(client_db):
    """404 is reserved for a REVA that lacks the route entirely — returning it
    here makes Odoo post a warning note that is simply false."""
    client, db, iid, headers = client_db  # no run seeded

    r = client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    assert r.status_code == 200
    assert r.json()["status"] == "unknown_issue"


def test_unknown_issue_records_a_warning_ops_event(client_db):
    client, db, iid, headers = client_db

    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    assert "reassign_unknown_issue" in _ops(db)


def test_accepted_move_records_an_ops_event(client_db):
    client, db, iid, headers = client_db
    _seed_issue(db, iid)

    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    assert "issue_reassigned" in _ops(db)


def test_unparseable_repo_is_422(client_db):
    client, db, iid, headers = client_db
    _seed_issue(db, iid)

    r = client.post(
        "/api/v1/reassign-issue",
        json={**PAYLOAD, "repo": "not-a-url"},
        headers=headers,
    )

    assert r.status_code == 422


def test_missing_from_is_422(client_db):
    client, db, iid, headers = client_db
    payload = {k: v for k, v in PAYLOAD.items() if k != "from"}

    r = client.post("/api/v1/reassign-issue", json=payload, headers=headers)

    assert r.status_code == 422


def test_estimate_gate_accepts_the_new_owner_after_a_move(client_db):
    """/update-issue-estimate 404s an issue the record does not own. After a
    move the target owns it, so the gate must let it through — and the source
    must stop being accepted for it."""
    client, db, iid, headers = client_db
    _seed_issue(db, iid)
    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    accepted = client.post(
        "/api/v1/update-issue-estimate",
        json={"ticket_id": 5678, "model_name": "helpdesk.ticket",
              "number": 42, "estimate_hours": 3.5},
        headers=headers,
    )
    assert accepted.status_code == 202

    rejected = client.post(
        "/api/v1/update-issue-estimate",
        json={"ticket_id": 1234, "model_name": "project.task",
              "number": 42, "estimate_hours": 3.5},
        headers=headers,
    )
    assert rejected.status_code == 404


def test_route_requires_an_instance_key(client_db):
    client, db, iid, headers = client_db
    _seed_issue(db, iid)

    gated = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0", api_key="master-key",
    )
    app.dependency_overrides[get_settings] = lambda: gated
    r = client.post(
        "/api/v1/reassign-issue", json=PAYLOAD,
        headers={"Authorization": "Bearer master-key"},
    )
    assert r.status_code in (401, 403)
