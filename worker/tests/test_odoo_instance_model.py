"""ORM-level tests for the odoo_instances table + per-instance ticket scoping.

SQLite enforces the partial unique index via sqlite_where, so the cross-instance
dedup constraint is exercised here (the raw 018 migration SQL is Postgres-only).
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from reva.db import Base, Database, create_engine_from_url
from reva.db.models import OdooInstance, TicketIssueRun


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _instance(s, name: str) -> int:
    inst = OdooInstance(name=name, key_hash=f"hash-{name}", key_prefix="reva_odoo_x")
    s.add(inst)
    s.flush()
    return inst.id


def _pending_run(s, *, instance_id: int, ticket_id: int) -> None:
    s.add(
        TicketIssueRun(
            odoo_instance_id=instance_id, ticket_id=ticket_id,
            model_name="helpdesk.ticket", github_url="https://github.com/o/r",
            name="n", description="d", analysis_html="", priority="1",
            ticket_url="https://odoo/1", status="pending",
        )
    )
    s.flush()


def test_two_instances_share_ticket_id(db: Database) -> None:
    with db.session() as s:
        a = _instance(s, "a")
        b = _instance(s, "b")
        _pending_run(s, instance_id=a, ticket_id=42)
        _pending_run(s, instance_id=b, ticket_id=42)  # different instance → OK


def test_same_instance_duplicate_pending_rejected(db: Database) -> None:
    with pytest.raises(IntegrityError):
        with db.session() as s:
            a = _instance(s, "a")
            _pending_run(s, instance_id=a, ticket_id=42)
            _pending_run(s, instance_id=a, ticket_id=42)  # same instance → reject
