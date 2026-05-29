"""Admin endpoints — manual triggers for background tasks."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.dependencies import get_db, get_github_client, get_settings, require_api_key
from app.settings import Settings
from reva._github_http import NotFound
from reva.db import writers
from reva.db.engine import Database

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_api_key)])
logger = structlog.get_logger()


@router.post("/weekly-report")
async def trigger_weekly_report(
    request: Request,
    days: int = 7,
) -> dict:
    """Manually enqueue a weekly report for the last `days` days.

    This does NOT record an entry in `weekly_reports`, so it won't delay
    the next scheduled send. Useful for testing the report format.
    """
    rq_queue = request.app.state.rq_queue
    job = rq_queue.enqueue("worker.runner.run_weekly_report", {"since_days": days})
    return {"status": "queued", "job_id": job.id, "since_days": days}


class TriggerReviewRequest(BaseModel):
    owner: str
    repo: str
    pr_number: int
    installation_id: int
    review_mode: str = "diff"


@router.post("/review", status_code=202)
def trigger_review(
    body: TriggerReviewRequest,
    request: Request,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Manually trigger a review for a PR that already exists on GitHub.

    Use this for PRs that were open before the GitHub App was installed.
    """
    try:
        token = github.get_installation_token(body.installation_id)
        pr_data = github.get_pull_request(token, body.owner, body.repo, body.pr_number)
    except NotFound:
        raise HTTPException(status_code=404, detail="PR not found on GitHub")

    repo_info = pr_data["base"]["repo"]
    repo_id = writers.upsert_repository(
        db,
        github_repository_id=repo_info["id"],
        owner=repo_info["owner"]["login"],
        name=repo_info["name"],
        default_branch=repo_info.get("default_branch", "main"),
        installation_id=body.installation_id,
    )

    head_sha = pr_data["head"]["sha"]
    pr_id = writers.upsert_pull_request(
        db,
        repository_id=repo_id,
        github_pr_id=pr_data["id"],
        pr_number=pr_data["number"],
        title=pr_data["title"],
        author_login=(pr_data.get("user") or {}).get("login"),
        base_branch=pr_data["base"]["ref"],
        head_branch=pr_data["head"]["ref"],
        head_sha=head_sha,
        state=pr_data.get("state", "open"),
        draft=pr_data.get("draft", False),
    )

    writers.upsert_pending_review(
        db,
        repository_id=repo_id,
        pull_request_id=pr_id,
        pr_number=pr_data["number"],
        head_sha=head_sha,
        installation_id=body.installation_id,
        trigger_event="manual",
        review_mode=body.review_mode,
        scheduled_at=datetime.now(timezone.utc),
    )

    logger.info(
        "admin_review_queued",
        owner=body.owner,
        repo=body.repo,
        pr=body.pr_number,
        sha=head_sha[:8],
        mode=body.review_mode,
    )
    return {
        "status": "queued",
        "pr_number": pr_data["number"],
        "head_sha": head_sha,
        "review_mode": body.review_mode,
    }
