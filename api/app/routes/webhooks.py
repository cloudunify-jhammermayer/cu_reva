"""POST /webhooks/github — receive, verify, and process GitHub webhook deliveries."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.dependencies import get_db, get_settings
from app.security import verify_signature
from app.settings import Settings
from reva.db import writers
from reva.db.engine import Database

router = APIRouter()
logger = structlog.get_logger()

# Actions that warrant a review; all others are stored but ignored.
_REVIEWABLE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


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

    # Idempotent: record_github_event returns None if delivery_id already exists.
    recorded = writers.record_github_event(
        db,
        delivery_id=x_github_delivery,
        event_type=x_github_event,
        action=payload.get("action"),
        repository_full_name=payload.get("repository", {}).get("full_name"),
        sender_login=payload.get("sender", {}).get("login"),
        payload=payload,
    )
    if recorded is None:
        log.info("webhook_duplicate")
        return {"status": "duplicate"}

    if x_github_event == "pull_request":
        _handle_pull_request(db, payload, settings)

    if x_github_event == "issue_comment":
        _handle_issue_comment(db, payload, settings)

    if x_github_event == "pull_request_review_comment":
        _handle_review_comment(payload, settings, request.app.state.rq_queue)

    log.info("webhook_accepted")
    return {"status": "accepted"}


def _handle_pull_requesty(db: Database, payload: dict, settings: Settings) -> None:
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
    "/deep-review": "full",
}


def _handle_issue_comment(db: Database, payload: dict, settings: Settings) -> None:
    if payload.get("action") != "created":
        return
    # Only PR comments, not plain issue comments
    if not payload.get("issue", {}).get("pull_request"):
        return

    body = (payload.get("comment", {}).get("body") or "").strip()
    command = body.split()[0].lower() if body else ""
    review_mode = _COMMENT_COMMANDS.get(command)
    if review_mode is None:
        return

    repo_data = payload["repository"]
    installation_id = payload["installation"]["id"]
    pr_number = payload["issue"]["number"]

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
