"""Review-path core knowledge wiring."""

from __future__ import annotations

from typing import get_args

import pytest


def test_standard_functionality_category_valid():
    from reva.types import Category

    assert "standard-functionality" in get_args(Category)


def test_runner_review_passes_add_dirs(tmp_path, monkeypatch):
    import reva.claude_code_runner as ccr

    runner = ccr.ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path),
        api_key="k",
        skills_dir=str(tmp_path),
        prompts_dir="",
    )
    (tmp_path / "myskill.md").write_text("skill body")
    captured: dict = {}

    class Proc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(ccr.subprocess, "run", fake_run)
    with pytest.raises(Exception):
        runner.review(
            repo_path=str(tmp_path),
            skill="myskill",
            params={},
            extra_dirs=["/core/19.0/odoo", "/core/19.0/documentation"],
        )
    cmd = captured["cmd"]
    assert cmd.count("--add-dir") == 2
    assert cmd[cmd.index("--add-dir") + 1] == "/core/19.0/odoo"


def test_runner_review_no_add_dirs_by_default(tmp_path, monkeypatch):
    import reva.claude_code_runner as ccr

    runner = ccr.ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path),
        api_key="k",
        skills_dir=str(tmp_path),
        prompts_dir="",
    )
    (tmp_path / "myskill.md").write_text("skill body")
    captured: dict = {}

    class Proc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr(
        ccr.subprocess,
        "run",
        lambda cmd, **kwargs: captured.update(cmd=cmd) or Proc(),
    )
    with pytest.raises(Exception):
        runner.review(repo_path=str(tmp_path), skill="myskill", params={})
    assert "--add-dir" not in captured["cmd"]


def test_redaction_covers_core_paths():
    from reva.review_formatter import _redact_internal_paths

    text = _redact_internal_paths("evidence at /core/19.0/odoo/addons/sale/x.py")
    assert "/core" not in text
