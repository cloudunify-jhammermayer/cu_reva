from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from rq import Queue

from app.dependencies import get_db, get_queue
from app.queries import repos as q
from app.schemas.repos import RepoPage, RepoSummary
from reva.db.engine import Database
from reva.db.repo_lookup import get_repo_meta

router = APIRouter()


@router.get("/repos", response_model=RepoPage)
def list_repos(db: Database = Depends(get_db)) -> dict:
    items, total = q.list_repos(db)
    return {"items": [RepoSummary.model_validate(r) for r in items], "total": total}


@router.post("/repos/{repository_id}/audit", status_code=202)
def trigger_audit(
    repository_id: int,
    db: Database = Depends(get_db),
    queue: Queue = Depends(get_queue),
) -> dict:
    """Enqueue a full repo audit job. Returns the RQ job ID."""
    from worker.audit_tasks import run_audit

    try:
        meta = get_repo_meta(db, repository_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Repository {repository_id} not found")

    job = queue.enqueue(
        run_audit,
        {
            "repository_id": repository_id,
            "installation_id": meta["installation_id"],
        },
    )
    return {"job_id": job.id, "repository_id": repository_id}
