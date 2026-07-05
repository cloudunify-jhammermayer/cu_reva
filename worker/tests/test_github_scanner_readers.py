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
