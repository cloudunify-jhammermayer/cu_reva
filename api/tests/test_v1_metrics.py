"""Tests for GET /api/v1/metrics/*.

Metrics queries use date aggregation that differs between Postgres and SQLite.
These tests verify response shape and 200 status only; exact values are not
asserted because SQLite's strftime approximations may differ from Postgres
date_trunc results.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_redis, get_settings
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
    app.dependency_overrides[get_redis] = lambda: None  # no RQ in tests → 0 workers
    yield TestClient(app), db
    app.dependency_overrides.clear()


def _seed_completed_review(db, *, pr_num=42, sha="abc", repo_num=1001) -> None:
    repo_id = writers.upsert_repository(
        db, github_repository_id=repo_num, owner="acme", name="widgets",
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
    writers.record_review_completed(
        db, params,
        ReviewResult(status="completed", summary="ok", findings=[], risk_level="low"),
    )


# --- dashboard ----------------------------------------------------------------


def test_dashboard_empty_db(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/metrics/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert "last_24h" in data
    assert "last_7d" in data
    assert "findings_24h" in data
    assert "total_cost_7d" in data
    assert "avg_cost_per_review_7d" in data


def test_dashboard_last_24h_shape(client_and_db):
    client, db = client_and_db
    _seed_completed_review(db)
    data = client.get("/api/v1/metrics/dashboard").json()
    p = data["last_24h"]
    assert "reviews_completed" in p
    assert "reviews_failed" in p
    assert "success_rate" in p
    assert "avg_duration_ms" in p


def test_dashboard_counts_completed(client_and_db):
    client, db = client_and_db
    _seed_completed_review(db, pr_num=1, sha="s1")
    _seed_completed_review(db, pr_num=2, sha="s2")
    data = client.get("/api/v1/metrics/dashboard").json()
    assert data["last_24h"]["reviews_completed"] == 2
    assert data["last_24h"]["success_rate"] == 1.0


def test_dashboard_findings_keys(client_and_db):
    client, _ = client_and_db
    fc = client.get("/api/v1/metrics/dashboard").json()["findings_24h"]
    assert set(fc.keys()) == {"critical", "major", "minor", "info"}


# --- developers ---------------------------------------------------------------


def test_developers_empty(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/metrics/developers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_developers_shape(client_and_db):
    client, db = client_and_db
    _seed_completed_review(db)
    items = client.get("/api/v1/metrics/developers").json()
    assert len(items) == 1
    d = items[0]
    assert "author_login" in d
    assert "review_count" in d
    assert "avg_findings" in d
    assert "avg_major_critical" in d
    assert d["trend"] in {"improving", "stable", "worsening"}


def test_developers_review_count_not_inflated_by_findings(client_and_db):
    """CORR-3: review_count counts reviews, not the ReviewFinding join fan-out."""
    from reva.types import Finding

    client, db = client_and_db
    repo_id = writers.upsert_repository(
        db, github_repository_id=1001, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=5042, pr_number=42, title="PR",
        author_login="alice", base_branch="main", head_branch="feat",
        head_sha="abc", state="open", draft=False,
    )
    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha="abc",
        installation_id=99, review_mode="diff", trigger_event="opened",
    )
    writers.record_review_started(db, params)
    writers.record_review_completed(db, params, ReviewResult(
        status="completed", summary="ok", risk_level="high",
        findings=[
            Finding(severity="critical", category="security", title="a", body="b", confidence=0.9),
            Finding(severity="major", category="bug", title="c", body="d", confidence=0.8),
            Finding(severity="minor", category="style", title="e", body="f", confidence=0.7),
        ],
    ))

    d = client.get("/api/v1/metrics/developers").json()[0]
    assert d["review_count"] == 1      # one review — NOT three findings
    assert d["avg_findings"] == 3.0     # 3 findings on that one review
    assert d["avg_major_critical"] == round(2 / 3, 2)  # 2 of 3 are major/critical


# --- cost ---------------------------------------------------------------------


def test_cost_empty(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/metrics/cost")
    assert resp.status_code == 200
    assert resp.json() == []


def test_cost_shape(client_and_db):
    client, db = client_and_db
    _seed_completed_review(db)
    # Reviews without estimated_cost_usd are excluded — none seeded here
    # so the endpoint still returns 200 with empty list.
    resp = client.get("/api/v1/metrics/cost")
    assert resp.status_code == 200


# --- feedback -----------------------------------------------------------------


def test_feedback_empty(client_and_db):
    client, _ = client_and_db
    resp = client.get("/api/v1/metrics/feedback")
    assert resp.status_code == 200
    assert resp.json() == []


def test_feedback_shape_with_findings(client_and_db):
    client, db = client_and_db
    _seed_completed_review(db)
    # Findings were not seeded with finding_count, so feedback will still be empty.
    resp = client.get("/api/v1/metrics/feedback")
    assert resp.status_code == 200
