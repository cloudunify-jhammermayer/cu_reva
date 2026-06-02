"""Lightweight per-client rate limiter for /api/v1.

A rolling-window cap keyed on the caller's API key (or client IP when
unauthenticated). In-memory and therefore per-instance — a basic abuse
guardrail, not a distributed quota. Disabled when the configured limit is 0.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from app.dependencies import get_settings
from app.settings import Settings

_WINDOW_SECONDS = 60.0
_hits: dict[str, deque[float]] = defaultdict(deque)


def reset() -> None:
    """Clear all counters (used by tests)."""
    _hits.clear()


def _client_key(request: Request) -> str:
    # nosemgrep rationale: the Flask "directly-returned-format-string" rule treats
    # a returned f-string as an HTTP response body (reflected XSS). This is FastAPI
    # and the value is an internal rate-limit dict key, never a response — false positive.
    auth = request.headers.get("Authorization", "")
    if auth:
        return f"key:{auth}"  # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
    return f"ip:{request.client.host if request.client else 'unknown'}"  # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string


def rate_limit(request: Request, settings: Settings = Depends(get_settings)) -> None:
    limit = settings.rate_limit_per_minute
    if not limit:
        return
    now = time.monotonic()
    window = _hits[_client_key(request)]
    cutoff = now - _WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    window.append(now)
