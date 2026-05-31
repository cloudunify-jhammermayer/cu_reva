"""Tests for Poller.poll — the scheduler's core loop.

Uses SQLite in-memory for the DB and a FakeQueue in place of Redis/RQ.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import PendingReview
from reva.types import JobParams
from scheduler.poller import Poller
from scheduler.settings import Settings


# --- Fake RQ Queue -----------------------------------------------------------


@dataclass
class FakeJob:
    id: str = "fake-job-id"


@dataclass
class FakeQueue:
    enqueued: list[dict] = field(default_factory=list)

    def enqueue(self, func_name: str, *args, **kwargs) -> FakeJob:
        self.enqueued.append({"func": func_name, "args": args, "kwargs": kwargs})
        return FakeJob()


# --- Fixtures ----------------------------------------------------------------


_SETTINGS = Settings(
    database_url="sqlite:///:memory:",
    redis_url="redis://localhost:6379/0",
)


@pytest.fixture()
def db_and_ids():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)

    repo_id = writers.upsert_repository(
        db,
        github_repository_id=1001,
        owner="acme",
        name="widgets",
        default_branch="main",
        installation_id=99,
    )
    pr_id = writers.upsert_pull_request(
        db,
        repository_id=repo_id,
        github_pr_id=5001,
        pr_number=42,
        title="Add feature",
        author_login="alice",
        base_branch="main",
        head_branch="feat/foo",
        head_sha="deadbeef",
        state="open",
        draft=False,
    )
    return db, repo_id, pr_id


def _poller(db: Database, queue: FakeQueue | None = None) -> tuple[Poller, FakeQueue]:
    q = queue or FakeQueue()
    return Poller(db=db, settings=_SETTINGS, queue=q), q


def _seed_pending(
    db: Database,
    repo_id: int,
    pr_id: int,
    *,
    sha: str = "deadbeef",
    scheduled_at: datetime | None = None,
    consumed: bool = False,
    review_mode: str = "diff",
) -> None:
    if scheduled_at is None:
        scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    writers.upsert_pending_review(
        db,
        repository_id=repo_id,
        pull_request_id=pr_id,
        pr_number=42,
        head_sha=sha,
        installation_id=99,
        trigger_event="opened",
        review_mode=review_mode,
        scheduled_at=scheduled_at,
    )
    if consumed:
        with db.session() as s:
            row = s.query(PendingReview).filter_by(
                repository_id=repo_id, pr_number=42
            ).one()
            row.consumed = True


# --- Tests -------------------------------------------------------------------


def test_no_pending_reviews_enqueues_nothing(db_and_ids):
    db, repo_id, pr_id = db_and_ids
    poller, queue = _poller(db)
    count = poller.poll()
    assert count == 0
    assert queue.enqueued == []


def test_due_review_is_enqueued(db_and_ids):
    db, repo_id, pr_id = db_and_ids
    _seed_pending(db, repo_id, pr_id)
    poller, queue = _poller(db)

    count = poller.poll()

    assert count == 1
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0]["func"] == "worker.tasks.run_review"


def test_future_review_is_not_enqueued(db_and_ids):
    db, repo_id, pr_id = db_and_ids
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    _seed_pending(db, repo_id, pr_id, scheduled_at=future)
    poller, queue = _poller(db)

    count = poller.poll()

    assert count == 0
    assert queue.enqueued == []


def test_poll_marks_pending_consumed(db_and_ids):
    db, repo_id, pr_id = db_and_ids
    _seed_pending(db, repo_id, pr_id)
    poller, _ = _poller(db)
    poller.poll()

    with db.session() as s:
        row = s.query(PendingReview).one()
        assert row.consumed is True


def test_already_consumed_not_enqueued_again(db_and_ids):
    db, repo_id, pr_id = db_and_ids
    _seed_pending(db, repo_id, pr_id, consumed=True)
    poller, queue = _poller(db)

    poller.poll()

    assert queue.enqueued == []


def test_already_reviewed_sha_not_enqueued(db_and_ids):
    """If a review_run already exists for this (sha, mode), skip but consume."""
    db, repo_id, pr_id = db_and_ids
    params = JobParams(
        repository_id=repo_id,
        pull_request_id=pr_id,
        head_sha="deadbeef",
        installation_id=99,
        review_mode="diff",
        trigger_event="opened",
    )
    writers.record_review_started(db, params)

    _seed_pending(db, repo_id, pr_id)
    poller, queue = _poller(db)

    count = poller.poll()

    assert count == 0
    assert queue.enqueued == []
    with db.session() as s:
        assert s.query(PendingReview).one().consumed is True


def test_enqueued_params_match_job_params_contract(db_and_ids):
    db, repo_id, pr_id = db_and_ids
    _seed_pending(db, repo_id, pr_id, sha="cafebabe", review_mode="deep")
    poller, queue = _poller(db)
    poller.poll()

    job_params = queue.enqueued[0]["args"][0]
    # Validate against the Pydantic contract — raises if any field is wrong.
    parsed = JobParams.model_validate(job_params)
    assert parsed.head_sha == "cafebabe"
    assert parsed.review_mode == "deep"
    assert parsed.repository_id == repo_id
    assert parsed.pull_request_id == pr_id
    assert parsed.installation_id == 99
    assert parsed.trigger_event == "opened"


def test_enqueue_uses_retry_config(db_and_ids):
    db, repo_id, pr_id = db_and_ids
    _seed_pending(db, repo_id, pr_id)
    poller, queue = _poller(db)
    poller.poll()

    kwargs = queue.enqueued[0]["kwargs"]
    retry = kwargs["retry"]
    assert retry.max == 3
    assert retry.intervals == [30, 120, 300]


def test_claim_locks_row_with_skip_locked_on_postgres():
    """Two schedulers must not both claim the same pending review. The claim
    fetches the row FOR UPDATE SKIP LOCKED so a second poller skips a row another
    is mid-consuming. (SQLite ignores the clause; this asserts the PG SQL.)"""
    from sqlalchemy.dialects import postgresql

    from scheduler.poller import _claim_stmt

    sql = str(_claim_stmt(1).compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql


def test_review_job_timeout_exceeds_subprocess_timeout(db_and_ids):
    """RQ must not SIGKILL the work-horse while the CLI subprocess is still
    allowed to run. The enqueued job_timeout must exceed the subprocess timeout
    (plus headroom for git + GitHub posting)."""
    from reva.claude_code_runner import SUBPROCESS_TIMEOUT

    db, repo_id, pr_id = db_and_ids
    _seed_pending(db, repo_id, pr_id)
    poller, queue = _poller(db)
    poller.poll()

    job_timeout = queue.enqueued[0]["kwargs"]["job_timeout"]
    assert job_timeout > SUBPROCESS_TIMEOUT


def test_multiple_due_reviews_all_enqueued(db_and_ids):
    db, repo_id, _ = db_and_ids
    # Add a second PR
    pr2_id = writers.upsert_pull_request(
        db,
        repository_id=repo_id,
        github_pr_id=5002,
        pr_number=43,
        title="Another feature",
        author_login="bob",
        base_branch="main",
        head_branch="feat/bar",
        head_sha="cafe1234",
        state="open",
        draft=False,
    )
    # Seed pending for both PRs
    writers.upsert_pending_review(
        db, repository_id=repo_id, pull_request_id=_,
        pr_number=42, head_sha="deadbeef", installation_id=99,
        trigger_event="opened", review_mode="diff",
        scheduled_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    writers.upsert_pending_review(
        db, repository_id=repo_id, pull_request_id=pr2_id,
        pr_number=43, head_sha="cafe1234", installation_id=99,
        trigger_event="opened", review_mode="diff",
        scheduled_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    poller, queue = _poller(db)

    count = poller.poll()

    assert count == 2
    assert len(queue.enqueued) == 2


def test_second_poll_does_not_reenqueue(db_and_ids):
    db, repo_id, pr_id = db_and_ids
    _seed_pending(db, repo_id, pr_id)
    poller, queue = _poller(db)

    poller.poll()
    count2 = poller.poll()

    assert count2 == 0
    assert len(queue.enqueued) == 1  # only from first poll
