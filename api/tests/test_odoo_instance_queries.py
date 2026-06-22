from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.queries import odoo_instances as q
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import TicketAnalysis, TicketIssueRun


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def test_resolve_by_key_active_only(db: Database) -> None:
    # resolve_odoo_instance_by_key takes the RAW token and hashes it, so seed
    # the instance with the SHA-256 of a known token.
    import hashlib

    token = "reva_odoo_secret"
    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash=hashlib.sha256(token.encode()).hexdigest(),
        key_prefix="p", callback_url="", callback_api_key_enc="",
    )
    assert q.resolve_odoo_instance_by_key(db, token) == (iid, "ACME")
    assert q.resolve_odoo_instance_by_key(db, "wrong-token") is None

    # Deactivated instances no longer resolve.
    writers.update_odoo_instance(db, iid, active=False)
    assert q.resolve_odoo_instance_by_key(db, token) is None


def test_cost_windows_split_by_task(db: Database) -> None:
    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash="h", key_prefix="p",
        callback_url="", callback_api_key_enc="",
    )
    now = datetime.now(timezone.utc)
    with db.session() as s:
        s.add(TicketAnalysis(
            odoo_instance_id=iid, ticket_id=1, model_name="m", field_name="f",
            input_text="t", status="completed", estimated_cost_usd=2,
            input_tokens=10, output_tokens=5, created_at=now,
        ))
        s.add(TicketAnalysis(  # 40 days ago → outside 30d window
            odoo_instance_id=iid, ticket_id=2, model_name="m", field_name="f",
            input_text="t", status="completed", estimated_cost_usd=7,
            input_tokens=1, output_tokens=1, created_at=now - timedelta(days=40),
        ))
        s.add(TicketIssueRun(
            odoo_instance_id=iid, ticket_id=3, model_name="m",
            github_url="g", name="n", description="d", analysis_html="",
            priority="1", ticket_url="u", status="completed",
            estimated_cost_usd=3, input_tokens=20, output_tokens=8, created_at=now,
        ))
    cost = q.get_odoo_instance_cost(db, iid)
    assert cost["lifetime"]["analysis"]["cost_usd"] == 9.0
    assert cost["last_30d"]["analysis"]["cost_usd"] == 2.0
    assert cost["lifetime"]["issues"]["cost_usd"] == 3.0
    assert cost["last_24h"]["issues"]["input_tokens"] == 20
