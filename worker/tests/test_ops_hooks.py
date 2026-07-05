"""Degradation paths must record ops events."""

from __future__ import annotations

import subprocess

import pytest

from reva.claude_code_runner import ClaudeCodeRunner
from reva.errors import TransientError


def _runner(tmp_path, events):
    return ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path),
        api_key="k",
        skills_dir=str(tmp_path),
        prompts_dir=str(tmp_path),
        codegraph_enabled=True,
        ops_recorder=lambda c, s, e, d: events.append((c, s, e, d)),
    )


def test_codegraph_failure_records_event(tmp_path, monkeypatch):
    events: list = []
    runner = _runner(tmp_path, events)

    def boom(*args, **kwargs):
        raise FileNotFoundError("codegraph not installed")

    monkeypatch.setattr("reva.claude_code_runner.subprocess.run", boom)
    assert runner._codegraph_prepare(str(tmp_path)) is None

    component, severity, event, detail = events[0]
    assert component == "codegraph"
    assert severity == "warning"
    assert event == "index_skipped"
    assert "codegraph not installed" in detail["error"]


def test_codegraph_nonzero_exit_records_event(tmp_path, monkeypatch):
    events: list = []
    runner = _runner(tmp_path, events)

    class R:
        returncode = 1
        stderr = "parse explosion"
        stdout = ""

    monkeypatch.setattr(
        "reva.claude_code_runner.subprocess.run", lambda *a, **k: R()
    )
    assert runner._codegraph_prepare(str(tmp_path)) is None
    assert events[0][2] == "index_failed"
    assert "parse explosion" in events[0][3]["error"]


def test_git_timeout_records_event(tmp_path, monkeypatch):
    events: list = []
    runner = _runner(tmp_path, events)

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr("reva.claude_code_runner.subprocess.run", slow)
    with pytest.raises(TransientError):
        runner._run_git(["-C", str(tmp_path), "fetch", "origin"], TransientError)
    assert events[0][0] == "git"
    assert events[0][2] == "timeout"
    assert events[0][3]["cmd"] == "fetch"


def test_no_recorder_is_safe(tmp_path, monkeypatch):
    runner = ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path),
        api_key="k",
        skills_dir=str(tmp_path),
        prompts_dir=str(tmp_path),
        codegraph_enabled=True,
    )

    def boom(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("reva.claude_code_runner.subprocess.run", boom)
    assert runner._codegraph_prepare(str(tmp_path)) is None


def test_recorder_exception_is_swallowed(tmp_path, monkeypatch):
    def bad_recorder(c, s, e, d):
        raise RuntimeError("recorder broken")

    runner = ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path),
        api_key="k",
        skills_dir=str(tmp_path),
        prompts_dir=str(tmp_path),
        codegraph_enabled=True,
        ops_recorder=bad_recorder,
    )

    def boom(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("reva.claude_code_runner.subprocess.run", boom)
    assert runner._codegraph_prepare(str(tmp_path)) is None
