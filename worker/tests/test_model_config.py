"""Model IDs come from one env-backed source (reva.config) so the direct-API
client and the Claude Code CLI runner can never drift apart."""

from __future__ import annotations

import importlib


def test_constructors_default_to_config_models():
    """Both clients default to the same model values, sourced from reva.config."""
    import reva.config as config
    from reva.claude_client import ClaudeClient
    from reva.claude_code_runner import ClaudeCodeRunner

    client = ClaudeClient(api_key="x")
    runner = ClaudeCodeRunner(repo_cache_dir="/tmp", api_key="x", skills_dir="/tmp")

    assert client.default_model == config.DEFAULT_MODEL == runner.default_model
    assert client.deep_model == config.DEEP_MODEL == runner.deep_model


def test_model_env_overrides_config(monkeypatch):
    """REVA_DEFAULT_MODEL / REVA_DEEP_MODEL override the pinned defaults."""
    import reva.config as config

    monkeypatch.setenv("REVA_DEFAULT_MODEL", "claude-test-default")
    monkeypatch.setenv("REVA_DEEP_MODEL", "claude-test-deep")
    importlib.reload(config)
    try:
        assert config.DEFAULT_MODEL == "claude-test-default"
        assert config.DEEP_MODEL == "claude-test-deep"
    finally:
        monkeypatch.delenv("REVA_DEFAULT_MODEL", raising=False)
        monkeypatch.delenv("REVA_DEEP_MODEL", raising=False)
        importlib.reload(config)


def test_models_default_when_env_unset():
    """With no env override, the pinned production defaults are unchanged."""
    import reva.config as config

    assert config.DEFAULT_MODEL == "claude-sonnet-4-6"
    assert config.DEEP_MODEL == "claude-opus-4-7"
