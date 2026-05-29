"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from redis import Redis
from rq import Queue

from app.routes import health, webhooks
from app.routes.v1 import router as v1_router
from app.settings import Settings
from reva.db.engine import Database, create_engine_from_url
from reva.github_client import GitHubClient

logger = structlog.get_logger()


def warn_if_no_api_key(settings: Settings) -> None:
    if not settings.api_key:
        logger.warning("api_key_not_set", detail="REVA_API_KEY is unset — all API endpoints are unauthenticated")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    warn_if_no_api_key(settings)
    engine = create_engine_from_url(settings.database_url)
    db = Database(engine)
    db.migrate(settings.migrations_dir)
    redis_conn = Redis.from_url(settings.redis_url)
    app.state.db = db
    app.state.settings = settings
    app.state.rq_queue = Queue(settings.queue_name, connection=redis_conn)
    app.state.github = GitHubClient(
        app_id=settings.github_app_id,
        private_key_pem=settings.github_private_key,
    )
    logger.info("api_started")
    yield
    redis_conn.close()
    logger.info("api_stopped")


app = FastAPI(title="REVA API", lifespan=lifespan)
app.include_router(webhooks.router)
app.include_router(health.router)
app.include_router(v1_router, prefix="/api/v1")
