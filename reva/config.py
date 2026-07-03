"""Environment / secret loading helpers.

Supports the Docker-secrets convention: for any variable `NAME`, if `NAME_FILE`
is set its file contents are used instead. This lets production inject secrets
as files (tmpfs-mounted, not visible in `docker inspect`) while plain env vars
keep working for local dev — fully backward compatible.
"""

from __future__ import annotations

import os

# Model selection — single source of truth so the direct-API client
# (`reva.claude_client`) and the Claude Code CLI runner (`reva.claude_code_runner`)
# always use the same models. Override per-deployment via env; the defaults are
# the pinned production models.
DEFAULT_MODEL = os.environ.get("REVA_DEFAULT_MODEL", "claude-sonnet-5")
DEEP_MODEL = os.environ.get("REVA_DEEP_MODEL", "claude-opus-4-8")
# Binary finding-verdict calls (second-pass self-critique + delta-resolution
# pass): short outputs, windowed inputs — Haiku-priced by default.
VERIFY_MODEL = os.environ.get("REVA_VERIFY_MODEL", "claude-haiku-4-5")


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
    """Like `env_or_file` but raises KeyError if the value is missing OR empty.

    'Present' must mean 'non-empty': an empty/truncated secret file or blank env
    var would otherwise boot a broken service — and for the webhook secret that
    means a forgeable HMAC (`hmac.new(b"", …)`), accepting arbitrary unsigned
    webhooks (SECU-2/CORR-9). Fail loud at startup instead.
    """
    value = env_or_file(name)
    if value is None or not value.strip():
        raise KeyError(name)
    return value
