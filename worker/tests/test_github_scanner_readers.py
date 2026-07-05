"""Security-alert readers: one page, unavailable -> None."""

from __future__ import annotations

import httpx
import pytest

from reva.errors import TransientError
from reva.github_client import GitHubClient


def _client(handler) -> GitHubClient:
    gh = GitHubClient.__new__(GitHubClient)
    gh.base_url = "https://api.github.com"
    gh._client = httpx.Client(transport=httpx.MockTransport(handler))
    return gh


def _alerts_handler(path_suffix: str, payload: list[dict], status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(path_suffix)
        assert request.url.params["state"] == "open"
        assert request.url.params["per_page"] == "100"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(status, json=payload)

    return handle


def test_code_scanning_alerts_happy():
    payload = [{
        "rule": {"id": "py/sql-injection", "severity": "error"},
        "most_recent_instance": {
            "location": {"path": "custom_addons/x/models/a.py", "start_line": 40},
            "message": {"text": "SQL injection"},
        },
    }]
    gh = _client(_alerts_handler("/code-scanning/alerts", payload))

    alerts = gh.list_code_scanning_alerts("tok", "acme", "widgets")

    assert alerts is not None
    assert alerts[0]["rule"]["id"] == "py/sql-injection"


def test_unavailable_is_none_not_error():
    for status in (403, 404):
        gh = _client(lambda req, s=status: httpx.Response(s, json={"message": "nope"}))
        assert gh.list_code_scanning_alerts("tok", "a", "r") is None
        assert gh.list_dependabot_alerts("tok", "a", "r") is None
        assert gh.list_secret_scanning_alerts("tok", "a", "r") is None


def test_dependabot_and_secret_paths():
    gh = _client(_alerts_handler("/dependabot/alerts", []))
    assert gh.list_dependabot_alerts("tok", "acme", "widgets") == []
    gh2 = _client(_alerts_handler("/secret-scanning/alerts", []))
    assert gh2.list_secret_scanning_alerts("tok", "acme", "widgets") == []


def test_server_error_still_raises_transient():
    gh = _client(lambda req: httpx.Response(502, text="bad gateway"))
    with pytest.raises(TransientError):
        gh.list_code_scanning_alerts("tok", "a", "r")


def test_rate_limit_403_is_transient_not_unavailable():
    """Review finding #8: a rate-limited 403 must retry, not mislabel the
    source as missing-permission/feature."""
    gh = _client(lambda req: httpx.Response(
        403, json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"},
    ))
    with pytest.raises(TransientError):
        gh.list_code_scanning_alerts("tok", "a", "r")


def test_plain_403_still_unavailable():
    gh = _client(lambda req: httpx.Response(
        403, json={"message": "Resource not accessible by integration"},
        headers={"x-ratelimit-remaining": "4999"},
    ))
    assert gh.list_secret_scanning_alerts("tok", "a", "r") is None


def test_secret_alert_locations_happy():
    """Review finding #1: locations come from the per-alert endpoint."""
    payload = [{"type": "commit",
                "details": {"path": "custom_addons/x/models/a.py",
                            "start_line": 12}}]

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/secret-scanning/alerts/3/locations")
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json=payload)

    gh = _client(handle)
    locations = gh.get_secret_alert_locations("tok", "acme", "widgets", 3)
    assert locations[0]["details"]["start_line"] == 12


def test_secret_alert_locations_unavailable_is_none():
    for status in (403, 404):
        gh = _client(lambda req, s=status: httpx.Response(s, json={}))
        assert gh.get_secret_alert_locations("tok", "a", "r", 3) is None
