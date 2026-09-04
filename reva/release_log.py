"""Release-log lookup helpers (spec docs/superpowers/specs/archive/2026-09-04-release-log-requirements.md).

Customer repos commit `docs/releases/<slug>.html`, an HTML fragment with
`rl-*` classes generated from the developer-written Markdown next to it. REVA
does not draft anything: on Odoo's request it finds that page and ships it with
the shared theme (`static/release-log.css`, compiled from
`docs-ui/src/release-log.scss` by `npm run build:theme`).
"""

from __future__ import annotations

import re
from importlib import resources
from urllib.parse import quote

from reva import config

RELEASE_DIR = "docs/releases"
_WHITESPACE = re.compile(r"\s+")


def release_slug(name: str) -> str:
    """`name.strip().lower()` with each whitespace run replaced by `-` (R2).
    Nothing else is normalised: the generator on the repo side applies the
    same rule, and both must agree byte for byte."""
    return _WHITESPACE.sub("-", name.strip().lower())


def is_safe_slug(slug: str) -> bool:
    """A slug names one file directly under docs/releases/: non-empty, no path
    separators, no leading dot. Anything else could walk out of the folder
    (httpx normalises `..` segments) and read another repo of the same GitHub
    App installation."""
    return bool(slug) and "/" not in slug and "\\" not in slug and not slug.startswith(".")


def release_log_path(slug: str) -> str:
    if not is_safe_slug(slug):
        raise ValueError(f"unsafe release slug: {slug!r}")
    return f"{RELEASE_DIR}/{slug}.html"


def docs_site_page_url(repository_id: int, path: str) -> str:
    """Docs-site deep link (`docs-ui/src/location.js`: `?repo=<id>&path=<file>`,
    no `ref` = default branch). Relative when REVA_DOCS_SITE_URL is unset."""
    return f"{config.DOCS_SITE_URL}/docs/?repo={repository_id}&path={quote(path, safe='/')}"


def theme_css() -> str:
    """The compiled theme, packaged with the library so every service image
    (the worker sends it) carries the same bytes the docs site renders with."""
    return (resources.files("reva") / "static" / "release-log.css").read_text(encoding="utf-8")
