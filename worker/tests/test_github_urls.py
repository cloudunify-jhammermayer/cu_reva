"""Tests for parse_github_repo_url (github-issues handoff: github_url is
free text on the Odoo side — REVA owns validation)."""

from __future__ import annotations

import pytest

from reva.github_urls import parse_github_repo_url


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
