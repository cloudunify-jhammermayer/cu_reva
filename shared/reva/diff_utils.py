"""Helpers for inspecting unified diffs."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

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
            yield line[len("+++ b/") :]


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
            current_file = line[len("+++ b/") :]
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
