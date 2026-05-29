"""FastAPI dependency providers — pull singletons from app.state."""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request

from app.settings import Settings
from reva.db.engine import Database


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_queue(request: Request) -> "Queue":
    return request.app.state.rq_queue


def get_github_client(request: Request):
    return request.app.state.github


def require_api_key(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Validate Bearer token if REVA_API_KEY is set. No-op when key is empty."""
    if not settings.api_key:
        return
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {settings.api_key}"):
        raise HTTPException(status_code=401, detail="Invalid API key")
