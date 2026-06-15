"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis import Redis
from rq import Queue

from app.routes import docs, health, webhooks
from app.routes.v1 import router as v1_router
from app.settings import Settings
from reva.db.engine import Database, create_engine_from_url
from reva.github_client import GitHubClient
from reva.logging import configure_logging

logger = structlog.get_logger()


def warn_if_no_api_key(settings: Settings) -> None:
    if not settings.api_key:
        logger.warning("api_key_not_set", detail="REVA_API_KEY is unset — all API endpoints are unauthenticated")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
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

# App-level body-size cap (SECU-12), defense-in-depth beside nginx's
# client_max_body_size. 26 MB sits just above GitHub's 25 MB webhook payload
# cap so legitimate deliveries pass. Covers Content-Length requests; chunked
# bodies with no Content-Length are bounded by nginx.
_MAX_BODY_BYTES = 26 * 1024 * 1024


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
    return await call_next(request)


app.include_router(webhooks.router)
app.include_router(health.router)
app.include_router(v1_router, prefix="/api/v1")
# Consultant docs browser — gated by Cloudflare Access, not the machine API key.
app.include_router(docs.router, prefix="/repo-docs")
