"""Tests for GET /api/v1/repos."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from unittest.mock import MagicMock

from app.dependencies import get_db, get_github_client, get_queue, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.errors import PermanentError
from reva.types import JobParams, ReviewResult


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
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app), db
    app.dependency_overrides.clear()


def test_repos_empty(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/repos")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_repos_returns_seeded(client_and_db):
    client, db = client_and_db
    writers.upsert_repository(
        db, github_repository_id=1001, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    resp = client.get("/api/v1/repos")
    assert resp.json()["total"] == 1
    item = resp.json()["items"][0]
    assert item["full_name"] == "acme/widgets"
    assert item["owner"] == "acme"
    assert item["enabled"] is True


def test_repos_review_count_is_accurate(client_and_db):
    client, db = client_and_db
    repo_id = writers.upsert_repository(
        db, github_repository_id=1001, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=5001, pr_number=42,
        title="PR", author_login="alice", base_branch="main",
        head_branch="feat", head_sha="abc", state="open", draft=False,
    )
    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha="abc",
        installation_id=99, review_mode="diff", trigger_event="opened",
    )
    writers.record_review_started(db, params)
    writers.record_review_completed(
        db, params,
        ReviewResult(status="completed", summary="s",
                     findings=[], risk_level="low"),
    )

    item = client.get("/api/v1/repos").json()["items"][0]
    assert item["review_count"] == 1
    assert item["last_review_at"] is not None


def test_repos_no_reviews_has_zero_count(client_and_db):
    client, db = client_and_db
    writers.upsert_repository(
        db, github_repository_id=1001, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    item = client.get("/api/v1/repos").json()["items"][0]
    assert item["review_count"] == 0
    assert item["last_review_at"] is None


# --- POST /repos/{id}/audit (CORR-1) ------------------------------------------

def test_trigger_audit_enqueues_by_string_path(client_and_db):
    """CORR-1: the api image installs only reva + api/app (no worker package),
    so importing worker.audit_tasks in the handler raises ModuleNotFoundError on
    the first real POST. The job must be enqueued by string path (resolved on the
    worker), like every other api enqueue site — the api must never import worker."""
    client, db = client_and_db
    repo_id = writers.upsert_repository(
        db, github_repository_id=1001, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    fake_queue = MagicMock()
    fake_queue.enqueue.return_value = MagicMock(id="job-123")
    app.dependency_overrides[get_queue] = lambda: fake_queue

    resp = client.post(f"/api/v1/repos/{repo_id}/audit")

    assert resp.status_code == 202
    assert resp.json()["job_id"] == "job-123"
    assert resp.json()["repository_id"] == repo_id
    # enqueued by string path, never by importing the worker package into the api
    assert fake_queue.enqueue.call_args.args[0] == "worker.audit_tasks.run_audit"
    job_params = fake_queue.enqueue.call_args.args[1]
    assert job_params == {"repository_id": repo_id, "installation_id": 99}


def test_trigger_audit_returns_404_for_unknown_repo(client_and_db):
    client, _ = client_and_db
    app.dependency_overrides[get_queue] = lambda: MagicMock()
    resp = client.post("/api/v1/repos/999/audit")
    assert resp.status_code == 404


# --- POST /repos (manual registration of an app-installed repo) ---------------

class _FakeGitHub:
    def __init__(self, installed=True):
        self.installed = installed

    def get_repo_installation_id(self, owner, repo):
        if not self.installed:
            raise PermanentError("404 — app not installed")
        return 9090

    def get_installation_token(self, installation_id):
        return "ghs_tok"

    def get_repo(self, token, owner, repo):
        return {
            "id": 555, "full_name": f"{owner}/{repo}", "name": repo,
            "owner": {"login": owner}, "default_branch": "develop",
        }


def test_add_repo_registers_and_lists(client_and_db):
    client, _ = client_and_db
    app.dependency_overrides[get_github_client] = lambda: _FakeGitHub(installed=True)

    resp = client.post("/api/v1/repos", json={"owner": "acme", "name": "widgets"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["full_name"] == "acme/widgets"
    assert data["installation_id"] == 9090
    assert data["default_branch"] == "develop"

    rid = data["repository_id"]
    listing = client.get("/api/v1/repos").json()
    assert any(it["id"] == rid and it["full_name"] == "acme/widgets" for it in listing["items"])


def test_add_repo_is_idempotent(client_and_db):
    client, _ = client_and_db
    app.dependency_overrides[get_github_client] = lambda: _FakeGitHub(installed=True)
    first = client.post("/api/v1/repos", json={"owner": "acme", "name": "widgets"}).json()
    second = client.post("/api/v1/repos", json={"owner": "acme", "name": "widgets"}).json()
    assert first["repository_id"] == second["repository_id"]
    assert client.get("/api/v1/repos").json()["total"] == 1


def test_add_repo_404_when_app_not_installed(client_and_db):
    client, _ = client_and_db
    app.dependency_overrides[get_github_client] = lambda: _FakeGitHub(installed=False)
    resp = client.post("/api/v1/repos", json={"owner": "acme", "name": "ghost"})
    assert resp.status_code == 404
