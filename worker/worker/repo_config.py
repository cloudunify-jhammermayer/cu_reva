"""Repo-context helpers for code-grounded worker runs."""

from __future__ import annotations

from reva.repo_config import load_repo_config


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
