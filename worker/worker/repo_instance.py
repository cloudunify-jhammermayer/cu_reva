"""The Odoo instance a repo declares in `.claude-review.yml` (`odoo_instance`)."""

from __future__ import annotations

import yaml

from reva.db import writers
from reva.errors import TransientError


class UnknownRepoInstance(Exception):
    """The repo names an instance REVA has no active instance for."""


class UnreadableRepoConfig(Exception):
    """The repo's config cannot be read, so what it declares is unknown."""


def declared_instance_id(ctx, repo: str, installation_id: int, log) -> int | None:
    """Id of the active instance the repo's default-branch config names, or
    None when it names none (repo not registered, no config file, empty YAML,
    no `odoo_instance` key or a null one: the caller keeps its default ladder).
    The key is read from the raw file, not through the validated config: an
    invalid value in an unrelated key must not hide the declared instance.
    Raises UnknownRepoInstance when the repo names an instance that is not an
    active one, and UnreadableRepoConfig (ops event recorded) when the file
    cannot be parsed, its top level is not a mapping, `odoo_instance` is not a
    non-empty string, or GitHub fails non-transiently: the caller must not fall
    back to the default instance then. TransientError -> RQ retry."""
    row = writers.get_repository_by_full_name(ctx.db, repo)
    if row is None:
        return None

    def _failed(reason: str) -> UnreadableRepoConfig:
        log.warning("repo_instance_config_failed", repo=repo, error=reason)
        writers.record_ops_event(
            ctx.db, "odoo_callback", "warning", "repo_instance_config_failed",
            {"repo": repo, "error": reason[:300]},
        )
        return UnreadableRepoConfig(reason)

    try:
        token = ctx.github.get_installation_token(installation_id)
        text = ctx.github.get_file_content(
            token, row["owner"], row["name"], ".claude-review.yml", row["default_branch"]
        )
    except TransientError:
        raise
    except Exception as exc:  # noqa: BLE001 — no ticket is routed, visibly
        raise _failed(str(exc)) from exc
    if not text:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _failed(f"invalid YAML: {exc}") from exc
    if data is None:
        return None
    if not isinstance(data, dict):
        raise _failed("top level is not a mapping")
    if data.get("odoo_instance") is None:
        return None
    name = data["odoo_instance"]
    if not isinstance(name, str) or not name.strip():
        raise _failed("odoo_instance is not a non-empty string")
    instance_id = writers.get_active_odoo_instance_id_by_name(ctx.db, name)
    if instance_id is None:
        raise UnknownRepoInstance(name)
    return instance_id
