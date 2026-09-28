from __future__ import annotations

import pytest

from reva.db import Base, Database, create_engine_from_url, writers


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def test_create_and_get(db: Database) -> None:
    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash="h1", key_prefix="reva_odoo_aa",
        callback_url="https://odoo.acme/write-field", callback_api_key_enc="enc",
    )
    row = writers.get_odoo_instance(db, iid)
    assert row["name"] == "ACME"
    assert row["callback_url"] == "https://odoo.acme/write-field"
    assert row["callback_api_key_enc"] == "enc"
    assert row["active"] is True


def test_rotate_changes_hash(db: Database) -> None:
    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash="h1", key_prefix="reva_odoo_aa",
        callback_url="", callback_api_key_enc="",
    )
    assert writers.rotate_odoo_instance_key(db, iid, key_hash="h2", key_prefix="reva_odoo_bb")
    row = writers.get_odoo_instance(db, iid)
    assert row["key_hash"] == "h2"
    assert row["key_prefix"] == "reva_odoo_bb"


def test_update_fields(db: Database) -> None:
    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash="h1", key_prefix="p", callback_url="", callback_api_key_enc="",
    )
    assert writers.update_odoo_instance(db, iid, active=False, callback_url="https://x/write-field")
    row = writers.get_odoo_instance(db, iid)
    assert row["active"] is False
    assert row["callback_url"] == "https://x/write-field"


def test_delete_clears_support_rows_and_removes_instance(db: Database) -> None:
    """support_threads/support_turns reference the instance through a plain FK;
    the delete must unlink them (SET NULL) or Postgres rejects it."""
    iid = writers.create_odoo_instance(
        db, name="local_home", key_hash="h1", key_prefix="p", callback_url="", callback_api_key_enc="",
    )
    thread_id = writers.get_or_create_support_thread(
        db, odoo_instance_id=iid, ticket_id=1, model_name="helpdesk.ticket", field_name="f",
    )
    turn_id = writers.record_support_turn_created(db, thread_id, iid, "Wie?")

    assert writers.delete_odoo_instance(db, iid) is True

    assert writers.get_odoo_instance(db, iid) is None
    assert writers.get_support_turn(db, turn_id)["odoo_instance_id"] is None
    assert writers.get_support_thread(db, thread_id)["odoo_instance_id"] is None
