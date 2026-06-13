"""Helpers for inspecting unified diffs."""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass

# Extensions stripped from diffs before size-guarding and Claude ingestion.
# These files are rarely useful for code review and can be extremely large
# (Odoo XML views, gettext .po/.pot catalogs).
DEFAULT_EXCLUDE_EXTENSIONS: frozenset[str] = frozenset({".xml", ".po", ".pot", ".md", ".rst"})

# Only files under these path prefixes are reviewed. Everything else
# (CI configs, root-level scripts, OCA modules, etc.) is dropped. Both the
# underscore and hyphen spellings of the custom-addons directory are accepted.
DEFAULT_REVIEW_PREFIXES: tuple[str, ...] = ("custom_addons/", "custom-addons/")

# Third-party Odoo core (`odoo/`) and Enterprise (`enterprise/`) trees. REVA may
# read them for context, but must NEVER review them or report findings on them —
# they are not the team's code. Dropped from EVERY diff and from findings in all
# review modes (including /review-all, full, deep, and audits), even when the
# include-prefix restriction is lifted.
DEFAULT_EXCLUDE_PREFIXES: tuple[str, ...] = ("odoo/", "enterprise/")


def is_excluded_path(path: str) -> bool:
    """Whether `path` is third-party Odoo core / Enterprise code REVA never reviews."""
    return path.startswith(DEFAULT_EXCLUDE_PREFIXES)

# Machine-generated / vendored files that exist in many repos and are never
# worth reviewing (matched against the file's basename). Dropped from every diff
# regardless of prefix — reviewing them is noise and wastes tokens.
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "*.lock",                # poetry.lock, Cargo.lock, *.lock, ...
    "*.min.js", "*.min.css",  # minified assets
    "*.map",                 # source maps
    "package-lock.json", "npm-shrinkwrap.json",
    "yarn.lock", "pnpm-lock.yaml",
    "go.sum", "composer.lock", "Gemfile.lock", "Pipfile.lock",
    "*.snap",                # test snapshots
)

# Matches a hunk header: `@@ -old_start,old_count +new_start,new_count @@`
# Counts are optional and default to 1.
_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def count_diff_lines(diff: str) -> int:
    """Count added + removed lines (excluding file headers)."""
    count = 0
    for line in diff.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            count += 1
        elif line.startswith("-") and not line.startswith("---"):
            count += 1
    return count


def estimate_diff_tokens(diff: str) -> int:
    """Rough token estimate (chars / 4). Pre-call guard so a giant diff
    can't blow the context window even when the line count is under the
    configured limit."""
    return len(diff) // 4


def iter_diff_files(diff: str) -> Iterator[str]:
    """Yield file paths referenced by `+++ b/<path>` headers."""
    for line in diff.split("\n"):
        if line.startswith("+++ b/"):
            # rstrip CR so CRLF diffs don't leave a trailing \r in the path
            # (which would break inline-comment file matching) — TEST-14.
            yield line[len("+++ b/") :].rstrip("\r")


def extract_file_paths(diff: str) -> set[str]:
    """Return the set of file paths touched by the diff."""
    return set(iter_diff_files(diff))


def filter_diff(
    diff: str,
    exclude_extensions: frozenset[str] = DEFAULT_EXCLUDE_EXTENSIONS,
    include_prefixes: tuple[str, ...] = DEFAULT_REVIEW_PREFIXES,
    exclude_globs: tuple[str, ...] = DEFAULT_EXCLUDE_GLOBS,
    exclude_prefixes: tuple[str, ...] = DEFAULT_EXCLUDE_PREFIXES,
) -> str:
    """Keep only per-file sections that pass all filters:

    - exclude_prefixes: file path must NOT start with any prefix (third-party
      odoo/ + enterprise/ trees). Applied in every mode, even when
      include_prefixes is empty — REVA never reviews third-party code.
    - include_prefixes: file path must start with at least one prefix
      (empty tuple = no restriction).
    - exclude_extensions: file extension must not be in this set.
    - exclude_globs: file basename must not match any glob (lockfiles, minified
      assets, source maps, etc. — generated noise present in many repos).

    Splits on `diff --git` boundaries and reassembles the kept sections.
    """
    if not diff:
        return diff
    sections = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    kept = []
    for section in sections:
        if not section:
            continue
        m = re.search(r"^\+\+\+ b/(.+)$", section, re.MULTILINE)
        if m:
            path = m.group(1)
            if exclude_prefixes and path.startswith(exclude_prefixes):
                continue
            if include_prefixes and not any(path.startswith(p) for p in include_prefixes):
                continue
            if os.path.splitext(path)[1].lower() in exclude_extensions:
                continue
            basename = os.path.basename(path)
            if any(fnmatch.fnmatch(basename, g) for g in exclude_globs):
                continue
        kept.append(section)
    return "".join(kept)


def filter_diff_by_paths(diff: str, patterns: list[str]) -> str:
    """Strip file sections whose path matches any glob in patterns.

    Unlike filter_diff (which strips fixed extensions/prefixes), this uses
    caller-supplied globs from .claude-review.yml skip_paths for per-file
    filtering without discarding the rest of the diff.
    """
    if not patterns:
        return diff
    sections = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    kept: list[str] = []
    for section in sections:
        if not section:
            continue
        m = re.search(r"^\+\+\+ b/(.+)$", section, re.MULTILINE)
        if m and any(fnmatch.fnmatch(m.group(1), p) for p in patterns):
            continue
        kept.append(section)
    return "".join(kept)


# --- Hunk parsing ------------------------------------------------------------


@dataclass(frozen=True)
class DiffHunk:
    """One @@ ... @@ block in a unified diff.

    `new_start` / `new_count` describe the post-change line range in the file
    — i.e. the lines a PR reviewer can comment on inline.
    """

    file_path: str
    new_start: int
    new_count: int

    @property
    def new_end(self) -> int:
        # Inclusive last new-side line. A 0-count hunk has no new lines.
        return self.new_start + max(self.new_count - 1, 0)


def parse_diff_hunks(diff: str) -> list[DiffHunk]:
    """Return every hunk in the diff with its new-side line range.

    Only the `+++ b/<path>` header and `@@ ... @@` headers are read;
    hunk bodies are ignored. Robust to multi-file diffs.
    """
    hunks: list[DiffHunk] = []
    current_file: str | None = None
    for line in diff.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/") :].rstrip("\r")  # CRLF-safe (TEST-14)
            continue
        if line.startswith("@@") and current_file is not None:
            match = _HUNK_RE.match(line)
            if not match:
                continue
            new_start = int(match.group("new_start"))
            new_count_raw = match.group("new_count")
            new_count = int(new_count_raw) if new_count_raw is not None else 1
            hunks.append(
                DiffHunk(file_path=current_file, new_start=new_start, new_count=new_count)
            )
    return hunks


def find_line_in_hunks(file_path: str, line: int, hunks: list[DiffHunk]) -> bool:
    """Whether (file_path, line) falls inside any new-side hunk range."""
    for h in hunks:
        if h.file_path == file_path and h.new_start <= line <= h.new_end:
            return True
    return False


# --- Triviality --------------------------------------------------------------

# Single-line comment marker per extension. Only languages REVA actually reviews
# need an entry; an unknown extension is treated as "not comment-only" (safe).
_COMMENT_MARKER: dict[str, str] = {".py": "#"}


def _body_lines(section: str, sign: str) -> list[str]:
    """Added (sign='+') or removed (sign='-') body lines of a diff section,
    excluding the +++/--- file headers and CRLF-safe."""
    header = sign * 3
    return [
        line[1:].rstrip("\r")
        for line in section.split("\n")
        if line.startswith(sign) and not line.startswith(header)
    ]


def _ws_normalized(lines: list[str]) -> list[str]:
    """Non-blank lines with all whitespace removed, IN ORDER. Two line lists that
    are equal under this normalization differ only in whitespace and/or blank
    lines — not in content or ordering. The comparison is positional on purpose:
    a reordering of identical lines (e.g. swapped statements) is substantive and
    must NOT be classified as a whitespace-only change."""
    return [re.sub(r"\s+", "", line) for line in lines if line.strip()]


def _nonblank_stripped(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip()]


def _all_imports(lines: list[str]) -> bool:
    stripped = _nonblank_stripped(lines)
    return bool(stripped) and all(
        s.startswith("import ") or s.startswith("from ") for s in stripped
    )


def _all_comments(lines: list[str], ext: str) -> bool:
    marker = _COMMENT_MARKER.get(ext)
    if marker is None:
        return False
    return all(not line.strip() or line.strip().startswith(marker) for line in lines)


def _section_is_trivial(added: list[str], removed: list[str], ext: str) -> bool:
    # whitespace/blank-line only: same content + same order, modulo whitespace
    if _ws_normalized(added) == _ws_normalized(removed):
        return True
    # pure import reordering (Python): same import lines, reordered
    if (
        ext == ".py"
        and _all_imports(added)
        and _all_imports(removed)
        and sorted(_nonblank_stripped(added)) == sorted(_nonblank_stripped(removed))
    ):
        return True
    # comment-only edit
    return _all_comments(added, ext) and _all_comments(removed, ext)


def is_trivial_diff(diff: str) -> bool:
    """Whether every changed file section is only whitespace, comment, or
    import-reordering changes — i.e. nothing worth a paid review.

    Conservative by construction: ANY real added/removed code line makes the
    whole diff non-trivial, and a deleted/renamed/binary file (no `+++ b/`
    header) is always treated as substantive. Used to short-circuit the review
    before calling Claude.
    """
    if not diff.strip():
        return False
    sections = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    seen = False
    for section in sections:
        if not section.strip():
            continue
        m = re.search(r"^\+\+\+ b/(.+)$", section, re.MULTILINE)
        if m is None:
            # Deleted file (+++ /dev/null), binary, or pure rename — substantive.
            return False
        ext = os.path.splitext(m.group(1).rstrip("\r"))[1].lower()
        added = _body_lines(section, "+")
        removed = _body_lines(section, "-")
        if not added and not removed:
            seen = True  # pure context / new empty file / mode change
            continue
        if not _section_is_trivial(added, removed, ext):
            return False
        seen = True
    return seen
