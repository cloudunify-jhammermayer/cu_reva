"""Lightweight per-client rate limiter for /api/v1.

A rolling-window cap keyed on the caller's API key (or client IP when
unauthenticated). In-memory and therefore per-instance — a basic abuse
guardrail, not a distributed quota. Disabled when the configured limit is 0.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from app.dependencies import get_settings
from app.settings import Settings

_WINDOW_SECONDS = 60.0
_SWEEP_INTERVAL = 300.0
_hits: dict[str, deque[float]] = defaultdict(deque)
_last_sweep = 0.0
# Sync FastAPI deps run on multiple threadpool threads; guard the shared counters
# so a concurrent _sweep del can't drop a bucket another request just fetched.
_lock = threading.Lock()


def reset() -> None:
    """Clear all counters (used by tests)."""
    global _last_sweep
    with _lock:
        _hits.clear()
        _last_sweep = 0.0


def _client_key(request: Request) -> str:
    # nosemgrep rationale: semgrep's Flask "directly-returned-format-string" rule
    # treats a returned request-derived string as an HTTP response body (reflected
    # XSS). This is FastAPI and the value is an internal rate-limit dict key, never
    # a response — false positive.
    auth = request.headers.get("Authorization", "")
    if auth:
        # Hash the token so raw bearer credentials aren't held in process memory
        # (SECU-11); the digest is a stable per-client bucket key.
        return "key:" + hashlib.sha256(auth.encode()).hexdigest()  # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
    host = request.client.host if request.client else "unknown"
    return "ip:" + host  # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string


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
    key = _client_key(request)
    global _last_sweep
    with _lock:
        if now - _last_sweep > _SWEEP_INTERVAL:
            _sweep(now)
            _last_sweep = now
        window = _hits[key]
        cutoff = now - _WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        window.append(now)
