"""Worker liveness healthcheck: an RQ worker key for this hostname must exist."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "worker_healthcheck",
    Path(__file__).resolve().parents[2] / "scripts" / "worker_healthcheck.py",
)
worker_healthcheck = importlib.util.module_from_spec(_SPEC)
sys.modules["worker_healthcheck"] = worker_healthcheck
assert _SPEC.loader is not None
_SPEC.loader.exec_module(worker_healthcheck)


class FakeRedis:
    def __init__(self, keys: list[bytes]) -> None:
        self._keys = keys

    def scan_iter(self, match: str, count: int = 100):
        yield from self._keys


def test_healthy_when_worker_key_matches_hostname():
    fake = FakeRedis([b"rq:worker:abc123.42"])
    assert worker_healthcheck.check(
        "redis://ignored", "abc123", connection_factory=lambda url: fake
    ) is True


def test_unhealthy_when_no_key_for_hostname():
    fake = FakeRedis([b"rq:worker:otherhost.7"])
    assert worker_healthcheck.check(
        "redis://ignored", "abc123", connection_factory=lambda url: fake
    ) is False


def test_unhealthy_when_redis_unreachable():
    def boom(url):
        raise ConnectionError("redis down")

    assert worker_healthcheck.check(
        "redis://ignored", "abc123", connection_factory=boom
    ) is False
