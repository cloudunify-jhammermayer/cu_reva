"""Tests for parse_github_repo_url (github-issues handoff: github_url is
free text on the Odoo side — REVA owns validation)."""

from __future__ import annotations

import pytest

from reva.github_urls import parse_github_project_url, parse_github_repo_url


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/org/repo", ("org", "repo")),
        ("https://github.com/org/repo/", ("org", "repo")),
        ("https://github.com/org/repo.git", ("org", "repo")),
        ("https://github.com/My-Org/my.repo-name", ("My-Org", "my.repo-name")),
        ("  https://github.com/org/repo  ", ("org", "repo")),
    ],
)
def test_valid_urls(url, expected):
    assert parse_github_repo_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "http://github.com/org/repo",            # not https
        "https://gitlab.com/org/repo",            # wrong host
        "https://github.com/org",                 # missing repo
        "https://github.com/org/repo/issues/1",   # extra path segments
        "https://github.com/org/repo/tree/main",
        "https://evil.com/https://github.com/org/repo",
        "https://github.com/-org/repo",           # invalid owner
    ],
)
def test_invalid_urls(url):
    assert parse_github_repo_url(url) is None


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/orgs/acme/projects/5", ("orgs", "acme", 5)),
        ("https://github.com/users/jo/projects/12", ("users", "jo", 12)),
        ("  https://github.com/orgs/acme/projects/5/  ", ("orgs", "acme", 5)),
        ("https://github.com/orgs/acme/projects/5/views/3", ("orgs", "acme", 5)),
    ],
)
def test_parse_github_project_url_accepts(url, expected):
    assert parse_github_project_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/widgets",                    # repo, not a project
        "https://github.com/orgs/acme/projects/",             # no number
        "https://github.com/orgs/acme/projects/abc",          # non-numeric
        "http://github.com/orgs/acme/projects/5",             # not https
        "https://gitlab.com/orgs/acme/projects/5",            # wrong host
        "https://github.com/orgs/acme/projects/5/settings",   # extra segment
        "",
    ],
)
def test_parse_github_project_url_rejects(url):
    assert parse_github_project_url(url) is None
