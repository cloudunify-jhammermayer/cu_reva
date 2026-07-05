from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from reva.db import Base, Database, create_engine_from_url, writers
from reva.errors import PermanentError


@pytest.fixture()
def db(monkeypatch) -> Database:
    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def test_builds_client_from_instance(db, monkeypatch):
    from reva import secrets_crypto
    from worker.runner import WorkerContext, build_odoo_client

    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash="h", key_prefix="p",
        callback_url="https://odoo.acme/write-field",
        callback_api_key_enc=secrets_crypto.encrypt("outbound-secret"),
    )
    ctx = WorkerContext.__new__(WorkerContext)  # only .db is needed by the builder
    object.__setattr__(ctx, "db", db)
    client = build_odoo_client(ctx, iid)
    # Legacy stored callback_url (old write-field endpoint) still derives the
    # /tickets/-namespaced endpoint (Odoo-side API change, 2026-07-05).
    assert client._callback_url == "https://odoo.acme/tickets/write-field"
    assert client._api_key == "outbound-secret"


def test_missing_instance_raises(db):
    from worker.runner import WorkerContext, build_odoo_client

    ctx = WorkerContext.__new__(WorkerContext)
    object.__setattr__(ctx, "db", db)
    with pytest.raises(PermanentError):
        build_odoo_client(ctx, 999)
