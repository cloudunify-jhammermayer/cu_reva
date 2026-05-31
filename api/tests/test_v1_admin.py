"""Tests for POST /api/v1/admin/review."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_github_client, get_settings
from app.main import app
from app.settings import Settings
from reva._github_http import NotFound
from reva.db import Base, Database, create_engine_from_url
from reva.db.models import PendingReview, PullRequest, Repository


# --- helpers ------------------------------------------------------------------


def _fake_github(pr_number: int = 42) -> MagicMock:
    gh = MagicMock()
    gh.get_installation_token.return_value = "fake-token"
    gh.get_pull_request.return_value = {
        "id": 5001,
        "number": pr_number,
        "title": "Fix old bug",
        "state": "open",
        "draft": False,
        "head": {"sha": "deadbeef", "ref": "feat/old"},
        "base": {"ref": "main"},
        "user": {"login": "alice"},
        "base": {"ref": "main", "repo": {
            "id": 1001,
            "name": "widgets",
            "full_name": "acme/widgets",
            "default_branch": "main",
            "owner": {"login": "acme"},
        }},
    }
    return gh


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
        github_app_id=1,
        github_webhook_secret="x",
        github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_github_client] = lambda: _fake_github()
    yield TestClient(app), db
    app.dependency_overrides.clear()


# --- tests --------------------------------------------------------------------


def test_admin_review_queues_pending_review(client_and_db):
    client, db = client_and_db
    resp = client.post(
        "/api/v1/admin/review",
        json={"owner": "acme", "repo": "widgets", "pr_number": 42, "installation_id": 99},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["head_sha"] == "deadbeef"
    assert body["review_mode"] == "diff"

    with db.session() as s:
        assert s.query(Repository).count() == 1
        assert s.query(PullRequest).count() == 1
        pending = s.query(PendingReview).one()
        assert pending.head_sha == "deadbeef"
        assert pending.trigger_event == "manual"
        assert pending.consumed is False


def test_admin_review_respects_review_mode(client_and_db):
    client, _ = client_and_db
    resp = client.post(
        "/api/v1/admin/review",
        json={"owner": "acme", "repo": "widgets", "pr_number": 42,
              "installation_id": 99, "review_mode": "full"},
    )
    assert resp.status_code == 202
    assert resp.json()["review_mode"] == "full"


def test_admin_review_returns_404_when_pr_not_found(client_and_db):
    client, _ = client_and_db
    gh = _fake_github()
    gh.get_pull_request.side_effect = NotFound("PR not found")
    app.dependency_overrides[get_github_client] = lambda: gh

    resp = client.post(
        "/api/v1/admin/review",
        json={"owner": "acme", "repo": "widgets", "pr_number": 999, "installation_id": 99},
    )
    assert resp.status_code == 404


def test_admin_review_requires_api_key_when_set(client_and_db):
    client, db = client_and_db
    settings = Settings(
        database_url="sqlite:///:memory:",
        github_app_id=1,
        github_webhook_secret="x",
        github_private_key="x",
        redis_url="redis://localhost:6379/0",
        api_key="secret",
    )
    app.dependency_overrides[get_settings] = lambda: settings

    resp = client.post(
        "/api/v1/admin/review",
        json={"owner": "acme", "repo": "widgets", "pr_number": 42, "installation_id": 99},
    )
    assert resp.status_code == 401


def test_auth_fails_closed_when_required_but_key_missing(client_and_db):
    """If auth is required but no key is configured, the API must NOT serve the
    request (fail closed), rather than silently allowing it through."""
    client, _ = client_and_db
    settings = Settings(
        database_url="sqlite:///:memory:",
        github_app_id=1,
        github_webhook_secret="x",
        github_private_key="x",
        redis_url="redis://localhost:6379/0",
        api_key="",
        require_api_key=True,
    )
    app.dependency_overrides[get_settings] = lambda: settings

    resp = client.post(
        "/api/v1/admin/review",
        json={"owner": "acme", "repo": "widgets", "pr_number": 42, "installation_id": 99},
    )
    assert resp.status_code == 503
