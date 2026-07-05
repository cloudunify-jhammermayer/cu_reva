"""Per-instance quota columns and spend sum writer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import TicketAnalysis, TicketIssueRun


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _instance(db: Database) -> int:
    return writers.create_odoo_instance(
        db, name="acme", key_hash="h", key_prefix="reva_odoo_x",
        callback_url="", callback_api_key_enc="",
    )


def _analysis_row(db, instance_id, cost, days_old=0):
    with db.session() as s:
        s.add(TicketAnalysis(
            odoo_instance_id=instance_id, ticket_id=1, model_name="m",
            field_name="f", input_text="t", status="completed",
            estimated_cost_usd=cost,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        ))


def _issue_row(db, instance_id, cost):
    with db.session() as s:
        s.add(TicketIssueRun(
            odoo_instance_id=instance_id, ticket_id=1, model_name="m",
            github_url="https://github.com/a/b", name="n", description="d",
            analysis_html="<p/>", priority="normal", ticket_url="u",
            status="created", estimated_cost_usd=cost,
        ))


def test_quota_fields_default_null_and_update(db):
    iid = _instance(db)
    row = writers.get_odoo_instance(db, iid)
    assert row["daily_budget_usd"] is None
    assert row["rate_limit_per_minute"] is None

    assert writers.update_odoo_instance(
        db, iid, daily_budget_usd=10.5, rate_limit_per_minute=30
    )
    row = writers.get_odoo_instance(db, iid)
    assert row["daily_budget_usd"] == pytest.approx(10.5)
    assert row["rate_limit_per_minute"] == 30

    assert writers.update_odoo_instance(db, iid, daily_budget_usd=None)
    assert writers.get_odoo_instance(db, iid)["daily_budget_usd"] is None


def test_sum_spans_both_run_tables_and_window(db):
    iid = _instance(db)
    other = writers.create_odoo_instance(
        db, name="other", key_hash="h2", key_prefix="reva_odoo_y",
        callback_url="", callback_api_key_enc="",
    )
    _analysis_row(db, iid, cost=1.25)
    _issue_row(db, iid, cost=0.75)
    _analysis_row(db, iid, cost=99.0, days_old=2)
    _analysis_row(db, other, cost=5.0)

    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert writers.sum_instance_cost_since(db, iid, since) == pytest.approx(2.0)
    assert writers.sum_instance_cost_since(db, other, since) == pytest.approx(5.0)


def test_sum_empty_is_zero(db):
    iid = _instance(db)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert writers.sum_instance_cost_since(db, iid, since) == 0.0
