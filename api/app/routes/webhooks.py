"""POST /webhooks/github — receive, verify, and process GitHub webhook deliveries."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import get_args

import structlog
import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from rq import Retry
from starlette.concurrency import run_in_threadpool

from app.dependencies import get_db, get_settings
from app.security import verify_signature
from app.settings import Settings
from reva.claude_code_runner import REVIEW_JOB_TIMEOUT
from reva.db import writers
from reva.db.engine import Database
from reva.ticket_links import parse_closing_refs
from reva.types import Category, RepoConfig

router = APIRouter()
logger = structlog.get_logger()

# Actions that warrant a review; all others are stored but ignored.
_REVIEWABLE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
# Actions that newly bring a PR into review — worth an 'on it' ack. Deliberately
# excludes 'synchronize' (fires on every push) so active PRs don't get spammed;
# the debounced review coalesces those pushes into one run anyway.
_ACK_PR_ACTIONS = frozenset({"opened", "reopened", "ready_for_review"})


def _is_bot_sender(payload: dict) -> bool:
    """Whether the event was sent by a Bot (incl. REVA itself). The anti-loop
    guard for every handler that would otherwise act on REVA's own actions."""
    return (payload.get("sender") or {}).get("type") == "Bot"


@router.post("/webhooks/github", status_code=202)
async def receive_webhook(
    request: Request,
    x_github_delivery: str = Header(...),
    x_hub_signature_256: str = Header(...),
    x_github_event: str = Header(...),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    body = await request.body()

    if not verify_signature(body, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    log = logger.bind(
        delivery_id=x_github_delivery,
        event=x_github_event,
        action=payload.get("action"),
        repo=payload.get("repository", {}).get("full_name"),
    )

    # The handlers do blocking SQLAlchemy / Redis I/O. Run them in the
    # threadpool so they don't stall the event loop (and serialize all other
    # in-flight webhook deliveries) for the duration of each DB round-trip.
    rq_queue = getattr(request.app.state, "rq_queue", None)
    github = getattr(request.app.state, "github", None)
    return await run_in_threadpool(
        _process_delivery,
        db, settings, rq_queue, github,
        x_github_event, x_github_delivery, payload, log,
    )


def _process_delivery(
    db: Database, settings: Settings, rq_queue, github, event: str,
    delivery_id: str, payload: dict, log,
) -> dict:
    """Synchronous event persistence + dispatch. Runs in the threadpool."""
    # record_github_event returns None only when this delivery was already
    # fully processed; a recorded-but-unfinished delivery (prior crash) is
    # returned for reprocessing. We mark it processed only after all the
    # downstream writes below have committed, so a failure mid-handling leaves
    # the event reprocessable instead of silently dropping the review.
    event_id = writers.record_github_event(
        db,
        delivery_id=delivery_id,
        event_type=event,
        action=payload.get("action"),
        repository_full_name=payload.get("repository", {}).get("full_name"),
        sender_login=payload.get("sender", {}).get("login"),
        payload=payload,
    )
    if event_id is None:
        log.info("webhook_duplicate")
        return {"status": "duplicate"}

    try:
        if event == "pull_request":
            _handle_pull_request(db, payload, settings, github, rq_queue)
        elif event == "issue_comment":
            _handle_issue_comment(db, payload, settings, github)
        elif event == "pull_request_review_comment":
            _handle_review_comment(db, payload, settings, rq_queue)
        elif event == "pull_request_review_thread":
            _handle_review_thread(db, payload)
        elif event == "issues":
            _handle_issues(payload, rq_queue)
        elif event == "installation":
            _handle_installation(db, payload, settings, github, rq_queue)
        elif event == "installation_repositories":
            _handle_installation_repositories(db, payload, settings, github, rq_queue)
    except (KeyError, TypeError) as exc:
        # A malformed/partial payload shape (missing key, wrong type) is not
        # fixable by redelivery — mark it processed so GitHub doesn't loop the
        # redelivery on a permanent 500 (CORR-13). Infra errors (DB/Redis) are
        # NOT caught here, so they still propagate → 5xx → legitimate redelivery.
        log.warning("webhook_malformed_payload", error=str(exc))
        writers.mark_event_processed(db, event_id)
        return {"status": "accepted", "warning": "malformed payload"}

    writers.mark_event_processed(db, event_id)
    log.info("webhook_accepted")
    return {"status": "accepted"}


def _upsert_repo_and_pr(db: Database, payload: dict) -> tuple[int, int]:
    """Upsert the repo + PR rows from a pull_request payload; return (repo_id, pr_id)."""
    repo_data = payload["repository"]
    pr_data = payload["pull_request"]
    repo_id = writers.upsert_repository(
        db,
        github_repository_id=repo_data["id"],
        owner=repo_data["owner"]["login"],
        name=repo_data["name"],
        default_branch=repo_data.get("default_branch", "main"),
        installation_id=payload["installation"]["id"],
    )
    pr_id = writers.upsert_pull_request(
        db,
        repository_id=repo_id,
        github_pr_id=pr_data["id"],
        pr_number=pr_data["number"],
        title=pr_data["title"],
        author_login=(pr_data.get("user") or {}).get("login"),
        base_branch=pr_data["base"]["ref"],
        head_branch=pr_data["head"]["ref"],
        head_sha=pr_data["head"]["sha"],
        state=pr_data["state"],
        draft=pr_data.get("draft", False),
    )
    return repo_id, pr_id


def _change_notes_enabled(github, payload: dict) -> bool:
    """Best-effort .claude-review.yml kill switch for merge change notes."""
    if github is None:
        return True
    try:
        repo = payload["repository"]
        pr = payload["pull_request"]
        token = github.get_installation_token(payload["installation"]["id"])
        raw = github.get_file_content(
            token,
            repo["owner"]["login"],
            repo["name"],
            ".claude-review.yml",
            pr["head"]["sha"],
        )
        if not raw:
            return True
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            return True
        return RepoConfig.model_validate(parsed).change_notes
    except Exception:
        logger.warning("change_notes_config_failed", exc_info=True)
        return True


def _handle_pull_request(db: Database, payload: dict, settings: Settings, github=None, rq_queue=None) -> None:
    action = payload.get("action", "")
    pr_data = payload["pull_request"]

    # A merged PR closes the loop on its findings: mark every still-open posted
    # finding as 'shipped without an observed fix' (outcome ledger). Only merges
    # count, not abandoned closes. Handled before the reviewable-action gate
    # (which excludes 'closed').
    if action == "closed" and pr_data.get("merged"):
        _, pr_id = _upsert_repo_and_pr(db, payload)
        marked = writers.mark_open_findings_at_merge(db, pr_id)
        logger.info("findings_marked_at_merge", pr=pr_data.get("number"), count=marked)
        if (
            rq_queue is not None
            and parse_closing_refs(pr_data.get("body"))
            and _change_notes_enabled(github, payload)
        ):
            repo_data = payload["repository"]
            rq_queue.enqueue(
                "worker.change_note_tasks.run_change_note",
                {
                    "repo_full_name": repo_data["full_name"].lower(),
                    "pr_number": pr_data["number"],
                    "pr_title": pr_data.get("title") or "",
                    "pr_body": pr_data.get("body") or "",
                    "pr_url": pr_data.get("html_url") or "",
                    "installation_id": payload["installation"]["id"],
                },
                retry=Retry(max=3, interval=[30, 120, 300]),
            )
        return

    if action not in _REVIEWABLE_ACTIONS:
        logger.info("pr_event_ignored", reason="non-reviewable action", action=action)
        return

    # Skip drafts unless this is the transition to ready_for_review.
    if pr_data.get("draft", False) and action != "ready_for_review":
        logger.info("pr_event_ignored", reason="draft PR", action=action,
                    pr=pr_data.get("number"))
        return

    repo_data = payload["repository"]
    installation_id = payload["installation"]["id"]
    repo_id, pr_id = _upsert_repo_and_pr(db, payload)

    scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=settings.debounce_seconds)
    writers.upsert_pending_review(
        db,
        repository_id=repo_id,
        pull_request_id=pr_id,
        pr_number=pr_data["number"],
        head_sha=pr_data["head"]["sha"],
        installation_id=installation_id,
        trigger_event=action,
        review_mode=settings.default_review_mode,
        scheduled_at=scheduled_at,
    )

    logger.info(
        "pending_review_upserted",
        repo=repo_data.get("full_name"),
        pr=pr_data["number"],
        sha=pr_data["head"]["sha"][:8],
        scheduled_in_s=settings.debounce_seconds,
    )

    # Immediate acknowledgement when a PR newly enters review (not on every push).
    if action in _ACK_PR_ACTIONS:
        _post_ack_comment(
            github, installation_id, repo_data["owner"]["login"], repo_data["name"],
            pr_data["number"], settings.default_review_mode,
        )


_INLINE_COMMANDS = frozenset({"/dismiss", "/mute", "/unmute"})
# Canonical finding categories that may be muted (kept in sync with reva.types.Category).
_MUTABLE_CATEGORIES = frozenset(get_args(Category))


def _handle_inline_command(db: Database, payload: dict, body: str, in_reply_to_id: int) -> None:
    """Record a structured inline command (zero Claude cost).

    `/dismiss [reason]` writes a negative review_feedback row on the replied-to
    finding. `/mute <category>` / `/unmute <category>` toggle a repo-wide category
    mute (defaulting to the replied-to finding's category if none is given).
    """
    parts = body.split()
    command = parts[0].lower()
    sender = (payload.get("sender") or {}).get("login") or ""

    if command == "/dismiss":
        finding = writers.lookup_finding_by_comment_id(db, in_reply_to_id)
        if finding is None:
            logger.info("dismiss_no_finding", comment_id=in_reply_to_id)
            return
        written = writers.record_feedback(
            db,
            review_finding_id=finding["id"],
            review_run_id=finding["review_run_id"],
            github_comment_id=in_reply_to_id,
            reactor_login=sender,
            reaction="dismissed",
            is_positive=False,
        )
        logger.info("finding_dismissed", finding_id=finding["id"], deduped=written is None)
        return

    # /mute | /unmute — resolve the category (explicit arg, else the finding's).
    category = parts[1].lower() if len(parts) > 1 else None
    if category is None:
        finding = writers.lookup_finding_by_comment_id(db, in_reply_to_id)
        category = finding["category"] if finding else None
    if category not in _MUTABLE_CATEGORIES:
        logger.info("mute_invalid_category", command=command, category=category)
        return

    repo_data = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    if not repo_data.get("id") or not installation.get("id"):
        return
    repo_id = writers.upsert_repository(
        db,
        github_repository_id=repo_data["id"],
        owner=repo_data["owner"]["login"],
        name=repo_data["name"],
        default_branch=repo_data.get("default_branch", "main"),
        installation_id=installation["id"],
    )
    active = command == "/mute"
    writers.set_category_mute(
        db, repository_id=repo_id, category=category, muted_by=sender, active=active
    )
    logger.info(
        "category_mute_set",
        repo=repo_data.get("full_name"), category=category, active=active,
    )


def _handle_review_comment(db: Database, payload: dict, settings: Settings, rq_queue) -> None:
    """Handle a reply to one of REVA's inline comments. A structured command
    (`/dismiss`, `/mute`, `/unmute`) is recorded at zero Claude cost; anything
    else enqueues the (paid) conversational reply."""
    if payload.get("action") != "created":
        return

    comment = payload.get("comment", {})
    in_reply_to_id = comment.get("in_reply_to_id")
    if not in_reply_to_id:
        return  # top-level comment, not a reply — ignore

    # Never reply to other bots (including ourselves — prevents reply loops)
    if _is_bot_sender(payload):
        return

    # SECU-3: a reply drives a paid Claude call, so only commenters with
    # write-equivalent standing may trigger one — same gate as slash commands.
    # Without this, any non-bot user (e.g. an external PR author) could rack up
    # spend by replying to REVA's inline comments.
    if comment.get("author_association") not in _TRUSTED_ASSOCIATIONS:
        logger.info(
            "comment_reply_ignored_untrusted",
            association=comment.get("author_association"),
            sender=payload.get("sender", {}).get("login"),
        )
        return

    question = (comment.get("body") or "").strip()
    if not question:
        return

    # Structured zero-cost commands short-circuit the paid reply.
    if question.split()[0].lower() in _INLINE_COMMANDS:
        _handle_inline_command(db, payload, question, in_reply_to_id)
        return

    pr_data = payload.get("pull_request", {})
    pr_number = pr_data.get("number")
    if not pr_number:
        return

    installation_id = (payload.get("installation") or {}).get("id")
    if not installation_id:
        return

    repo_data = payload.get("repository", {})
    owner = (repo_data.get("owner") or {}).get("login")
    repo = repo_data.get("name")
    if not owner or not repo or rq_queue is None:
        return

    rq_queue.enqueue(
        "worker.tasks.run_comment_reply",
        {
            "installation_id": installation_id,
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "comment_id": in_reply_to_id,
            "question": question,
        },
        # M9: a transient chat()/GitHub blip shouldn't silently drop the
        # developer's question. The reply is idempotent enough to retry (worst
        # case a duplicate reply comment on repeated transient failures), and the
        # task contract keeps a PermanentError from being retried.
        retry=Retry(max=3, interval=[30, 120, 300]),
    )
    logger.info(
        "comment_reply_queued",
        owner=owner,
        repo=repo,
        pr=pr_number,
        in_reply_to=in_reply_to_id,
    )


_REVIEW_THREAD_ACTIONS = frozenset({"resolved", "unresolved"})


def _handle_review_thread(db: Database, payload: dict) -> None:
    """Capture developer feedback when a REVA finding's comment thread is marked
    resolved (accept) or unresolved (reject/reopen) — the only webhook-delivered
    signal for this (GitHub fires no webhook for 👍/👎 reactions). Writes a
    review_feedback row keyed to the owning finding via the thread's root comment.
    """
    action = payload.get("action")
    if action not in _REVIEW_THREAD_ACTIONS:
        return
    # Anti-loop: ignore a thread resolved/unresolved by a bot (incl. REVA itself).
    if _is_bot_sender(payload):
        return

    comments = (payload.get("thread") or {}).get("comments") or []
    root = next((c for c in comments if c.get("in_reply_to_id") is None), None)
    if root is None or root.get("id") is None:
        return
    comment_id = root["id"]

    finding = writers.lookup_finding_by_comment_id(db, comment_id)
    if finding is None:
        return  # not one of REVA's finding threads

    written = writers.record_feedback(
        db,
        review_finding_id=finding["id"],
        review_run_id=finding["review_run_id"],
        github_comment_id=comment_id,
        reactor_login=(payload.get("sender") or {}).get("login") or "",
        reaction=action,
        is_positive=(action == "resolved"),
    )
    logger.info(
        "review_feedback_recorded",
        finding_id=finding["id"], reaction=action, deduped=written is None,
    )


# Label REVA puts on every issue it creates from an Odoo ticket — cheap
# pre-filter so unrelated repo issues never hit the DB or the queue.
_TICKET_ISSUE_LABEL = "reva-ticket"
# GitHub issue actions that change the state we track per issue.
_ISSUE_STATE_ACTIONS: dict[str, str] = {"closed": "closed", "reopened": "open"}


def _handle_issues(payload: dict, rq_queue) -> None:
    """A ticket issue was closed (done) or reopened → sync the per-issue state
    to the DB and notify Odoo, via the worker (it owns the Odoo client)."""
    state = _ISSUE_STATE_ACTIONS.get(payload.get("action", ""))
    if state is None:
        return

    issue = payload.get("issue") or {}
    labels = {(label.get("name") or "") for label in issue.get("labels") or []}
    if _TICKET_ISSUE_LABEL not in labels:
        return

    repo_data = payload.get("repository") or {}
    owner = (repo_data.get("owner") or {}).get("login")
    repo = repo_data.get("name")
    number = issue.get("number")
    if not owner or not repo or not number or rq_queue is None:
        return

    rq_queue.enqueue(
        "worker.ticket_issue_tasks.sync_ticket_issue_state",
        {"owner": owner, "repo": repo, "number": number, "state": state,
         # closed_at → per-issue complete_date; None on reopen (cleared).
         "closed_at": issue.get("closed_at")},
        # The Odoo notify must survive a transient Odoo outage (same policy as
        # the issues-created callback); the sync is idempotent.
        retry=Retry(max=3, interval=[30, 120, 300]),
    )
    logger.info(
        "ticket_issue_state_queued",
        repo=repo_data.get("full_name"), issue=number, state=state,
    )


_COMMENT_COMMANDS: dict[str, str] = {
    "/review": "diff",
    "/review-all": "diff-all",  # diff review over ALL paths, not just custom_addons
    "/full-review": "full",
    "/deep-review": "deep",  # full repo exploration + Opus model
}

# Only commenters with write-equivalent standing may trigger a (paid) review.
_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


_ACK_LABEL: dict[str, str] = {
    "diff": "a standard review",
    "full": "a full repository-aware review",
    "deep": "a deep review (this one takes a little longer)",
}


def _post_ack_comment(github, installation_id: int, owner: str, repo: str,
                      pr_number: int, review_mode: str) -> None:
    """Best-effort 'on it' comment so the developer knows the review is queued.

    Never let a failed ack break webhook processing — the review still runs.
    """
    if github is None:
        return
    try:
        token = github.get_installation_token(installation_id)
        github.create_issue_comment(
            token=token, owner=owner, repo=repo, pr_number=pr_number,
            body=(
                f"👀 **REVA** is on it — running {_ACK_LABEL.get(review_mode, 'a review')}. "
                f"I'll post my findings on this PR shortly."
            ),
        )
    except Exception:
        logger.warning("comment_ack_post_failed", repo=f"{owner}/{repo}", pr=pr_number, exc_info=True)


def _fetch_and_upsert_pr(db: Database, github, repo_id: int, installation_id: int,
                         owner: str, repo: str, pr_number: int) -> dict | None:
    """Fetch a PR from GitHub and record it; return its lookup dict or None.

    Used when a comment command targets a PR REVA has never seen (e.g. one
    opened before the app was installed). Best-effort: returns None if the
    GitHub client is unavailable or the fetch fails.
    """
    if github is None:
        return None
    try:
        token = github.get_installation_token(installation_id)
        pr_data = github.get_pull_request(token, owner, repo, pr_number)
    except Exception:
        logger.warning(
            "comment_trigger_pr_fetch_failed",
            repo=f"{owner}/{repo}", pr=pr_number, exc_info=True,
        )
        return None

    pr_id = writers.upsert_pull_request(
        db,
        repository_id=repo_id,
        github_pr_id=pr_data["id"],
        pr_number=pr_data["number"],
        title=pr_data["title"],
        author_login=(pr_data.get("user") or {}).get("login"),
        base_branch=pr_data["base"]["ref"],
        head_branch=pr_data["head"]["ref"],
        head_sha=pr_data["head"]["sha"],
        state=pr_data["state"],
        draft=pr_data.get("draft", False),
    )
    return {"id": pr_id, "head_sha": pr_data["head"]["sha"], "installation_id": installation_id}


def _handle_issue_comment(db: Database, payload: dict, settings: Settings, github=None) -> None:
    if payload.get("action") != "created":
        return
    # Only PR comments, not plain issue comments
    if not payload.get("issue", {}).get("pull_request"):
        return

    # Never act on a bot's comment — prevents REVA triggering itself (cost loop).
    if _is_bot_sender(payload):
        return

    comment = payload.get("comment", {})
    body = (comment.get("body") or "").strip()
    command = body.split()[0].lower() if body else ""
    review_mode = _COMMENT_COMMANDS.get(command)
    if review_mode is None:
        return

    # Authorization: only repo owners/members/collaborators may spend on reviews.
    if comment.get("author_association") not in _TRUSTED_ASSOCIATIONS:
        logger.warning(
            "comment_trigger_unauthorized",
            repo=payload.get("repository", {}).get("full_name"),
            association=comment.get("author_association"),
            sender=payload.get("sender", {}).get("login"),
        )
        return

    repo_data = payload.get("repository")
    installation = payload.get("installation") or {}
    pr_number = payload.get("issue", {}).get("number")
    if not repo_data or not installation.get("id") or not pr_number:
        return
    installation_id = installation["id"]

    repo_id = writers.upsert_repository(
        db,
        github_repository_id=repo_data["id"],
        owner=repo_data["owner"]["login"],
        name=repo_data["name"],
        default_branch=repo_data.get("default_branch", "main"),
        installation_id=installation_id,
    )

    pr_info = writers.lookup_pull_request(db, repo_id, pr_number)
    if pr_info is None:
        # PR predates the installation — GitHub doesn't replay past 'opened'
        # events, so REVA has no row for it. Fetch it from GitHub and record it
        # so the comment-triggered review can proceed.
        pr_info = _fetch_and_upsert_pr(
            db, github, repo_id, installation_id,
            repo_data["owner"]["login"], repo_data["name"], pr_number,
        )
    if pr_info is None:
        logger.warning(
            "comment_trigger_pr_not_found",
            repo=repo_data.get("full_name"),
            pr=pr_number,
        )
        return

    writers.upsert_pending_review(
        db,
        repository_id=repo_id,
        pull_request_id=pr_info["id"],
        pr_number=pr_number,
        head_sha=pr_info["head_sha"],
        installation_id=pr_info["installation_id"],
        trigger_event="comment",
        review_mode=review_mode,
        scheduled_at=datetime.now(timezone.utc),  # immediate — no debounce
    )

    logger.info(
        "comment_trigger_queued",
        repo=repo_data.get("full_name"),
        pr=pr_number,
        mode=review_mode,
        command=command,
    )

    # Immediate acknowledgement so the developer sees REVA picked up the request.
    _post_ack_comment(
        github, installation_id, repo_data["owner"]["login"], repo_data["name"],
        pr_number, review_mode,
    )


def _handle_installation(db: Database, payload: dict, settings: Settings, github,
                         rq_queue) -> None:
    """App freshly installed → register and audit every repo it was granted.

    Only the 'created' action matters; 'deleted'/'suspend'/etc. are stored but
    ignored. GitHub does not replay past activity, so this is the only chance to
    audit pre-existing repos automatically (the rest comes via PR webhooks).
    """
    if payload.get("action") != "created":
        return
    installation_id = (payload.get("installation") or {}).get("id")
    if not installation_id:
        return
    _audit_installed_repos(db, settings, github, rq_queue, installation_id,
                           payload.get("repositories"))


def _handle_installation_repositories(db: Database, payload: dict, settings: Settings,
                                      github, rq_queue) -> None:
    """Repos added to an existing installation → audit the newly-added ones."""
    if payload.get("action") != "added":
        return
    installation_id = (payload.get("installation") or {}).get("id")
    if not installation_id:
        return
    _audit_installed_repos(
        db, settings, github, rq_queue, installation_id,
        payload.get("repositories_added"),
    )


def _audit_installed_repos(db: Database, settings: Settings, github, rq_queue,
                           installation_id: int, repos: list | None) -> None:
    """Register each repo and enqueue a full audit job (which clones it itself).

    Best-effort per repo: a missing client or a GitHub fetch failure skips that
    repo and logs, never the whole delivery — the same resilience as the ack and
    PR-fetch paths. The audit job clones the repo and respects the spend cap.

    When `settings.auto_audit_repos` is false the repos are still registered, but
    the audit job is not enqueued.
    """
    repos = repos or []
    if not repos:
        return
    if github is None or rq_queue is None:
        logger.warning("installation_audit_skipped_no_client",
                       installation_id=installation_id, repos=len(repos))
        return

    try:
        token = github.get_installation_token(installation_id)
    except Exception:
        logger.warning("installation_token_failed",
                       installation_id=installation_id, exc_info=True)
        return

    for entry in repos:
        owner, _, name = (entry.get("full_name") or "").partition("/")
        if not owner or not name:
            continue
        # Fetch metadata for the canonical id + default_branch (not in the
        # installation payload), mirroring the on-demand add_repo path.
        try:
            meta = github.get_repo(token, owner, name)
        except Exception:
            logger.warning("installation_repo_fetch_failed",
                           repo=f"{owner}/{name}", exc_info=True)
            continue

        repo_id = writers.upsert_repository(
            db,
            github_repository_id=meta["id"],
            owner=meta["owner"]["login"],
            name=meta["name"],
            default_branch=meta.get("default_branch") or "main",
            installation_id=installation_id,
        )
        if not settings.auto_audit_repos:
            logger.info("installation_audit_disabled", repo=meta["full_name"],
                        repository_id=repo_id)
            continue
        job = rq_queue.enqueue(
            "worker.audit_tasks.run_audit",
            {
                "repository_id": repo_id,
                "installation_id": installation_id,
                "requested_by": "installation",
            },
            job_timeout=REVIEW_JOB_TIMEOUT,
        )
        logger.info("installation_audit_queued", repo=meta["full_name"],
                    repository_id=repo_id, job_id=job.id)
