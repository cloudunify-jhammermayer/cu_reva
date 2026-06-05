from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from rq import Queue

from app.dependencies import actor_from_request, get_db, get_github_client, get_queue
from app.queries import repos as q
from app.schemas.repos import AddRepoRequest, RepoPage, RepoSummary
from reva.claude_code_runner import REVIEW_JOB_TIMEOUT
from reva.db import writers
from reva.db.engine import Database
from reva.db.repo_lookup import get_repo_meta
from reva.errors import PermanentError

router = APIRouter()


@router.get("/repos", response_model=RepoPage)
def list_repos(db: Database = Depends(get_db)) -> dict:
    items, total = q.list_repos(db)
    return {"items": [RepoSummary.model_validate(r) for r in items], "total": total}


@router.post("/repos", status_code=201)
def add_repo(
    body: AddRepoRequest,
    request: Request,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> dict:
    """Register an app-installed repo on demand (so it can be audited without a PR).

    Resolves the installation + metadata from GitHub, then upserts the repo.
    404 if the repo doesn't exist or the REVA app isn't installed on it.
    """
    owner, name = body.owner.strip(), body.name.strip()
    if not owner or not name:
        raise HTTPException(status_code=422, detail="owner and name are required")
    try:
        installation_id = github.get_repo_installation_id(owner, name)
        token = github.get_installation_token(installation_id)
        meta = github.get_repo(token, owner, name)
    except PermanentError:
        raise HTTPException(
            status_code=404,
            detail=f"{owner}/{name} not found, or the REVA GitHub App is not installed on it",
        )

    repo_id = writers.upsert_repository(
        db,
        github_repository_id=meta["id"],
        owner=meta["owner"]["login"],
        name=meta["name"],
        default_branch=meta.get("default_branch") or "main",
        installation_id=installation_id,
    )
    writers.record_admin_action(
        db, action="add_repo", actor=actor_from_request(request),
        target=meta["full_name"],
        detail={"repository_id": repo_id, "installation_id": installation_id},
    )
    return {
        "repository_id": repo_id,
        "full_name": meta["full_name"],
        "default_branch": meta.get("default_branch") or "main",
        "installation_id": installation_id,
    }


@router.post("/repos/{repository_id}/audit", status_code=202)
def trigger_audit(
    repository_id: int,
    request: Request,
    db: Database = Depends(get_db),
    queue: Queue = Depends(get_queue),
) -> dict:
    """Enqueue a full repo audit job. Returns the RQ job ID."""
    try:
        meta = get_repo_meta(db, repository_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Repository {repository_id} not found")

    # Enqueue by string path (resolved on the worker): the api image ships only
    # reva + api/app, so importing the worker package here would 500 (CORR-1).
    job = queue.enqueue(
        "worker.audit_tasks.run_audit",
        {
            "repository_id": repository_id,
            "installation_id": meta["installation_id"],
        },
        job_timeout=REVIEW_JOB_TIMEOUT,
    )
    writers.record_admin_action(
        db, action="audit", actor=actor_from_request(request),
        target=f"repository_id={repository_id}", detail={"job_id": job.id},
    )
    return {"job_id": job.id, "repository_id": repository_id}
