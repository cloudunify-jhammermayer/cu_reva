"""Shared .claude-review.yml loading for every service.

Reviews load the config at the PR head SHA; audits and the docs site's
product page at a branch. All degrade to the empty (default) config on a
missing, malformed, or invalid file — a bad config must never fail a run.
"""

from __future__ import annotations

from collections.abc import Callable
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
    github: FileContentReader,
    token: str,
    owner: str,
    name: str,
    ref: str,
    *,
    on_invalid: Callable[[str], None] | None = None,
) -> RepoConfig:
    """Load .claude-review.yml at ref. Malformed or missing YAML -> empty config.

    `on_invalid` lets a caller record its own ops event for a present-but-unusable
    file; the default keeps today's log-only behaviour for reviews and audits.
    """
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
        if on_invalid is not None:
            on_invalid(str(exc))
        return RepoConfig()
    if parsed is None:
        return RepoConfig()
    if not isinstance(parsed, dict):
        if on_invalid is not None:
            on_invalid("not a mapping")
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
        if on_invalid is not None:
            on_invalid(str(exc))
        return RepoConfig()
