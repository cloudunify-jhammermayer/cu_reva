"""Per-author review spend writer and the global-ledger kind exclusion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import ReviewRun
from reva.types import JobParams


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


_REPO_IDS = {"one": 1, "two": 2}


def _repo(db: Database, name: str) -> int:
    return writers.upsert_repository(
        db, github_repository_id=_REPO_IDS[name], owner="acme", name=name,
        default_branch="main", installation_id=500,
    )


def _pr(db: Database, repo_id: int, number: int, author: str | None) -> int:
    return writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=repo_id * 1000 + number,
        pr_number=number, title="t", author_login=author, base_branch="main",
        head_branch="f", head_sha=f"sha{number}", state="open", draft=False,
    )


def _run(db: Database, repo_id: int, pr_id: int, cost: float | None, *,
         status: str = "completed", hours_old: float = 0) -> None:
    with db.session() as s:
        s.add(ReviewRun(
            repository_id=repo_id, pull_request_id=pr_id, head_sha=f"h{pr_id}{cost}{status}",
            status=status, trigger_event="opened", review_mode="diff",
            estimated_cost_usd=cost,
            completed_at=datetime.now(timezone.utc) - timedelta(hours=hours_old),
        ))


SINCE = datetime.now(timezone.utc) - timedelta(days=1)


def test_author_sum_spans_repos_for_same_login(db):
    r1, r2 = _repo(db, "one"), _repo(db, "two")
    p1, p2 = _pr(db, r1, 1, "alice"), _pr(db, r2, 7, "alice")
    _run(db, r1, p1, 3.0)
    _run(db, r2, p2, 4.5)
    assert writers.sum_author_review_cost_since(db, p1, SINCE) == pytest.approx(7.5)


def test_author_sum_ignores_other_authors(db):
    r = _repo(db, "one")
    pa, pb = _pr(db, r, 1, "alice"), _pr(db, r, 2, "bob")
    _run(db, r, pa, 3.0)
    _run(db, r, pb, 40.0)
    assert writers.sum_author_review_cost_since(db, pa, SINCE) == pytest.approx(3.0)


def test_author_sum_counts_paid_rows_regardless_of_status_and_ignores_old(db):
    r = _repo(db, "one")
    p = _pr(db, r, 1, "alice")
    _run(db, r, p, 3.0)
    _run(db, r, p, 50.0, status="declined")
    _run(db, r, p, 60.0, status="failed")
    _run(db, r, p, None, status="declined")
    _run(db, r, p, None, status="running")
    _run(db, r, p, 70.0, hours_old=25)
    assert writers.sum_author_review_cost_since(db, p, SINCE) == pytest.approx(113.0)


def test_author_sum_survives_decline_on_reviewed_sha(db):
    r = _repo(db, "one")
    p = _pr(db, r, 1, "alice")
    head_sha = "deadbeef"
    with db.session() as s:
        s.add(ReviewRun(
            repository_id=r, pull_request_id=p, head_sha=head_sha,
            status="completed", trigger_event="opened", review_mode="diff",
            estimated_cost_usd=3.0,
            completed_at=datetime.now(timezone.utc),
        ))
    writers.record_review_declined(
        db,
        JobParams(
            repository_id=r, pull_request_id=p, head_sha=head_sha,
            installation_id=500, review_mode="diff", trigger_event="comment",
        ),
        "over budget",
    )
    assert writers.sum_author_review_cost_since(db, p, SINCE) == pytest.approx(3.0)


def test_author_sum_none_without_author(db):
    r = _repo(db, "one")
    p = _pr(db, r, 1, None)
    _run(db, r, p, 3.0)
    assert writers.sum_author_review_cost_since(db, p, SINCE) is None


def test_author_sum_zero_when_no_runs(db):
    r = _repo(db, "one")
    p = _pr(db, r, 1, "alice")
    assert writers.sum_author_review_cost_since(db, p, SINCE) == 0.0


def test_global_sum_excludes_kinds(db):
    writers.record_claude_spend(db, "review", 50.0)
    writers.record_claude_spend(db, "delta_verify", 0.5)
    writers.record_claude_spend(db, "audit", 2.0)
    assert writers.sum_estimated_cost_since(db, SINCE) == pytest.approx(52.5)
    assert writers.sum_estimated_cost_since(
        db, SINCE, exclude_kinds=("review", "delta_verify")
    ) == pytest.approx(2.0)
