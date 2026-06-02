"""Tests for GET /api/v1/pending — the 'open reviews' tab.

The tab must show every in-flight review: waiting for debounce, enqueued and
waiting for a worker, or actively running. A review enqueued but not yet picked
up (no review_run row yet) must not disappear from the tab.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import PendingReview
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


def _seed_repo_and_pr(db, *, repo_num=1001, pr_num=42, sha="deadbeef") -> tuple[int, int]:
    repo_id = writers.upsert_repository(
        db, github_repository_id=repo_num, owner="acme", name="widgets",
        default_branch="main", installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=5000 + pr_num, pr_number=pr_num,
        title=f"PR #{pr_num}", author_login="alice", base_branch="main",
        head_branch="feat/x", head_sha=sha, state="open", draft=False,
    )
    return repo_id, pr_id


def _add_pending(db, *, repo_id, pr_id, pr_num=42, sha="deadbeef", consumed=False) -> None:
    writers.upsert_pending_review(
        db, repository_id=repo_id, pull_request_id=pr_id, pr_number=pr_num,
        head_sha=sha, installation_id=99, trigger_event="opened",
        review_mode="diff", scheduled_at=datetime.now(timezone.utc),
    )
    if consumed:
        with db.session() as s:
            s.query(PendingReview).filter_by(repository_id=repo_id, pr_number=pr_num).update(
                {"consumed": True}
            )


def _params(repo_id, pr_id, sha="deadbeef") -> JobParams:
    return JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha=sha,
        installation_id=99, review_mode="diff", trigger_event="opened",
    )


def test_pending_shows_debounce_waiting_as_pending(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _add_pending(db, repo_id=repo_id, pr_id=pr_id, consumed=False)

    items = client.get("/api/v1/pending").json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "pending"


def test_pending_shows_enqueued_not_started_as_queued(client_and_db):
    # The gap: consumed (enqueued to RQ) but no review_run row yet. Must remain
    # visible — this is what 'vanished' before the fix.
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _add_pending(db, repo_id=repo_id, pr_id=pr_id, consumed=True)

    items = client.get("/api/v1/pending").json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "queued"
    assert items[0]["repo_full_name"] == "acme/widgets"
    assert items[0]["pr_number"] == 42


def test_pending_shows_running_review(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _add_pending(db, repo_id=repo_id, pr_id=pr_id, consumed=True)
    writers.record_review_started(db, _params(repo_id, pr_id))

    items = client.get("/api/v1/pending").json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "running"


def test_pending_hides_completed_review(client_and_db):
    client, db = client_and_db
    repo_id, pr_id = _seed_repo_and_pr(db)
    _add_pending(db, repo_id=repo_id, pr_id=pr_id, consumed=True)
    params = _params(repo_id, pr_id)
    writers.record_review_started(db, params)
    writers.record_review_completed(
        db, params, ReviewResult(status="completed", summary="ok", findings=[], risk_level="low"),
    )

    items = client.get("/api/v1/pending").json()["items"]
    assert items == []
