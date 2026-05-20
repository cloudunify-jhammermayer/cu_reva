"""Core review orchestration — pure, no side effects.

`Reviewer.execute` takes job parameters, fetches read-only context from
GitHub via a Protocol (so it can be faked in tests), calls Claude, and
returns a ReviewResult. It does NOT write to Postgres, post to GitHub, or
send notifications — those side effects live in `tasks.run_review`.
"""

from __future__ import annotations

import fnmatch
from datetime import datetime, timezone
from typing import Protocol

import structlog
import yaml
from pydantic import ValidationError

from reva.claude_client import ClaudeClient
from reva.cost import estimate_cost
from reva.diff_utils import count_diff_lines, estimate_diff_tokens
from reva.errors import PermanentError
from reva.prompt_builder import PromptBuilder
from reva.review_tool import build_review_tool_schema, tool_choice_force_submit
from reva.types import (
    Finding,
    JobParams,
    ReviewResult,
    RiskLevel,
)

logger = structlog.get_logger()

DEFAULT_MAX_DIFF_LINES = 1000
DEFAULT_MAX_DIFF_TOKENS = 60_000
MAX_FINDINGS = 15

# severity weights for capping and risk_level recomputation
_SEVERITY_WEIGHT: dict[str, int] = {"info": 1, "minor": 2, "major": 3, "critical": 4}


class GitHubReader(Protocol):
    """Minimal read surface Reviewer needs from a GitHub client.

    Implemented in the next slice by `github_client.GitHubClient`. Kept as a
    Protocol so the reviewer is unit-testable with a fake.
    """

    def get_installation_token(self, installation_id: int) -> str: ...

    def get_pull_request(self, token: str, owner: str, repo: str, pr_number: int) -> dict: ...

    def get_pull_request_diff(
        self, token: str, owner: str, repo: str, pr_number: int
    ) -> str: ...

    def get_changed_files(
        self, token: str, owner: str, repo: str, pr_number: int
    ) -> list[dict]: ...

    def get_file_content(
        self, token: str, owner: str, repo: str, path: str, ref: str
    ) -> str | None: ...


class RepoLookup(Protocol):
    """Reviewer needs the repo's owner/name to call GitHub. The DB layer
    provides this; we don't want Reviewer touching SQLAlchemy directly."""

    def get_owner_name(self, repository_id: int) -> tuple[str, str]: ...

    def get_pr_basic(self, pull_request_id: int) -> dict:
        """Returns {pr_number, title, body, base_branch, head_branch}."""
        ...


class Reviewer:
    def __init__(
        self,
        claude: ClaudeClient,
        github: GitHubReader,
        repos: RepoLookup,
        prompts: PromptBuilder,
        max_diff_lines: int = DEFAULT_MAX_DIFF_LINES,
        max_diff_tokens: int = DEFAULT_MAX_DIFF_TOKENS,
    ) -> None:
        self.claude = claude
        self.github = github
        self.repos = repos
        self.prompts = prompts
        self.max_diff_lines = max_diff_lines
        self.max_diff_tokens = max_diff_tokens

    # ------------------------------------------------------------------ public

    def execute(self, params: JobParams) -> ReviewResult:
        """Run a single review attempt. See module docstring for the contract.

        Raises:
            TransientError: bubbles up from the Claude client; RQ will retry.
            PermanentError: malformed Claude response (invalid Finding shape,
                missing tool_use, etc.). RQ marks the job failed.
        """
        # 1-2. Resolve repo + PR metadata from the DB lookup.
        owner, name = self.repos.get_owner_name(params.repository_id)
        pr_basic = self.repos.get_pr_basic(params.pull_request_id)
        pr_number = pr_basic["pr_number"]

        # 3. Installation token.
        token = self.github.get_installation_token(params.installation_id)

        # 4. Stale check.
        pr_detail = self.github.get_pull_request(token, owner, name, pr_number)
        current_sha = pr_detail.get("head", {}).get("sha")
        if current_sha and current_sha != params.head_sha:
            logger.info(
                "review_stale",
                expected=params.head_sha[:8],
                current=current_sha[:8],
            )
            return ReviewResult(
                status="stale",
                summary="Head SHA changed before review started.",
                risk_level="low",
            )

        # 5. Fetch diff + changed files.
        diff = self.github.get_pull_request_diff(token, owner, name, pr_number)
        changed_files_payload = self.github.get_changed_files(token, owner, name, pr_number)
        changed_files = [f["filename"] for f in changed_files_payload]

        # 6. Load .claude-review.yml + CLAUDE.md (both optional).
        repo_config = self._load_repo_config(token, owner, name, params.head_sha)
        claude_md = self.github.get_file_content(
            token, owner, name, "CLAUDE.md", params.head_sha
        )

        # 7. Resolve per-review limits (repo config overrides global defaults).
        max_lines, max_tokens = self._resolve_limits(repo_config)

        # 8. Diff size guards (line count AND token estimate).
        diff_lines = count_diff_lines(diff)
        diff_tokens = estimate_diff_tokens(diff)
        if diff_lines > max_lines:
            return _decline(
                f"Diff too large ({diff_lines} lines > {max_lines} max). "
                f"Please split this PR into smaller, focused changes."
            )
        if diff_tokens > max_tokens:
            return _decline(
                f"Diff exceeds the token budget ({diff_tokens} tokens > "
                f"{max_tokens} max). Please split this PR."
            )

        # 9. skip_paths short-circuit: if every changed file matches a skip
        # glob, there's nothing to review. Per-hunk filtering is a TODO.
        skip_patterns = _extract_skip_patterns(repo_config)
        if changed_files and skip_patterns and all(
            _matches_any(p, skip_patterns) for p in changed_files
        ):
            return _decline(
                "All changed files match skip_paths; nothing to review."
            )

        # 10. Build prompts.
        system_blocks = self.prompts.build_system_blocks(repo_config, claude_md)
        user_prompt = self.prompts.build_user_prompt(
            mode=params.review_mode,
            pr_title=pr_basic.get("title", ""),
            pr_body=pr_basic.get("body") or pr_detail.get("body") or "",
            diff=diff,
            changed_files=changed_files,
            base_branch=pr_basic["base_branch"],
            head_branch=pr_basic["head_branch"],
        )

        # 11. Select model.
        model = (
            self.claude.deep_model
            if params.review_mode == "deep"
            else self.claude.default_model
        )

        # 12. Call Claude.
        started_at = datetime.now(timezone.utc)
        response = self.claude.review(
            system_blocks=system_blocks,
            user_prompt=user_prompt,
            tools=[build_review_tool_schema()],
            tool_choice=tool_choice_force_submit(),
            model=model,
        )
        completed_at = datetime.now(timezone.utc)
        duration_ms = int((completed_at - started_at).total_seconds() * 1000)

        # 13. Validate and parse findings (strict per pr-review-requirements §5).
        summary, findings = _parse_tool_use(response.tool_use_input)

        # 14. Cap findings by severity * confidence, then recompute risk_level.
        capped = _cap_findings(findings, MAX_FINDINGS)
        risk_level = _recompute_risk_level(capped)

        # 15. Cost.
        cost = estimate_cost(
            response.model or model,
            response.input_tokens,
            response.output_tokens,
            response.cache_read_tokens,
            response.cache_creation_tokens,
        )

        # 16. Prompt version (best-effort).
        try:
            prompt_version = self.prompts.get_version()
        except Exception as exc:  # noqa: BLE001 — non-fatal
            logger.warning("prompt_version_unavailable", error=str(exc))
            prompt_version = None

        return ReviewResult(
            status="completed",
            summary=summary,
            risk_level=risk_level,
            findings=capped,
            model=response.model or model,
            prompt_version=prompt_version,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_read_tokens=response.cache_read_tokens,
            cache_creation_tokens=response.cache_creation_tokens,
            estimated_cost_usd=cost,
        )

    # ----------------------------------------------------------------- helpers

    def _load_repo_config(
        self, token: str, owner: str, name: str, head_sha: str
    ) -> dict:
        """Load .claude-review.yml. Malformed YAML -> empty config + warning."""
        raw = self.github.get_file_content(
            token, owner, name, ".claude-review.yml", head_sha
        )
        if not raw:
            return {}
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            logger.warning(
                "claude_review_yml_parse_failed",
                owner=owner,
                name=name,
                head_sha=head_sha[:8],
                error=str(exc),
            )
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _resolve_limits(self, repo_config: dict) -> tuple[int, int]:
        """Per-repo overrides for diff size guards."""
        max_lines = repo_config.get("max_diff_lines", self.max_diff_lines)
        max_tokens = repo_config.get("max_diff_tokens", self.max_diff_tokens)
        return int(max_lines), int(max_tokens)


# --- Module-level helpers -----------------------------------------------------


def _decline(reason: str) -> ReviewResult:
    return ReviewResult(
        status="declined",
        summary=reason,
        risk_level="low",
        decline_reason=reason,
    )


def _extract_skip_patterns(repo_config: dict) -> list[str]:
    raw = repo_config.get("skip_paths") or []
    if not isinstance(raw, list):
        return []
    return [str(p) for p in raw if isinstance(p, str)]


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _parse_tool_use(tool_use_input: dict | None) -> tuple[str, list[Finding]]:
    """Validate Claude's submit_review tool input strictly.

    Returns (summary, findings). Raises PermanentError on any schema
    violation per pr-review-requirements §5.
    """
    if not isinstance(tool_use_input, dict):
        raise PermanentError("Claude tool_use input missing or not an object")
    summary = tool_use_input.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise PermanentError("Claude tool_use input missing non-empty summary")
    raw_findings = tool_use_input.get("findings", [])
    if not isinstance(raw_findings, list):
        raise PermanentError("Claude tool_use input findings is not a list")
    try:
        findings = [Finding.model_validate(f) for f in raw_findings]
    except ValidationError as exc:
        raise PermanentError(f"Claude finding failed validation: {exc}") from exc
    return summary, findings


def _cap_findings(findings: list[Finding], max_count: int) -> list[Finding]:
    """Keep the top N findings ordered by severity_weight * confidence (desc).
    Ties broken by severity weight (higher first) then index (stable)."""
    if len(findings) <= max_count:
        return findings
    indexed = list(enumerate(findings))
    indexed.sort(
        key=lambda pair: (
            -(_SEVERITY_WEIGHT[pair[1].severity] * pair[1].confidence),
            -_SEVERITY_WEIGHT[pair[1].severity],
            pair[0],
        )
    )
    return [f for _, f in indexed[:max_count]]


def _recompute_risk_level(findings: list[Finding]) -> RiskLevel:
    """Deterministic mapping from pr-review-requirements §4."""
    has_critical = any(f.severity == "critical" for f in findings)
    has_major = any(f.severity == "major" for f in findings)
    minor_count = sum(1 for f in findings if f.severity == "minor")
    if has_critical:
        return "critical"
    if has_major:
        return "high"
    if minor_count >= 3:
        return "medium"
    return "low"
