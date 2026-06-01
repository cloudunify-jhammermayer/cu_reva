"""Core review orchestration — pure, no side effects.

`Reviewer.execute` takes job parameters, fetches read-only context from
GitHub via a Protocol (so it can be faked in tests), calls Claude, and
returns a ReviewResult. It does NOT write to Postgres, post to GitHub, or
send notifications — those side effects live in `tasks.run_review`.
"""

from __future__ import annotations

import fnmatch
import os
from datetime import datetime, timezone
from typing import Protocol

import structlog
import yaml
from pydantic import ValidationError

from reva.claude_code_runner import ClaudeCodeRunner
from reva.cost import estimate_cost
from reva.diff_utils import (
    DEFAULT_EXCLUDE_EXTENSIONS,
    DEFAULT_REVIEW_PREFIXES,
    count_diff_lines,
    estimate_diff_tokens,
    filter_diff,
    filter_diff_by_paths,
)
from reva.errors import PermanentError
from reva.prompt_builder import PromptBuilder  # kept for type annotation (prompts param)
from reva.types import (
    Finding,
    JobParams,
    RepoConfig,
    ReviewResult,
    RiskLevel,
)

logger = structlog.get_logger()

DEFAULT_MAX_DIFF_LINES = 2000
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

    def get_compare_diff(
        self, token: str, owner: str, repo: str, base_sha: str, head_sha: str
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

    def get_last_completed_review(self, pull_request_id: int) -> dict | None:
        """Returns {id, head_sha} or None if no completed review exists."""
        ...


class Reviewer:
    def __init__(
        self,
        runner: ClaudeCodeRunner,
        github: GitHubReader,
        repos: RepoLookup,
        prompts: PromptBuilder,
        max_diff_lines: int = DEFAULT_MAX_DIFF_LINES,
        max_diff_tokens: int = DEFAULT_MAX_DIFF_TOKENS,
    ) -> None:
        self.runner = runner
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
        # The /review-all command ("diff-all" mode) reviews every changed path;
        # all other modes restrict to the custom_addons prefixes.
        review_prefixes = () if params.review_mode == "diff-all" else DEFAULT_REVIEW_PREFIXES
        # Delta detection: if a prior completed review exists, use the compare diff.
        last_review = self.repos.get_last_completed_review(params.pull_request_id)
        if last_review:
            raw_diff = self.github.get_compare_diff(
                token, owner, name, last_review["head_sha"], params.head_sha
            )
            diff = filter_diff(raw_diff, include_prefixes=review_prefixes)
            if not diff.strip():
                return ReviewResult(
                    status="stale",
                    summary="No reviewable changes since last review.",
                    risk_level="low",
                )
            skill = "reva-delta-review"
            delta_base_sha: str | None = last_review["head_sha"]
        else:
            raw_diff = self.github.get_pull_request_diff(token, owner, name, pr_number)
            diff = filter_diff(raw_diff, include_prefixes=review_prefixes)
            # deep == full repo exploration (like full) but on the Opus model.
            # diff/diff-all stay on the cheap diff skill; only full/deep explore.
            skill = (
                "reva-diff-review"
                if params.review_mode in ("diff", "diff-all")
                else "reva-full-review"
            )
            delta_base_sha = None

        if len(diff) < len(raw_diff):
            logger.info(
                "diff_filtered",
                owner=owner, repo=name, pr=pr_number,
                raw_bytes=len(raw_diff), filtered_bytes=len(diff),
                review_prefixes=review_prefixes,
                excluded_extensions=sorted(DEFAULT_EXCLUDE_EXTENSIONS),
            )
        if not diff.strip():
            if review_prefixes:
                prefixes = ", ".join(f"`{p}`" for p in review_prefixes)
                return _decline(
                    f"No reviewable files found. Only changes under {prefixes} "
                    f"are reviewed (excluding {', '.join(sorted(DEFAULT_EXCLUDE_EXTENSIONS))})."
                )
            return _decline(
                f"No reviewable files found (excluding "
                f"{', '.join(sorted(DEFAULT_EXCLUDE_EXTENSIONS))})."
            )

        changed_files_payload = self.github.get_changed_files(token, owner, name, pr_number)
        changed_files = [
            f["filename"] for f in changed_files_payload
            if (not review_prefixes or any(f["filename"].startswith(p) for p in review_prefixes))
            and os.path.splitext(f["filename"])[1].lower() not in DEFAULT_EXCLUDE_EXTENSIONS
        ]

        # 6. Load .claude-review.yml (CLAUDE.md is picked up automatically by Claude Code).
        repo_config = self._load_repo_config(token, owner, name, params.head_sha)

        # 7. Resolve per-review limits.
        max_lines, max_tokens = self._resolve_limits(repo_config)

        # 8. Diff size guards.
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

        # 9. skip_paths filtering.
        if repo_config.skip_paths:
            diff = filter_diff_by_paths(diff, repo_config.skip_paths)
            if not diff.strip():
                return _decline(
                    "All changed files matched skip_paths; nothing reviewable remains."
                )
            diff_lines = count_diff_lines(diff)
            diff_tokens = estimate_diff_tokens(diff)
            if diff_lines > max_lines:
                return _decline(
                    f"Diff still too large after skip_paths filtering "
                    f"({diff_lines} lines > {max_lines} max). "
                    f"Add more patterns to skip_paths or split the PR."
                )
            if diff_tokens > max_tokens:
                return _decline(
                    f"Diff still too large after skip_paths filtering "
                    f"({diff_tokens} tokens > {max_tokens} max). "
                    f"Add more patterns to skip_paths or split the PR."
                )

        # 10. Select model.
        model = self.runner.deep_model if params.review_mode == "deep" else self.runner.default_model

        skill_params = {
            "pr_title": pr_basic.get("title", ""),
            "pr_body": pr_basic.get("body") or pr_detail.get("body") or "",
            "diff": diff,
            "changed_files": "\n".join(f"- {f}" for f in changed_files),
            "base_branch": pr_basic["base_branch"],
            "head_branch": pr_basic["head_branch"],
        }

        # 11. Ensure repo is cloned/updated, then call Claude Code. The lock
        # spans both so a concurrent job can't checkout a different SHA into the
        # shared working tree while Claude is reading it.
        started_at = datetime.now(timezone.utc)
        with self.runner.repo_lock(owner, name):
            repo_path = self.runner.ensure_repo(owner, name, params.head_sha, token)
            response = self.runner.review(repo_path=repo_path, skill=skill, params=skill_params, model=model)
        completed_at = datetime.now(timezone.utc)
        duration_ms = int((completed_at - started_at).total_seconds() * 1000)

        # 12. Validate and parse findings.
        summary, findings = _parse_tool_use(response.tool_use_input)

        # 13. Drop findings citing files absent from the clone (hallucinated or
        # injection-fabricated), then cap by severity * confidence and recompute risk.
        grounded = _ground_findings(findings, repo_path)
        capped = _cap_findings(grounded, MAX_FINDINGS)
        risk_level = _recompute_risk_level(capped)

        # 14. Cost: prefer the CLI's authoritative total_cost_usd; fall back to
        # the token-based estimate (Messages-API path, or older CLI output).
        cost = response.total_cost_usd or estimate_cost(
            response.model or model,
            response.input_tokens,
            response.output_tokens,
            response.cache_read_tokens,
            response.cache_creation_tokens,
        )

        # 15. Prompt version (best-effort).
        try:
            prompt_version = self.prompts.get_version()
        except Exception as exc:  # noqa: BLE001
            logger.warning("prompt_version_unavailable", error=str(exc))
            prompt_version = None

        return ReviewResult(
            status="completed",
            summary=summary,
            risk_level=risk_level,
            findings=capped,
            diff=diff,
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
            delta_base_sha=delta_base_sha,
        )

    # ----------------------------------------------------------------- helpers

    def _load_repo_config(
        self, token: str, owner: str, name: str, head_sha: str
    ) -> RepoConfig:
        """Load .claude-review.yml. Malformed or missing YAML -> empty config."""
        raw = self.github.get_file_content(
            token, owner, name, ".claude-review.yml", head_sha
        )
        if not raw:
            return RepoConfig()
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
            return RepoConfig()
        if not isinstance(parsed, dict):
            return RepoConfig()
        return RepoConfig.model_validate(parsed)

    def _resolve_limits(self, repo_config: RepoConfig) -> tuple[int, int]:
        """Per-repo overrides for diff size guards."""
        max_lines = repo_config.max_diff_lines if repo_config.max_diff_lines is not None else self.max_diff_lines
        max_tokens = repo_config.max_diff_tokens if repo_config.max_diff_tokens is not None else self.max_diff_tokens
        return max_lines, max_tokens


# --- Module-level helpers -----------------------------------------------------


def _decline(reason: str) -> ReviewResult:
    return ReviewResult(
        status="declined",
        summary=reason,
        risk_level="low",
        decline_reason=reason,
    )


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _parse_tool_use(tool_use_input: dict | None) -> tuple[str, list[Finding]]:
    """Validate Claude's submit_review tool input strictly.

    Returns (summary, findings). Raises PermanentError on any schema
    violation per pr-review-requirements §5.
    """
    if not isinstance(tool_use_input, dict):
        raise PermanentError("Claude returned no tool_use input (expected an object)")
    summary = tool_use_input.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise PermanentError("Claude tool_use input has missing or empty summary")
    raw_findings = tool_use_input.get("findings", [])
    if not isinstance(raw_findings, list):
        raise PermanentError("Claude tool_use input: 'findings' field is not a list")
    try:
        findings = [Finding.model_validate(f) for f in raw_findings]
    except ValidationError as exc:
        raise PermanentError(f"Claude finding failed schema validation: {exc}") from exc
    return summary, findings


def _ground_findings(findings: list[Finding], repo_path: str) -> list[Finding]:
    """Drop findings that cite a file not present in the cloned repo.

    A finding pointing at a nonexistent path (or one escaping the clone via
    `../`) is almost always a hallucination or an injection-fabricated location;
    dropping it improves precision and limits a prompt injection's ability to put
    attacker-chosen text on the PR. Findings with no file (general findings) are
    kept. Fail-open: if the clone path is absent we can't verify, so we drop
    nothing rather than nuking every finding.
    """
    if not os.path.isdir(repo_path):
        return findings
    root = os.path.realpath(repo_path)
    kept: list[Finding] = []
    dropped: list[str] = []
    for f in findings:
        if not f.file:
            kept.append(f)
            continue
        resolved = os.path.realpath(os.path.join(root, f.file))
        within = resolved == root or resolved.startswith(root + os.sep)
        if within and os.path.isfile(resolved):
            kept.append(f)
        else:
            dropped.append(f.file)
    if dropped:
        logger.warning("findings_dropped_ungrounded", count=len(dropped), files=dropped)
    return kept


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
