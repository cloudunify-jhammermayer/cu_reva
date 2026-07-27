"""Shared .claude-review.yml loading for worker pipelines.

Reviews load the config at the PR head SHA; audits at the default branch.
Both degrade to the empty (default) config on a missing, malformed, or
invalid file — a bad config must never fail a run.
"""

from __future__ import annotations

from typing import Protocol

import structlog
import yaml
from pydantic import ValidationError

from reva.types import RepoConfig

logger = structlog.get_logger()


class FileContentReader(Protocol):
    def get_file_content(
        self, token: str, owner: str, repo: str, path: str, ref: str
    ) -> str | None: ...


def load_repo_config(
    github: FileContentReader, token: str, owner: str, name: str, ref: str
) -> RepoConfig:
    """Load .claude-review.yml at ref. Malformed or missing YAML -> empty config."""
    raw = github.get_file_content(token, owner, name, ".claude-review.yml", ref)
    if not raw:
        return RepoConfig()
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.warning(
            "claude_review_yml_parse_failed",
            owner=owner,
            name=name,
            ref=ref[:8],
            error=str(exc),
        )
        return RepoConfig()
    if not isinstance(parsed, dict):
        return RepoConfig()
    try:
        return RepoConfig.model_validate(parsed)
    except ValidationError as exc:
        # A bad value for a known field (e.g. block_on_severity: high) would
        # otherwise fail every run on this repo. Degrade to defaults, same as
        # the malformed-YAML path above.
        logger.warning(
            "claude_review_yml_invalid",
            owner=owner,
            name=name,
            ref=ref[:8],
            error=str(exc),
        )
        return RepoConfig()


def resolve_repo_context(github, github_url: str | None, log):
    """Resolve ``(owner, name, token, RepoConfig)`` for a code-grounded run.

    Returns None when the repo can't be reached — no URL, unparseable, or the
    GitHub App isn't installed on it. Callers degrade to the docs-only path and
    record their own ops event, so the reason lands on one channel per feature.

    Shared by the ticket-analysis and support-answer runners: both gate the
    same CLI escalation on the same preconditions, and duplicating the
    token/config dance in two runners is how the two would drift.
    """
    from reva.github_urls import parse_github_repo_url

    if not github_url:
        return None
    parsed = parse_github_repo_url(github_url)
    if parsed is None:
        return None
    owner, name = parsed
    try:
        installation_id = github.get_repo_installation_id(owner, name)
        token = github.get_installation_token(installation_id)
        default_branch = github.get_repo(token, owner, name).get("default_branch") or "main"
        config = load_repo_config(github, token, owner, name, default_branch)
    except Exception:
        log.warning("repo_context_failed", owner=owner, name=name, exc_info=True)
        return None
    return owner, name, token, config


def code_grounding_allowed(config) -> bool:
    """Both brakes must be released: the global env switch and the per-repo
    `.claude-review.yml` flag."""
    from reva.config import TICKET_CODE_GROUNDING

    return TICKET_CODE_GROUNDING and (config is None or config.code_grounding)
