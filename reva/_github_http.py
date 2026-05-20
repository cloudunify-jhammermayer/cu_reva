"""Shared GitHub HTTP plumbing — error mapping, retry-after parsing.

Imported by both `github_client` (reader+writer methods) so the error
contract is identical regardless of which side of the API is in use.
"""

from __future__ import annotations

import time

import httpx

from reva.errors import PermanentError, TransientError


class NotFound(Exception):
    """Internal sentinel for 404 responses the caller wants to treat as None."""


def map_github_status(response: httpx.Response, action: str) -> Exception:
    """Map a non-2xx GitHub response to a worker exception."""
    sc = response.status_code
    body_snippet = (response.text or "")[:200]
    msg = f"GitHub {sc} ({action}): {body_snippet}"

    if sc == 401:
        return PermanentError(f"{msg} — App auth invalid")
    if sc == 403:
        if response.headers.get("x-ratelimit-remaining") == "0":
            retry_after = seconds_until(response.headers.get("x-ratelimit-reset"))
            return TransientError(f"{msg} — rate limited", retry_after=retry_after)
        return PermanentError(f"{msg} — forbidden")
    if sc == 404:
        return PermanentError(msg)
    if sc == 422:
        return PermanentError(msg)
    if sc == 429:
        retry_after = parse_retry_after(response.headers.get("retry-after"))
        return TransientError(msg, retry_after=retry_after)
    if sc >= 500:
        return TransientError(msg)
    return PermanentError(msg)


def parse_retry_after(value: str | None) -> int | None:
    """Anthropic/GitHub return integer-seconds Retry-After. Be defensive."""
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def seconds_until(unix_ts: str | None) -> int | None:
    """Seconds from now until a Unix-epoch timestamp string. None if past."""
    if not unix_ts:
        return None
    try:
        target = int(unix_ts)
    except ValueError:
        return None
    delta = target - int(time.time())
    return delta if delta > 0 else None
