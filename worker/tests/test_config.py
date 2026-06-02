"""Tests for reva.config — env / Docker-secret-file loading."""

from __future__ import annotations

import pytest

from reva.config import env_or_file, required_env_or_file


def test_env_or_file_prefers_file(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    secret.write_text("  from-file\n")  # whitespace is stripped
    monkeypatch.setenv("MY_SECRET", "from-env")
    monkeypatch.setenv("MY_SECRET_FILE", str(secret))
    assert env_or_file("MY_SECRET") == "from-file"


def test_env_or_file_falls_back_to_env(monkeypatch):
    monkeypatch.delenv("MY_SECRET_FILE", raising=False)
    monkeypatch.setenv("MY_SECRET", "from-env")
    assert env_or_file("MY_SECRET") == "from-env"


def test_env_or_file_default_when_absent(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    monkeypatch.delenv("NOPE_FILE", raising=False)
    assert env_or_file("NOPE", "fallback") == "fallback"


def test_required_env_or_file_raises_when_missing(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    monkeypatch.delenv("NOPE_FILE", raising=False)
    with pytest.raises(KeyError):
        required_env_or_file("NOPE")


def test_required_env_or_file_raises_on_empty_file(tmp_path, monkeypatch):
    """SECU-2/CORR-9: an empty/truncated Docker-secret file must fail loud at
    startup, not boot the service with secret='' (which makes the webhook HMAC
    forgeable). 'Present' must mean 'non-empty'."""
    secret = tmp_path / "secret"
    secret.write_text("")
    monkeypatch.delenv("MY_SECRET", raising=False)
    monkeypatch.setenv("MY_SECRET_FILE", str(secret))
    with pytest.raises(KeyError):
        required_env_or_file("MY_SECRET")


def test_required_env_or_file_raises_on_whitespace_only_file(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    secret.write_text("   \n")  # strips to empty
    monkeypatch.delenv("MY_SECRET", raising=False)
    monkeypatch.setenv("MY_SECRET_FILE", str(secret))
    with pytest.raises(KeyError):
        required_env_or_file("MY_SECRET")


def test_required_env_or_file_raises_on_empty_env(monkeypatch):
    monkeypatch.delenv("MY_SECRET_FILE", raising=False)
    monkeypatch.setenv("MY_SECRET", "")
    with pytest.raises(KeyError):
        required_env_or_file("MY_SECRET")
