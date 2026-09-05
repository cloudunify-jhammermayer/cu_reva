"""Tests for the release_notes writers (migration 048, spec 2026-09-04 R2)."""

from __future__ import annotations

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import Repository


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _create(db, name="Lollipop", slug="lollipop"):
    return writers.record_release_note_created(
        db, odoo_instance_id=1, release_id=3275, release_name=name, slug=slug
    )


def test_created_row_is_pending(db):
    note_id = _create(db)
    writers.attach_release_note_job_id(db, note_id, "rq:job:1")
    row = writers.get_release_note(db, note_id)
    assert row["status"] == "pending"
    assert row["job_id"] == "rq:job:1"
    assert (row["odoo_instance_id"], row["release_id"]) == (1, 3275)
    assert (row["release_name"], row["slug"]) == ("Lollipop", "lollipop")
    assert row["source_repo_id"] is None and row["url"] is None and row["error"] is None
    assert row["completed_at"] is None and row["callback_sent_at"] is None


def test_completed_sets_source_and_both_timestamps(db):
    note_id = _create(db)
    writers.record_release_note_completed(
        db, note_id, source_repo_id=4, source_path="docs/releases/lollipop.html",
        url="https://reva.example.com/docs/?repo=4&path=docs/releases/lollipop.html",
    )
    row = writers.get_release_note(db, note_id)
    assert row["status"] == "completed"
    assert row["source_repo_id"] == 4
    assert row["source_path"] == "docs/releases/lollipop.html"
    assert row["url"].endswith("lollipop.html")
    assert row["completed_at"] is not None and row["callback_sent_at"] is not None


def test_failed_keeps_error_and_marks_callback_separately(db):
    note_id = _create(db)
    writers.record_release_note_failed(
        db, note_id, "Kein Release-Log 'docs/releases/lollipop.html' in acme/widgets"
    )
    row = writers.get_release_note(db, note_id)
    assert row["status"] == "failed" and row["error"].startswith("Kein Release-Log")
    assert row["completed_at"] is not None and row["callback_sent_at"] is None
    writers.record_release_note_callback_sent(db, note_id)
    assert writers.get_release_note(db, note_id)["callback_sent_at"] is not None


def test_get_release_note_missing(db):
    assert writers.get_release_note(db, 999) is None


def test_get_pending_release_note(db):
    note_id = _create(db)
    pending = writers.get_pending_release_note(db, odoo_instance_id=1, release_id=3275)
    assert pending is not None and pending["id"] == note_id

    writers.record_release_note_failed(db, note_id, "boom")
    assert writers.get_pending_release_note(db, odoo_instance_id=1, release_id=3275) is None

    assert writers.get_pending_release_note(db, odoo_instance_id=1, release_id=9999) is None


def test_list_enabled_repositories_orders_by_id_and_skips_disabled(db):
    with db.session() as s:
        s.add(Repository(id=2, github_repository_id=1002, owner="acme", name="second",
                         full_name="acme/second", installation_id=7, enabled=True))
        s.add(Repository(id=1, github_repository_id=1001, owner="acme", name="first",
                         full_name="acme/first", installation_id=7, enabled=True,
                         default_branch="develop"))
        s.add(Repository(id=3, github_repository_id=1003, owner="acme", name="off",
                         full_name="acme/off", installation_id=7, enabled=False))
    repos = writers.list_enabled_repositories(db)
    assert [r["full_name"] for r in repos] == ["acme/first", "acme/second"]
    assert repos[0] == {
        "id": 1, "owner": "acme", "name": "first", "full_name": "acme/first",
        "default_branch": "develop", "installation_id": 7,
    }
    assert repos[1]["default_branch"] == "main"


def test_change_note_source_defaults_to_claude_and_records_release_log(db):
    note_id, row = writers.get_or_create_change_note(
        db, "acme/widgets", 7, 97, 1, "helpdesk.ticket", pr_title="t", pr_url="u"
    )
    assert row["source"] == "claude"
    writers.record_change_note_completed(db, note_id, "", 0.0, source="release-log")
    notes = writers.get_undelivered_change_notes(db, 1, 97, "helpdesk.ticket")
    # get_undelivered_change_notes does not check readiness; "" is not None, so the row is listed
    assert [(n["pr_number"], n["source"], n["note_html"]) for n in notes] == [(7, "release-log", "")]


def test_get_repository_by_full_name_is_case_insensitive(db):
    with db.session() as s:
        s.add(Repository(id=5, github_repository_id=1005, owner="Acme", name="Widgets",
                         full_name="Acme/Widgets", installation_id=7, enabled=True,
                         default_branch="dev"))
    row = writers.get_repository_by_full_name(db, "acme/widgets")
    assert row == {"id": 5, "owner": "Acme", "name": "Widgets", "full_name": "Acme/Widgets",
                   "default_branch": "dev", "installation_id": 7, "enabled": True}
    assert writers.get_repository_by_full_name(db, "acme/other") is None
