"""Fernet wrapper for instance outbound callback keys (REVA_SECRET_KEY)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from reva import secrets_crypto


@pytest.fixture()
def key(monkeypatch) -> str:
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("REVA_SECRET_KEY", k)
    return k


def test_round_trip(key) -> None:
    token = secrets_crypto.encrypt("super-secret")
    assert token != "super-secret"
    assert secrets_crypto.decrypt(token) == "super-secret"


def test_empty_passthrough(key) -> None:
    assert secrets_crypto.encrypt("") == ""
    assert secrets_crypto.decrypt("") == ""


def test_missing_key_raises_on_nonempty(monkeypatch) -> None:
    monkeypatch.delenv("REVA_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        secrets_crypto.encrypt("x")
    # Empty value never needs the key.
    assert secrets_crypto.encrypt("") == ""
