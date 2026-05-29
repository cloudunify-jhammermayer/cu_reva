"""Environment / secret loading helpers.

Supports the Docker-secrets convention: for any variable `NAME`, if `NAME_FILE`
is set its file contents are used instead. This lets production inject secrets
as files (tmpfs-mounted, not visible in `docker inspect`) while plain env vars
keep working for local dev — fully backward compatible.
"""

from __future__ import annotations

import os


def env_or_file(name: str, default: str | None = None) -> str | None:
    """Return env var `name`, or the stripped contents of the file at `{name}_FILE`.

    The `_FILE` variant wins when set. Falls back to `default` if neither exists.
    """
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        with open(file_path) as f:
            return f.read().strip()
    return os.environ.get(name, default)


def required_env_or_file(name: str) -> str:
    """Like `env_or_file` but raises KeyError if neither the var nor its `_FILE` is set."""
    value = env_or_file(name)
    if value is None:
        raise KeyError(name)
    return value
