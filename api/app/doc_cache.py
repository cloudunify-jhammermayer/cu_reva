"""Small in-process TTL caches for the docs surface.

Every /repo-docs read hits GitHub live, and the SPA's filter fans out a tree
call per repo — so a short cache cuts latency and keeps us well under the App
installation token's hourly rate limit. Per-replica; staleness is bounded by
the TTLs below (docs change rarely, so minutes are fine).
"""

from __future__ import annotations

import threading
import time


class TTLCache:
    def __init__(self, ttl: float, maxsize: int = 1024) -> None:
        self._ttl = ttl
        self._max = maxsize
        self._d: dict = {}  # key -> (expires_at, value)
        self._lock = threading.Lock()

    def get(self, key):
        now = time.monotonic()
        with self._lock:
            item = self._d.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at < now:
                self._d.pop(key, None)
                return None
            return value

    def set(self, key, value) -> None:
        with self._lock:
            if len(self._d) >= self._max and key not in self._d:
                # Drop the soonest-to-expire entries to make room (cheap, approximate).
                for k in sorted(self._d, key=lambda k: self._d[k][0])[: max(1, self._max // 8)]:
                    self._d.pop(k, None)
            self._d[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


# Branches change rarely; trees/files a bit more often.
branches_cache = TTLCache(ttl=300)
tree_cache = TTLCache(ttl=120)
file_cache = TTLCache(ttl=120)


def clear_all() -> None:
    """Reset every cache (used by tests to avoid cross-test bleed)."""
    branches_cache.clear()
    tree_cache.clear()
    file_cache.clear()
