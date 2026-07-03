"""Process-wide configuration loaded from environment variables at startup.

`Settings.from_env()` raises `KeyError` for missing required vars — by
design. Failing loudly at boot is preferable to discovering a missing
secret on the first review.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import structlog

from reva.config import required_env_or_file

logger = structlog.get_logger()


@dataclass(frozen=True)
class Settings:
    redis_url: str
    database_url: str
    anthropic_api_key: str
    github_app_id: int
    github_private_key: str  # PEM contents, not a path
    github_base_url: str = "https://api.github.com"
    prompts_dir: str = "/app/prompts"
    migrations_dir: str = "/app/db/migrations"
    queue_name: str = "reviews"
    google_chat_webhook_url: str = ""  # empty = notifications disabled
    repo_cache_dir: str = "/repos"
    repo_cache_ttl_days: int = 30
    skills_dir: str = "/app/prompts/skills"
    # Rolling 24-hour Anthropic spend cap (USD). None = no cap. When trailing
    # 24-hour spend reaches this, new reviews are declined instead of run.
    daily_budget_usd: float | None = None
    # CodeGraph engine layer (repo-aware reviews/audits). Default off; pinned and
    # validated against the live CLI before enabling. See the engine-layer spec.
    codegraph_enabled: bool = False
    codegraph_version: str = "0.9.8"
    codegraph_index_timeout: int = 180
    # Second-pass self-critique: re-verify blocking-threshold findings before
    # posting. Default ON (Haiku-priced, windowed, bounded); per-repo
    # `.claude-review.yml verify_findings` overrides it.
    verify_findings_default: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        """Load settings from environment.

        Required env vars:
            REDIS_URL, DATABASE_URL, ANTHROPIC_API_KEY,
            GITHUB_APP_ID, GITHUB_PRIVATE_KEY_PATH

        Optional:
            GITHUB_BASE_URL, REVA_PROMPTS_DIR, REVA_MIGRATIONS_DIR, REVA_QUEUE_NAME
        """
        with open(os.environ["GITHUB_PRIVATE_KEY_PATH"]) as fp:
            private_key = fp.read()
        return cls(
            redis_url=required_env_or_file("REDIS_URL"),
            database_url=required_env_or_file("DATABASE_URL"),
            anthropic_api_key=required_env_or_file("ANTHROPIC_API_KEY"),
            github_app_id=int(os.environ["GITHUB_APP_ID"]),
            github_private_key=private_key,
            github_base_url=os.environ.get("GITHUB_BASE_URL", "https://api.github.com"),
            prompts_dir=os.environ.get("REVA_PROMPTS_DIR", "/app/prompts"),
            migrations_dir=os.environ.get("REVA_MIGRATIONS_DIR", "/app/db/migrations"),
            queue_name=os.environ.get("REVA_QUEUE_NAME", "reviews"),
            google_chat_webhook_url=os.environ.get("GOOGLE_CHAT_WEBHOOK_URL", ""),
            repo_cache_dir=os.environ.get("REVA_REPO_CACHE_DIR", "/repos"),
            repo_cache_ttl_days=int(os.environ.get("REVA_REPO_CACHE_TTL_DAYS", "30")),
            skills_dir=os.environ.get("REVA_SKILLS_DIR", "/app/prompts/skills"),
            daily_budget_usd=(
                float(os.environ["REVA_DAILY_BUDGET_USD"])
                if os.environ.get("REVA_DAILY_BUDGET_USD")
                else None
            ),
            codegraph_enabled=os.environ.get("REVA_CODEGRAPH_ENABLED", "false").lower()
            in ("1", "true", "yes"),
            codegraph_version=os.environ.get("REVA_CODEGRAPH_VERSION", "0.9.8"),
            codegraph_index_timeout=int(os.environ.get("REVA_CODEGRAPH_INDEX_TIMEOUT", "180")),
            verify_findings_default=_verify_findings_default_from_env(),
        )


def _verify_findings_default_from_env() -> bool:
    """REVA_VERIFY_FINDINGS (default on). Legacy REVA_VERIFY_HIGH_COST is
    honored when the new var is unset — flips the default without an ops
    change at deploy; remove after the fleet migrates."""
    value = os.environ.get("REVA_VERIFY_FINDINGS")
    if value is None:
        legacy = os.environ.get("REVA_VERIFY_HIGH_COST")
        if legacy is not None:
            logger.warning(
                "deprecated_env_var",
                var="REVA_VERIFY_HIGH_COST",
                replacement="REVA_VERIFY_FINDINGS",
            )
            return legacy.lower() in ("1", "true", "yes")
        return True
    return value.lower() in ("1", "true", "yes")
