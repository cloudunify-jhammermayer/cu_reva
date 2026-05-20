"""Tests for GET /api/v1/reviews and GET /api/v1/reviews/{id}."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import ReviewFinding, ReviewRun
from reva.types import Finding, JobParams, ReviewResult


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
    yield TestClient(app), db
    app.dependency_overrides.clear()


# --- seed helpers -------------------------------------------------------------


def _seed_repo_and_pr(db, *, repo_num=1001, pr_num=42) -> tuple[int, int]:
    repo_id = writers.upsert_repository(
        db, github_repository_id=repo_num, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=5000 + pr_num, pr_number=pr_num,
        title=f"PR #{pr_num}", author_login="alice", base_branch="main",
        head_branch="feat/x", head_sha="deadbeef", state="open", draft=False,
    )
    return repo_id, pr_id


def _seed_review(
    db, *, repo_id: int, pr_id: int, pr_num: int = 42,
    status: str = "completed", finding_count: int = 0,
    author: str = "alice",
) -> int:
    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id,
        head_sha="deadbeef", installation_id=99,
        review_mode="diff", trigger_event="opened",
    )
    rr_id = writers.record_review_started(db, params)
    if status == "completed":
        findings = [
            Finding(severity="major", category="bug", title=f"Bug {i}", body="details",
                    confidence=0.9)
            for i in range(finding_count)
        ]
        result = ReviewResult(
            status="completed", summary="Looks OK",
            findings=findings, risk_level="low",
        )
        writers.record_review_completed(db, params, result)
    elif status == "failed":
        writers.record_review_failed(db, params, "transient", "Claude 503")
    elif status == "declined":
        writers.record_review_declined(db, params, "diff too large")
    return rr_id


# --- tests --------------------------------------------------------------------


def test_reviews_empty_returns_empty_list(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/reviews")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_reviews_returns_completed_review(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id)

    resp = client.get("/api/v1/reviews")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["status"] == "completed"
    assert item["repo_full_name"] == "acme/widgets"
    assert item["pr_number"] == 42
    assert item["author_login"] == "alice"


def test_reviews_filter_by_repo(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db, repo_num=1001)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id)

    resp = client.get("/api/v1/reviews?repo=acme/widgets")
    assert resp.json()["total"] == 1

    resp = client.get("/api/v1/reviews?repo=other/repo")
    assert resp.json()["total"] == 0


def test_reviews_filter_by_status(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id, status="completed")

    resp = client.get("/api/v1/reviews?status=completed")
    assert resp.json()["total"] == 1

    resp = client.get("/api/v1/reviews?status=failed")
    assert resp.json()["total"] == 0


def test_reviews_filter_by_status_csv(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id, status="completed")

    resp = client.get("/api/v1/reviews?status=completed,failed")
    assert resp.json()["total"] == 1


def test_reviews_pagination(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id)

    resp = client.get("/api/v1/reviews?limit=10&offset=0")
    assert resp.json()["total"] == 1

    resp = client.get("/api/v1/reviews?limit=10&offset=1")
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 1


def test_review_detail_404_unknown_id(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/reviews/9999")
    assert resp.status_code == 404


def test_review_detail_returns_findings(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id, status="completed", finding_count=3)

    # Get the review id.
    with db.session() as s:
        rr = s.query(ReviewRun).one()
        rr_id = rr.id

    resp = client.get(f"/api/v1/reviews/{rr_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["summary"] == "Looks OK"
    assert len(data["findings"]) == 3


def test_review_detail_findings_sorted_by_severity(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)

    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha="deadbeef",
        installation_id=99, review_mode="diff", trigger_event="opened",
    )
    writers.record_review_started(db, params)
    result = ReviewResult(
        status="completed", summary="s",
        findings=[
            Finding(severity="info", category="style", title="Info finding", body="b", confidence=0.5),
            Finding(severity="critical", category="security", title="Crit finding", body="b", confidence=0.99),
            Finding(severity="minor", category="bug", title="Minor finding", body="b", confidence=0.7),
        ],
        risk_level="high",
    )
    writers.record_review_completed(db, params, result)

    with db.session() as s:
        rr_id = s.query(ReviewRun).one().id

    resp = client.get(f"/api/v1/reviews/{rr_id}")
    severities = [f["severity"] for f in resp.json()["findings"]]
    assert severities == ["critical", "minor", "info"]


def test_review_detail_thumbs_counts_default_zero(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id, finding_count=1)

    with db.session() as s:
        rr_id = s.query(ReviewRun).one().id

    resp = client.get(f"/api/v1/reviews/{rr_id}")
    finding = resp.json()["findings"][0]
    assert finding["thumbs_up"] == 0
    assert finding["thumbs_down"] == 0


def test_reviews_filter_by_author(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _seed_review(db, repo_id=repo_id, pr_id=pr_id)

    resp = client.get("/api/v1/reviews?author=alice")
    assert resp.json()["total"] == 1

    resp = client.get("/api/v1/reviews?author=bob")
    assert resp.json()["total"] == 0


def test_reviews_limit_capped_at_200(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/reviews?limit=9999")
    assert resp.status_code == 200  # capped internally, doesn't error
