"""Fail-loud worker startup for enabled core knowledge."""

from __future__ import annotations

import pytest

from reva.db import Base
from worker.settings import Settings


def _base_env(monkeypatch, tmp_path) -> None:
    key = tmp_path / "key.pem"
    key.write_text("dummy")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", str(key))


def test_settings_defaults(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    settings = Settings.from_env()
    assert settings.core_knowledge_enabled is False
    assert settings.core_knowledge_dir == "/core"
    assert settings.core_versions == []


def test_settings_parse_versions(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("REVA_CORE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("REVA_CORE_VERSIONS", "17.0, 18.0,19.0")
    settings = Settings.from_env()
    assert settings.core_knowledge_enabled is True
    assert settings.core_versions == ["17.0", "18.0", "19.0"]


def test_build_context_refuses_bad_core_config(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("REVA_CORE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("REVA_CORE_VERSIONS", "19.0")
    monkeypatch.setenv("REVA_CORE_KNOWLEDGE_DIR", str(tmp_path / "core"))
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    monkeypatch.setenv("REVA_MIGRATIONS_DIR", str(migrations))

    from worker.runner import build_worker_context

    def create_schema(self, migrations_dir):
        Base.metadata.create_all(self.engine)
        return []

    monkeypatch.setattr("reva.db.engine.Database.migrate", create_schema)

    with pytest.raises(RuntimeError, match="core knowledge misconfigured"):
        build_worker_context(Settings.from_env())
