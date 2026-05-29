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
