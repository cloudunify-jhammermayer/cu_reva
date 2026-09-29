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
from reva.errors import PermanentError, ProviderCreditExhausted, TransientError
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
               repo_full_name="acme/widgets", modules=None, submodules=None):
    with db.session() as s:
        s.add(ChangeNote(
            repo_full_name=repo_full_name, pr_number=pr_number, ticket_id=_TICKET,
            odoo_instance_id=_INSTANCE, model_name=_MODEL, status=status,
            note_html=note_html if status == "completed" else None,
            source=source,
            modules=modules,
            submodules=submodules,
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
        "modules": [],
        "submodules": [],
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
    github.get_changed_files.return_value = [
        {"filename": "custom_addons/cu_auth/models/login.py", "status": "modified",
         "patch": "@@ -1 +1 @@\n-a\n+b"}
    ]
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


def test_change_note_waits_when_over_budget_and_queue_present(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    s["ctx"].rq_queue = q
    s["ctx"].budget_retry_seconds = 900
    s["ctx"].budget_wait_max_seconds = 86400
    monkeypatch.setattr("worker.change_note_runner.budget_exceeded", lambda c: 50.0)
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready

    out = run_change_note(_cn_params())

    assert out["status"] == "waiting_budget"
    assert _note_rows(s["db"])[0].status == "pending"   # not skipped_budget
    assert s["odoo"].calls == []                         # nothing delivered yet
    assert q.enqueue_in.call_args.args[1] == "worker.change_note_tasks.run_change_note"
    assert q.enqueue_in.call_args.args[2]["pr_number"] == 7


def test_change_note_waits_on_provider_credit_exhausted_when_queue_present(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    s["ctx"].rq_queue = q
    s["ctx"].budget_retry_seconds = 900
    s["ctx"].budget_wait_max_seconds = 86400
    monkeypatch.setattr(
        "worker.change_note_runner.build_note",
        lambda *a, **k: (_ for _ in ()).throw(ProviderCreditExhausted("Credit balance is too low")),
    )
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready

    out = run_change_note(_cn_params())

    assert out["status"] == "waiting_budget"
    assert out["reason"] == "provider_credit"
    assert _note_rows(s["db"])[0].status == "pending"
    assert s["odoo"].calls == []
    assert q.enqueue_in.call_args.args[1] == "worker.change_note_tasks.run_change_note"
    assert q.enqueue_in.call_args.args[2]["pr_number"] == 7


def test_change_note_skips_note_on_provider_credit_exhausted_without_queue(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    s["ctx"].rq_queue = None  # no queue to defer through: give up like the cap path
    s["ctx"].budget_retry_seconds = 900
    monkeypatch.setattr(
        "worker.change_note_runner.build_note",
        lambda *a, **k: (_ for _ in ()).throw(ProviderCreditExhausted("Credit balance is too low")),
    )
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready

    out = run_change_note(_cn_params())

    # The only note for this ticket is skipped — nothing left to deliver, so
    # the convergent condition still runs (no crash) but sends nothing.
    assert out == {"status": "completed", "delivered": 0}
    row = _note_rows(s["db"])[0]
    assert row.status == "skipped_budget"
    assert "Anthropic credit balance too low" in row.error_message


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
                              "note_html": "", "modules": ["cu_auth"], "submodules": []}]
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


# --- affected modules (spec 2026-09-29) ----------------------------------------


def _changed(*paths, renamed=None, submodules=()):
    files = [
        {"filename": path, "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"} for path in paths
    ]
    for new, old in (renamed or {}).items():
        files.append({"filename": new, "previous_filename": old, "status": "renamed"})
    for path in submodules:
        files.append({
            "filename": path, "status": "modified",
            "patch": "@@ -1 +1 @@\n-Subproject commit " + "a" * 40 + "\n+Subproject commit " + "b" * 40,
        })
    return files


def _events_with_severity(db):
    with db.session() as s:
        return [(r.event, r.severity) for r in s.execute(select(OpsEvent)).scalars().all()]


def test_merge_job_stores_the_affected_modules(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    s["github"].get_changed_files.return_value = _changed(
        "custom_addons/cu_sale/models/sale.py",
        "custom_addons/cu_auth/views/login.xml",
        ".github/workflows/ci.yml",
    )
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    run_change_note(_cn_params())
    assert _note_rows(s["db"])[0].modules == ["cu_auth", "cu_sale"]


def test_a_renamed_file_counts_for_the_module_it_left(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    s["github"].get_changed_files.return_value = _changed(
        renamed={"custom_addons/cu_new/models/x.py": "custom_addons/cu_old/models/x.py"}
    )
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    run_change_note(_cn_params())
    assert _note_rows(s["db"])[0].modules == ["cu_new", "cu_old"]


def test_a_pr_outside_the_addons_stores_an_empty_list(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    s["github"].get_changed_files.return_value = _changed("docs/releases/lollipop.md")
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    run_change_note(_cn_params())
    assert _note_rows(s["db"])[0].modules == []


def test_merge_job_stores_and_sends_a_moved_submodule(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    s["github"].get_changed_files.return_value = _changed(
        "custom_addons/cu_sale/models/sale.py", submodules=["3rd_party_addons/cu/queue"]
    )
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready
    run_change_note(_cn_params())
    row = _note_rows(s["db"])[0]
    assert (row.modules, row.submodules) == (["cu_sale"], ["3rd_party_addons/cu/queue"])
    note = s["odoo"].calls[0]["notes"][0]
    assert (note["modules"], note["submodules"]) == (["cu_sale"], ["3rd_party_addons/cu/queue"])


def test_release_log_notes_carry_modules_too(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _with_release_log(s)
    s["github"].get_changed_files.return_value = _changed("custom_addons/cu_auth/models/login.py")
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    run_change_note(_cn_params())
    row = _note_rows(s["db"])[0]
    assert (row.source, row.modules) == ("release-log", ["cu_auth"])


def test_a_failed_module_lookup_still_completes_the_note(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    s["github"].get_changed_files.side_effect = PermanentError("GitHub 404")
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready
    out = run_change_note(_cn_params())
    assert out == {"status": "completed", "delivered": 1}
    row = _note_rows(s["db"])[0]
    assert (row.status, row.modules, row.submodules) == ("completed", None, None)
    note = s["odoo"].calls[0]["notes"][0]
    assert (note["modules"], note["submodules"]) == ([], [])
    assert ("modules_lookup_failed", "warning") in _events_with_severity(s["db"])


def test_a_transient_error_in_the_module_lookup_propagates(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    s["github"].get_changed_files.side_effect = TransientError("GitHub 503")
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    with pytest.raises(TransientError):
        run_change_note(_cn_params())


def test_a_rerun_reuses_the_stored_lists_without_asking_github(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    s["github"].get_changed_files.return_value = _changed("custom_addons/cu_auth/models/login.py")
    run_change_note(_cn_params())
    s["github"].get_changed_files.return_value = _changed("custom_addons/cu_other/models/x.py")
    run_change_note(_cn_params())
    assert s["github"].get_changed_files.call_count == 1
    assert _note_rows(s["db"])[0].modules == ["cu_auth"]


def test_delivery_sends_the_modules_and_submodules_of_each_note(db):
    _ready_run(db)
    _seed_note(db, pr_number=7, modules=["cu_auth", "cu_sale"], submodules=["3rd_party_addons/cu/queue"])
    _seed_note(db, pr_number=8)  # a row from before the columns existed
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is True
    assert [n["modules"] for n in odoo.calls[0]["notes"]] == [["cu_auth", "cu_sale"], []]
    assert [n["submodules"] for n in odoo.calls[0]["notes"]] == [["3rd_party_addons/cu/queue"], []]


# --- branch-linked tickets (spec 2026-09-29) -----------------------------------


def _seed_default_instance(db):
    from reva.db.models import OdooInstance

    with db.session() as s:
        s.add(OdooInstance(name="prod", key_hash="h1", key_prefix="rk_1", is_default=True))


def _branch_params(**overrides):
    params = _cn_params()
    params.update({"pr_body": "", "head_ref": "cr/2010"})
    params.update(overrides)
    return params


def test_ticket_without_issues_delivers_once_no_note_is_pending(db):
    _seed_note(db, pr_number=7)  # no ticket_issue_runs row at all
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is True
    assert [n["pr"]["number"] for n in odoo.calls[0]["notes"]] == [7]


def test_ticket_without_issues_still_waits_for_a_pending_note(db):
    _seed_note(db, pr_number=7)
    _seed_note(db, pr_number=8, status="pending")
    odoo = FakeOdoo()
    assert _deliver(db, odoo) is False
    assert odoo.calls == []


def test_branch_linked_ticket_gets_its_summary_at_merge(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    out = run_change_note(_branch_params())
    assert out == {"status": "completed", "delivered": 1}
    call = s["odoo"].calls[0]
    assert (call["ticket_id"], call["model_name"]) == (2010, "project.task")
    assert call["notes"][0]["pr"]["number"] == 7


def test_helpdesk_prefix_resolves_a_helpdesk_ticket(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    run_change_note(_branch_params(head_ref="sup/H1213"))
    call = s["odoo"].calls[0]
    assert (call["ticket_id"], call["model_name"]) == (1213, "helpdesk.ticket")


def test_the_title_names_the_ticket_when_the_branch_does_not(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    run_change_note(_branch_params(head_ref="feature/misc", pr_title="[CR] 2010 - Login rework"))
    assert s["odoo"].calls[0]["ticket_id"] == 2010


def test_closing_refs_win_over_the_branch(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ticket 97
    run_change_note(_branch_params(pr_body="Closes #50"))
    assert [c["ticket_id"] for c in s["odoo"].calls] == [_TICKET]
    assert [r.ticket_id for r in _note_rows(s["db"])] == [_TICKET]


def test_closing_refs_that_resolve_nothing_fall_back_to_the_branch(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    run_change_note(_branch_params(pr_body="Closes #999"))
    assert s["odoo"].calls[0]["ticket_id"] == 2010


def test_branch_ticket_without_a_default_instance_records_the_event(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    out = run_change_note(_branch_params())
    assert out == {"status": "no_tickets"}
    assert _note_rows(s["db"]) == []
    assert "no_default_instance" in _ops_events(s["db"])


def test_job_params_without_head_ref_run_as_before(cn_ctx):
    # A job enqueued before this shipped carries no head_ref key.
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])
    assert "head_ref" not in _cn_params()
    assert run_change_note(_cn_params()) == {"status": "completed", "delivered": 1}
    params = _cn_params()
    params["pr_body"] = ""
    assert run_change_note(params) == {"status": "no_tickets"}


def test_a_rejected_summary_of_a_branch_ticket_does_not_fail_the_job(cn_ctx):
    # The branch number is not a record id in Odoo: 4xx.
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    s["odoo"].raise_exc = PermanentError("Odoo /change-summary 404")
    out = run_change_note(_branch_params())
    assert out == {"status": "completed", "delivered": 0}
    row = _note_rows(s["db"])[0]
    assert (row.status, row.delivered_at) == ("completed", None)
    assert "change_summary_rejected" in _ops_events(s["db"])


def _capture_ticket_name(monkeypatch):
    seen: dict = {}

    def fake(claude, prompts_dir, ticket_name, *args, **kwargs):
        seen["ticket_name"] = ticket_name
        return "<p>merged</p>", 0.01

    monkeypatch.setattr("worker.change_note_runner.build_note", fake)
    return seen


def test_a_branch_ticket_with_a_run_is_drafted_in_the_ticket_language(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    seen = _capture_ticket_name(monkeypatch)
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ticket 97, name "t"
    run_change_note(_branch_params(head_ref="sup/H97"))
    assert seen["ticket_name"] == "t"
    assert s["odoo"].calls[0]["ticket_id"] == _TICKET


def test_a_branch_ticket_unknown_to_reva_has_no_name(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    seen = _capture_ticket_name(monkeypatch)
    _seed_default_instance(s["db"])
    run_change_note(_branch_params())
    assert seen["ticket_name"] == ""


def test_an_issue_linked_ticket_is_still_drafted_in_the_ticket_language(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    seen = _capture_ticket_name(monkeypatch)
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    run_change_note(_cn_params())
    assert seen["ticket_name"] == "t"


# --- follow-ups (spec 2026-09-30): stored lists, nothing to deploy --------------


def test_a_row_created_on_a_rerun_gets_the_stored_lists(cn_ctx):
    # The PR's first ticket already stores the lists; a second ticket resolved
    # on the re-run gets the same ones, still without a GitHub call.
    from reva.db import writers
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    other_id, _ = writers.get_or_create_change_note(
        s["db"], "acme/widgets", 7, 4242, _INSTANCE, _MODEL
    )
    writers.record_change_note_modules(s["db"], other_id, ["cu_auth"], ["3rd_party_addons/cu/queue"])
    run_change_note(_cn_params())
    s["github"].get_changed_files.assert_not_called()
    row = next(r for r in _note_rows(s["db"]) if r.ticket_id == _TICKET)
    assert (row.modules, row.submodules) == (["cu_auth"], ["3rd_party_addons/cu/queue"])


def test_branch_linked_pr_without_modules_or_submodules_is_not_drafted(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    s["github"].get_changed_files.return_value = _changed(".github/workflows/ci.yml")
    monkeypatch.setattr(
        "worker.change_note_runner.build_note",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Claude must not be called")),
    )
    assert run_change_note(_branch_params()) == {"status": "nothing_to_deploy"}
    assert _note_rows(s["db"]) == []
    assert s["odoo"].calls == []


def test_nothing_to_deploy_never_strands_a_row_an_earlier_run_wrote(cn_ctx, monkeypatch):
    # First run: the lookup fails and the job defers for budget, leaving its row
    # pending. Re-run: the lookup now succeeds with nothing to deploy. The job
    # must finish its row instead of exiting and leaving it pending.
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    q = MagicMock()
    q.enqueue_in.return_value = MagicMock(id="rq:job:deferred")
    s["ctx"].rq_queue = q
    s["ctx"].budget_retry_seconds = 900
    s["ctx"].budget_wait_max_seconds = 86400
    s["github"].get_changed_files.side_effect = PermanentError("GitHub 404")
    monkeypatch.setattr("worker.change_note_runner.budget_exceeded", lambda c: 50.0)
    assert run_change_note(_branch_params())["status"] == "waiting_budget"
    assert [r.status for r in _note_rows(s["db"])] == ["pending"]

    s["github"].get_changed_files.side_effect = None
    s["github"].get_changed_files.return_value = _changed(".github/workflows/ci.yml")
    monkeypatch.setattr("worker.change_note_runner.budget_exceeded", lambda c: None)
    out = run_change_note(_branch_params())
    assert out["status"] == "completed"
    assert [r.status for r in _note_rows(s["db"])] == ["completed"]


def test_branch_linked_pr_that_moves_a_submodule_is_drafted(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    s["github"].get_changed_files.return_value = _changed(submodules=["3rd_party_addons/cu/queue"])
    assert run_change_note(_branch_params()) == {"status": "completed", "delivered": 1}


def test_branch_linked_pr_with_a_failed_lookup_is_still_drafted(cn_ctx):
    # None means "could not look", not "nothing to deploy".
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    s["github"].get_changed_files.side_effect = PermanentError("GitHub 404")
    assert run_change_note(_branch_params()) == {"status": "completed", "delivered": 1}


def test_issue_linked_pr_without_modules_is_still_drafted(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready
    s["github"].get_changed_files.return_value = _changed("docs/releases/lollipop.md")
    assert run_change_note(_cn_params()) == {"status": "completed", "delivered": 1}


def test_branch_ticket_goes_to_the_instance_the_repo_declares(cn_ctx, monkeypatch):
    from reva.db.models import OdooInstance
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    with s["db"].session() as sess:
        sess.add(OdooInstance(name="customer", key_hash="h2", key_prefix="rk_2"))
    with s["db"].session() as sess:
        customer_id = sess.query(OdooInstance).filter_by(name="customer").one().id
    _seed_repo(s["db"])
    s["github"].get_file_content.return_value = "odoo_instance: customer\n"
    asked = []

    def _build(ctx, instance_id):
        asked.append(instance_id)
        return s["odoo"]

    monkeypatch.setattr("worker.change_note_runner.build_odoo_client", _build)
    out = run_change_note(_branch_params())
    assert out == {"status": "completed", "delivered": 1}
    assert asked == [customer_id]
    assert [r.odoo_instance_id for r in _note_rows(s["db"])] == [customer_id]
    assert s["odoo"].calls[0]["ticket_id"] == 2010


def test_branch_ticket_of_a_repo_declaring_an_unknown_instance_gets_no_note(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    _seed_repo(s["db"])
    s["github"].get_file_content.return_value = "odoo_instance: nowhere\n"
    out = run_change_note(_branch_params())
    assert out == {"status": "no_tickets"}
    assert _note_rows(s["db"]) == []
    assert "unknown_repo_instance" in _ops_events(s["db"])
    assert s["odoo"].calls == []


def test_branch_ticket_of_a_repo_with_an_unreadable_config_gets_no_note(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    _seed_repo(s["db"])
    s["github"].get_file_content.return_value = "odoo_instance: [unclosed\n"
    out = run_change_note(_branch_params())
    assert out == {"status": "no_tickets"}
    assert _note_rows(s["db"]) == []
    events = _ops_events(s["db"])
    assert "repo_instance_config_failed" in events
    assert "unknown_repo_instance" not in events
    assert "no_default_instance" not in events
    assert s["odoo"].calls == []


def test_delivery_job_delivers_the_ready_ticket(cn_ctx):
    from worker.change_note_runner import run_change_note_delivery

    s = cn_ctx
    _ready_run(s["db"])
    _seed_note(s["db"], pr_number=7)

    out = run_change_note_delivery(
        {"odoo_instance_id": _INSTANCE, "ticket_id": _TICKET, "model_name": _MODEL}
    )

    assert out == {"status": "completed", "delivered": 1}
    assert len(s["odoo"].calls) == 1
    assert _note_rows(s["db"])[0].delivered_at is not None


def test_delivery_job_with_nothing_to_deliver(cn_ctx):
    from worker.change_note_runner import run_change_note_delivery

    s = cn_ctx
    _ready_run(s["db"])

    out = run_change_note_delivery(
        {"odoo_instance_id": _INSTANCE, "ticket_id": _TICKET, "model_name": _MODEL}
    )

    assert out == {"status": "completed", "delivered": 0}
    assert s["odoo"].calls == []
