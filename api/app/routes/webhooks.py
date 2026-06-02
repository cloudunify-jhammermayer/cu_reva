"""POST /webhooks/github — receive, verify, and process GitHub webhook deliveries."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.dependencies import get_db, get_settings
from app.security import verify_signature
from app.settings import Settings
from reva.db import writers
from reva.db.engine import Database

router = APIRouter()
logger = structlog.get_logger()

# Actions that warrant a review; all others are stored but ignored.
_REVIEWABLE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
# Actions that newly bring a PR into review — worth an 'on it' ack. Deliberately
# excludes 'synchronize' (fires on every push) so active PRs don't get spammed;
# the debounced review coalesces those pushes into one run anyway.
_ACK_PR_ACTIONS = frozenset({"opened", "reopened", "ready_for_review"})


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
            _handle_pull_request(db, payload, settings, github)
        elif event == "issue_comment":
            _handle_issue_comment(db, payload, settings, github)
        elif event == "pull_request_review_comment":
            _handle_review_comment(payload, settings, rq_queue)
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


def _handle_pull_request(db: Database, payload: dict, settings: Settings, github=None) -> None:
    action = payload.get("action", "")
    if action not in _REVIEWABLE_ACTIONS:
        return

    pr_data = payload["pull_request"]
    # Skip drafts unless this is the transition to ready_for_review.
    if pr_data.get("draft", False) and action != "ready_for_review":
        return

    repo_data = payload["repository"]
    installation_id = payload["installation"]["id"]

    repo_id = writers.upsert_repository(
        db,
        github_repository_id=repo_data["id"],
        owner=repo_data["owner"]["login"],
        name=repo_data["name"],
        default_branch=repo_data.get("default_branch", "main"),
        installation_id=installation_id,
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


def _handle_review_comment(payload: dict, settings: Settings, rq_queue) -> None:
    """Enqueue a reply when a developer replies to one of REVA's inline comments."""
    if payload.get("action") != "created":
        return

    comment = payload.get("comment", {})
    in_reply_to_id = comment.get("in_reply_to_id")
    if not in_reply_to_id:
        return  # top-level comment, not a reply — ignore

    # Never reply to other bots (including ourselves — prevents reply loops)
    if payload.get("sender", {}).get("type") == "Bot":
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
    if not owner or not repo:
        return

    rq_queue.enqueue(
        "worker.runner.run_comment_reply",
        {
            "installation_id": installation_id,
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "comment_id": in_reply_to_id,
            "question": question,
        },
    )
    logger.info(
        "comment_reply_queued",
        owner=owner,
        repo=repo,
        pr=pr_number,
        in_reply_to=in_reply_to_id,
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
    if payload.get("sender", {}).get("type") == "Bot":
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
