"""Tests for runner.run_review — the end-to-end orchestration.

Real Database on SQLite (so the writer + idempotency paths are exercised
against actual SQL). Fakes for Reviewer and GitHubClient — Claude is never
touched here; that layer is covered by test_claude_client + test_reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock, patch

from reva.db import Base, Database, create_engine_from_url, writers
from reva.errors import PermanentError, TransientError
from worker.runner import WorkerContext, run_review, set_context
from reva.types import Finding, JobParams, ReviewResult


SAMPLE_DIFF = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -10,3 +10,5 @@
 ctx
-old
+new1
+new2
+new3
"""


# --- Fakes -------------------------------------------------------------------


@dataclass
class FakeReviewer:
    result: ReviewResult | None = None
    raise_exc: Exception | None = None
    call_count: int = 0

    def execute(self, params: JobParams) -> ReviewResult:
        self.call_count += 1
        if self.raise_exc:
            raise self.raise_exc
        assert self.result is not None
        return self.result


@dataclass
class FakeGitHub:
    diff: str = SAMPLE_DIFF
    installation_token: str = "ghs_test"
    next_check_run_id: int = 100
    next_review_id: int = 200
    next_comment_id: int = 300

    created_check_runs: list[dict] = field(default_factory=list)
    created_pr_reviews: list[dict] = field(default_factory=list)
    created_issue_comments: list[dict] = field(default_factory=list)
    diff_fetch_count: int = 0

    def get_installation_token(self, installation_id: int) -> str:
        return self.installation_token

    def get_pull_request_diff(self, token, owner, repo, pr_number) -> str:
        self.diff_fetch_count += 1
        return self.diff

    def create_check_run(self, **kwargs) -> int:
        self.created_check_runs.append(kwargs)
        cr_id = self.next_check_run_id
        self.next_check_run_id += 1
        return cr_id

    def create_pr_review(self, **kwargs) -> int:
        self.created_pr_reviews.append(kwargs)
        rid = self.next_review_id
        self.next_review_id += 1
        return rid

    def create_issue_comment(self, **kwargs) -> int:
        self.created_issue_comments.append(kwargs)
        cid = self.next_comment_id
        self.next_comment_id += 1
        return cid


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def ctx_and_fakes():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)

    # Seed repository + PR so writers.get_owner_name / get_pr_basic work.
    repo_id = writers.upsert_repository(
        db,
        github_repository_id=1,
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

    reviewer = FakeReviewer()
    github = FakeGitHub()
    context = WorkerContext(
        db=db,
        claude=None,  # type: ignore[arg-type] — unused; reviewer is faked
        runner=None,  # type: ignore[arg-type] — unused; reviewer is faked
        github=github,  # type: ignore[arg-type]
        reviewer=reviewer,  # type: ignore[arg-type]
        auditor=None,  # type: ignore[arg-type]
        ticket_analyzer=None,  # type: ignore[arg-type] — unused in review tests
        verifier=None,  # type: ignore[arg-type] — unused in review tests
        odoo=None,  # type: ignore[arg-type] — unused in review tests
    )
    set_context(context)
    return {
        "ctx": context,
        "db": db,
        "github": github,
        "reviewer": reviewer,
        "repo_id": repo_id,
        "pr_id": pr_id,
    }


def _params(s: dict, **overrides) -> dict:
    base = {
        "repository_id": s["repo_id"],
        "pull_request_id": s["pr_id"],
        "head_sha": "deadbeef",
        "installation_id": 500,
        "review_mode": "diff",
        "trigger_event": "opened",
    }
    base.update(overrides)
    return base


def _completed_result(findings: list[Finding] | None = None, diff: str = SAMPLE_DIFF) -> ReviewResult:
    return ReviewResult(
        status="completed",
        summary="Looks fine.",
        risk_level="low",
        findings=findings or [],
        diff=diff,
        model="claude-sonnet-4-6",
        prompt_version="v1.0",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_ms=1500,
        input_tokens=100,
        output_tokens=50,
        estimated_cost_usd=0.001,
    )


def _f(severity, *, file=None, line_start=None, confidence=0.8) -> Finding:
    return Finding(
        severity=severity,
        category="bug",
        file=file,
        line_start=line_start,
        line_end=line_start,
        title=f"{severity} finding",
        body="b",
        confidence=confidence,
        is_odoo_specific=False,
    )


# --- Tests -------------------------------------------------------------------


def test_completed_run_posts_check_and_review(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].result = _completed_result(
        findings=[
            _f("major", file="x.py", line_start=12),   # inline
            _f("info"),                                # unmapped (no file)
        ]
    )

    run_review(_params(s))

    assert len(s["github"].created_pr_reviews) == 1
    assert len(s["github"].created_check_runs) == 1
    assert len(s["github"].created_issue_comments) == 0
    assert s["github"].diff_fetch_count == 0  # diff is carried in result.diff, not re-fetched

    review = s["github"].created_pr_reviews[0]
    assert review["event"] == "COMMENT"
    assert review["commit_id"] == "deadbeef"
    assert len(review["comments"]) == 1  # only the in-hunk one
    assert review["comments"][0]["line"] == 12
    assert "**GENERAL**" in review["body"]  # unmapped found

    check = s["github"].created_check_runs[0]
    assert check["conclusion"] == "failure"  # major → failure
    assert check["status"] == "completed"


def test_completed_with_no_findings_is_success(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].result = _completed_result(findings=[])
    run_review(_params(s))

    check = s["github"].created_check_runs[0]
    assert check["conclusion"] == "success"
    review = s["github"].created_pr_reviews[0]
    assert review["comments"] == []
    assert "**GENERAL**" not in review["body"]


def test_completed_with_only_unmapped_findings_skips_inlines(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].result = _completed_result(
        findings=[_f("major", file="x.py", line_start=999)]  # out of hunk
    )
    run_review(_params(s))

    review = s["github"].created_pr_reviews[0]
    assert review["comments"] == []
    assert "**GENERAL**" in review["body"]


def test_declined_posts_issue_comment_and_neutral_check(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].result = ReviewResult(
        status="declined",
        summary="Diff too large.",
        risk_level="low",
        decline_reason="Diff too large (2000 lines > 1000 max).",
    )
    run_review(_params(s))

    assert len(s["github"].created_pr_reviews) == 0
    assert len(s["github"].created_issue_comments) == 1
    assert len(s["github"].created_check_runs) == 1
    assert s["github"].diff_fetch_count == 0  # no diff needed

    check = s["github"].created_check_runs[0]
    assert check["conclusion"] == "neutral"
    assert "Declined" in s["github"].created_issue_comments[0]["body"]


def test_stale_posts_only_skipped_check(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].result = ReviewResult(
        status="stale",
        summary="Head SHA drifted.",
        risk_level="low",
    )
    run_review(_params(s))

    assert len(s["github"].created_check_runs) == 1
    assert len(s["github"].created_pr_reviews) == 0
    assert len(s["github"].created_issue_comments) == 0

    assert s["github"].created_check_runs[0]["conclusion"] == "skipped"


def test_permanent_error_records_failed_and_posts_failure_check(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].raise_exc = PermanentError("Claude returned invalid JSON")

    with pytest.raises(PermanentError):
        run_review(_params(s))

    # Failed Check Run posted (failure conclusion).
    assert len(s["github"].created_check_runs) == 1
    assert s["github"].created_check_runs[0]["conclusion"] == "failure"
    # No PR review, no issue comment.
    assert s["github"].created_pr_reviews == []
    assert s["github"].created_issue_comments == []


def test_transient_error_bubbles_without_failed_record(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].raise_exc = TransientError("rate limited", retry_after=30)

    with pytest.raises(TransientError):
        run_review(_params(s))

    # No GitHub posting on transient — RQ will retry the whole job.
    assert s["github"].created_check_runs == []
    assert s["github"].created_pr_reviews == []
    # The review_run row exists in "running" state (record_review_started succeeded).


def test_idempotent_retry_skips_post(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].result = _completed_result()
    run_review(_params(s))

    # First call posted once.
    assert len(s["github"].created_check_runs) == 1
    first_review_count = len(s["github"].created_pr_reviews)
    assert s["reviewer"].call_count == 1

    # Second call: check_run_id is now set on the row → skip.
    out = run_review(_params(s))
    assert out == {"status": "already_posted"}
    assert len(s["github"].created_check_runs) == 1
    assert len(s["github"].created_pr_reviews) == first_review_count
    # Reviewer was NOT re-invoked because the early-return fires first.
    assert s["reviewer"].call_count == 1


def test_completed_persists_findings_to_db(ctx_and_fakes):
    s = ctx_and_fakes
    s["reviewer"].result = _completed_result(
        findings=[_f("major", file="x.py", line_start=12)]
    )
    run_review(_params(s))

    from reva.db.models import ReviewFinding, ReviewRun
    with s["db"].session() as session:
        runs = session.query(ReviewRun).all()
        assert len(runs) == 1
        assert runs[0].status == "completed"
        assert runs[0].check_run_id == 100
        assert runs[0].review_id == 200
        assert session.query(ReviewFinding).count() == 1


def test_settings_from_env_raises_on_missing(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY_PATH", raising=False)

    from worker.settings import Settings
    with pytest.raises(KeyError):
        Settings.from_env()


# --- run_comment_reply -------------------------------------------------------


def test_run_comment_reply_raises_permanent_on_missing_key(ctx_and_fakes):
    from worker.runner import run_comment_reply

    # Missing 'comment_id' key — should raise PermanentError, not KeyError
    with pytest.raises(PermanentError, match="missing required param"):
        run_comment_reply({
            "installation_id": 500,
            "owner": "acme",
            "repo": "widgets",
            "pr_number": 42,
            "question": "Is this safe?",
            # 'comment_id' intentionally absent
        })


# --- _verify_and_resolve_findings --------------------------------------------


def test_verify_and_resolve_calls_resolve_for_fixed_finding():
    """When Claude says resolved and thread exists, resolve_review_thread is called."""
    from worker.runner import _verify_and_resolve_findings

    ctx = MagicMock()
    ctx.github.get_review_threads.return_value = {12345: "THREAD_NODE_1"}
    ctx.github.get_file_content.return_value = "def foo(): pass"
    ctx.verifier.is_resolved.return_value = True

    params = MagicMock()
    params.pull_request_id = 1
    params.head_sha = "newsha"

    result = MagicMock()
    result.diff = (
        "diff --git a/custom_addons/foo.py b/custom_addons/foo.py\n"
        "+++ b/custom_addons/foo.py\n+fixed\n"
    )

    with patch("worker.runner.writers") as mock_writers:
        mock_writers.get_open_findings_for_pr.return_value = [{
            "id": 1,
            "file_path": "custom_addons/foo.py",
            "line_start": 10,
            "title": "Missing null check",
            "body": "user may be None",
            "severity": "major",
            "category": "bug",
            "github_comment_id": 12345,
        }]
        _verify_and_resolve_findings(ctx, params, result, "tok", "acme", "widgets", 42)

    ctx.github.resolve_review_thread.assert_called_once_with("tok", "THREAD_NODE_1")


def test_verify_and_resolve_skips_unfixed_finding():
    from worker.runner import _verify_and_resolve_findings

    ctx = MagicMock()
    ctx.github.get_review_threads.return_value = {12345: "THREAD_NODE_1"}
    ctx.github.get_file_content.return_value = "def foo(): x = user.name"
    ctx.verifier.is_resolved.return_value = False

    params = MagicMock()
    params.pull_request_id = 1
    params.head_sha = "newsha"

    result = MagicMock()
    result.diff = (
        "diff --git a/custom_addons/foo.py b/custom_addons/foo.py\n"
        "+++ b/custom_addons/foo.py\n+changed\n"
    )

    with patch("worker.runner.writers") as mock_writers:
        mock_writers.get_open_findings_for_pr.return_value = [{
            "id": 1,
            "file_path": "custom_addons/foo.py",
            "line_start": 10,
            "title": "Missing null check",
            "body": "user may be None",
            "severity": "major",
            "category": "bug",
            "github_comment_id": 12345,
        }]
        _verify_and_resolve_findings(ctx, params, result, "tok", "acme", "widgets", 42)

    ctx.github.resolve_review_thread.assert_not_called()


def test_verify_and_resolve_swallows_verification_error():
    from worker.runner import _verify_and_resolve_findings
    from reva.errors import TransientError

    ctx = MagicMock()
    ctx.github.get_review_threads.return_value = {12345: "THREAD_NODE_1"}
    ctx.github.get_file_content.return_value = "content"
    ctx.verifier.is_resolved.side_effect = TransientError("rate limited")

    params = MagicMock()
    params.pull_request_id = 1
    params.head_sha = "newsha"

    result = MagicMock()
    result.diff = (
        "diff --git a/custom_addons/foo.py b/custom_addons/foo.py\n"
        "+++ b/custom_addons/foo.py\n+changed\n"
    )

    with patch("worker.runner.writers") as mock_writers:
        mock_writers.get_open_findings_for_pr.return_value = [{
            "id": 1,
            "file_path": "custom_addons/foo.py",
            "line_start": 10,
            "title": "t",
            "body": "b",
            "severity": "minor",
            "category": "bug",
            "github_comment_id": 12345,
        }]
        # Must not raise
        _verify_and_resolve_findings(ctx, params, result, "tok", "acme", "widgets", 42)
