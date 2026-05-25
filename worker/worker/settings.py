"""Process-wide configuration loaded from environment variables at startup.

`Settings.from_env()` raises `KeyError` for missing required vars — by
design. Failing loudly at boot is preferable to discovering a missing
secret on the first review.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


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
    odoo_callback_url: str = ""        # empty = Odoo callback disabled
    odoo_callback_api_key: str = ""
    repo_cache_dir: str = "/repos"
    repo_cache_ttl_days: int = 30
    skills_dir: str = "/app/prompts/skills"

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
            redis_url=os.environ["REDIS_URL"],
            database_url=os.environ["DATABASE_URL"],
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            github_app_id=int(os.environ["GITHUB_APP_ID"]),
            github_private_key=private_key,
            github_base_url=os.environ.get("GITHUB_BASE_URL", "https://api.github.com"),
            prompts_dir=os.environ.get("REVA_PROMPTS_DIR", "/app/prompts"),
            migrations_dir=os.environ.get("REVA_MIGRATIONS_DIR", "/app/db/migrations"),
            queue_name=os.environ.get("REVA_QUEUE_NAME", "reviews"),
            google_chat_webhook_url=os.environ.get("GOOGLE_CHAT_WEBHOOK_URL", ""),
            odoo_callback_url=os.environ.get("ODOO_CALLBACK_URL", ""),
            odoo_callback_api_key=os.environ.get("ODOO_CALLBACK_API_KEY", ""),
            repo_cache_dir=os.environ.get("REVA_REPO_CACHE_DIR", "/repos"),
            repo_cache_ttl_days=int(os.environ.get("REVA_REPO_CACHE_TTL_DAYS", "30")),
            skills_dir=os.environ.get("REVA_SKILLS_DIR", "/app/prompts/skills"),
        )
