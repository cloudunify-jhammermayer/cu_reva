"""Read queries for repositories."""

from __future__ import annotations

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import Repository, ReviewRun


def list_repos(db: Database) -> tuple[list[dict], int]:
    with db.session() as s:
        # Subquery: count completed review_runs per repo + latest created_at.
        stats = (
            select(
                ReviewRun.repository_id,
                func.count(ReviewRun.id).label("review_count"),
                func.max(ReviewRun.created_at).label("last_review_at"),
            )
            .where(ReviewRun.status == "completed")
            .group_by(ReviewRun.repository_id)
            .subquery()
        )

        rows = s.execute(
            select(
                Repository,
                func.coalesce(stats.c.review_count, 0).label("review_count"),
                stats.c.last_review_at,
            )
            .outerjoin(stats, stats.c.repository_id == Repository.id)
            .order_by(Repository.full_name)
        ).all()

        items = [
            {
                "id": repo.id,
                "full_name": repo.full_name,
                "owner": repo.owner,
                "name": repo.name,
                "default_branch": repo.default_branch,
                "installation_id": repo.installation_id,
                "enabled": repo.enabled,
                "review_count": review_count,
                "last_review_at": last_review_at,
                "created_at": repo.created_at,
            }
            for repo, review_count, last_review_at in rows
        ]
    return items, len(items)
