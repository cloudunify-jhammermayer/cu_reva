"""Issue + PR listing for the docs site's product page: one page each, raw payloads."""

from __future__ import annotations

import httpx

from reva.github_client import GitHubClient


def _client(handler) -> GitHubClient:
    gh = GitHubClient.__new__(GitHubClient)
    gh.base_url = "https://api.github.com"
    gh._client = httpx.Client(transport=httpx.MockTransport(handler))
    return gh


def test_list_issues_open_page():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/issues"
        assert request.url.params["state"] == "open"
        assert request.url.params["per_page"] == "100"
        assert "since" not in request.url.params
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json=[{"number": 1, "state": "open"}])

    assert _client(handle).list_issues("tok", "acme", "widgets", state="open") == [
        {"number": 1, "state": "open"},
    ]


def test_list_issues_closed_since():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["state"] == "closed"
        assert request.url.params["since"] == "2026-07-09T12:00:00Z"
        return httpx.Response(200, json=[])

    assert _client(handle).list_issues(
        "tok", "acme", "widgets", state="closed", since="2026-07-09T12:00:00Z"
    ) == []


def test_list_open_pull_requests():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/pulls"
        assert request.url.params["state"] == "open"
        assert request.url.params["per_page"] == "100"
        return httpx.Response(200, json=[{"number": 7, "title": "Fix #3"}])

    assert _client(handle).list_open_pull_requests("tok", "acme", "widgets") == [
        {"number": 7, "title": "Fix #3"},
    ]
