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
