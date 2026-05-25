"""Tests for ClaudeCodeRunner.ensure_repo and review."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from reva.claude_code_runner import ClaudeCodeRunner
from reva.errors import PermanentError, TransientError


@pytest.fixture
def runner(tmp_path):
    return ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path / "repos"),
        api_key="test-key",
        skills_dir=str(tmp_path / "skills"),
    )


def _ok():
    m = MagicMock()
    m.returncode = 0
    m.stderr = ""
    return m


def _fail(code=1, stderr="error"):
    m = MagicMock()
    m.returncode = code
    m.stderr = stderr
    return m


# ---- ensure_repo ----

def test_ensure_repo_clones_when_missing(runner, tmp_path):
    with patch("subprocess.run", return_value=_ok()) as mock_run:
        result = runner.ensure_repo("acme", "widgets", "abc123", "tok")

    assert result.endswith("acme/widgets")
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert calls[0][:2] == ["git", "clone"]
    assert "x-access-token:tok@github.com/acme/widgets" in calls[0][2]
    assert any("checkout" in c for c in calls)
    assert any("abc123" in str(c) for c in calls)


def test_ensure_repo_fetches_when_exists(runner, tmp_path):
    repo_path = tmp_path / "repos" / "acme" / "widgets"
    repo_path.mkdir(parents=True)

    with patch("subprocess.run", return_value=_ok()) as mock_run:
        runner.ensure_repo("acme", "widgets", "abc123", "tok")

    calls = [c.args[0] for c in mock_run.call_args_list]
    assert not any(c[1] == "clone" for c in calls)
    assert any("fetch" in c for c in calls)
    assert any("checkout" in c for c in calls)


def test_ensure_repo_no_sha_resets_to_fetch_head(runner, tmp_path):
    repo_path = tmp_path / "repos" / "acme" / "widgets"
    repo_path.mkdir(parents=True)

    with patch("subprocess.run", return_value=_ok()) as mock_run:
        runner.ensure_repo("acme", "widgets", None, "tok")

    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any("reset" in c and "FETCH_HEAD" in c for c in calls)


def test_ensure_repo_clone_failure_raises_transient(runner):
    with patch("subprocess.run", return_value=_fail(code=128, stderr="network error")):
        with pytest.raises(TransientError, match="clone failed"):
            runner.ensure_repo("acme", "widgets", "abc123", "tok")


def test_ensure_repo_checkout_failure_raises_permanent(runner, tmp_path):
    repo_path = tmp_path / "repos" / "acme" / "widgets"
    repo_path.mkdir(parents=True)

    responses = [_ok(), _ok(), _fail(code=1, stderr="pathspec 'badsha' not found")]
    with patch("subprocess.run", side_effect=responses):
        with pytest.raises(PermanentError, match="checkout failed"):
            runner.ensure_repo("acme", "widgets", "badsha", "tok")
