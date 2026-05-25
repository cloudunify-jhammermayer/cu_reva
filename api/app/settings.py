"""Process-wide configuration for the API container."""

from __future__ import annotations

import os
from dataclasses import dataclass


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

    @classmethod
    def from_env(cls) -> "Settings":
        with open(os.environ["GITHUB_PRIVATE_KEY_PATH"]) as fp:
            private_key = fp.read()
        return cls(
            database_url=os.environ["DATABASE_URL"],
            github_app_id=int(os.environ["GITHUB_APP_ID"]),
            github_webhook_secret=os.environ["GITHUB_WEBHOOK_SECRET"],
            github_private_key=private_key,
            redis_url=os.environ["REDIS_URL"],
            debounce_seconds=int(os.environ.get("REVA_DEBOUNCE_SECONDS", "600")),
            migrations_dir=os.environ.get("REVA_MIGRATIONS_DIR", "/app/db/migrations"),
            queue_name=os.environ.get("REVA_QUEUE_NAME", "reviews"),
            default_review_mode=os.environ.get("REVA_DEFAULT_REVIEW_MODE", "diff"),
            odoo_callback_url=os.environ.get("ODOO_CALLBACK_URL", ""),
            odoo_callback_api_key=os.environ.get("ODOO_CALLBACK_API_KEY", ""),
            api_key=os.environ.get("REVA_API_KEY", ""),
        )
