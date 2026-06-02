"""Tests for GET /api/v1/repos."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from unittest.mock import MagicMock

from app.dependencies import get_db, get_queue, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
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
