"""Tests for the DB layer.

Uses SQLite in-memory (fast, no Docker). Production runs against Postgres;
Postgres-only constructs (JSONB ops, partial-index WHERE) are
dialect-guarded and not exercised here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from reva.db import (
    Base,
    Database,
    DatabaseRepoLookup,
    create_engine_from_url,
    migrate,
    writers,
)
from reva.db.models import GithubEvent, PendingReview, Repository, ReviewFinding, ReviewRun
from reva.types import Finding, JobParams, ReviewResult


# --- fixtures ----------------------------------------------------------------


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


@pytest.fixture()
def seeded(db: Database) -> dict:
    """Insert one repository + one pull_request and return their ids."""
    repo_id = writers.upsert_repository(
        db,
        github_repository_id=1001,
        owner="acme",
        name="widgets",
        default_branch="main",
        installation_id=500,
    )
    pr_id = writers.upsert_pull_request(
        db,
        repository_id=repo_id,
        github_pr_id=9001,
        pr_number=42,
        title="Add foo",
        author_login="alice",
        base_branch="main",
        head_branch="feat/foo",
        head_sha="deadbeef",
        state="open",
        draft=False,
    )
    return {"repository_id": repo_id, "pull_request_id": pr_id}


def _params(seeded: dict, **overrides) -> JobParams:
    base = {
        "repository_id": seeded["repository_id"],
        "pull_request_id": seeded["pull_request_id"],
        "head_sha": "deadbeef",
        "installation_id": 500,
        "review_mode": "diff",
        "trigger_event": "opened",
    }
    base.update(overrides)
    return JobParams(**base)


# --- RepoLookup --------------------------------------------------------------


def test_repo_lookup_returns_owner_name(db, seeded):
    lookup = DatabaseRepoLookup(db)
    assert lookup.get_owner_name(seeded["repository_id"]) == ("acme", "widgets")


def test_repo_lookup_pr_basic_shape(db, seeded):
    lookup = DatabaseRepoLookup(db)
    pr = lookup.get_pr_basic(seeded["pull_request_id"])
    assert pr["pr_number"] == 42
    assert pr["base_branch"] == "main"
    assert pr["head_branch"] == "feat/foo"
    assert "body" in pr


def test_repo_lookup_missing_id_raises(db):
    lookup = DatabaseRepoLookup(db)
    with pytest.raises(LookupError):
        lookup.get_owner_name(999_999)


# --- review_runs lifecycle ---------------------------------------------------


def test_record_review_started_creates_running_row(db, seeded):
    rid = writers.record_review_started(db, _params(seeded))
    with db.session() as s:
        run = s.get(ReviewRun, rid)
        assert run.status == "running"
        assert run.started_at is not None


def test_record_review_completed_persists_findings(db, seeded):
    finding = Finding(
        severity="major",
        category="bug",
        file="x.py",
        line_start=10,
        line_end=10,
        title="off-by-one",
        body="indexes from 0",
        confidence=0.85,
        is_odoo_specific=False,
    )
    result = ReviewResult(
        status="completed",
        summary="One real issue.",
        risk_level="high",
        findings=[finding],
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        estimated_cost_usd=0.001,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_ms=1234,
    )
    rid = writers.record_review_completed(db, _params(seeded), result)
    with db.session() as s:
        run = s.get(ReviewRun, rid)
        assert run.status == "completed"
        assert run.risk_level == "high"
        assert run.finding_count == 1
        rows = s.query(ReviewFinding).filter_by(review_run_id=rid).all()
        assert len(rows) == 1
        assert rows[0].title == "off-by-one"


def test_record_review_completed_is_idempotent_on_retry(db, seeded):
    """Same (repo, pr, head_sha, review_mode) -> updates, not duplicates."""
    finding = Finding(
        severity="minor", category="style", title="t", body="b", confidence=0.7
    )
    result = ReviewResult(
        status="completed",
        summary="first",
        risk_level="low",
        findings=[finding],
    )
    p = _params(seeded)
    rid1 = writers.record_review_completed(db, p, result)

    # Second attempt with a different finding count: same row, refreshed.
    result2 = ReviewResult(
        status="completed",
        summary="second",
        risk_level="low",
        findings=[finding, finding],
    )
    rid2 = writers.record_review_completed(db, p, result2)
    assert rid1 == rid2
    with db.session() as s:
        run = s.get(ReviewRun, rid2)
        assert run.summary == "second"
        assert run.finding_count == 2
        assert s.query(ReviewFinding).filter_by(review_run_id=rid2).count() == 2


def test_record_review_declined(db, seeded):
    rid = writers.record_review_declined(db, _params(seeded), "diff too large")
    with db.session() as s:
        run = s.get(ReviewRun, rid)
        assert run.status == "declined"
        assert run.decline_reason == "diff too large"
        assert run.finding_count == 0


def test_record_review_stale(db, seeded):
    rid = writers.record_review_stale(db, _params(seeded))
    with db.session() as s:
        assert s.get(ReviewRun, rid).status == "stale"


def test_record_review_failed_stores_error_class(db, seeded):
    rid = writers.record_review_failed(db, _params(seeded), "permanent", "bad json")
    with db.session() as s:
        run = s.get(ReviewRun, rid)
        assert run.status == "failed"
        assert run.error_class == "permanent"
        assert run.error_message == "bad json"


def test_attach_github_ids(db, seeded):
    rid = writers.record_review_completed(
        db,
        _params(seeded),
        ReviewResult(status="completed", summary="ok", risk_level="low"),
    )
    writers.attach_github_ids(db, rid, check_run_id=111, review_id=222)
    with db.session() as s:
        run = s.get(ReviewRun, rid)
        assert run.check_run_id == 111
        assert run.review_id == 222


# --- upserts -----------------------------------------------------------------


def test_upsert_repository_updates_on_conflict(db):
    rid1 = writers.upsert_repository(db, 7, "acme", "widgets", "main", 100)
    rid2 = writers.upsert_repository(db, 7, "acme", "widgets", "develop", 100)
    assert rid1 == rid2
    with db.session() as s:
        repo = s.get(Repository, rid1)
        assert repo.default_branch == "develop"


def test_upsert_pull_request_updates_head_sha(db, seeded):
    new_id = writers.upsert_pull_request(
        db,
        repository_id=seeded["repository_id"],
        github_pr_id=9001,
        pr_number=42,
        title="Add foo (renamed)",
        author_login="alice",
        base_branch="main",
        head_branch="feat/foo",
        head_sha="cafef00d",
        state="open",
        draft=False,
    )
    assert new_id == seeded["pull_request_id"]


def test_upsert_pending_review_overwrites_scheduled_at(db, seeded):
    t1 = datetime.now(timezone.utc)
    pr_args = dict(
        repository_id=seeded["repository_id"],
        pull_request_id=seeded["pull_request_id"],
        pr_number=42,
        installation_id=500,
        trigger_event="synchronize",
        review_mode="diff",
    )
    pid1 = writers.upsert_pending_review(db, head_sha="aaa", scheduled_at=t1, **pr_args)

    t2 = t1 + timedelta(minutes=5)
    pid2 = writers.upsert_pending_review(db, head_sha="bbb", scheduled_at=t2, **pr_args)

    assert pid1 == pid2
    with db.session() as s:
        row = s.get(PendingReview, pid1)
        assert row.head_sha == "bbb"
        # SQLite may strip tz info; compare iso form.
        assert row.scheduled_at.replace(tzinfo=None) == t2.replace(tzinfo=None)


def test_record_github_event_is_idempotent_on_delivery_id(db):
    eid1 = writers.record_github_event(
        db,
        delivery_id="abc-123",
        event_type="pull_request",
        action="opened",
        repository_full_name="acme/widgets",
        sender_login="alice",
        payload={"hello": "world"},
    )
    eid2 = writers.record_github_event(
        db,
        delivery_id="abc-123",
        event_type="pull_request",
        action="synchronize",
        repository_full_name="acme/widgets",
        sender_login="bob",
        payload={"hello": "different"},
    )
    assert eid1 is not None
    assert eid2 is None  # duplicate skipped
    with db.session() as s:
        assert s.query(GithubEvent).count() == 1


# --- structured logging in writers -------------------------------------------


import structlog.testing


def test_upsert_repository_logs_on_new_repo(db):
    with structlog.testing.capture_logs() as logs:
        writers.upsert_repository(
            db, github_repository_id=9999, owner="acme", name="new-repo",
            default_branch="main", installation_id=1,
        )
    assert any(log.get("event") == "repository_registered" for log in logs)


def test_upsert_repository_no_log_on_update(db):
    writers.upsert_repository(
        db, github_repository_id=9999, owner="acme", name="new-repo",
        default_branch="main", installation_id=1,
    )
    with structlog.testing.capture_logs() as logs:
        writers.upsert_repository(
            db, github_repository_id=9999, owner="acme", name="new-repo",
            default_branch="main", installation_id=1,
        )
    assert not any(log.get("event") == "repository_registered" for log in logs)


def test_record_github_event_logs_duplicate(db):
    writers.record_github_event(
        db, delivery_id="dup-001", event_type="pull_request", action="opened",
        repository_full_name="acme/widgets", sender_login="alice", payload={},
    )
    with structlog.testing.capture_logs() as logs:
        writers.record_github_event(
            db, delivery_id="dup-001", event_type="pull_request", action="opened",
            repository_full_name="acme/widgets", sender_login="alice", payload={},
        )
    assert any(log.get("event") == "github_event_duplicate" for log in logs)


def test_record_review_failed_logs(db, seeded):
    params = _params(seeded)
    with structlog.testing.capture_logs() as logs:
        writers.record_review_failed(db, params, error_class="permanent", message="test error")
    assert any(log.get("event") == "review_run_failed" for log in logs)


# --- db_session fixture + seed helpers for new query tests ------------------


@pytest.fixture()
def db_session(db: Database) -> Database:
    """Alias for `db` used by the new query tests."""
    return db


def _seed_repo_and_pr(db: Database) -> tuple[int, int]:
    """Insert a minimal repository + pull_request and return (repo_id, pr_id)."""
    repo_id = writers.upsert_repository(
        db,
        github_repository_id=2001,
        owner="test-org",
        name="test-repo",
        default_branch="main",
        installation_id=600,
    )
    pr_id = writers.upsert_pull_request(
        db,
        repository_id=repo_id,
        github_pr_id=5001,
        pr_number=99,
        title="Test PR",
        author_login="bob",
        base_branch="main",
        head_branch="feat/test",
        head_sha="cafebabe",
        state="open",
        draft=False,
    )
    return repo_id, pr_id


def _seed_review_run(db: Database, pr_id: int, repo_id: int, *, head_sha: str = "abc123", status: str = "completed", completed_at=None) -> int:
    with db.session() as s:
        run = ReviewRun(
            repository_id=repo_id,
            pull_request_id=pr_id,
            head_sha=head_sha,
            status=status,
            trigger_event="synchronize",
            review_mode="diff",
            completed_at=completed_at or datetime.now(timezone.utc),
        )
        s.add(run)
        s.flush()
        return run.id


def _seed_finding(db: Database, run_id: int, *, file_path: str = "custom_addons/foo.py", github_comment_id=None) -> int:
    with db.session() as s:
        f = ReviewFinding(
            review_run_id=run_id,
            severity="minor",
            category="bug",
            file_path=file_path,
            line_start=10,
            title="Test finding",
            body="Test body",
            confidence=0.8,
            github_comment_id=github_comment_id,
        )
        s.add(f)
        s.flush()
        return f.id


# --- get_last_completed_review -----------------------------------------------

from reva.db.repo_lookup import get_last_completed_review
from reva.db.writers import get_open_findings_for_pr


def test_get_last_completed_review_returns_none_when_no_reviews(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    assert get_last_completed_review(db_session, pr_id) is None


def test_get_last_completed_review_returns_most_recent_completed(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    _seed_review_run(db_session, pr_id, repo_id, head_sha="aaa", status="completed")
    _seed_review_run(db_session, pr_id, repo_id, head_sha="bbb", status="completed")
    result = get_last_completed_review(db_session, pr_id)
    assert result is not None
    assert result["head_sha"] == "bbb"
    assert "id" in result


def test_get_last_completed_review_ignores_failed_runs(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    _seed_review_run(db_session, pr_id, repo_id, head_sha="aaa", status="failed")
    assert get_last_completed_review(db_session, pr_id) is None


# --- get_open_findings_for_pr ------------------------------------------------


def test_get_open_findings_for_pr_returns_findings_with_comment_ids(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    run_id = _seed_review_run(db_session, pr_id, repo_id, head_sha="abc", status="completed")
    _seed_finding(db_session, run_id, file_path="custom_addons/a.py", github_comment_id=999)
    _seed_finding(db_session, run_id, file_path="custom_addons/b.py", github_comment_id=None)

    findings = get_open_findings_for_pr(db_session, pr_id)
    assert len(findings) == 1
    assert findings[0]["file_path"] == "custom_addons/a.py"
    assert findings[0]["github_comment_id"] == 999


def test_get_open_findings_for_pr_uses_most_recent_run(db_session):
    from datetime import timedelta
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    now = datetime.now(timezone.utc)
    old_run = _seed_review_run(db_session, pr_id, repo_id, head_sha="old", status="completed",
                                completed_at=now - timedelta(seconds=10))
    new_run = _seed_review_run(db_session, pr_id, repo_id, head_sha="new", status="completed",
                                completed_at=now)
    _seed_finding(db_session, old_run, file_path="custom_addons/old.py", github_comment_id=111)
    _seed_finding(db_session, new_run, file_path="custom_addons/new.py", github_comment_id=222)

    findings = get_open_findings_for_pr(db_session, pr_id)
    assert len(findings) == 1
    assert findings[0]["file_path"] == "custom_addons/new.py"


# --- migration runner --------------------------------------------------------


def test_migration_runner_applies_files_in_order(tmp_path):
    engine = create_engine_from_url("sqlite:///:memory:")
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "001_a.sql").write_text("CREATE TABLE a (id INTEGER PRIMARY KEY);")
    (mdir / "002_b.sql").write_text("CREATE TABLE b (id INTEGER PRIMARY KEY);")

    applied = migrate(engine, mdir)
    assert applied == [1, 2]

    with engine.connect() as conn:
        versions = [
            row[0]
            for row in conn.execute(text("SELECT version FROM schema_migrations ORDER BY version"))
        ]
        assert versions == [1, 2]


def test_migration_runner_is_idempotent(tmp_path):
    engine = create_engine_from_url("sqlite:///:memory:")
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "001_a.sql").write_text("CREATE TABLE a (id INTEGER PRIMARY KEY);")

    assert migrate(engine, mdir) == [1]
    assert migrate(engine, mdir) == []  # nothing left to apply


def test_migration_runner_rejects_unnamed_files(tmp_path):
    engine = create_engine_from_url("sqlite:///:memory:")
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "no_version.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="version number"):
        migrate(engine, mdir)
