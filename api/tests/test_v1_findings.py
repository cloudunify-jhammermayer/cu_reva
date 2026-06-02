"""Tests for GET /api/v1/findings."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.types import Finding, JobParams, ReviewResult, ReviewStatus


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


def _seed_findings(db, *, severity="major", category="bug", repo_num=1001) -> None:
    repo_id = writers.upsert_repository(
        db, github_repository_id=repo_num, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=5001, pr_number=42,
        title="PR", author_login="alice", base_branch="main",
        head_branch="feat/x", head_sha="deadbeef", state="open", draft=False,
    )
    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
        installation_id=99, review_mode="diff", trigger_event="opened",
    )
    writers.record_review_started(db, params)
    writers.record_review_completed(
        db, params,
        ReviewResult(
            status="completed", summary="s",
            findings=[Finding(severity=severity, category=category,
                              title="Issue", body="body", confidence=0.9)],
            risk_level="low",
        ),
    )


def test_findings_empty(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/findings")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_findings_returns_seeded(client_and_db):
    client, db = client_and_db
    _seed_findings(db)
    resp = client.get("/api/v1/findings")
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["severity"] == "major"


def test_findings_negative_limit_is_clamped(client_and_db):
    """CORR-6/CODE-1: a negative ?limit must be floored to 1, not passed to SQL
    (Postgres 500s on LIMIT -1). min(limit, max) caps but doesn't floor."""
    client, db = client_and_db
    repo_id = writers.upsert_repository(
        db, github_repository_id=1001, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=5001, pr_number=42, title="PR",
        author_login="alice", base_branch="main", head_branch="feat",
        head_sha="deadbeef", state="open", draft=False,
    )
    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
        installation_id=99, review_mode="diff", trigger_event="opened",
    )
    writers.record_review_started(db, params)
    writers.record_review_completed(
        db, params,
        ReviewResult(status="completed", summary="s", risk_level="low",
                     findings=[Finding(severity="major", category="bug", title=f"I{i}",
                                       body="b", confidence=0.9) for i in range(3)]),
    )
    resp = client.get("/api/v1/findings?limit=-1")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1  # floored to 1, not all 3


def test_findings_filter_by_severity(client_and_db):
    client, db = client_and_db
    _seed_findings(db, severity="critical")
    assert client.get("/api/v1/findings?severity=critical").json()["total"] == 1
    assert client.get("/api/v1/findings?severity=minor").json()["total"] == 0


def test_findings_filter_by_category(client_and_db):
    client, db = client_and_db
    _seed_findings(db, category="security")
    assert client.get("/api/v1/findings?category=security").json()["total"] == 1
    assert client.get("/api/v1/findings?category=bug").json()["total"] == 0


def test_findings_filter_by_repo(client_and_db):
    client, db = client_and_db
    _seed_findings(db, repo_num=1001)
    assert client.get("/api/v1/findings?repo=acme/widgets").json()["total"] == 1
    assert client.get("/api/v1/findings?repo=other/repo").json()["total"] == 0
