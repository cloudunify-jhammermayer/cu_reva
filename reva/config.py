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

# Global kill switch for cross-branch review reuse (spec 2026-07-24). Default on.
CROSS_BRANCH_REUSE = os.environ.get("REVA_CROSS_BRANCH_REUSE", "true").strip().lower() not in ("false", "0", "no")

# Global kill switch for planner-gated code grounding on the ticket-analysis
# and support-answer paths. False keeps both on the docs-only Messages API
# path — the brake to pull if the planner over-escalates in production, since
# an escalated run costs roughly 10-30x and takes the per-repo lock.
TICKET_CODE_GROUNDING = os.environ.get(
    "REVA_TICKET_CODE_GROUNDING", "true"
).strip().lower() not in ("false", "0", "no")

# Global kill switch for golden-estimate anchoring (spec 2026-08-04). Default
# on: the feature is self-gating, because a file with no anchors renders a
# bands-only block, which is today's behaviour. Off forces bands-only even when
# anchors exist.
GOLDEN_ESTIMATES = os.environ.get(
    "REVA_GOLDEN_ESTIMATES", "true"
).strip().lower() not in ("false", "0", "no")

# Maximum anchor *stories* rendered into a prompt. Overflow is dropped in file
# order and recorded as an ops event — a silent truncation would read as full
# coverage.
try:
    GOLDEN_ESTIMATE_LIMIT = int(os.environ.get("REVA_GOLDEN_ESTIMATE_LIMIT", "30"))
except ValueError:
    GOLDEN_ESTIMATE_LIMIT = 30

# Public base URL of the docs site (nginx serves the SPA under /docs/), used to
# build the release-log page link sent to Odoo (spec 2026-09-04-release-log,
# R2). Empty = unset: the worker still sends a relative link and records an ops
# event, so a missing value is visible on the first request, not silent.
DOCS_SITE_URL = os.environ.get("REVA_DOCS_SITE_URL", "").strip().rstrip("/")


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
