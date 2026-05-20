"""Tests for POST /webhooks/github and GET /health.

Uses FastAPI TestClient + SQLite in-memory. No live network calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url
from reva.db.models import GithubEvent, PendingReview, PullRequest, Repository


# --- helpers ------------------------------------------------------------------


_SECRET = "test_secret"
_DELIVERY = "delivery-abc-123"


def _sig(body: bytes, secret: str = _SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client: TestClient, payload: dict, *, event: str = "pull_request",
          delivery: str = _DELIVERY, secret: str = _SECRET) -> ...:
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": _sig(body, secret),
            "X-GitHub-Event": event,
        },
    )


def _pr_payload(action: str = "opened", draft: bool = False, sha: str = "deadbeef") -> dict:
    return {
        "action": action,
        "installation": {"id": 99},
        "repository": {
            "id": 1001,
            "name": "widgets",
            "full_name": "acme/widgets",
            "default_branch": "main",
            "owner": {"login": "acme"},
        },
        "pull_request": {
            "id": 5001,
            "number": 42,
            "title": "Add feature",
            "state": "open",
            "draft": draft,
            "head": {"sha": sha, "ref": "feat/foo"},
            "base": {"ref": "main"},
            "user": {"login": "alice"},
        },
        "sender": {"login": "alice"},
    }


# --- fixture ------------------------------------------------------------------


@pytest.fixture()
def client_and_db():
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:",
        github_app_id=12345,
        github_webhook_secret=_SECRET,
        github_private_key="fake",
        redis_url="redis://localhost:6379/0",
        debounce_seconds=600,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app), db
    app.dependency_overrides.clear()


# --- webhook tests ------------------------------------------------------------


def test_valid_pr_opened_returns_202(client_and_db):
    client, _ = client_and_db
    resp = _post(client, _pr_payload("opened"))
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}


def test_pr_opened_creates_repo_pr_and_pending_review(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened", sha="aabbccdd"))

    with db.session() as s:
        assert s.query(Repository).count() == 1
        assert s.query(PullRequest).count() == 1
        pending = s.query(PendingReview).one()
        assert pending.head_sha == "aabbccdd"
        assert pending.consumed is False
        assert pending.trigger_event == "opened"
        assert pending.review_mode == "diff"


def test_pr_synchronize_resets_debounce(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened", sha="sha1"), delivery="d1")
    _post(client, _pr_payload("synchronize", sha="sha2"), delivery="d2")

    with db.session() as s:
        # Still only one pending_review row (debounce upsert)
        assert s.query(PendingReview).count() == 1
        pending = s.query(PendingReview).one()
        assert pending.head_sha == "sha2"
        assert pending.trigger_event == "synchronize"


def test_pr_closed_action_does_not_create_pending_review(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("closed"))

    with db.session() as s:
        assert s.query(PendingReview).count() == 0


def test_draft_pr_does_not_create_pending_review(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened", draft=True))

    with db.session() as s:
        assert s.query(PendingReview).count() == 0


def test_ready_for_review_creates_pending_review(client_and_db):
    client, db = client_and_db
    # Draft PR opened — skipped
    _post(client, _pr_payload("opened", draft=True), delivery="d1")
    # Same PR transitions to ready — should be scheduled
    payload = _pr_payload("ready_for_review", draft=False)
    payload["pull_request"]["draft"] = False
    _post(client, payload, delivery="d2")

    with db.session() as s:
        assert s.query(PendingReview).count() == 1
        assert s.query(PendingReview).one().trigger_event == "ready_for_review"


def test_invalid_signature_returns_401(client_and_db):
    client, _ = client_and_db
    payload = _pr_payload()
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Delivery": _DELIVERY,
            "X-Hub-Signature-256": "sha256=badhash",
            "X-GitHub-Event": "pull_request",
        },
    )
    assert resp.status_code == 401


def test_duplicate_delivery_returns_duplicate(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened"), delivery="dup-1")
    resp = _post(client, _pr_payload("synchronize", sha="newsha"), delivery="dup-1")

    assert resp.status_code == 202
    assert resp.json() == {"status": "duplicate"}

    with db.session() as s:
        # Only one github_event row (the first delivery)
        assert s.query(GithubEvent).count() == 1
        # Only one pending_review (the first push's sha)
        pending = s.query(PendingReview).one()
        assert pending.head_sha == "deadbeef"


def test_event_stored_in_github_events(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened"))

    with db.session() as s:
        ev = s.query(GithubEvent).one()
        assert ev.delivery_id == _DELIVERY
        assert ev.event_type == "pull_request"
        assert ev.action == "opened"
        assert ev.repository_full_name == "acme/widgets"
        assert ev.sender_login == "alice"


def test_unknown_event_type_accepted_and_stored(client_and_db):
    client, db = client_and_db
    resp = _post(client, {"action": "labeled"}, event="issues")

    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    with db.session() as s:
        ev = s.query(GithubEvent).one()
        assert ev.event_type == "issues"
        assert s.query(PendingReview).count() == 0


def test_missing_signature_header_returns_422(client_and_db):
    client, _ = client_and_db
    body = json.dumps(_pr_payload()).encode()
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Delivery": _DELIVERY,
            "X-GitHub-Event": "pull_request",
            # X-Hub-Signature-256 intentionally omitted
        },
    )
    assert resp.status_code == 422


# --- health -------------------------------------------------------------------


def test_health_returns_ok(client_and_db):
    client, _ = client_and_db
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": True}
