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
# NB: .xml is deliberately NOT excluded — Odoo view/QWeb XML under custom_addons/
# is reviewed by the reva-xml-review skill. Third-party odoo/enterprise XML is
# still dropped by exclude_prefixes, and translation/doc catalogs stay excluded.
DEFAULT_EXCLUDE_EXTENSIONS: frozenset[str] = frozenset({".po", ".pot", ".md", ".rst"})

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


# Odoo upgrade scripts: .../migrations/<version>/{pre,post,end}-migrate.py.
# Suffix-anchored (not on the addons prefix) so it matches custom_addons/ and
# custom-addons/ and, under diff-all, a migration outside custom_addons too.
MIGRATION_PATH_RE = re.compile(r"/migrations/[^/]+/(?:pre|post|end)-migrate\.py$")


def migration_paths(diff: str) -> list[str]:
    """Migration-script paths in `diff` (operates on the same filtered diff the
    reviewer sends, so third-party/skip_paths-stripped files never match)."""
    return [p for p in iter_diff_files(diff) if MIGRATION_PATH_RE.search(p)]


def xml_only_diff(diff: str) -> bool:
    """True iff the diff touches at least one file and every touched path is .xml.
    Routing predicate for the dedicated XML/QWeb review skill."""
    paths = list(iter_diff_files(diff))
    return bool(paths) and all(p.lower().endswith(".xml") for p in paths)


def _section_path(section: str) -> str | None:
    """Best-effort file path for a `diff --git` section, for scope filtering.

    Prefers the post-image `+++ b/<path>`, but falls back so deletions, binary
    files, and pure renames — which have no `+++ b/` line — are still filtered
    instead of being kept unconditionally (which let odoo//enterprise/ deletions
    and out-of-scope binaries slip through every filter):
      - deletion (`+++ /dev/null`): use the pre-image `--- a/<path>`;
      - binary / rename-only (no +++/--- hunks): use the new-side path from the
        `diff --git a/<old> b/<new>` header.
    Returns None only for a section with no recognizable header.
    """
    m = re.search(r"^\+\+\+ b/(.+)$", section, re.MULTILINE)
    if m:
        return m.group(1).rstrip("\r")
    m = re.search(r"^--- a/(.+)$", section, re.MULTILINE)
    if m:
        return m.group(1).rstrip("\r")
    m = re.search(r"^diff --git a/.+ b/(.+)$", section, re.MULTILINE)
    if m:
        return m.group(1).rstrip("\r")
    return None


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
        path = _section_path(section)
        if path is not None:
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
        path = _section_path(section)
        if path is not None and any(fnmatch.fnmatch(path, p) for p in patterns):
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


# --- Test-coverage signal ----------------------------------------------------

# Subdirs of an Odoo module that hold reviewable Python logic (Odoo layout).
LOGIC_SUBDIRS = ("models", "controllers", "wizard", "wizards", "report")


def module_root(path: str) -> str | None:
    """Return `custom_addons/<module>` (or the hyphen spelling) for a path under
    a reviewed addons prefix, else None."""
    for prefix in DEFAULT_REVIEW_PREFIXES:
        if path.startswith(prefix):
            module = path[len(prefix):].split("/", 1)[0]
            if module:
                return prefix + module
    return None


def is_in_tests_dir(path: str) -> bool:
    """True if `path` has a `tests` directory segment under its module root."""
    root = module_root(path)
    if root is None:
        return False
    dir_segments = path[len(root) + 1:].split("/")[:-1]
    return "tests" in dir_segments


def is_logic_path(path: str) -> bool:
    """True if `path` is a .py file under a module's logic subdir."""
    root = module_root(path)
    if root is None or not path.endswith(".py"):
        return False
    return path[len(root) + 1:].split("/")[0] in LOGIC_SUBDIRS


@dataclass(frozen=True)
class ModuleCoverage:
    """A module that adds new logic without an accompanying tests/ change."""

    module: str
    added_controller: bool  # added a new @route/@http.route (untested route is worse)


def analyze_test_coverage(diff: str) -> list[ModuleCoverage]:
    """Modules that ADD new logic (.py under models/controllers/wizard/report)
    with NO change under that module's tests/ dir. Conservative: any tests/ touch
    in the module clears it, and a pure deletion is not 'added logic'. Computed
    from the already-filtered diff, so it works on both the full and delta paths."""
    if not diff.strip():
        return []
    added_route: dict[str, bool] = {}   # module with added logic -> added a route
    has_test_change: set[str] = set()
    for section in re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE):
        if not section.strip():
            continue
        m = re.search(r"^\+\+\+ b/(.+)$", section, re.MULTILINE)
        if m is None:
            continue
        path = m.group(1).rstrip("\r")
        root = module_root(path)
        if root is None:
            continue
        if is_in_tests_dir(path):
            has_test_change.add(root)
        elif is_logic_path(path):
            added = _body_lines(section, "+")
            if not added:
                continue  # pure deletion / no added lines — not new logic
            is_controller = path[len(root) + 1:].split("/")[0] == "controllers"
            has_route = is_controller and any(
                "@route" in ln or "@http.route" in ln for ln in added
            )
            added_route[root] = added_route.get(root, False) or has_route
    return [
        ModuleCoverage(module=mod, added_controller=route)
        for mod, route in added_route.items()
        if mod not in has_test_change
    ]
