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
    is_excluded_path,
    is_trivial_diff,
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


def test_filter_diff_drops_generated_noise_under_prefix():
    """Lockfiles / minified assets are dropped even when under a reviewed prefix."""
    diff = (
        "diff --git a/custom_addons/m/models/x.py b/custom_addons/m/models/x.py\n"
        "--- a/custom_addons/m/models/x.py\n+++ b/custom_addons/m/models/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/custom_addons/m/poetry.lock b/custom_addons/m/poetry.lock\n"
        "--- a/custom_addons/m/poetry.lock\n+++ b/custom_addons/m/poetry.lock\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/custom_addons/m/static/app.min.js b/custom_addons/m/static/app.min.js\n"
        "--- a/custom_addons/m/static/app.min.js\n+++ b/custom_addons/m/static/app.min.js\n@@ -1 +1 @@\n-x\n+y\n"
    )
    out = filter_diff(diff)
    assert "models/x.py" in out
    assert "poetry.lock" not in out
    assert "app.min.js" not in out


def test_filter_diff_all_paths_still_drops_lockfiles():
    """diff-all mode (no prefix) still strips universally-generated files."""
    diff = (
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n+++ b/package-lock.json\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/src/app.js b/src/app.js\n"
        "--- a/src/app.js\n+++ b/src/app.js\n@@ -1 +1 @@\n-a\n+b\n"
    )
    out = filter_diff(diff, include_prefixes=())
    assert "package-lock.json" not in out
    assert "src/app.js" in out


def test_filter_diff_drops_thirdparty_odoo_and_enterprise():
    """odoo/ and enterprise/ are third-party — dropped even in diff-all mode."""
    diff = (
        "diff --git a/odoo/addons/base/models/res_partner.py b/odoo/addons/base/models/res_partner.py\n"
        "--- a/odoo/addons/base/models/res_partner.py\n+++ b/odoo/addons/base/models/res_partner.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/enterprise/account_accountant/models/x.py b/enterprise/account_accountant/models/x.py\n"
        "--- a/enterprise/account_accountant/models/x.py\n+++ b/enterprise/account_accountant/models/x.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/custom_addons/mod/models/x.py b/custom_addons/mod/models/x.py\n"
        "--- a/custom_addons/mod/models/x.py\n+++ b/custom_addons/mod/models/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    # diff-all (no include restriction) must still drop the third-party trees.
    out = filter_diff(diff, include_prefixes=())
    assert "odoo/addons" not in out
    assert "enterprise/" not in out
    assert "custom_addons/mod/models/x.py" in out


def test_is_excluded_path():
    assert is_excluded_path("odoo/addons/base/models/res_partner.py")
    assert is_excluded_path("enterprise/account/models/x.py")
    assert not is_excluded_path("custom_addons/mod/models/x.py")
    # A custom module merely named with an odoo* prefix is NOT excluded.
    assert not is_excluded_path("custom_addons/odoo_helper/x.py")


# --- is_trivial_diff ---------------------------------------------------------


def _pydiff(body: str, path: str = "custom_addons/m/a.py") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,3 +1,3 @@\n"
        f"{body}"
    )


def test_trivial_whitespace_reindent():
    assert is_trivial_diff(_pydiff(" def f():\n-    return 1\n+\treturn 1\n")) is True


def test_trivial_trailing_whitespace():
    assert is_trivial_diff(_pydiff("-x = 1\n+x = 1 \n")) is True


def test_trivial_blank_line_added():
    assert is_trivial_diff(_pydiff(" x = 1\n+\n")) is True


def test_trivial_comment_only_python():
    assert is_trivial_diff(_pydiff("-# old comment\n+# new comment\n")) is True


def test_trivial_import_reorder():
    assert is_trivial_diff(
        _pydiff("-import os\n-import sys\n+import sys\n+import os\n")
    ) is True


def test_nontrivial_import_added():
    assert is_trivial_diff(_pydiff("+import json\n")) is False


def test_nontrivial_comment_plus_real_line():
    assert is_trivial_diff(_pydiff("+# a comment\n+x = compute()\n")) is False


def test_nontrivial_add_line_and_reindent():
    assert is_trivial_diff(
        _pydiff(" def f():\n-  return 1\n+    return 1\n+    extra()\n")
    ) is False


def test_nontrivial_real_code_change():
    assert is_trivial_diff(_pydiff("-    return 1\n+    return 2\n")) is False


def test_nontrivial_deleted_file():
    diff = (
        "diff --git a/custom_addons/m/a.py b/custom_addons/m/a.py\n"
        "deleted file mode 100644\n"
        "--- a/custom_addons/m/a.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-x = 1\n"
        "-y = 2\n"
    )
    assert is_trivial_diff(diff) is False


def test_nontrivial_new_file_with_content():
    diff = (
        "diff --git a/custom_addons/m/new.py b/custom_addons/m/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/custom_addons/m/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+import os\n"
        "+print(os.getcwd())\n"
    )
    assert is_trivial_diff(diff) is False


def test_nontrivial_mixed_whitespace_and_real_change():
    diff = (
        _pydiff("-x = 1\n+x = 1 \n", path="custom_addons/m/a.py")
        + _pydiff("-y = 1\n+y = 2\n", path="custom_addons/m/b.py")
    )
    assert is_trivial_diff(diff) is False


def test_trivial_two_files_both_whitespace_only():
    diff = (
        _pydiff("-x = 1\n+x = 1 \n", path="custom_addons/m/a.py")
        + _pydiff("- y = 2\n+y = 2\n", path="custom_addons/m/b.py")
    )
    assert is_trivial_diff(diff) is True


def test_trivial_crlf_whitespace_only():
    diff = (
        "diff --git a/custom_addons/m/a.py b/custom_addons/m/a.py\r\n"
        "--- a/custom_addons/m/a.py\r\n"
        "+++ b/custom_addons/m/a.py\r\n"
        "@@ -1 +1 @@\r\n"
        "-x=1\r\n"
        "+ x=1\r\n"
    )
    assert is_trivial_diff(diff) is True


def test_empty_diff_is_not_trivial():
    assert is_trivial_diff("") is False
