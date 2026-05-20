"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.routes import health, webhooks
from app.routes.v1 import router as v1_router
from app.settings import Settings
from reva.db.engine import Database, create_engine_from_url

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    engine = create_engine_from_url(settings.database_url)
    db = Database(engine)
    db.migrate(settings.migrations_dir)
    app.state.db = db
    app.state.settings = settings
    logger.info("api_started")
    yield
    logger.info("api_stopped")


app = FastAPI(title="REVA API", lifespan=lifespan)
app.include_router(webhooks.router)
app.include_router(health.router)
app.include_router(v1_router, prefix="/api/v1")
