"""FastAPI dependency providers — pull singletons from app.state."""

from __future__ import annotations

from fastapi import Request

from app.settings import Settings
from reva.db.engine import Database


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings
