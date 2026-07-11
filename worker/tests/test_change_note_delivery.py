"""Batched change-note delivery on the ready convergence (spec 2026-07-11).

Covers maybe_deliver_change_notes directly (the delivery matrix) plus the
change-note job tail — which no longer posts a per-PR note, only defers to the
convergent condition. Fakes only, no network."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from reva.db.engine import Database, create_engine_from_url
from reva.db.models import Base, ChangeNote, OpsEvent, TicketIssueRun
from reva.errors import PermanentError, TransientError
from worker.change_note_delivery import maybe_deliver_change_notes

_INSTANCE = 1
_TICKET = 97
_MODEL = "helpdesk.ticket"


@dataclass
class FakeOdoo:
    raise_exc: Exception | None = None
    calls: list[dict] = field(default_factory=list)

    def change_summary(self, ticket_id, model_name, notes):
        self.calls.append(
            {"ticket_id": ticket_id, "model_name": model_name, "notes": notes}
        )
        if self.raise_exc:
            raise self.raise_exc


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _seed_run(db, *, issues):
    with db.session() as s:
        s.add(TicketIssueRun(
            odoo_instance_id=_INSTANCE, ticket_id=_TICKET, model_name=_MODEL,
            github_url="https://github.com/acme/widgets",
            repo_full_name="acme/widgets", status="completed",
            name="t", description="d", analysis_html="<p/>",
            priority="1", ticket_url="https://odoo.example/tickets/97",
            issues=issues,
        ))


def _ready_run(db):
    _seed_run(db, issues=[
        {"number": 50, "title": "a", "url": "https://gh/50", "state": "closed"},
        {"number": 51, "title": "b", "url": "https://gh/51", "state": "closed"},
    ])


def _seed_note(db, *, pr_number, status="completed", note_html="<p>n</p>",
               delivered_at=None, pr_title="PR title", pr_url=None):
    with db.session() as s:
        s.add(ChangeNote(
            repo_full_name="acme/widgets", pr_number=pr_number, ticket_id=_TICKET,
            odoo_instance_id=_INSTANCE, model_name=_MODEL, status=status,
            note_html=note_html if status == "completed" else None,
            pr_title=pr_title,
            pr_url=pr_url or f"https://github.com/acme/widgets/pull/{pr_number}",
            delivered_at=delivered_at,
        ))


def _deliver(db, odoo):
    return maybe_deliver_change_notes(
        SimpleNamespace(db=db), odoo, _INSTANCE, _TICKET, _MODEL
    )


def _note_rows(db):
    with db.session() as s:
        return s.execute(
            select(ChangeNote).order_by(ChangeNote.pr_number)
        ).scalars().all()


def _ops_events(db):
    with db.session() as s:
        return [r.event for r in s.execute(select(OpsEvent)).scalars().all()]


# --- delivery matrix ----------------------------------------------------------


def test_ready_with_one_completed_note_delivers_batch(db):
    _ready_run(db)
    _seed_note(db, pr_number=7)
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is True
    assert len(odoo.calls) == 1
    call = odoo.calls[0]
    assert call["ticket_id"] == _TICKET and call["model_name"] == _MODEL
    assert call["notes"] == [{
        "pr": {"number": 7, "title": "PR title",
               "url": "https://github.com/acme/widgets/pull/7", "repo": "acme/widgets"},
        "note_html": "<p>n</p>",
    }]
    assert _note_rows(db)[0].delivered_at is not None


def test_not_ready_does_not_deliver(db):
    _seed_run(db, issues=[
        {"number": 50, "state": "closed"}, {"number": 51, "state": "open"},
    ])
    _seed_note(db, pr_number=7)
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is False
    assert odoo.calls == []
    assert _note_rows(db)[0].delivered_at is None


def test_pending_note_blocks_delivery(db):
    _ready_run(db)
    _seed_note(db, pr_number=7, status="completed")
    _seed_note(db, pr_number=8, status="pending")
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is False
    assert odoo.calls == []


def test_failed_note_does_not_block_and_is_excluded(db):
    _ready_run(db)
    _seed_note(db, pr_number=7, status="completed")
    _seed_note(db, pr_number=8, status="failed")
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is True
    numbers = [n["pr"]["number"] for n in odoo.calls[0]["notes"]]
    assert numbers == [7]  # failed note excluded from the batch


def test_post_ready_single_note_batch(db):
    # Ready already held; a late PR's note completes → a batch of one.
    _ready_run(db)
    _seed_note(db, pr_number=9)
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is True
    assert len(odoo.calls[0]["notes"]) == 1


def test_reopen_reready_delivers_only_new_rows(db):
    _ready_run(db)
    _seed_note(db, pr_number=7)
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is True  # ships note 7
    _seed_note(db, pr_number=8)        # a new PR after re-ready
    assert _deliver(db, odoo) is True  # ships only note 8
    assert [n["pr"]["number"] for n in odoo.calls[1]["notes"]] == [8]


def test_delivered_at_stamped_once_idempotent_on_retry(db):
    _ready_run(db)
    _seed_note(db, pr_number=7)
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is True
    stamp = _note_rows(db)[0].delivered_at
    # A retry (RQ re-run) finds nothing undelivered → no second send, stamp intact.
    assert _deliver(db, odoo) is False
    assert len(odoo.calls) == 1
    assert _note_rows(db)[0].delivered_at == stamp


def test_permanent_error_leaves_rows_undelivered_with_ops_event(db):
    _ready_run(db)
    _seed_note(db, pr_number=7)
    odoo = FakeOdoo(raise_exc=PermanentError("Odoo /change-summary 400"))
    assert _deliver(db, odoo) is False
    assert _note_rows(db)[0].delivered_at is None  # stays for the next event
    assert "change_summary_rejected" in _ops_events(db)


def test_transient_error_reraises_for_rq_retry(db):
    _ready_run(db)
    _seed_note(db, pr_number=7)
    odoo = FakeOdoo(raise_exc=TransientError("Odoo 503"))
    with pytest.raises(TransientError):
        _deliver(db, odoo)
    assert _note_rows(db)[0].delivered_at is None


def test_no_notes_is_noop(db):
    _ready_run(db)
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is False
    assert odoo.calls == []


# --- change-note job tail: generation stays, delivery defers ------------------


@pytest.fixture()
def cn_ctx(db, monkeypatch):
    """WorkerContext-shaped stub for run_change_note with a shared FakeOdoo."""
    odoo = FakeOdoo()
    github = MagicMock()
    github.get_installation_token.return_value = "tok"
    github.get_pull_request_diff.return_value = "diff --git a b\n+x\n"
    ctx = SimpleNamespace(
        db=db, github=github, claude=MagicMock(), prompts_dir="/app/prompts",
    )
    monkeypatch.setattr("worker.change_note_runner.get_context", lambda: ctx)
    monkeypatch.setattr("worker.change_note_runner.build_odoo_client", lambda c, _id: odoo)
    monkeypatch.setattr("worker.change_note_runner.budget_exceeded", lambda c: None)
    monkeypatch.setattr(
        "worker.change_note_runner.build_note", lambda *a, **k: ("<p>merged</p>", 0.01)
    )
    return {"ctx": ctx, "db": db, "odoo": odoo, "github": github}


def _cn_params():
    return {
        "repo_full_name": "acme/widgets", "pr_number": 7,
        "pr_title": "Login rework", "pr_body": "Closes #50",
        "pr_url": "https://github.com/acme/widgets/pull/7", "installation_id": 99,
    }


def test_change_note_job_generates_but_defers_when_not_ready(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])  # not ready
    out = run_change_note(_cn_params())
    assert out == {"status": "completed", "delivered": 0}
    # Note is generated + persisted, but NOT delivered (no per-PR change_note).
    row = _note_rows(s["db"])[0]
    assert row.status == "completed" and row.note_html == "<p>merged</p>"
    assert row.pr_title == "Login rework"
    assert row.delivered_at is None
    assert s["odoo"].calls == []


def test_change_note_job_delivers_when_ticket_already_ready(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready
    out = run_change_note(_cn_params())
    assert out == {"status": "completed", "delivered": 1}
    assert s["odoo"].calls[0]["notes"][0]["pr"]["number"] == 7
    assert _note_rows(s["db"])[0].delivered_at is not None
