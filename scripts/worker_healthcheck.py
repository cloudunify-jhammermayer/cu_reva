"""Container healthcheck for the RQ worker.

RQ registers each worker under `rq:worker:<name>` and keeps the key alive with a
TTL-refreshed heartbeat. A worker key for this container's hostname therefore
means the worker process is alive. Exit 0 = healthy, 1 = unhealthy.
"""

from __future__ import annotations

import os
import socket
import sys


def _default_connection(url: str):
    from redis import Redis

    return Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)


def check(redis_url: str, hostname: str, connection_factory=None) -> bool:
    factory = connection_factory or _default_connection
    try:
        conn = factory(redis_url)
        prefix = f"rq:worker:{hostname}".encode()
        for key in conn.scan_iter(match="rq:worker:*", count=100):
            if key.startswith(prefix):
                return True
        return False
    except Exception:
        return False


if __name__ == "__main__":
    url = os.environ.get("REDIS_URL", "")
    ok = bool(url) and check(url, socket.gethostname())
    sys.exit(0 if ok else 1)
