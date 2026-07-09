"""Parse GitHub repository URLs from Odoo project settings.

Odoo guarantees only that github_url is non-empty free text (github-issues
handoff, Contract 1) — REVA owns validation. Shared by the api route (reject
at accept time with 422, while Odoo still shows the error to the user) and
the worker (derive owner/repo). Stdlib-only so both images can import it.
"""

from __future__ import annotations

import re

_REPO_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def parse_github_repo_url(url: str) -> tuple[str, str] | None:
    """Return (owner, repo) for an https://github.com/{owner}/{repo} URL, else None.

    Tolerates surrounding whitespace, a trailing slash, and a .git suffix;
    rejects extra path segments, other hosts, and non-https schemes.
    """
    match = _REPO_URL_RE.match(url.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


_PROJECT_URL_RE = re.compile(
    r"^https://github\.com/(orgs|users)/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"/projects/(\d+)(?:/views/\d+)?/?$"
)


def parse_github_project_url(url: str) -> tuple[str, str, int] | None:
    """Return ("orgs"|"users", owner, number) for a Projects v2 URL, else None.

    Tolerates surrounding whitespace, a trailing slash, and a /views/{n} suffix
    (what you get copying the address bar from an open board view); rejects
    other hosts, schemes, and extra path segments. Shared by the api route
    (reject at accept time with 422) and the worker (resolve the board).
    """
    match = _PROJECT_URL_RE.match(url.strip())
    if match is None:
        return None
    return match.group(1), match.group(2), int(match.group(3))
