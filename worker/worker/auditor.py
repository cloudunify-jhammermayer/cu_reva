"""Standalone repo audit — pure, no side effects.

`Auditor.execute` clones or fetches the repo at its latest HEAD, runs Claude
Code with the reva-repo-audit skill, and returns an `AuditResult`. It does
NOT write to Postgres or post to GitHub — those side effects live in
`audit_tasks.run_audit`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

import structlog
from pydantic import ValidationError

from reva.claude_code_runner import ClaudeCodeRunner
from reva.cost import estimate_cost
from reva.errors import PermanentError
from reva.types import AuditJobParams, AuditResult, Finding

logger = structlog.get_logger()


class GitHubReader(Protocol):
    def get_installation_token(self, installation_id: int) -> str: ...


class RepoMetaLookup(Protocol):
    def get_repo_meta(self, repository_id: int) -> dict: ...


class Auditor:
    def __init__(
        self,
        runner: ClaudeCodeRunner,
        github: GitHubReader,
        repos: RepoMetaLookup,
    ) -> None:
        self.runner = runner
        self.github = github
        self.repos = repos

    def execute(self, params: AuditJobParams) -> AuditResult:
        """Run a full repo audit. Returns AuditResult.

        Raises:
            TransientError: bubbles from ClaudeCodeRunner (network/git failure).
            PermanentError: Claude output invalid, or repo not found.
        """
        meta = self.repos.get_repo_meta(params.repository_id)
        owner, name = meta["owner"], meta["name"]

        token = self.github.get_installation_token(params.installation_id)

        started_at = datetime.now(timezone.utc)
        with self.runner.repo_lock(owner, name):
            repo_path = self.runner.ensure_repo(owner, name, None, token)
            response = self.runner.review(
                repo_path=repo_path,
                skill="reva-repo-audit",
                params={"repo": f"{owner}/{name}", "default_branch": meta["default_branch"]},
            )
        completed_at = datetime.now(timezone.utc)
        duration_ms = int((completed_at - started_at).total_seconds() * 1000)

        tool_input = response.tool_use_input
        if not isinstance(tool_input, dict):
            raise PermanentError("Audit: Claude returned no tool_use input")
        summary = tool_input.get("summary", "")
        if not summary:
            raise PermanentError("Audit: Claude returned empty summary")

        raw_findings = tool_input.get("findings", [])
        try:
            findings = [Finding.model_validate(f) for f in raw_findings]
        except ValidationError as exc:
            raise PermanentError(f"Audit finding failed schema validation: {exc}") from exc

        # Cost (CORR-11): prefer the CLI's authoritative total, fall back to the
        # token estimate — same as the review path, so audits feed the spend cap.
        cost = response.total_cost_usd or estimate_cost(
            response.model or "",
            response.input_tokens,
            response.output_tokens,
            response.cache_read_tokens,
            response.cache_creation_tokens,
        )

        return AuditResult(
            status="completed",
            summary=summary,
            findings=findings,
            model=response.model,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            estimated_cost_usd=cost,
        )
