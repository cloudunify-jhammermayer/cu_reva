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

    def change_summary(self, ticket_id, model_name, notes, release_log=None):
        self.calls.append(
            {"ticket_id": ticket_id, "model_name": model_name, "notes": notes,
             "release_log": release_log}
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
               delivered_at=None, pr_title="PR title", pr_url=None, source="claude",
               repo_full_name="acme/widgets"):
    with db.session() as s:
        s.add(ChangeNote(
            repo_full_name=repo_full_name, pr_number=pr_number, ticket_id=_TICKET,
            odoo_instance_id=_INSTANCE, model_name=_MODEL, status=status,
            note_html=note_html if status == "completed" else None,
            source=source,
            pr_title=pr_title,
            pr_url=pr_url or f"https://github.com/{repo_full_name}/pull/{pr_number}",
            delivered_at=delivered_at,
        ))


def _deliver(db, odoo):
    return maybe_deliver_change_notes(
        SimpleNamespace(db=db, github=MagicMock()), odoo, _INSTANCE, _TICKET, _MODEL
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


# --- release-log entries instead of Claude drafts (spec 2026-09-04) -----------

_OPEN_LOG = (
    "---\nrelease: lollipop\nstatus: open\ndate: 2026-09-30\n---\n# R\n\n"
    "## 97 — Login\n\n- Status: umgesetzt\n- Module: cu_auth 19.0.1.0.0\n\n"
    "### Gebaut\n\nNeue Anmeldung.\n\n### To-do\n\n- Rollen prüfen\n"
)


def _seed_repo(db, *, id=3, github_repository_id=1003, owner="acme", name="widgets",
               full_name="acme/widgets"):
    from reva.db.models import Repository

    with db.session() as s:
        s.add(Repository(id=id, github_repository_id=github_repository_id, owner=owner, name=name,
                         full_name=full_name, installation_id=99, enabled=True,
                         default_branch="main"))


def _with_release_log(cn_ctx, text=_OPEN_LOG):
    gh = cn_ctx["github"]
    gh.get_tree.return_value = {"tree": [{"path": "docs/releases/lollipop.md", "type": "blob"}], "truncated": False}
    gh.get_file_content.return_value = text
    _seed_repo(cn_ctx["db"])


def test_covered_ticket_skips_claude_and_records_release_log_source(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _with_release_log(s)
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    monkeypatch.setattr("worker.change_note_runner.build_note",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Claude must not be called")))
    out = run_change_note(_cn_params())
    assert out == {"status": "completed", "delivered": 0}
    row = _note_rows(s["db"])[0]
    assert (row.status, row.source, row.note_html, float(row.estimated_cost_usd)) == ("completed", "release-log", "", 0.0)
    s["github"].get_pull_request_diff.assert_not_called()


def test_uncovered_ticket_still_drafts_with_claude(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _with_release_log(s, text=_OPEN_LOG.replace("## 97 — Login", "## 4242 — Other"))
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    run_change_note(_cn_params())
    row = _note_rows(s["db"])[0]
    assert (row.source, row.note_html) == ("claude", "<p>merged</p>")


def test_delivery_sends_the_entry_once_with_empty_pr_notes(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _with_release_log(s)
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready
    out = run_change_note(_cn_params())
    assert out["delivered"] == 1
    call = s["odoo"].calls[0]
    assert call["notes"] == [{"pr": {"number": 7, "title": "Login rework",
                                     "url": "https://github.com/acme/widgets/pull/7", "repo": "acme/widgets"},
                              "note_html": ""}]
    assert call["release_log"]["ticket"] == 97
    assert call["release_log"]["title"] == "Login"
    assert call["release_log"]["html"].startswith("<p><strong>Gebaut</strong></p><p>Neue Anmeldung.</p>")
    assert call["release_log"]["modules"] == ["cu_auth 19.0.1.0.0"]


def test_delivery_without_release_log_rows_sends_no_block(db):
    _ready_run(db)
    _seed_note(db, pr_number=1)
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is True
    assert odoo.calls[0]["release_log"] is None


def test_entry_missing_at_delivery_sends_without_block_and_records_event(db, monkeypatch):
    _ready_run(db)
    _seed_repo(db)
    _seed_note(db, pr_number=1, note_html="", source="release-log")
    gh = MagicMock()
    gh.get_installation_token.return_value = "tok"
    gh.get_tree.return_value = {"tree": [], "truncated": False}
    odoo = FakeOdoo()
    assert maybe_deliver_change_notes(SimpleNamespace(db=db, github=gh), odoo, _INSTANCE, _TICKET, _MODEL) is True
    assert odoo.calls[0]["release_log"] is None
    assert "release_log_entry_missing" in _ops_events(db)


def test_release_log_rows_with_empty_html_are_still_delivered(db):
    _ready_run(db)
    _seed_note(db, pr_number=1, note_html="", source="release-log")
    assert writers_undelivered(db) == [1]


def writers_undelivered(db):
    from reva.db import writers

    return [n["pr_number"] for n in writers.get_undelivered_change_notes(db, _INSTANCE, _TICKET, _MODEL)]


# --- fix wave: guard the GitHub lookup (item 1) --------------------------------


def test_merge_job_falls_back_to_claude_when_github_lookup_fails_permanently(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_repo(s["db"])
    s["github"].get_tree.side_effect = PermanentError("GitHub 404")
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    out = run_change_note(_cn_params())
    assert out == {"status": "completed", "delivered": 0}
    row = _note_rows(s["db"])[0]
    assert (row.status, row.source, row.note_html) == ("completed", "claude", "<p>merged</p>")
    with s["db"].session() as sess:
        events = [(r.event, r.severity) for r in sess.execute(select(OpsEvent)).scalars().all()]
    assert ("release_log_lookup_failed", "error") in events


def test_delivery_returns_false_when_github_lookup_fails_permanently(db):
    _ready_run(db)
    _seed_repo(db)
    _seed_note(db, pr_number=1, note_html="", source="release-log")
    gh = MagicMock()
    gh.get_installation_token.return_value = "tok"
    gh.get_tree.side_effect = PermanentError("GitHub 404")
    odoo = FakeOdoo()
    assert maybe_deliver_change_notes(SimpleNamespace(db=db, github=gh), odoo, _INSTANCE, _TICKET, _MODEL) is False
    assert odoo.calls == []
    assert _note_rows(db)[0].delivered_at is None
    assert "release_log_lookup_failed" in _ops_events(db)


def test_delivery_reraises_transient_error_from_github_lookup(db):
    _ready_run(db)
    _seed_repo(db)
    _seed_note(db, pr_number=1, note_html="", source="release-log")
    gh = MagicMock()
    gh.get_installation_token.return_value = "tok"
    gh.get_tree.side_effect = TransientError("GitHub 503")
    odoo = FakeOdoo()
    with pytest.raises(TransientError):
        maybe_deliver_change_notes(SimpleNamespace(db=db, github=gh), odoo, _INSTANCE, _TICKET, _MODEL)
    assert odoo.calls == []


# --- fix wave: look the entry up in the right repo (item 2) --------------------


def test_release_log_lookup_uses_the_release_log_repo_not_the_first_note(db):
    _ready_run(db)
    _seed_note(db, pr_number=3, repo_full_name="acme/alpha", source="claude", note_html="<p>draft</p>")
    _seed_note(db, pr_number=9, repo_full_name="acme/beta", source="release-log", note_html="")
    _seed_repo(db, id=5, github_repository_id=2005, owner="acme", name="beta", full_name="acme/beta")
    gh = MagicMock()
    gh.get_installation_token.return_value = "tok"
    gh.get_tree.return_value = {"tree": [{"path": "docs/releases/lollipop.md", "type": "blob"}], "truncated": False}
    gh.get_file_content.return_value = _OPEN_LOG
    odoo = FakeOdoo()
    assert maybe_deliver_change_notes(SimpleNamespace(db=db, github=gh), odoo, _INSTANCE, _TICKET, _MODEL) is True
    assert odoo.calls[0]["release_log"] is not None
    assert odoo.calls[0]["release_log"]["ticket"] == _TICKET


# --- fix wave: a found entry drops every Claude draft in the batch (item 3) ----


def test_found_entry_drops_every_claude_draft_in_the_batch(db):
    _ready_run(db)
    _seed_repo(db)
    _seed_note(db, pr_number=1, source="claude", note_html="<p>draft</p>")
    _seed_note(db, pr_number=2, source="release-log", note_html="")
    gh = MagicMock()
    gh.get_installation_token.return_value = "tok"
    gh.get_tree.return_value = {"tree": [{"path": "docs/releases/lollipop.md", "type": "blob"}], "truncated": False}
    gh.get_file_content.return_value = _OPEN_LOG
    odoo = FakeOdoo()
    assert maybe_deliver_change_notes(SimpleNamespace(db=db, github=gh), odoo, _INSTANCE, _TICKET, _MODEL) is True
    assert odoo.calls[0]["release_log"] is not None
    assert [n["note_html"] for n in odoo.calls[0]["notes"]] == ["", ""]
