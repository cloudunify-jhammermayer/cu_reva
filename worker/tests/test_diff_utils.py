"""Tests for the diff_utils hunk parser and line lookup."""

from __future__ import annotations

from reva.diff_utils import (
    DiffHunk,
    count_diff_lines,
    estimate_diff_tokens,
    extract_file_paths,
    filter_diff,
    filter_diff_by_paths,
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


TWO_FILE_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-old
+new
diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1 @@
-old
+new
"""


def test_filter_diff_by_paths_strips_matching():
    result = filter_diff_by_paths(TWO_FILE_DIFF, ["*.json"])
    assert "package-lock.json" not in result
    assert "src/app.py" in result


def test_filter_diff_by_paths_keeps_non_matching():
    result = filter_diff_by_paths(TWO_FILE_DIFF, ["*.lock"])
    assert "src/app.py" in result
    assert "package-lock.json" in result  # .json doesn't match *.lock


def test_filter_diff_by_paths_empty_patterns():
    result = filter_diff_by_paths(TWO_FILE_DIFF, [])
    assert result == TWO_FILE_DIFF


def test_filter_diff_by_paths_all_stripped():
    result = filter_diff_by_paths(TWO_FILE_DIFF, ["*.py", "*.json"])
    assert result.strip() == ""


def test_extract_file_paths_returns_set_of_touched_files():
    diff = (
        "diff --git a/custom_addons/a.py b/custom_addons/a.py\n"
        "+++ b/custom_addons/a.py\n"
        "+added\n"
        "diff --git a/custom_addons/b.py b/custom_addons/b.py\n"
        "+++ b/custom_addons/b.py\n"
        "+added\n"
    )
    assert extract_file_paths(diff) == {"custom_addons/a.py", "custom_addons/b.py"}


def test_extract_file_paths_empty_diff_returns_empty_set():
    assert extract_file_paths("") == set()


def test_filter_diff_reviews_both_custom_addons_spellings():
    """Both custom_addons/ and custom-addons/ are reviewable; other paths dropped."""
    diff = (
        "diff --git a/custom_addons/a.py b/custom_addons/a.py\n"
        "--- a/custom_addons/a.py\n+++ b/custom_addons/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/custom-addons/b.py b/custom-addons/b.py\n"
        "--- a/custom-addons/b.py\n+++ b/custom-addons/b.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/oca_modules/c.py b/oca_modules/c.py\n"
        "--- a/oca_modules/c.py\n+++ b/oca_modules/c.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    result = filter_diff(diff)
    assert "custom_addons/a.py" in result
    assert "custom-addons/b.py" in result
    assert "oca_modules/c.py" not in result


# --- edge cases (TEST-14) ----------------------------------------------------

def test_parse_diff_hunks_multiple_hunks_one_file():
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1,2 +1,3 @@\n+a\n"
        "@@ -20,1 +25,4 @@\n+b\n"
    )
    hunks = parse_diff_hunks(diff)
    assert len(hunks) == 2
    assert hunks[0].new_start == 1 and hunks[1].new_start == 25


def test_parse_diff_hunks_deletion_only_file_has_no_hunks():
    # A deleted file ends with `+++ /dev/null`, not `+++ b/...`, so there's
    # nothing to comment inline on.
    diff = "diff --git a/x.py b/x.py\ndeleted file mode 100644\n--- a/x.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-gone\n"
    assert parse_diff_hunks(diff) == []
    assert extract_file_paths(diff) == set()


def test_parse_diff_hunks_malformed_hunk_header_is_skipped():
    diff = "+++ b/x.py\n@@ this is not a valid hunk header @@\n+a\n"
    assert parse_diff_hunks(diff) == []  # no crash, no bogus hunk


def test_parse_diff_hunks_crlf_path_has_no_trailing_cr():
    diff = "diff --git a/x.py b/x.py\r\n--- a/x.py\r\n+++ b/x.py\r\n@@ -1,2 +1,3 @@\r\n+new\r\n"
    hunks = parse_diff_hunks(diff)
    assert hunks and hunks[0].file_path == "x.py"  # no trailing \r
    assert extract_file_paths(diff) == {"x.py"}


def test_parse_diff_hunks_rename_with_edits_maps_new_path():
    diff = (
        "diff --git a/old.py b/new.py\nsimilarity index 90%\n"
        "rename from old.py\nrename to new.py\n--- a/old.py\n+++ b/new.py\n@@ -1,1 +1,2 @@\n+x\n"
    )
    hunks = parse_diff_hunks(diff)
    assert [h.file_path for h in hunks] == ["new.py"]
