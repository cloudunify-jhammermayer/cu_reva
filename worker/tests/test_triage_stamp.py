"""triage_escalation flows ReviewResult -> review_runs."""

from __future__ import annotations

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import ReviewRun
from reva.types import JobParams, ReviewResult


def _params() -> JobParams:
    return JobParams(
        repository_id=1,
        pull_request_id=1,
        head_sha="a" * 40,
        installation_id=5,
        review_mode="diff",
        trigger_event="synchronize",
    )


def test_stamp_persisted():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)
    result = ReviewResult(status="completed", summary="s", risk_level="low",
                          triage_escalation="deep")
    run_id = writers.record_review_completed(db, _params(), result)
    with db.session() as s:
        assert s.get(ReviewRun, run_id).triage_escalation == "deep"


def test_stamp_defaults_null():
    result = ReviewResult(status="completed", summary="s", risk_level="low")
    assert result.triage_escalation is None
