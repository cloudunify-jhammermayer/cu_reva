"""Tests for Auditor.execute."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from reva.types import AuditJobParams, AuditResult, ClaudeResponse, Finding
from worker.auditor import Auditor


@dataclass
class FakeGitHub:
    def get_installation_token(self, installation_id: int) -> str:
        return "ghs_tok"


@dataclass
class FakeRunner:
    response: ClaudeResponse | None = None
    raise_exc: Exception | None = None
    default_model: str = "claude-sonnet-4-6"
    deep_model: str = "claude-opus-4-7"
    last_skill: str | None = None
    last_params: dict | None = None

    def repo_lock(self, owner, name):
        import contextlib
        return contextlib.nullcontext()

    def ensure_repo(self, owner, name, head_sha, token) -> str:
        return f"/fake/repos/{owner}/{name}"

    def review(self, repo_path, skill, params, model=None) -> ClaudeResponse:
        self.last_skill = skill
        self.last_params = params
        if self.raise_exc:
            raise self.raise_exc
        return self.response


@dataclass
class FakeRepos:
    meta: dict = field(default_factory=lambda: {
        "owner": "acme",
        "name": "widgets",
        "installation_id": 100,
        "default_branch": "main",
    })

    def get_repo_meta(self, repository_id: int) -> dict:
        return self.meta


def _audit_response(findings=None) -> ClaudeResponse:
    return ClaudeResponse(
        model="claude-sonnet-4-6",
        stop_reason="tool_use",
        tool_use_input={
            "summary": "Codebase looks reasonable.",
            "findings": findings or [],
        },
    )


def _params(**overrides) -> AuditJobParams:
    base = {"repository_id": 1, "installation_id": 100}
    base.update(overrides)
    return AuditJobParams(**base)


def _make_auditor(**overrides):
    runner = overrides.pop("runner", None) or FakeRunner(response=_audit_response())
    github = overrides.pop("github", None) or FakeGitHub()
    repos = overrides.pop("repos", None) or FakeRepos()
    return Auditor(runner=runner, github=github, repos=repos), runner, github, repos


def test_audit_completed_returns_result():
    auditor, _, _, _ = _make_auditor()
    result = auditor.execute(_params())
    assert result.status == "completed"
    assert result.summary == "Codebase looks reasonable."
    assert result.findings == []


def test_audit_uses_reva_repo_audit_skill():
    auditor, runner, _, _ = _make_auditor()
    auditor.execute(_params())
    assert runner.last_skill == "reva-repo-audit"


def test_audit_ensure_repo_called_with_none_sha():
    called_with = []

    class TrackingRunner(FakeRunner):
        def ensure_repo(self, owner, name, head_sha, token):
            called_with.append(head_sha)
            return "/fake"

    auditor, _, _, _ = _make_auditor(runner=TrackingRunner(response=_audit_response()))
    auditor.execute(_params())
    assert called_with == [None]


def test_audit_propagates_transient_error():
    from reva.errors import TransientError
    runner = FakeRunner(raise_exc=TransientError("timeout"))
    auditor, _, _, _ = _make_auditor(runner=runner)
    with pytest.raises(TransientError):
        auditor.execute(_params())
