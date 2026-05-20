"""End-to-end review orchestration.

`run_review` is what RQ calls for each dequeued job. It composes:
  - DB writers          : persist review_run + findings, attach GitHub IDs
  - Reviewer (pure)     : produce the ReviewResult
  - GitHubClient        : mint installation token, fetch diff, post Check Run + PR Review

Exception contract:
  - TransientError → bubbles to RQ for retry
  - PermanentError → recorded as a failed run + failure Check Run, then re-raised
  - Any other Exception → recorded as failed (class="permanent") + re-raised

Idempotency: on retry, if review_runs.check_run_id is already set, the
post step is skipped. Reviewer still re-runs (Claude tokens re-spent) —
documented in HANDOFF.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from reva.claude_client import ClaudeClient
from reva.db import (
    Database,
    DatabaseRepoLookup,
    create_engine_from_url,
    repo_lookup,
    writers,
)
from reva.diff_utils import parse_diff_hunks
from reva.errors import PermanentError, TransientError
from reva.github_client import GitHubClient
from reva.prompt_builder import PromptBuilder
from reva.review_formatter import (
    CHECK_RUN_NAME,
    REVIEW_EVENT,
    compute_check_conclusion,
    format_check_run_output,
    format_decline_body,
    format_inline_comment_payload,
    format_pr_review_body,
    split_findings,
)
from worker.reviewer import Reviewer
from worker.settings import Settings
from reva.types import JobParams, ReviewResult

logger = structlog.get_logger()


# ----------------------------------------------------------------- context


@dataclass(frozen=True)
class WorkerContext:
    db: Database
    claude: ClaudeClient
    github: GitHubClient
    reviewer: Reviewer


# Module-level singleton so RQ task functions (which can't take extra args)
# can reach the constructed clients. Initialized once in main.py.
_CONTEXT: WorkerContext | None = None


def set_context(context: WorkerContext) -> None:
    global _CONTEXT
    _CONTEXT = context


def get_context() -> WorkerContext:
    if _CONTEXT is None:
        raise RuntimeError(
            "WorkerContext not initialized; call set_context() at process startup"
        )
    return _CONTEXT


def build_worker_context(settings: Settings) -> WorkerContext:
    """Construct the singletons, register them as the process context, and run migrations."""
    engine = create_engine_from_url(settings.database_url)
    db = Database(engine)
    db.migrate(settings.migrations_dir)

    claude = ClaudeClient(api_key=settings.anthropic_api_key)
    github = GitHubClient(
        app_id=settings.github_app_id,
        private_key_pem=settings.github_private_key,
        base_url=settings.github_base_url,
    )
    prompts = PromptBuilder(prompts_dir=settings.prompts_dir)
    reviewer = Reviewer(
        claude=claude,
        github=github,
        repos=DatabaseRepoLookup(db),
        prompts=prompts,
    )
    context = WorkerContext(db=db, claude=claude, github=github, reviewer=reviewer)
    set_context(context)
    return context


# ---------------------------------------------------------------- task entry


def run_review(job_params: dict) -> dict:
    """RQ task entry point."""
    ctx = get_context()
    params = JobParams.model_validate(job_params)

    log = logger.bind(
        repository_id=params.repository_id,
        pull_request_id=params.pull_request_id,
        head_sha=params.head_sha[:8],
        review_mode=params.review_mode,
    )
    log.info("review_job_start")

    # Idempotent retry: if a previous attempt already created the Check Run,
    # there's nothing more to do.
    if writers.is_already_posted(ctx.db, params):
        log.info("review_already_posted")
        return {"status": "already_posted"}

    run_id = writers.record_review_started(ctx.db, params)

    # Reviewer.execute is pure. Errors flow through dedicated handlers below.
    try:
        result = ctx.reviewer.execute(params)
    except TransientError:
        # Don't write a "failed" row — RQ will retry; we want started status preserved.
        log.warning("review_transient_error", exc_info=True)
        raise
    except PermanentError as exc:
        log.error("review_permanent_error", error=str(exc))
        writers.record_review_failed(ctx.db, params, "permanent", str(exc))
        _post_failure_check_run(ctx, params, run_id, str(exc))
        raise
    except Exception as exc:
        # Truly unexpected — keep the surface narrow but don't lose data.
        log.exception("review_unexpected_error")
        writers.record_review_failed(ctx.db, params, "permanent", str(exc))
        _post_failure_check_run(ctx, params, run_id, str(exc))
        raise

    # Persist the outcome.
    if result.status == "completed":
        writers.record_review_completed(ctx.db, params, result)
    elif result.status == "declined":
        writers.record_review_declined(ctx.db, params, result.decline_reason or "Declined.")
    elif result.status == "stale":
        writers.record_review_stale(ctx.db, params)

    # Post to GitHub.
    owner, name = repo_lookup.get_owner_name(ctx.db, params.repository_id)
    pr_basic = repo_lookup.get_pr_basic(ctx.db, params.pull_request_id)
    pr_number = pr_basic["pr_number"]
    token = ctx.github.get_installation_token(params.installation_id)

    if result.status == "completed":
        check_run_id, review_id = _post_completed(
            ctx, params, result, run_id, token, owner, name, pr_number
        )
        writers.attach_github_ids(ctx.db, run_id, check_run_id=check_run_id, review_id=review_id)
    elif result.status == "declined":
        check_run_id = _post_declined(ctx, params, result, run_id, token, owner, name, pr_number)
        writers.attach_github_ids(ctx.db, run_id, check_run_id=check_run_id)
    elif result.status == "stale":
        check_run_id = _post_simple_check_run(
            ctx, params, result, run_id, token, owner, name, conclusion="skipped"
        )
        writers.attach_github_ids(ctx.db, run_id, check_run_id=check_run_id)

    log.info("review_job_done", status=result.status)
    return result.model_dump(mode="json")


# ---------------------------------------------------------------- post paths


def _post_completed(
    ctx: WorkerContext,
    params: JobParams,
    result: ReviewResult,
    run_id: int,
    token: str,
    owner: str,
    name: str,
    pr_number: int,
) -> tuple[int, int]:
    """Post a completed review: PR Review (with inline comments) + Check Run."""
    hunks = parse_diff_hunks(result.diff)
    inline, unmapped = split_findings(result.findings, hunks)

    body = format_pr_review_body(result, unmapped=unmapped, run_id=run_id)
    comments = [format_inline_comment_payload(f) for f in inline]

    review_id = ctx.github.create_pr_review(
        token=token,
        owner=owner,
        repo=name,
        pr_number=pr_number,
        commit_id=params.head_sha,
        event=REVIEW_EVENT,
        body=body,
        comments=comments,
    )

    check_run_id = ctx.github.create_check_run(
        token=token,
        owner=owner,
        repo=name,
        head_sha=params.head_sha,
        name=CHECK_RUN_NAME,
        status="completed",
        conclusion=compute_check_conclusion(result),
        started_at=_iso(result.started_at),
        completed_at=_iso(result.completed_at),
        output=format_check_run_output(result, run_id=run_id),
    )
    return check_run_id, review_id


def _post_declined(
    ctx: WorkerContext,
    params: JobParams,
    result: ReviewResult,
    run_id: int,
    token: str,
    owner: str,
    name: str,
    pr_number: int,
) -> int:
    """Post a decline: standalone PR comment + neutral Check Run."""
    ctx.github.create_issue_comment(
        token=token,
        owner=owner,
        repo=name,
        pr_number=pr_number,
        body=format_decline_body(result, run_id=run_id),
    )
    return ctx.github.create_check_run(
        token=token,
        owner=owner,
        repo=name,
        head_sha=params.head_sha,
        name=CHECK_RUN_NAME,
        status="completed",
        conclusion="neutral",
        started_at=None,
        completed_at=_now_iso(),
        output=format_check_run_output(result, run_id=run_id),
    )


def _post_simple_check_run(
    ctx: WorkerContext,
    params: JobParams,
    result: ReviewResult,
    run_id: int,
    token: str,
    owner: str,
    name: str,
    conclusion: str,
) -> int:
    """Post only a Check Run — used for stale and failed outcomes."""
    return ctx.github.create_check_run(
        token=token,
        owner=owner,
        repo=name,
        head_sha=params.head_sha,
        name=CHECK_RUN_NAME,
        status="completed",
        conclusion=conclusion,
        started_at=None,
        completed_at=_now_iso(),
        output=format_check_run_output(result, run_id=run_id),
    )


def _post_failure_check_run(
    ctx: WorkerContext, params: JobParams, run_id: int, error_message: str
) -> None:
    """Best-effort failure Check Run on PermanentError. Posting failures here
    must not mask the original error, so we swallow exceptions."""
    try:
        owner, name = repo_lookup.get_owner_name(ctx.db, params.repository_id)
        token = ctx.github.get_installation_token(params.installation_id)
        failed_result = ReviewResult(
            status="failed",
            summary="",
            risk_level="low",
            error_message=error_message,
            error_class="permanent",
        )
        check_run_id = _post_simple_check_run(
            ctx, params, failed_result, run_id, token, owner, name, conclusion="failure"
        )
        writers.attach_github_ids(ctx.db, run_id, check_run_id=check_run_id)
    except Exception:  # noqa: BLE001
        logger.exception("failure_check_run_post_failed")


# --------------------------------------------------------------- formatting


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
