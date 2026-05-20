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

    payload = json.loads(body)
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

    log.info("webhook_accepted")
    return {"status": "accepted"}


def _handle_pull_request(db: Database, payload: dict, settings: Settings) -> None:
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
