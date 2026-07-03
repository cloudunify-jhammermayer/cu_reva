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
from reva.db.models import (
    GithubEvent,
    PendingReview,
    Repository,
    ReviewFeedback,
    ReviewFinding,
    ReviewRun,
    TicketAnalysis,
)
from reva.types import Finding, JobParams, ReviewResult, TicketJobParams


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


def test_record_admin_action_writes_row(db):
    from reva.db.models import AdminAudit

    writers.record_admin_action(
        db, action="requeue", actor="10.0.0.5", target="review_run_id=7",
        detail={"mode": "diff"},
    )
    with db.session() as s:
        row = s.query(AdminAudit).one()
        assert row.action == "requeue"
        assert row.actor == "10.0.0.5"
        assert row.target == "review_run_id=7"
        assert row.detail == {"mode": "diff"}
        assert row.created_at is not None


def _age_running_run(db: Database, run_id: int, seconds: int) -> None:
    """Backdate a running run's started_at to simulate a long-dead worker."""
    with db.session() as s:
        run = s.get(ReviewRun, run_id)
        run.started_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)


def test_reap_stale_running_marks_old_running_as_failed(db, seeded):
    rid = writers.record_review_started(db, _params(seeded))
    _age_running_run(db, rid, seconds=4000)

    reaped = writers.reap_stale_running_reviews(db, older_than_seconds=3600)

    assert reaped == 1
    with db.session() as s:
        run = s.get(ReviewRun, rid)
        assert run.status == "failed"
        assert run.error_class == "stale"
        assert run.completed_at is not None


def test_reap_stale_running_leaves_recent_running_untouched(db, seeded):
    rid = writers.record_review_started(db, _params(seeded))
    _age_running_run(db, rid, seconds=60)

    reaped = writers.reap_stale_running_reviews(db, older_than_seconds=3600)

    assert reaped == 0
    with db.session() as s:
        assert s.get(ReviewRun, rid).status == "running"


def test_reap_stale_running_ignores_completed_runs(db, seeded):
    rid = writers.record_review_completed(
        db, _params(seeded),
        ReviewResult(status="completed", summary="ok", risk_level="low"),
    )
    _age_running_run(db, rid, seconds=99999)  # old, but not 'running'

    reaped = writers.reap_stale_running_reviews(db, older_than_seconds=3600)

    assert reaped == 0
    with db.session() as s:
        assert s.get(ReviewRun, rid).status == "completed"


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


def test_sum_estimated_cost_since_serialize_is_safe_on_sqlite(db, seeded):
    """The serialized spend read (used by the budget guard to make the check
    non-interleaving on Postgres) must still return the correct total on
    SQLite, where the advisory lock is a no-op."""
    writers.record_review_completed(
        db, _params(seeded),
        ReviewResult(status="completed", summary="s", risk_level="low",
                     estimated_cost_usd=1.25),
    )
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert writers.sum_estimated_cost_since(db, since, serialize=True) == pytest.approx(1.25)


def test_purge_old_ticket_text_scrubs_pii_keeps_analysis(db):
    """F1/SECU-8: raw customer ticket text older than the retention window is
    scrubbed (data minimisation), but the derived analysis is kept; recent rows
    are untouched and the purge is idempotent."""
    def _mk(ticket_id, text_):
        return writers.record_ticket_analysis_created(
            db, TicketJobParams(analysis_id=0, odoo_instance_id=1, ticket_id=ticket_id,
                                model_name="helpdesk.ticket", field_name="description", text=text_)
        )

    old_id = _mk(1, "secret customer PII and account details")
    recent_id = _mk(2, "still within retention")
    with db.session() as s:
        old = s.get(TicketAnalysis, old_id)
        old.created_at = datetime.now(timezone.utc) - timedelta(days=40)
        old.result_html = "<p>analysis output</p>"

    n = writers.purge_old_ticket_text(db, older_than_days=30)

    assert n == 1
    with db.session() as s:
        old = s.get(TicketAnalysis, old_id)
        assert "secret customer PII" not in old.input_text
        assert old.result_html == "<p>analysis output</p>"  # analysis retained
        assert s.get(TicketAnalysis, recent_id).input_text == "still within retention"
    # idempotent — already-purged rows aren't re-counted
    assert writers.purge_old_ticket_text(db, older_than_days=30) == 0


def test_sum_estimated_cost_counts_all_kinds(db):
    """The rolling cap counts every Claude call via the ledger — not just reviews
    (SECU-3/SECU-4): audits and replies record spend through record_claude_spend."""
    writers.record_claude_spend(db, "audit", 2.0)
    writers.record_claude_spend(db, "reply", 0.5)
    writers.record_claude_spend(db, "review", None)  # None cost is treated as 0
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert writers.sum_estimated_cost_since(db, since) == pytest.approx(2.5)


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


def test_upsert_pending_review_push_does_not_downgrade_queued_deep(db, seeded):
    """CORR-7: a synchronize push must not silently downgrade a queued deep/full
    review to diff — but it should still re-point at the new head SHA."""
    base = dict(
        repository_id=seeded["repository_id"],
        pull_request_id=seeded["pull_request_id"],
        pr_number=42,
        installation_id=500,
    )
    t1 = datetime.now(timezone.utc)
    pid = writers.upsert_pending_review(
        db, head_sha="aaa", scheduled_at=t1,
        trigger_event="comment", review_mode="deep", **base,
    )
    t2 = t1 + timedelta(minutes=5)
    writers.upsert_pending_review(
        db, head_sha="bbb", scheduled_at=t2,
        trigger_event="synchronize", review_mode="diff", **base,
    )
    with db.session() as s:
        row = s.get(PendingReview, pid)
        assert row.review_mode == "deep"   # not downgraded by the push
        assert row.head_sha == "bbb"        # but the latest commit is reviewed
        assert row.scheduled_at.replace(tzinfo=None) == t2.replace(tzinfo=None)


def test_upsert_pending_review_comment_sets_mode(db, seeded):
    """An explicit comment command sets the mode (e.g. upgrade diff -> deep)."""
    base = dict(
        repository_id=seeded["repository_id"],
        pull_request_id=seeded["pull_request_id"],
        pr_number=42,
        installation_id=500,
    )
    t = datetime.now(timezone.utc)
    pid = writers.upsert_pending_review(
        db, head_sha="aaa", scheduled_at=t,
        trigger_event="synchronize", review_mode="diff", **base,
    )
    writers.upsert_pending_review(
        db, head_sha="aaa", scheduled_at=t,
        trigger_event="comment", review_mode="deep", **base,
    )
    with db.session() as s:
        assert s.get(PendingReview, pid).review_mode == "deep"


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


def _record_event(db, delivery_id="abc-123", **overrides):
    base = dict(
        delivery_id=delivery_id, event_type="pull_request", action="opened",
        repository_full_name="acme/widgets", sender_login="alice", payload={},
    )
    base.update(overrides)
    return writers.record_github_event(db, **base)


def test_record_github_event_stores_one_row_per_delivery(db):
    eid1 = _record_event(db)
    _record_event(db, action="synchronize")
    assert eid1 is not None
    with db.session() as s:
        assert s.query(GithubEvent).count() == 1


def test_unprocessed_redelivery_is_reprocessable(db):
    """A delivery recorded but not yet marked processed (prior attempt crashed
    mid-handling) must be handed back for reprocessing, not skipped."""
    eid1 = _record_event(db)
    eid2 = _record_event(db)
    assert eid2 == eid1  # same row, returned again so the retry can finish


def test_processed_redelivery_is_skipped(db):
    eid1 = _record_event(db)
    writers.mark_event_processed(db, eid1)
    eid2 = _record_event(db)
    assert eid2 is None  # genuine duplicate of fully-processed work


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
    eid = writers.record_github_event(
        db, delivery_id="dup-001", event_type="pull_request", action="opened",
        repository_full_name="acme/widgets", sender_login="alice", payload={},
    )
    writers.mark_event_processed(db, eid)
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


def _seed_finding(db: Database, run_id: int, *, file_path: str = "custom_addons/foo.py", github_comment_id=None, outcome="open") -> int:
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
            outcome=outcome,
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


def test_get_open_findings_for_pr_unions_across_runs_oldest_first(db_session):
    # PR-wide: open threads from every completed run are returned, oldest first.
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    old_run = _seed_review_run(db_session, pr_id, repo_id, head_sha="old", status="completed")
    new_run = _seed_review_run(db_session, pr_id, repo_id, head_sha="new", status="completed")
    _seed_finding(db_session, old_run, file_path="custom_addons/old.py", github_comment_id=111)
    _seed_finding(db_session, new_run, file_path="custom_addons/new.py", github_comment_id=222)

    findings = get_open_findings_for_pr(db_session, pr_id)
    assert [f["file_path"] for f in findings] == ["custom_addons/old.py", "custom_addons/new.py"]


def test_get_open_findings_for_pr_reproduction_survives_intermediate_empty_run(db_session):
    # The bug: run 1 posts F, run 2 completes with nothing, and the lookback before
    # run 3 must STILL see F (it used to hide behind the empty run 2).
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    run1 = _seed_review_run(db_session, pr_id, repo_id, head_sha="s1", status="completed")
    _seed_finding(db_session, run1, file_path="custom_addons/f.py", github_comment_id=111)
    _seed_review_run(db_session, pr_id, repo_id, head_sha="s2", status="completed")  # empty
    run3 = _seed_review_run(db_session, pr_id, repo_id, head_sha="s3", status="completed")

    findings = get_open_findings_for_pr(db_session, pr_id, before_run_id=run3)
    assert [f["github_comment_id"] for f in findings] == [111]


def test_get_open_findings_for_pr_excludes_non_open_outcome(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    run_id = _seed_review_run(db_session, pr_id, repo_id, head_sha="abc", status="completed")
    _seed_finding(db_session, run_id, file_path="custom_addons/open.py", github_comment_id=111)
    _seed_finding(db_session, run_id, file_path="custom_addons/done.py", github_comment_id=222,
                  outcome="resolved_by_fix")

    findings = get_open_findings_for_pr(db_session, pr_id)
    assert [f["github_comment_id"] for f in findings] == [111]


def test_get_open_findings_for_pr_excludes_dismissed(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    run_id = _seed_review_run(db_session, pr_id, repo_id, head_sha="abc", status="completed")
    keep = _seed_finding(db_session, run_id, file_path="custom_addons/keep.py", github_comment_id=111)
    drop = _seed_finding(db_session, run_id, file_path="custom_addons/drop.py", github_comment_id=222)
    with db_session.session() as s:
        s.add(ReviewFeedback(
            review_finding_id=drop, review_run_id=run_id, github_comment_id=222,
            reactor_login="dev", reaction="dismissed", is_positive=False,
        ))

    findings = get_open_findings_for_pr(db_session, pr_id)
    assert [f["id"] for f in findings] == [keep]


def test_get_open_findings_for_pr_excludes_current_run_with_before_run_id(db_session):
    from datetime import timedelta
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    now = datetime.now(timezone.utc)
    old_run = _seed_review_run(db_session, pr_id, repo_id, head_sha="old", status="completed",
                                completed_at=now - timedelta(seconds=10))
    new_run = _seed_review_run(db_session, pr_id, repo_id, head_sha="new", status="completed",
                                completed_at=now)
    _seed_finding(db_session, old_run, file_path="custom_addons/old.py", github_comment_id=111)
    _seed_finding(db_session, new_run, file_path="custom_addons/new.py", github_comment_id=222)

    # Pass new_run as before_run_id — should get old run's findings
    findings = get_open_findings_for_pr(db_session, pr_id, before_run_id=new_run)
    assert len(findings) == 1
    assert findings[0]["file_path"] == "custom_addons/old.py"


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


def test_retry_on_conflict_retries_once_then_succeeds():
    from sqlalchemy.exc import IntegrityError

    from reva.db.writers import _retry_on_conflict

    calls = []

    @_retry_on_conflict
    def upsert():
        calls.append(1)
        if len(calls) == 1:
            raise IntegrityError(
                "INSERT", None,
                Exception('duplicate key value violates unique constraint "foo_key"'),
            )
        return "updated"

    assert upsert() == "updated"
    assert len(calls) == 2  # first INSERT lost the race, retry took UPDATE branch


def test_retry_on_conflict_reraises_if_still_conflicting():
    from sqlalchemy.exc import IntegrityError

    from reva.db.writers import _retry_on_conflict

    @_retry_on_conflict
    def upsert():
        raise IntegrityError(
            "INSERT", None,
            Exception('duplicate key value violates unique constraint "foo_key"'),
        )

    with pytest.raises(IntegrityError):
        upsert()


def test_retry_on_conflict_does_not_retry_non_unique_errors():
    """CORR-17: only a unique-violation race is retryable. A different integrity
    error (e.g. FK) must propagate immediately, not trigger a wasteful retry."""
    from sqlalchemy.exc import IntegrityError

    from reva.db.writers import _retry_on_conflict

    calls = []

    @_retry_on_conflict
    def insert():
        calls.append(1)
        raise IntegrityError(
            "INSERT", None,
            Exception('insert violates foreign key constraint "fk_repo"'),
        )

    with pytest.raises(IntegrityError):
        insert()
    assert len(calls) == 1  # re-raised immediately, no retry


def test_migration_runner_rejects_unnamed_files(tmp_path):
    engine = create_engine_from_url("sqlite:///:memory:")
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "no_version.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="version number"):
        migrate(engine, mdir)


# --- ticket_issue_runs writers -------------------------------------------------


def _issue_params(
    ticket_id: int = 7, run_id: int = 0,
    github_url: str = "https://github.com/acme/widgets",
) -> "TicketIssueJobParams":
    from reva.types import TicketIssueJobParams
    return TicketIssueJobParams(
        run_id=run_id,
        odoo_instance_id=1,
        ticket_id=ticket_id,
        model_name="helpdesk.ticket",
        github_url=github_url,
        name="Login page broken",
        description="We need a login page.",
        analysis_html="<h2>Summary</h2>",
        priority="1",
        ticket_url="https://odoo.example.com/web#id=7&model=helpdesk.ticket&view_type=form",
    )


def _claude_response() -> "ClaudeResponse":
    from reva.types import ClaudeResponse
    return ClaudeResponse(
        model="claude-sonnet-4-6",
        stop_reason="tool_use",
        input_tokens=1000,
        output_tokens=400,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )


def test_ticket_issue_run_create_and_get_roundtrip(db):
    run_id = writers.record_ticket_issue_run_created(db, _issue_params())
    writers.attach_ticket_issue_job_id(db, run_id, "rq:job:ti-1")

    row = writers.get_ticket_issue_run(db, run_id)
    assert row["id"] == run_id
    assert row["job_id"] == "rq:job:ti-1"
    assert row["status"] == "pending"
    assert row["github_url"] == "https://github.com/acme/widgets"
    assert row["name"] == "Login page broken"
    assert row["description"] == "We need a login page."
    assert row["analysis_html"] == "<h2>Summary</h2>"
    assert row["priority"] == "1"
    assert row["ticket_url"].startswith("https://odoo.example.com/")
    assert row["issues"] is None

    assert writers.get_ticket_issue_run(db, 99999) is None


def test_ticket_issue_run_pending_dedup(db):
    run_id = writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=7))
    existing = writers.get_pending_ticket_issue_run(db, 7, "helpdesk.ticket")
    assert existing is not None and existing["id"] == run_id
    # different record -> no match
    assert writers.get_pending_ticket_issue_run(db, 8, "helpdesk.ticket") is None
    assert writers.get_pending_ticket_issue_run(db, 7, "project.task") is None
    # non-pending rows don't match
    writers.record_ticket_issue_run_failed(db, run_id, "boom")
    assert writers.get_pending_ticket_issue_run(db, 7, "helpdesk.ticket") is None


def test_ticket_issue_plan_persists_usage_and_returns_cost(db):
    run_id = writers.record_ticket_issue_run_created(db, _issue_params())
    plan = [{"title": "A", "body": "b", "acceptance_criteria": ["c"],
             "number": None, "url": None}]

    cost = writers.record_ticket_issue_plan(db, run_id, plan, _claude_response())

    assert cost > 0
    row = writers.get_ticket_issue_run(db, run_id)
    assert row["status"] == "pending"  # plan persists BEFORE completion
    assert row["issues"] == plan
    assert row["model"] == "claude-sonnet-4-6"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 400
    assert row["estimated_cost_usd"] == pytest.approx(cost)


def test_ticket_issue_progress_and_completion(db):
    run_id = writers.record_ticket_issue_run_created(db, _issue_params())
    plan = [
        {"title": "A", "body": "b", "acceptance_criteria": [], "number": None, "url": None},
        {"title": "B", "body": "b", "acceptance_criteria": [], "number": None, "url": None},
    ]
    writers.record_ticket_issue_plan(db, run_id, plan, _claude_response())

    plan[0]["number"], plan[0]["url"] = 42, "https://github.com/acme/widgets/issues/42"
    writers.update_ticket_issue_progress(db, run_id, plan)
    row = writers.get_ticket_issue_run(db, run_id)
    assert row["issues"][0]["number"] == 42
    assert row["issues"][1]["number"] is None
    assert row["status"] == "pending"

    plan[1]["number"], plan[1]["url"] = 43, "https://github.com/acme/widgets/issues/43"
    writers.record_ticket_issue_run_completed(db, run_id, plan)
    row = writers.get_ticket_issue_run(db, run_id)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert [i["number"] for i in row["issues"]] == [42, 43]


def test_ticket_issue_run_failed_and_reset_keeps_plan(db):
    run_id = writers.record_ticket_issue_run_created(db, _issue_params())
    plan = [{"title": "A", "body": "b", "acceptance_criteria": [],
             "number": 42, "url": "https://github.com/acme/widgets/issues/42"}]
    writers.record_ticket_issue_plan(db, run_id, plan, _claude_response())
    writers.record_ticket_issue_run_failed(db, run_id, "GitHub 403")

    row = writers.get_ticket_issue_run(db, run_id)
    assert row["status"] == "failed"
    assert row["error_message"] == "GitHub 403"

    writers.reset_ticket_issue_run(db, run_id)
    row = writers.get_ticket_issue_run(db, run_id)
    assert row["status"] == "pending"
    assert row["error_message"] is None
    assert row["job_id"] is None
    assert row["completed_at"] is None
    # the persisted plan survives the reset so a requeue resumes, not re-plans
    assert row["issues"] == plan


def test_purge_old_ticket_issue_text_scrubs_inputs_keeps_issues(db):
    from reva.db.models import TicketIssueRun

    old_id = writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=1))
    recent_id = writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=2))
    issues = [{"title": "A", "body": "b", "acceptance_criteria": [],
               "number": 42, "url": "https://github.com/acme/widgets/issues/42"}]
    with db.session() as s:
        old = s.get(TicketIssueRun, old_id)
        old.created_at = datetime.now(timezone.utc) - timedelta(days=40)
        old.issues = issues

    n = writers.purge_old_ticket_issue_text(db, older_than_days=30)

    assert n == 1
    with db.session() as s:
        old = s.get(TicketIssueRun, old_id)
        assert old.description == writers.PURGED_TICKET_TEXT
        assert old.analysis_html == writers.PURGED_TICKET_TEXT
        # derived link refs retained; the Claude-rendered plan body
        # (customer-derived text) is scrubbed with the rest
        assert old.issues == [{"title": "A", "number": 42,
                               "url": "https://github.com/acme/widgets/issues/42"}]
        recent = s.get(TicketIssueRun, recent_id)
        assert recent.description == "We need a login page."
    # idempotent
    assert writers.purge_old_ticket_issue_text(db, older_than_days=30) == 0


def test_ticket_issue_pending_unique_per_record(db):
    """Partial unique index: only one pending run per instance per
    (ticket_id, model_name) — closes the dedup check-then-insert race.

    Uses direct ORM inserts with an explicit odoo_instance_id so the index
    fires (NULL != NULL in SQLite unique indexes, so the writer path — which
    doesn't yet stamp odoo_instance_id — cannot exercise this constraint).
    """
    from sqlalchemy.exc import IntegrityError

    from reva.db.models import OdooInstance, TicketIssueRun

    def _add_pending(s, *, instance_id: int, ticket_id: int) -> None:
        s.add(TicketIssueRun(
            odoo_instance_id=instance_id, ticket_id=ticket_id,
            model_name="helpdesk.ticket", github_url="https://github.com/o/r",
            name="n", description="d", analysis_html="", priority="1",
            ticket_url="https://odoo/1", status="pending",
        ))
        s.flush()

    with db.session() as s:
        inst = OdooInstance(name="x", key_hash="h", key_prefix="p")
        s.add(inst)
        s.flush()
        iid = inst.id
        _add_pending(s, instance_id=iid, ticket_id=7)

    with pytest.raises(IntegrityError):
        with db.session() as s:
            _add_pending(s, instance_id=iid, ticket_id=7)

    # a non-pending sibling doesn't block a new run
    run2 = writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=8))
    writers.record_ticket_issue_run_failed(db, run2, "boom")
    writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=8))


def test_get_latest_ticket_issue_plan(db):
    a = writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=7))
    plan = [{"title": "A", "body": "b", "acceptance_criteria": [], "number": None, "url": None}]
    writers.record_ticket_issue_plan(db, a, plan, _claude_response())
    writers.record_ticket_issue_run_failed(db, a, "boom")
    b = writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=7))

    prior = writers.get_latest_ticket_issue_plan(db, 7, "helpdesk.ticket", exclude_run_id=b)
    assert prior is not None and prior["id"] == a and prior["issues"] == plan
    # the current run is excluded, runs without a plan don't match
    assert writers.get_latest_ticket_issue_plan(db, 7, "helpdesk.ticket", exclude_run_id=a) is None


def test_update_ticket_issue_state_matches_repo_and_number(db):
    """Closing issue 42 updates every run carrying it (case-insensitive repo
    match on the free-text github_url) and returns the newest snapshot per
    Odoo record."""
    issues = [
        {"title": "A", "number": 42, "url": "https://github.com/acme/widgets/issues/42",
         "state": "open"},
        {"title": "B", "number": 43, "url": "https://github.com/acme/widgets/issues/43",
         "state": "open"},
    ]
    old_run = writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=7))
    writers.update_ticket_issue_progress(db, old_run, issues)
    writers.record_ticket_issue_run_failed(db, old_run, "boom")
    new_run = writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=7))
    writers.update_ticket_issue_progress(db, new_run, issues)
    # same issue number in a DIFFERENT repo must not match
    other = writers.record_ticket_issue_run_created(
        db, _issue_params(ticket_id=8, github_url="https://github.com/acme/other")
    )
    writers.update_ticket_issue_progress(db, other, [dict(issues[0])])

    affected = writers.update_ticket_issue_state(db, "Acme", "Widgets", 42, "closed")

    assert len(affected) == 1
    rec = affected[0]
    assert (rec["ticket_id"], rec["model_name"]) == (7, "helpdesk.ticket")
    assert rec["issues"][0]["state"] == "closed"
    assert rec["issues"][1]["state"] == "open"
    # both runs of the record were updated; the other repo untouched
    assert writers.get_ticket_issue_run(db, old_run)["issues"][0]["state"] == "closed"
    assert writers.get_ticket_issue_run(db, new_run)["issues"][0]["state"] == "closed"
    assert writers.get_ticket_issue_run(db, other)["issues"][0]["state"] == "open"


def test_update_ticket_issue_state_no_match_returns_empty(db):
    run = writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=7))
    writers.update_ticket_issue_progress(db, run, [
        {"title": "A", "number": 42, "url": "u", "state": "open"},
    ])
    assert writers.update_ticket_issue_state(db, "acme", "widgets", 999, "closed") == []
    assert writers.update_ticket_issue_state(db, "acme", "elsewhere", 42, "closed") == []


def test_repo_full_name_normalized_at_creation(db):
    """M15: repo_full_name is the lowercased owner/repo derived from github_url,
    tolerating .git and case, so the state-sync webhook can equality-match it."""
    from reva.db.models import TicketIssueRun

    run = writers.record_ticket_issue_run_created(
        db, _issue_params(ticket_id=7, github_url="https://github.com/Acme/Widgets.git")
    )
    with db.session() as s:
        assert s.get(TicketIssueRun, run).repo_full_name == "acme/widgets"


def test_planning_basis_stored_not_the_doc(db):
    from reva.types import Attachment

    docx = _issue_params(ticket_id=1).model_copy(update={
        "description_docx": Attachment(filename="spec.docx", content_base64="UEsDBABzZQ=="),
    })
    docx_id = writers.record_ticket_issue_run_created(db, docx)
    text_id = writers.record_ticket_issue_run_created(db, _issue_params(ticket_id=2))

    docx_row = writers.get_ticket_issue_run(db, docx_id)
    text_row = writers.get_ticket_issue_run(db, text_id)
    # the document is not persisted — only a small typed digest
    assert "description_docx" not in docx_row
    assert docx_row["planning_basis"].startswith("docx:")
    assert text_row["planning_basis"].startswith("text:")
    assert len(docx_row["planning_basis"]) <= 25


def test_planning_basis_changes_when_doc_changes(db):
    from reva.types import Attachment

    def basis_for(content):
        p = _issue_params(ticket_id=1).model_copy(update={
            "description_docx": Attachment(filename="s.docx", content_base64=content),
        })
        return writers.compute_planning_basis(p)

    assert basis_for("UEsDBABhAA==") == basis_for("UEsDBABhAA==")
    assert basis_for("UEsDBABhAA==") != basis_for("UEsDBABiBB==")


# --- register_prompt_version (prompt-version drift detection) ----------------


def test_register_prompt_version_created_then_unchanged(db: Database):
    assert writers.register_prompt_version(db, "v1.5", "sysA", "revA") == "created"
    # Identical re-registration is a no-op.
    assert writers.register_prompt_version(db, "v1.5", "sysA", "revA") == "unchanged"
    from reva.db.models import PromptVersion
    with db.session() as s:
        rows = s.query(PromptVersion).all()
        assert len(rows) == 1
        assert rows[0].version == "v1.5"
        assert rows[0].system_prompt_hash == "sysA"


def test_register_prompt_version_detects_drift_without_mutating_baseline(db: Database):
    writers.register_prompt_version(db, "v1.5", "sysA", "revA")
    # Same version, changed review hash -> drift, baseline preserved.
    assert writers.register_prompt_version(db, "v1.5", "sysA", "revB") == "drift"
    from reva.db.models import PromptVersion
    with db.session() as s:
        row = s.query(PromptVersion).filter_by(version="v1.5").one()
        assert row.review_prompt_hash == "revA"  # baseline untouched


def test_register_prompt_version_new_version_is_separate_row(db: Database):
    writers.register_prompt_version(db, "v1.5", "sysA", "revA")
    assert writers.register_prompt_version(db, "v1.6", "sysB", "revB") == "created"
    from reva.db.models import PromptVersion
    with db.session() as s:
        assert s.query(PromptVersion).count() == 2


# --- outcome ledger (set_finding_outcome / mark_open_findings_at_merge) -------


def _seed_findings(db: Database, seeded: dict, n: int) -> list[int]:
    """Record a completed review with n findings; return their ids in order."""
    findings = [
        Finding(
            severity="major", category="bug", file=f"x{i}.py", line_start=1,
            line_end=1, title=f"f{i}", body="b", confidence=0.8, is_odoo_specific=False,
        )
        for i in range(n)
    ]
    result = ReviewResult(
        status="completed", summary="s", risk_level="high", findings=findings,
        model="claude-sonnet-4-6", input_tokens=1, output_tokens=1,
        estimated_cost_usd=0.0, started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc), duration_ms=1,
    )
    rid = writers.record_review_completed(db, _params(seeded), result)
    with db.session() as s:
        return [
            r.id for r in
            s.query(ReviewFinding).filter_by(review_run_id=rid).order_by(ReviewFinding.id).all()
        ]


def test_findings_default_to_open_outcome(db, seeded):
    [fid] = _seed_findings(db, seeded, 1)
    with db.session() as s:
        assert s.get(ReviewFinding, fid).outcome == "open"


def test_set_finding_outcome_sets_outcome_and_timestamp(db, seeded):
    [fid] = _seed_findings(db, seeded, 1)
    writers.set_finding_outcome(db, fid, "resolved_by_fix")
    with db.session() as s:
        f = s.get(ReviewFinding, fid)
        assert f.outcome == "resolved_by_fix"
        assert f.outcome_at is not None


def test_mark_open_findings_at_merge_only_touches_open_posted(db, seeded):
    ids = _seed_findings(db, seeded, 3)
    # Post two of three; leave the third unposted (github_comment_id NULL).
    writers.attach_finding_comment_ids(db, {ids[0]: 111, ids[1]: 222})
    # One posted finding was already resolved_by_fix — must be left alone.
    writers.set_finding_outcome(db, ids[0], "resolved_by_fix")

    marked = writers.mark_open_findings_at_merge(db, seeded["pull_request_id"])
    assert marked == 1  # only ids[1]: open AND posted

    with db.session() as s:
        assert s.get(ReviewFinding, ids[0]).outcome == "resolved_by_fix"   # untouched
        assert s.get(ReviewFinding, ids[1]).outcome == "still_open_at_merge"
        assert s.get(ReviewFinding, ids[2]).outcome == "open"              # never posted


def test_mark_open_findings_at_merge_is_idempotent(db, seeded):
    [fid] = _seed_findings(db, seeded, 1)
    writers.attach_finding_comment_ids(db, {fid: 111})
    assert writers.mark_open_findings_at_merge(db, seeded["pull_request_id"]) == 1
    assert writers.mark_open_findings_at_merge(db, seeded["pull_request_id"]) == 0  # no-op


# --- feedback capture (record_feedback / lookup review_run_id) ---------------


def test_lookup_finding_by_comment_id_returns_review_run_id(db, seeded):
    [fid] = _seed_findings(db, seeded, 1)
    writers.attach_finding_comment_ids(db, {fid: 555})
    found = writers.lookup_finding_by_comment_id(db, 555)
    assert found is not None and found["id"] == fid
    assert found["review_run_id"] is not None


def test_record_feedback_inserts_then_dedups(db, seeded):
    from reva.db.models import ReviewFeedback
    [fid] = _seed_findings(db, seeded, 1)
    writers.attach_finding_comment_ids(db, {fid: 555})
    rr = writers.lookup_finding_by_comment_id(db, 555)["review_run_id"]
    kw = dict(review_finding_id=fid, review_run_id=rr, github_comment_id=555,
              reactor_login="alice", reaction="resolved", is_positive=True)
    assert writers.record_feedback(db, **kw) is not None
    assert writers.record_feedback(db, **kw) is None  # dedup on unique constraint
    with db.session() as s:
        assert s.query(ReviewFeedback).count() == 1


def test_record_feedback_resolve_then_unresolve_two_rows(db, seeded):
    from reva.db.models import ReviewFeedback
    [fid] = _seed_findings(db, seeded, 1)
    writers.attach_finding_comment_ids(db, {fid: 555})
    rr = writers.lookup_finding_by_comment_id(db, 555)["review_run_id"]
    base = dict(review_finding_id=fid, review_run_id=rr, github_comment_id=555, reactor_login="alice")
    writers.record_feedback(db, reaction="resolved", is_positive=True, **base)
    writers.record_feedback(db, reaction="unresolved", is_positive=False, **base)
    with db.session() as s:
        assert s.query(ReviewFeedback).count() == 2  # distinct reaction values


# --- get_prior_open_findings (delta suppression source) ----------------------


def test_get_prior_open_findings_returns_only_posted(db, seeded):
    ids = _seed_findings(db, seeded, 2)
    writers.attach_finding_comment_ids(db, {ids[0]: 901})  # only the first is posted
    found = DatabaseRepoLookup(db).get_prior_open_findings(seeded["pull_request_id"])
    assert {f["id"] for f in found} == {ids[0]}  # unposted finding excluded
    assert found[0]["github_comment_id"] == 901


def test_get_prior_open_findings_empty_when_no_completed_run(db, seeded):
    assert DatabaseRepoLookup(db).get_prior_open_findings(seeded["pull_request_id"]) == []


def test_get_prior_open_findings_caps_at_30_newest(db, seeded):
    # Prompt-context cap: a long-lived PR can accumulate many open threads, but the
    # "already flagged" list is capped at the 30 newest (list is oldest-first).
    ids = _seed_findings(db, seeded, 31)
    writers.attach_finding_comment_ids(db, {fid: 900 + i for i, fid in enumerate(ids)})
    found = DatabaseRepoLookup(db).get_prior_open_findings(seeded["pull_request_id"])
    assert len(found) == 30
    got = {f["id"] for f in found}
    assert ids[0] not in got      # oldest dropped
    assert ids[-1] in got         # newest kept


# --- muted categories (Tier 3 feature A) -------------------------------------


def test_set_and_get_muted_categories(db, seeded):
    repo_id = seeded["repository_id"]
    writers.set_category_mute(db, repo_id, "style", muted_by="alice", active=True)
    writers.set_category_mute(db, repo_id, "docs", muted_by="bob", active=True)
    assert writers.get_muted_categories(db, repo_id) == {"style", "docs"}


def test_mute_is_idempotent_upsert(db, seeded):
    repo_id = seeded["repository_id"]
    writers.set_category_mute(db, repo_id, "style", muted_by="alice", active=True)
    writers.set_category_mute(db, repo_id, "style", muted_by="alice", active=True)
    from reva.db.models import MutedCategory
    with db.session() as s:
        assert s.query(MutedCategory).count() == 1  # one row per (repo, category)


def test_unmute_excludes_from_active_set(db, seeded):
    repo_id = seeded["repository_id"]
    writers.set_category_mute(db, repo_id, "style", muted_by="alice", active=True)
    writers.set_category_mute(db, repo_id, "style", muted_by="alice", active=False)
    assert writers.get_muted_categories(db, repo_id) == set()  # active-only
