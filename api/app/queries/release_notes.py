"""Read queries for the release-log lookup endpoints."""

from __future__ import annotations

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import ReleaseNote


def list_release_notes(
    db: Database,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    with db.session() as s:
        base = select(ReleaseNote)
        count_q = select(func.count()).select_from(ReleaseNote)
        if status:
            base = base.where(ReleaseNote.status == status)
            count_q = count_q.where(ReleaseNote.status == status)

        total = s.execute(count_q).scalar_one()
        rows = s.execute(
            base.order_by(ReleaseNote.created_at.desc(), ReleaseNote.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()

        items = [
            {
                "id": r.id,
                "odoo_instance_id": r.odoo_instance_id,
                "release_id": r.release_id,
                "release_name": r.release_name,
                "slug": r.slug,
                "status": r.status,
                "source_repo_id": r.source_repo_id,
                "source_path": r.source_path,
                "url": r.url,
                "error": r.error,
                "callback_sent_at": r.callback_sent_at,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            }
            for r in rows
        ]
    return items, total
