"""Lightweight per-client rate limiter for /api/v1.

A rolling-window cap keyed on the caller's API key (or client IP when
unauthenticated). In-memory and therefore per-instance — a basic abuse
guardrail, not a distributed quota. Disabled when the configured limit is 0.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from app.dependencies import get_settings
from app.settings import Settings

_WINDOW_SECONDS = 60.0
_SWEEP_INTERVAL = 300.0
_hits: dict[str, deque[float]] = defaultdict(deque)
_last_sweep = 0.0


def reset() -> None:
    """Clear all counters (used by tests)."""
    global _last_sweep
    _hits.clear()
    _last_sweep = 0.0


def _client_key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth:
        # Hash the token so raw bearer credentials aren't held in process memory
        # (SECU-11); the digest is a stable per-client bucket key.
        return "key:" + hashlib.sha256(auth.encode()).hexdigest()
    host = request.client.host if request.client else "unknown"
    return "ip:" + host


def _sweep(now: float) -> None:
    """Drop buckets whose newest hit is older than the window — bounds memory
    growth from one-off clients/IPs (PERF-4)."""
    cutoff = now - _WINDOW_SECONDS
    for key in [k for k, w in _hits.items() if not w or w[-1] < cutoff]:
        del _hits[key]


def rate_limit(request: Request, settings: Settings = Depends(get_settings)) -> None:
    limit = settings.rate_limit_per_minute
    if not limit:
        return
    now = time.monotonic()
    global _last_sweep
    if now - _last_sweep > _SWEEP_INTERVAL:
        _sweep(now)
        _last_sweep = now
    window = _hits[_client_key(request)]
    cutoff = now - _WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    window.append(now)
