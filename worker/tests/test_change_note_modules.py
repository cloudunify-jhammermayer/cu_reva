"""Affected modules on change notes (spec 2026-09-29)."""

from __future__ import annotations

import pytest

from reva.db import writers
from reva.db.engine import Database, create_engine_from_url
from reva.db.models import Base, ChangeNote, TicketIssueRun

_INSTANCE = 1
_TICKET = 97
_MODEL = "helpdesk.ticket"


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _note(db, pr_number=7):
    note_id, _ = writers.get_or_create_change_note(
        db, "acme/widgets", pr_number, _TICKET, _INSTANCE, _MODEL,
        pr_title="Login rework", pr_url=f"https://github.com/acme/widgets/pull/{pr_number}",
    )
    return note_id


def _row(db, pr_number=7):
    _, row = writers.get_or_create_change_note(
        db, "acme/widgets", pr_number, _TICKET, _INSTANCE, _MODEL
    )
    return row


def test_a_new_note_has_no_modules(db):
    _note(db)
    row = _row(db)
    assert (row["modules"], row["submodules"]) == (None, None)


def test_modules_and_submodules_are_stored_and_returned(db):
    writers.record_change_note_modules(
        db, _note(db), ["cu_auth", "cu_sale"], ["3rd_party_addons/cu/queue"]
    )
    row = _row(db)
    assert row["modules"] == ["cu_auth", "cu_sale"]
    assert row["submodules"] == ["3rd_party_addons/cu/queue"]


def test_stored_lists_are_kept_on_a_second_write(db):
    note_id = _note(db)
    writers.record_change_note_modules(db, note_id, ["cu_auth"], [])
    writers.record_change_note_modules(db, note_id, ["cu_other"], ["3rd_party_addons/cu/queue"])
    row = _row(db)
    assert (row["modules"], row["submodules"]) == (["cu_auth"], [])


def test_an_empty_list_counts_as_stored(db):
    # [] means "looked up, nothing under the addons prefixes", not "unknown".
    note_id = _note(db)
    writers.record_change_note_modules(db, note_id, [], [])
    writers.record_change_note_modules(db, note_id, ["cu_auth"], [])
    assert _row(db)["modules"] == []


def test_an_unknown_note_id_is_ignored(db):
    writers.record_change_note_modules(db, 999, ["cu_auth"], [])
    with db.session() as s:
        assert s.get(ChangeNote, 999) is None


def test_undelivered_notes_carry_modules_and_submodules(db):
    note_id = _note(db)
    writers.record_change_note_modules(db, note_id, ["cu_auth"], ["3rd_party_addons/cu/queue"])
    writers.record_change_note_completed(db, note_id, "<p>n</p>", 0.01)
    notes = writers.get_undelivered_change_notes(db, _INSTANCE, _TICKET, _MODEL)
    assert (notes[0]["modules"], notes[0]["submodules"]) == (["cu_auth"], ["3rd_party_addons/cu/queue"])


# --- ticket name for branch-linked tickets -------------------------------------


def _run(db, *, name, ticket_id=_TICKET, model_name=_MODEL, instance=_INSTANCE):
    with db.session() as s:
        s.add(TicketIssueRun(
            odoo_instance_id=instance, ticket_id=ticket_id, model_name=model_name,
            github_url="https://github.com/acme/widgets", repo_full_name="acme/widgets",
            status="completed", name=name, description="d", analysis_html="<p/>",
            priority="1", ticket_url="https://odoo.example/t", issues=[],
        ))


def test_ticket_name_comes_from_the_newest_run(db):
    _run(db, name="Alt")
    _run(db, name="Anmeldung überarbeiten")
    assert writers.get_ticket_name(db, _INSTANCE, _TICKET, _MODEL) == "Anmeldung überarbeiten"


def test_ticket_name_is_empty_without_a_run(db):
    _run(db, name="Other record", ticket_id=5)
    _run(db, name="Other model", model_name="project.task")
    assert writers.get_ticket_name(db, _INSTANCE, _TICKET, _MODEL) == ""


# --- stored lists of a PR (follow-ups, spec 2026-09-30) -------------------------


def test_stored_lists_are_none_for_an_unknown_pr(db):
    assert writers.get_stored_change_note_lists(db, "acme/widgets", 7) is None


def test_stored_lists_are_none_while_no_row_has_them(db):
    _note(db)
    assert writers.get_stored_change_note_lists(db, "acme/widgets", 7) is None


def test_stored_lists_come_from_a_row_of_the_same_pr(db):
    writers.record_change_note_modules(db, _note(db), ["cu_auth"], ["3rd_party_addons/cu/queue"])
    _note(db, pr_number=8)
    assert writers.get_stored_change_note_lists(db, "Acme/Widgets", 7) == (
        ["cu_auth"], ["3rd_party_addons/cu/queue"]
    )
    assert writers.get_stored_change_note_lists(db, "acme/widgets", 8) is None


def test_stored_empty_lists_count_as_stored(db):
    writers.record_change_note_modules(db, _note(db), [], [])
    assert writers.get_stored_change_note_lists(db, "acme/widgets", 7) == ([], [])


def test_has_change_notes_is_false_for_an_unknown_pr(db):
    assert writers.has_change_notes_for_pr(db, "acme/widgets", 7) is False


def test_has_change_notes_is_true_once_a_row_exists(db):
    _note(db)
    assert writers.has_change_notes_for_pr(db, "Acme/Widgets", 7) is True
    assert writers.has_change_notes_for_pr(db, "acme/widgets", 8) is False


def _age(db, note_id, *, seconds, status=None):
    from datetime import datetime, timedelta, timezone

    with db.session() as s:
        row = s.get(ChangeNote, note_id)
        row.created_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        if status:
            row.status = status


def _status(db, note_id):
    with db.session() as s:
        row = s.get(ChangeNote, note_id)
        return row.status, row.error_message, row.completed_at


def test_a_stale_pending_note_is_failed_and_returned(db):
    note_id = _note(db)
    _age(db, note_id, seconds=1000)

    reaped = writers.reap_stale_pending_change_notes(db, 500)

    assert reaped == [{
        "id": note_id, "repo_full_name": "acme/widgets", "pr_number": 7,
        "odoo_instance_id": _INSTANCE, "ticket_id": _TICKET, "model_name": _MODEL,
    }]
    status, message, completed_at = _status(db, note_id)
    assert status == "failed"
    assert message == "Reaped: stuck in 'pending' >500s (worker likely died mid-job)."
    assert completed_at is not None


def test_a_young_pending_note_is_left_alone(db):
    note_id = _note(db)
    _age(db, note_id, seconds=100)

    assert writers.reap_stale_pending_change_notes(db, 500) == []
    assert _status(db, note_id) == ("pending", None, None)


def test_terminal_notes_are_never_reaped(db):
    ids = [_note(db, pr_number=n) for n in (1, 2, 3)]
    for note_id, status in zip(ids, ("completed", "failed", "skipped_budget")):
        _age(db, note_id, seconds=10_000, status=status)

    assert writers.reap_stale_pending_change_notes(db, 500) == []
    assert [_status(db, i)[0] for i in ids] == ["completed", "failed", "skipped_budget"]


def test_a_second_reap_finds_nothing(db):
    _age(db, _note(db), seconds=1000)

    assert len(writers.reap_stale_pending_change_notes(db, 500)) == 1
    assert writers.reap_stale_pending_change_notes(db, 500) == []
