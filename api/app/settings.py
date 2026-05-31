"""Process-wide configuration for the API container."""

from __future__ import annotations

import os
from dataclasses import dataclass

from reva.config import env_or_file, required_env_or_file


@dataclass(frozen=True)
class Settings:
    database_url: str
    github_app_id: int
    github_webhook_secret: str
    github_private_key: str
    redis_url: str
    debounce_seconds: int = 600
    migrations_dir: str = "/app/db/migrations"
    queue_name: str = "reviews"
    default_review_mode: str = "diff"
    odoo_callback_url: str = ""
    odoo_callback_api_key: str = ""
    api_key: str = ""
    require_api_key: bool = False
    # Per-client (API key / IP) request cap for /api/v1 over a rolling minute.
    # 0 disables. Per-instance (not shared across API replicas).
    rate_limit_per_minute: int = 0

    @classmethod
    def from_env(cls) -> "Settings":
        with open(os.environ["GITHUB_PRIVATE_KEY_PATH"]) as fp:
            private_key = fp.read()
        api_key = env_or_file("REVA_API_KEY", "") or ""
        require_api_key = os.environ.get("REVA_REQUIRE_API_KEY", "false").lower() in (
            "1", "true", "yes",
        )
        if require_api_key and not api_key:
            raise RuntimeError(
                "REVA_REQUIRE_API_KEY is set but REVA_API_KEY is empty — refusing "
                "to start an unauthenticated API. Set REVA_API_KEY."
            )
        return cls(
            database_url=required_env_or_file("DATABASE_URL"),
            github_app_id=int(os.environ["GITHUB_APP_ID"]),
            github_webhook_secret=required_env_or_file("GITHUB_WEBHOOK_SECRET"),
            github_private_key=private_key,
            redis_url=required_env_or_file("REDIS_URL"),
            debounce_seconds=int(os.environ.get("REVA_DEBOUNCE_SECONDS", "600")),
            migrations_dir=os.environ.get("REVA_MIGRATIONS_DIR", "/app/db/migrations"),
            queue_name=os.environ.get("REVA_QUEUE_NAME", "reviews"),
            default_review_mode=os.environ.get("REVA_DEFAULT_REVIEW_MODE", "diff"),
            odoo_callback_url=os.environ.get("ODOO_CALLBACK_URL", ""),
            odoo_callback_api_key=env_or_file("ODOO_CALLBACK_API_KEY", "") or "",
            api_key=api_key,
            require_api_key=require_api_key,
            rate_limit_per_minute=int(os.environ.get("REVA_API_RATE_LIMIT_PER_MINUTE", "0")),
        )
