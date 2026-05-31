"""Pagination clamping for list endpoints.

Keeps `limit`/`offset` within safe bounds so a client can't trigger an
unbounded result set or an expensive deep-offset table scan.
"""

from __future__ import annotations

# Beyond this, keyset pagination would be needed anyway; cap to avoid deep scans.
MAX_OFFSET = 100_000


def clamp_limit(limit: int, max_limit: int) -> int:
    return max(1, min(limit, max_limit))


def clamp_offset(offset: int) -> int:
    return max(0, min(offset, MAX_OFFSET))
