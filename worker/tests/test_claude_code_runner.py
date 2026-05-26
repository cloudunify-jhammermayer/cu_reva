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


# ---- review ----

@pytest.fixture
def runner_with_skill(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "reva-diff-review.md").write_text("You are REVA. Write JSON to output_path.")
    return ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path / "repos"),
        api_key="test-key",
        skills_dir=str(skills_dir),
    )


def _extract_output_path(task_str: str) -> str:
    """Helper: parse output_path from the task string Claude receives."""
    for line in task_str.splitlines():
        if line.startswith("output_path:"):
            return line.split(": ", 1)[1].strip()
    raise ValueError("output_path not found in task string")


def test_review_returns_claude_response(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)
    review_output = {"summary": "Looks good.", "findings": []}

    def fake_run(args, **kwargs):
        task_str = args[-1]
        out_path = _extract_output_path(task_str)
        with open(out_path, "w") as f:
            json.dump(review_output, f)
        return _ok()

    with patch("subprocess.run", side_effect=fake_run):
        resp = runner_with_skill.review(
            repo_path=repo_path,
            skill="reva-diff-review",
            params={"pr_title": "Fix bug", "diff": "diff content"},
        )

    assert resp.tool_use_input == review_output
    assert resp.model == "claude-sonnet-4-6"
    assert resp.stop_reason == "tool_use"


def test_review_passes_correct_subprocess_args(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    def fake_run(args, **kwargs):
        out_path = _extract_output_path(args[-1])
        with open(out_path, "w") as f:
            json.dump({"summary": "ok", "findings": []}, f)
        return _ok()

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})

    call_args = mock_run.call_args
    args = call_args.args[0]
    assert args[0] == "claude"
    assert "--print" in args
    assert "--output-format" in args
    assert "json" in args
    assert call_args.kwargs["cwd"] == repo_path
    assert call_args.kwargs["env"]["ANTHROPIC_API_KEY"] == "test-key"


def test_review_cleans_up_output_file(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)
    captured = []

    def fake_run(args, **kwargs):
        out_path = _extract_output_path(args[-1])
        captured.append(out_path)
        with open(out_path, "w") as f:
            json.dump({"summary": "ok", "findings": []}, f)
        return _ok()

    with patch("subprocess.run", side_effect=fake_run):
        runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})

    assert captured, "output path was never captured"
    assert not os.path.exists(captured[0]), "temp file was not cleaned up"


def test_review_cleans_up_even_on_error(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)
    captured = []

    def fake_run(args, **kwargs):
        out_path = _extract_output_path(args[-1])
        captured.append(out_path)
        # Do NOT write the file — simulates Claude not writing output
        return _ok()

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(PermanentError):
            runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})

    assert captured
    assert not os.path.exists(captured[0])


def test_review_raises_permanent_on_exit_1(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    with patch("subprocess.run", return_value=_fail(code=1, stderr="bad prompt")):
        with pytest.raises(PermanentError, match="exited 1"):
            runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})


def test_review_raises_transient_on_exit_2(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    with patch("subprocess.run", return_value=_fail(code=2, stderr="killed")):
        with pytest.raises(TransientError, match="exited 2"):
            runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})


def test_review_raises_permanent_on_invalid_json(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    def fake_run(args, **kwargs):
        out_path = _extract_output_path(args[-1])
        with open(out_path, "w") as f:
            f.write("not valid json {{{")
        return _ok()

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(PermanentError, match="invalid JSON"):
            runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})


def test_review_raises_permanent_for_missing_skill(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    with pytest.raises(PermanentError, match="Skill file not found"):
        runner_with_skill.review(repo_path=repo_path, skill="nonexistent-skill", params={})
