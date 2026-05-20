"""Tests for GET /api/v1/failures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
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


def _seed_review(db, *, status="failed", pr_num=42, sha="deadbeef") -> None:
    repo_id = writers.upsert_repository(
        db, github_repository_id=1001, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=5000 + pr_num, pr_number=pr_num,
        title="PR", author_login="alice", base_branch="main",
        head_branch="feat", head_sha=sha, state="open", draft=False,
    )
    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha=sha,
        installation_id=99, review_mode="diff", trigger_event="opened",
    )
    writers.record_review_started(db, params)
    if status == "failed":
        writers.record_review_failed(db, params, "transient", "Claude 503")
    elif status == "stale":
        writers.record_review_stale(db, params)
    elif status == "completed":
        writers.record_review_completed(
            db, params,
            ReviewResult(status="completed", summary="s", findings=[], risk_level="low"),
        )


def test_failures_empty(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/failures")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_failures_returns_failed_reviews(client_and_db):
    client, db = client_and_db
    _seed_review(db, status="failed")
    resp = client.get("/api/v1/failures")
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["status"] == "failed"
    assert resp.json()["items"][0]["error_message"] == "Claude 503"


def test_failures_includes_stale(client_and_db):
    client, db = client_and_db
    _seed_review(db, status="stale", pr_num=42, sha="aaa")
    _seed_review(db, status="failed", pr_num=43, sha="bbb")
    resp = client.get("/api/v1/failures")
    assert resp.json()["total"] == 2


def test_failures_excludes_completed(client_and_db):
    client, db = client_and_db
    _seed_review(db, status="completed")
    resp = client.get("/api/v1/failures")
    assert resp.json()["total"] == 0


def test_failures_limit_respected(client_and_db):
    client, db = client_and_db
    for i in range(5):
        _seed_review(db, status="failed", pr_num=40 + i, sha=f"sha{i}")
    resp = client.get("/api/v1/failures?limit=2")
    assert len(resp.json()["items"]) == 2
    assert resp.json()["total"] == 5
