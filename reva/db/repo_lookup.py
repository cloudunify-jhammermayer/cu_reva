"""Read-only DB helpers and the DatabaseRepoLookup adapter.

Module-level functions satisfy the Reviewer's RepoLookup Protocol and are
also called directly by runner.run_review without going through the adapter.
"""

from __future__ import annotations

from sqlalchemy import select

from reva.db.engine import Database
from reva.db.models import PullRequest, Repository, ReviewRun


def get_owner_name(db: Database, repository_id: int) -> tuple[str, str]:
    with db.session() as s:
        row = s.execute(
            select(Repository.owner, Repository.name).where(Repository.id == repository_id)
        ).first()
    if not row:
        raise LookupError(f"repository_id={repository_id} not found")
    return row[0], row[1]


def get_pr_basic(db: Database, pull_request_id: int) -> dict:
    with db.session() as s:
        row = s.execute(
            select(
                PullRequest.pr_number,
                PullRequest.title,
                PullRequest.base_branch,
                PullRequest.head_branch,
            ).where(PullRequest.id == pull_request_id)
        ).first()
    if not row:
        raise LookupError(f"pull_request_id={pull_request_id} not found")
    return {
        "pr_number": row[0],
        "title": row[1],
        "body": "",  # body is fetched from GitHub at review time
        "base_branch": row[2],
        "head_branch": row[3],
    }


def get_repo_meta(db: Database, repository_id: int) -> dict:
    """Return {owner, name, installation_id, default_branch} for a repo."""
    with db.session() as s:
        row = s.execute(
            select(
                Repository.owner,
                Repository.name,
                Repository.installation_id,
                Repository.default_branch,
            ).where(Repository.id == repository_id)
        ).first()
    if not row:
        raise LookupError(f"Repository {repository_id} not found")
    return {
        "owner": row.owner,
        "name": row.name,
        "installation_id": row.installation_id,
        "default_branch": row.default_branch or "main",
    }


def get_last_completed_review(db: Database, pull_request_id: int) -> dict | None:
    """Return {id, head_sha} of the most recent completed review_run, or None."""
    with db.session() as s:
        row = s.execute(
            select(ReviewRun.id, ReviewRun.head_sha)
            .where(ReviewRun.pull_request_id == pull_request_id)
            .where(ReviewRun.status == "completed")
            .order_by(ReviewRun.completed_at.desc())
            .limit(1)
        ).first()
    if not row:
        return None
    return {"id": row[0], "head_sha": row[1]}


class DatabaseRepoLookup:
    """Implements `worker.reviewer.RepoLookup` against the live DB."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def get_owner_name(self, repository_id: int) -> tuple[str, str]:
        return get_owner_name(self._db, repository_id)

    def get_pr_basic(self, pull_request_id: int) -> dict:
        return get_pr_basic(self._db, pull_request_id)

    def get_repo_meta(self, repository_id: int) -> dict:
        return get_repo_meta(self._db, repository_id)
