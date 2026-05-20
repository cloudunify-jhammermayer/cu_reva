"""Tests for the diff_utils hunk parser and line lookup."""

from __future__ import annotations

from reva.diff_utils import (
    DiffHunk,
    count_diff_lines,
    estimate_diff_tokens,
    find_line_in_hunks,
    iter_diff_files,
    parse_diff_hunks,
)


SAMPLE_DIFF = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -10,3 +10,5 @@
 ctx
-old
+new1
+new2
+new3
diff --git a/bar.py b/bar.py
--- a/bar.py
+++ b/bar.py
@@ -1 +1 @@
-only_old
+only_new
"""


def test_parse_diff_hunks_two_files():
    hunks = parse_diff_hunks(SAMPLE_DIFF)
    assert hunks == [
        DiffHunk(file_path="foo.py", new_start=10, new_count=5),
        DiffHunk(file_path="bar.py", new_start=1, new_count=1),
    ]


def test_parse_diff_hunks_empty_diff():
    assert parse_diff_hunks("") == []


def test_parse_diff_hunks_default_count_is_one():
    """Hunk headers may omit `,count` — `@@ -1 +1 @@` means count=1."""
    hunks = parse_diff_hunks(SAMPLE_DIFF)
    assert hunks[1].new_count == 1
    assert hunks[1].new_end == 1


def test_find_line_in_hunks_within_range():
    hunks = parse_diff_hunks(SAMPLE_DIFF)
    assert find_line_in_hunks("foo.py", 10, hunks) is True
    assert find_line_in_hunks("foo.py", 14, hunks) is True
    assert find_line_in_hunks("foo.py", 15, hunks) is False  # one past end
    assert find_line_in_hunks("bar.py", 1, hunks) is True
    assert find_line_in_hunks("baz.py", 10, hunks) is False  # wrong file


def test_count_diff_lines_existing_behavior():
    # regression guard for the helper used by the size guard
    assert count_diff_lines(SAMPLE_DIFF) == 6  # 4 added + 2 removed


def test_estimate_diff_tokens_simple_ratio():
    assert estimate_diff_tokens(SAMPLE_DIFF) == len(SAMPLE_DIFF) // 4


def test_iter_diff_files_yields_both():
    assert list(iter_diff_files(SAMPLE_DIFF)) == ["foo.py", "bar.py"]
