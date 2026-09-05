"""Release-log lookup helpers (spec docs/superpowers/specs/archive/2026-09-04-release-log-requirements.md).

Customer repos commit `docs/releases/<slug>.html`, an HTML fragment with
`rl-*` classes generated from the developer-written Markdown next to it. REVA
does not draft anything: on Odoo's request it finds that page and ships it with
the shared theme (`static/release-log.css`, compiled from
`docs-ui/src/release-log.scss` by `npm run build:theme`).

The module also parses the underlying `docs/releases/<name>.md` and renders one
ticket's entry for the merged-PR change summary (spec
`docs/superpowers/specs/archive/2026-09-04-release-log-change-notes-design.md`).
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
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


# --- Release-log entries (spec 2026-09-04-release-log-change-notes) -----------
#
# The customer repos' docs/releases/<name>.md is the developer-written source
# of what shipped per ticket. Parsed here so the merged-PR summary Odoo posts
# carries that text instead of a Claude draft.

_FRONTMATTER_FENCE = "---"
_ENTRY_HEADING_RE = re.compile(r"^##\s+(\d+)\s*[—–-]\s*(.+?)\s*$")
_OTHER_H2_RE = re.compile(r"^##\s+")
_H3_RE = re.compile(r"^###\s+(.+?)\s*$")
_META_RE = re.compile(r"^-\s+(Status|Module):\s*(.*?)\s*$")
_LIST_ITEM_RE = re.compile(r"^-\s+(.*)$")
_STRONG_RE = re.compile(r"\*\*(.+?)\*\*")
_EM_RE = re.compile(r"(?<![\w*])_(.+?)_(?![\w*])")
_CODE_RE = re.compile(r"`([^`]+)`")


class ReleaseLogParseError(ValueError):
    """The text is not a release log: no frontmatter, or no `release` key."""


@dataclass(frozen=True)
class ReleaseEntry:
    ticket: int
    title: str
    status: str
    modules: tuple[str, ...]
    built: str  # raw Markdown of "### Gebaut", "" when absent
    todo: str  # raw Markdown of "### To-do", "" when absent


@dataclass(frozen=True)
class ReleaseLogDoc:
    release: str
    status: str
    date: str
    entries: dict[int, ReleaseEntry]


def _parse_frontmatter(lines: list[str]) -> tuple[dict[str, str], int]:
    """(key -> value, index of the first body line). Values may carry a
    trailing `# comment` (the wenatex spec's example does)."""
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        raise ReleaseLogParseError("no frontmatter")
    meta: dict[str, str] = {}
    for i in range(1, len(lines)):
        line = lines[i].strip()
        if line == _FRONTMATTER_FENCE:
            return meta, i + 1
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.split(" #", 1)[0].strip()
    raise ReleaseLogParseError("unterminated frontmatter")


def parse_release_log(text: str) -> ReleaseLogDoc:
    """Parse a release log into its entries, keyed by ticket number.

    Lenient beyond the frontmatter: unknown metadata, extra `###` sections
    and prose outside entries are ignored, and the trailing `##` sections
    (Entscheidungen, Nicht in diesem Release) simply end the entry list."""
    lines = text.splitlines()
    meta, start = _parse_frontmatter(lines)
    if not meta.get("release"):
        raise ReleaseLogParseError("frontmatter has no release")

    entries: dict[int, ReleaseEntry] = {}
    current: dict | None = None
    section: str | None = None

    def _close() -> None:
        if current is not None:
            entries[current["ticket"]] = ReleaseEntry(
                ticket=current["ticket"],
                title=current["title"],
                status=current["status"],
                modules=tuple(current["modules"]),
                built="\n".join(current["built"]).strip(),
                todo="\n".join(current["todo"]).strip(),
            )

    for raw in lines[start:]:
        line = raw.rstrip()
        heading = _ENTRY_HEADING_RE.match(line)
        if heading:
            _close()
            current = {
                "ticket": int(heading.group(1)),
                "title": heading.group(2),
                "status": "",
                "modules": [],
                "built": [],
                "todo": [],
            }
            section = None
            continue
        if _OTHER_H2_RE.match(line):
            _close()
            current = None
            section = None
            continue
        if current is None:
            continue
        h3 = _H3_RE.match(line)
        if h3:
            name = h3.group(1).casefold()
            section = "built" if name == "gebaut" else "todo" if name == "to-do" else "other"
            continue
        if section is None:
            meta_line = _META_RE.match(line)
            if meta_line:
                if meta_line.group(1) == "Status":
                    current["status"] = meta_line.group(2)
                else:
                    current["modules"] = [
                        m.strip() for m in meta_line.group(2).split("·") if m.strip()
                    ]
            continue
        if section in ("built", "todo"):
            current[section].append(line)
    _close()
    return ReleaseLogDoc(
        release=meta.get("release", ""),
        status=meta.get("status", ""),
        date=meta.get("date", ""),
        entries=entries,
    )


def _inline_html(text: str) -> str:
    """Escape, then the three inline marks the format uses."""
    out = html_lib.escape(text, quote=False)
    out = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _STRONG_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _EM_RE.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    return out


def _block_html(markdown: str) -> str:
    """Paragraphs -> <p>, `- ` lists -> <ul><li> (indented continuation lines
    belong to the item), everything joined without newlines."""
    parts: list[str] = []
    for para in re.split(r"\n\s*\n", markdown.strip()):
        lines = [ln for ln in para.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(_LIST_ITEM_RE.match(ln) or ln.startswith((" ", "\t")) for ln in lines) and _LIST_ITEM_RE.match(
            lines[0]
        ):
            items: list[str] = []
            for ln in lines:
                item = _LIST_ITEM_RE.match(ln)
                if item:
                    items.append(item.group(1).strip())
                else:
                    items[-1] = f"{items[-1]} {ln.strip()}"
            parts.append("<ul>" + "".join(f"<li>{_inline_html(i)}</li>" for i in items) + "</ul>")
        else:
            parts.append("<p>" + _inline_html(" ".join(ln.strip() for ln in lines)) + "</p>")
    return "".join(parts)


def render_entry_html(entry: ReleaseEntry) -> str:
    """The chatter body for one entry: Gebaut then To-do, each with a bold
    caption; a section without content is omitted."""
    out = ""
    if entry.built:
        out += "<p><strong>Gebaut</strong></p>" + _block_html(entry.built)
    if entry.todo:
        out += "<p><strong>To-do</strong></p>" + _block_html(entry.todo)
    return out
