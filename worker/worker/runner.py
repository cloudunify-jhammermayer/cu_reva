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
from datetime import datetime, timedelta, timezone

import structlog

from reva.claude_client import ClaudeClient
from reva.claude_code_runner import ClaudeCodeRunner
from reva.notifications import notify_worker_error
from reva.odoo_client import OdooCallbackClient
from reva.ticket_analyzer import TicketAnalyzer
from reva.weekly_report import build_weekly_report
from reva.db import (
    Database,
    DatabaseRepoLookup,
    create_engine_from_url,
    repo_lookup,
    writers,
)
from reva.diff_utils import extract_file_paths, parse_diff_hunks
from reva.errors import PermanentError, TransientError
from reva.finding_verifier import FindingVerifier, StoredFinding
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
from worker.auditor import Auditor
from worker.reviewer import Reviewer
from worker.settings import Settings
from reva.types import JobParams, ReviewResult

logger = structlog.get_logger()


# ----------------------------------------------------------------- context


@dataclass(frozen=True)
class WorkerContext:
    db: Database
    claude: ClaudeClient
    runner: ClaudeCodeRunner
    github: GitHubClient
    reviewer: Reviewer
    auditor: Auditor
    ticket_analyzer: TicketAnalyzer
    verifier: FindingVerifier
    odoo: OdooCallbackClient
    google_chat_webhook_url: str = ""


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
    runner = ClaudeCodeRunner(
        repo_cache_dir=settings.repo_cache_dir,
        api_key=settings.anthropic_api_key,
        skills_dir=settings.skills_dir,
    )
    runner.evict_stale_repos(ttl_days=settings.repo_cache_ttl_days)
    logger.info("repo_cache_eviction_done", ttl_days=settings.repo_cache_ttl_days)
    github = GitHubClient(
        app_id=settings.github_app_id,
        private_key_pem=settings.github_private_key,
        base_url=settings.github_base_url,
    )
    prompts = PromptBuilder(prompts_dir=settings.prompts_dir)
    reviewer = Reviewer(
        runner=runner,
        github=github,
        repos=DatabaseRepoLookup(db),
        prompts=prompts,
    )
    auditor = Auditor(
        runner=runner,
        github=github,
        repos=DatabaseRepoLookup(db),
    )
    ticket_analyzer = TicketAnalyzer(claude=claude, prompts_dir=settings.prompts_dir)
    verifier = FindingVerifier(claude=claude)
    odoo = OdooCallbackClient(
        callback_url=settings.odoo_callback_url,
        api_key=settings.odoo_callback_api_key,
    )
    context = WorkerContext(
        db=db,
        claude=claude,
        runner=runner,
        github=github,
        reviewer=reviewer,
        auditor=auditor,
        ticket_analyzer=ticket_analyzer,
        verifier=verifier,
        odoo=odoo,
        google_chat_webhook_url=settings.google_chat_webhook_url,
    )
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

    # Resolve repo/PR metadata once — reused on all success and error paths
    # to avoid repeated DB round-trips per job.
    try:
        owner, name = repo_lookup.get_owner_name(ctx.db, params.repository_id)
        pr_basic = repo_lookup.get_pr_basic(ctx.db, params.pull_request_id)
        pr_number = pr_basic["pr_number"]
    except LookupError as exc:
        writers.record_review_failed(ctx.db, params, "permanent", str(exc))
        raise PermanentError(f"Repository or PR not found: {exc}") from exc

    log = log.bind(owner=owner, repo=name, pr_number=pr_number)

    result = _execute_and_persist(ctx, params, run_id, owner, name, pr_number, log)
    _post_result_to_github(ctx, params, result, run_id, owner, name, pr_number, log)

    log.info("review_job_done", status=result.status)
    return result.model_dump(mode="json")


def _execute_and_persist(
    ctx: WorkerContext,
    params: JobParams,
    run_id: int,
    owner: str,
    name: str,
    pr_number: int,
    log,
) -> ReviewResult:
    """Execute the reviewer and persist its outcome. Re-raises on any error."""
    try:
        result = ctx.reviewer.execute(params)
    except TransientError:
        # Don't write a "failed" row — RQ will retry; started status preserved.
        log.warning("review_transient_error", exc_info=True)
        raise
    except PermanentError as exc:
        log.error("review_permanent_error", error=str(exc))
        writers.record_review_failed(ctx.db, params, "permanent", str(exc))
        _post_failure_check_run(ctx, params, run_id, str(exc), owner, name)
        _notify_error(ctx, params, "PermanentError", str(exc), owner, name, pr_number)
        raise
    except Exception as exc:
        log.exception("review_unexpected_error")
        writers.record_review_failed(ctx.db, params, "permanent", str(exc))
        _post_failure_check_run(ctx, params, run_id, str(exc), owner, name)
        _notify_error(ctx, params, type(exc).__name__, str(exc), owner, name, pr_number)
        raise

    if result.status == "completed":
        writers.record_review_completed(ctx.db, params, result)
    elif result.status == "declined":
        writers.record_review_declined(ctx.db, params, result.decline_reason or "Declined.")
    elif result.status == "stale":
        writers.record_review_stale(ctx.db, params)

    return result


def _post_result_to_github(
    ctx: WorkerContext,
    params: JobParams,
    result: ReviewResult,
    run_id: int,
    owner: str,
    name: str,
    pr_number: int,
    log,
) -> None:
    """Mint a token, post Check Run / PR Review to GitHub, attach IDs to DB row."""
    try:
        token = ctx.github.get_installation_token(params.installation_id)

        if result.status == "completed":
            check_run_id, review_id = _post_completed(
                ctx, params, result, run_id, token, owner, name, pr_number
            )
            writers.attach_github_ids(ctx.db, run_id, check_run_id=check_run_id, review_id=review_id)
            _backfill_comment_ids(ctx, run_id, token, owner, name, pr_number, review_id)
            if result.delta_base_sha:
                _verify_and_resolve_findings(ctx, params, result, token, owner, name, pr_number, run_id)
        elif result.status == "declined":
            check_run_id = _post_declined(ctx, params, result, run_id, token, owner, name, pr_number)
            writers.attach_github_ids(ctx.db, run_id, check_run_id=check_run_id)
        elif result.status == "stale":
            check_run_id = _post_simple_check_run(
                ctx, params, result, run_id, token, owner, name, conclusion="skipped"
            )
            writers.attach_github_ids(ctx.db, run_id, check_run_id=check_run_id)
    except TransientError:
        log.warning("review_post_transient_error", exc_info=True)
        raise
    except Exception:
        log.exception("review_post_unexpected_error")
        raise


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
    ctx: WorkerContext, params: JobParams, run_id: int, error_message: str,
    owner: str, name: str,
) -> None:
    """Best-effort failure Check Run on PermanentError. Must not mask the original error."""
    try:
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


def _backfill_comment_ids(
    ctx: WorkerContext,
    run_id: int,
    token: str,
    owner: str,
    name: str,
    pr_number: int,
    review_id: int,
) -> None:
    """Best-effort: fetch the review's inline comments and persist their IDs
    so reply webhooks can be matched back to findings later."""
    try:
        gh_comments = ctx.github.get_review_comments(token, owner, name, pr_number, review_id)
        # Build lookup: (path, line_as_sent) → github_comment_id
        comment_by_loc: dict[tuple[str, int], int] = {}
        for c in gh_comments:
            path = c.get("path")
            line = c.get("line")
            if path and line:
                comment_by_loc[(path, line)] = c["id"]

        db_findings = writers.get_findings_for_run(ctx.db, run_id)
        id_map: dict[int, int] = {}
        for f in db_findings:
            if not f["file_path"] or not f["line_start"]:
                continue
            # GitHub stores `line` as line_end for multi-line, line_start for single-line.
            github_line = (
                f["line_end"]
                if f["line_end"] and f["line_end"] > f["line_start"]
                else f["line_start"]
            )
            key = (f["file_path"], github_line)
            if key in comment_by_loc:
                id_map[f["id"]] = comment_by_loc[key]

        if id_map:
            writers.attach_finding_comment_ids(ctx.db, id_map)
            logger.info("finding_comment_ids_backfilled", count=len(id_map), run_id=run_id)
    except Exception:
        logger.warning("backfill_comment_ids_failed", exc_info=True)


def _verify_and_resolve_findings(
    ctx: WorkerContext,
    params: JobParams,
    result: ReviewResult,
    token: str,
    owner: str,
    name: str,
    pr_number: int,
    run_id: int,
) -> None:
    """Best-effort: for old findings in touched files, verify if fixed and resolve threads."""
    try:
        threads = ctx.github.get_review_threads(token, owner, name, pr_number)
    except Exception:
        logger.warning("get_review_threads_failed", exc_info=True)
        return

    old_findings = writers.get_open_findings_for_pr(ctx.db, params.pull_request_id, before_run_id=run_id)
    touched_files = extract_file_paths(result.diff)

    for f in old_findings:
        if f["file_path"] not in touched_files:
            continue
        thread_id = threads.get(f["github_comment_id"])
        if not thread_id:
            continue  # thread already resolved by user — skip
        try:
            content = ctx.github.get_file_content(
                token, owner, name, f["file_path"], params.head_sha
            )
            if content is None:
                continue
            stored = StoredFinding(
                file_path=f["file_path"],
                line_start=f["line_start"],
                title=f["title"],
                body=f["body"],
                severity=f["severity"],
                category=f["category"],
            )
            if ctx.verifier.is_resolved(stored, content):
                ctx.github.resolve_review_thread(token, thread_id)
                logger.info(
                    "finding_resolved",
                    finding_id=f["id"],
                    file=f["file_path"],
                )
        except Exception:
            logger.warning("finding_verification_failed", finding_id=f["id"], exc_info=True)


def run_comment_reply(params: dict) -> None:
    """RQ task: reply to a developer's question on one of REVA's inline findings.

    params keys: installation_id, owner, repo, pr_number, comment_id (REVA's
    original comment), question (text of the developer's reply).
    """
    ctx = get_context()
    try:
        comment_id = params["comment_id"]
        installation_id = params["installation_id"]
        question = params["question"]
        owner = params["owner"]
        repo = params["repo"]
        pr_number = params["pr_number"]
    except KeyError as exc:
        raise PermanentError(f"run_comment_reply: missing required param {exc}") from exc

    log = logger.bind(comment_id=comment_id, owner=owner, repo=repo, pr=pr_number)

    finding = writers.lookup_finding_by_comment_id(ctx.db, comment_id)
    if finding is None:
        log.warning("reply_finding_not_found")
        return

    token = ctx.github.get_installation_token(installation_id)

    location = ""
    if finding["file_path"]:
        location = f"File: `{finding['file_path']}`"
        if finding["line_start"]:
            location += f" line {finding['line_start']}"

    system = (
        "You are REVA, an automated code review assistant. "
        "A developer has replied to one of your inline review comments with a question or comment. "
        "Respond concisely (2–4 sentences). Stay focused on the specific finding. "
        "If you're uncertain, say so. Do not repeat the finding title back to them."
    )
    user_prompt = (
        f"## Original finding ({finding['severity'].upper()}): {finding['title']}\n\n"
        + (f"{location}\n\n" if location else "")
        + f"{finding['body']}\n\n"
        + (
            f"**Suggestion:**\n```\n{finding['suggestion']}\n```\n\n"
            if finding["suggestion"]
            else ""
        )
        + f"## Developer's reply\n\n{question}"
    )

    reply_text = ctx.claude.chat(system=system, user=user_prompt)
    ctx.github.reply_to_review_comment(
        token=token,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        comment_id=comment_id,
        body=reply_text,
    )
    log.info("comment_reply_posted")


def _notify_error(
    ctx: WorkerContext, params: JobParams, error_class: str, message: str,
    owner: str, name: str, pr_number: int,
) -> None:
    """Best-effort Google Chat notification for server/API errors."""
    if not ctx.google_chat_webhook_url:
        return
    try:
        notify_worker_error(
            ctx.google_chat_webhook_url,
            repo_full_name=f"{owner}/{name}",
            pr_number=pr_number,
            error_class=error_class,
            message=message,
        )
    except Exception:
        logger.warning("notify_error_failed", exc_info=True)  # never mask the original error


# --------------------------------------------------------------- formatting


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------- weekly report task


def run_weekly_report(params: dict | None = None) -> None:
    """RQ task: build and post the weekly stats report to Google Chat.

    params (all optional):
      since_days  int   look-back window in days (default 7)
    """
    ctx = get_context()
    if not ctx.google_chat_webhook_url:
        logger.info("weekly_report_skipped_no_webhook")
        return

    since_days = int((params or {}).get("since_days", 7))
    since = datetime.now(timezone.utc) - timedelta(days=since_days)

    try:
        message = build_weekly_report(ctx.db, since=since)
    except Exception:
        logger.exception("weekly_report_build_failed")
        return

    try:
        import httpx
        httpx.post(ctx.google_chat_webhook_url, json={"text": message}, timeout=10)
        logger.info("weekly_report_sent", since_days=since_days)
    except Exception:
        logger.warning("weekly_report_send_failed", exc_info=True)
