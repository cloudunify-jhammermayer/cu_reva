"""Tests for the diff_utils hunk parser and line lookup."""

from __future__ import annotations

from reva.diff_utils import (
    DiffHunk,
    analyze_test_coverage,
    count_diff_lines,
    estimate_diff_tokens,
    extract_file_paths,
    filter_diff,
    filter_diff_by_paths,
    find_line_in_hunks,
    is_excluded_path,
    is_in_tests_dir,
    is_logic_path,
    is_trivial_diff,
    iter_diff_files,
    migration_paths,
    module_root,
    parse_diff_hunks,
    xml_only_diff,
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


def test_nontrivial_statement_reorder():
    # Swapping two statements is a permutation of identical lines — behavior can
    # change, so it must NOT be classified as a whitespace-only (trivial) change.
    assert is_trivial_diff(
        _pydiff("-self.x = compute_a()\n-self.y = compute_b()\n+self.y = compute_b()\n+self.x = compute_a()\n")
    ) is False


def test_nontrivial_reorder_with_reindent():
    # Reorder + reindent together is still substantive (the reorder dominates).
    assert is_trivial_diff(
        _pydiff("-a()\n-b()\n+    b()\n+    a()\n")
    ) is False


# --- test-coverage signal ----------------------------------------------------


def _file_diff(path: str, body: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"


def test_module_root_resolves_both_spellings():
    assert module_root("custom_addons/sale_ext/models/x.py") == "custom_addons/sale_ext"
    assert module_root("custom-addons/sale_ext/models/x.py") == "custom-addons/sale_ext"
    assert module_root("scripts/deploy.py") is None
    assert module_root("odoo/addons/base/x.py") is None


def test_is_in_tests_dir():
    assert is_in_tests_dir("custom_addons/m/tests/test_x.py") is True
    assert is_in_tests_dir("custom_addons/m/models/sub/tests/test_y.py") is True
    assert is_in_tests_dir("custom_addons/m/models/tests_helper.py") is False  # no tests segment


def test_is_logic_path():
    assert is_logic_path("custom_addons/m/models/account/move.py") is True
    assert is_logic_path("custom_addons/m/controllers/main.py") is True
    assert is_logic_path("custom_addons/m/views/v.xml") is False
    assert is_logic_path("custom_addons/m/data/d.csv") is False


def test_coverage_flags_new_logic_without_tests():
    diff = _file_diff("custom_addons/m/models/x.py", "+def f():\n+    return 1\n")
    cov = analyze_test_coverage(diff)
    assert [c.module for c in cov] == ["custom_addons/m"]
    assert cov[0].added_controller is False


def test_coverage_cleared_by_a_tests_change():
    diff = (
        _file_diff("custom_addons/m/models/x.py", "+def f():\n+    return 1\n")
        + _file_diff("custom_addons/m/tests/test_x.py", "+def test_f():\n+    assert True\n")
    )
    assert analyze_test_coverage(diff) == []


def test_coverage_detects_new_controller_route():
    diff = _file_diff("custom_addons/m/controllers/main.py",
                      "+    @http.route('/x', auth='user')\n+    def x(self):\n+        return 1\n")
    cov = analyze_test_coverage(diff)
    assert cov[0].added_controller is True


def test_coverage_ignores_pure_deletion():
    diff = (
        "diff --git a/custom_addons/m/models/x.py b/custom_addons/m/models/x.py\n"
        "deleted file mode 100644\n--- a/custom_addons/m/models/x.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-def f():\n-    return 1\n"
    )
    assert analyze_test_coverage(diff) == []


def test_coverage_multi_module_only_flags_uncovered():
    diff = (
        _file_diff("custom_addons/a/models/x.py", "+def f():\n+    return 1\n")
        + _file_diff("custom_addons/b/models/y.py", "+def g():\n+    return 2\n")
        + _file_diff("custom_addons/b/tests/test_y.py", "+def test_g():\n+    pass\n")
    )
    assert {c.module for c in analyze_test_coverage(diff)} == {"custom_addons/a"}


def test_coverage_empty_and_outside_paths():
    assert analyze_test_coverage("") == []
    assert analyze_test_coverage(_file_diff("scripts/deploy.py", "+x = 1\n")) == []
    # view-only change (no logic .py) -> no flag
    assert analyze_test_coverage(_file_diff("custom_addons/m/views/v.xml", "+<x/>\n")) == []


# --- migration_paths (feature 7) ---------------------------------------------


def test_migration_paths_matches_pre_post_end():
    for fname in ("pre-migrate.py", "post-migrate.py", "end-migrate.py"):
        path = f"custom_addons/foo/migrations/18.0.1.0/{fname}"
        assert migration_paths(_file_diff(path, "+x = 1\n")) == [path]


def test_migration_paths_accepts_hyphen_addons_spelling():
    path = "custom-addons/foo/migrations/17.0.2.0/pre-migrate.py"
    assert migration_paths(_file_diff(path, "+x = 1\n")) == [path]


def test_migration_paths_ignores_non_migrate_file_in_migrations_dir():
    path = "custom_addons/foo/migrations/18.0.1.0/helpers.py"
    assert migration_paths(_file_diff(path, "+x = 1\n")) == []


def test_migration_paths_ignores_migrate_outside_migrations_dir():
    path = "custom_addons/foo/migrate.py"
    assert migration_paths(_file_diff(path, "+x = 1\n")) == []


def test_migration_paths_is_crlf_safe():
    path = "custom_addons/foo/migrations/18.0.1.0/post-migrate.py"
    diff = _file_diff(path, "+x = 1\n").replace("\n", "\r\n")
    assert migration_paths(diff) == [path]


def test_migration_paths_empty_diff():
    assert migration_paths("") == []


# --- XML review routing (feature 8) ------------------------------------------


def test_filter_diff_keeps_custom_addons_xml():
    diff = _file_diff("custom_addons/m/views/partner_views.xml", "+<field name='x'/>\n")
    assert "partner_views.xml" in filter_diff(diff)


def test_filter_diff_still_strips_po_pot_md_rst():
    for path in ("custom_addons/m/i18n/de.po", "custom_addons/m/i18n/m.pot",
                 "custom_addons/m/README.md", "custom_addons/m/doc/index.rst"):
        assert filter_diff(_file_diff(path, "+x\n")).strip() == "", path


def test_filter_diff_still_strips_thirdparty_xml_by_prefix():
    diff = (
        _file_diff("odoo/addons/base/views/base.xml", "+<x/>\n")
        + _file_diff("enterprise/web/views/w.xml", "+<x/>\n")
    )
    assert filter_diff(diff).strip() == ""  # XML no longer in ext set must not leak third-party


def test_xml_only_diff_true_for_pure_xml():
    diff = (
        _file_diff("custom_addons/m/views/a.xml", "+<x/>\n")
        + _file_diff("custom_addons/m/views/b.xml", "+<y/>\n")
    )
    assert xml_only_diff(diff) is True


def test_xml_only_diff_false_for_mixed_and_code_and_empty():
    mixed = (
        _file_diff("custom_addons/m/views/a.xml", "+<x/>\n")
        + _file_diff("custom_addons/m/models/x.py", "+x = 1\n")
    )
    assert xml_only_diff(mixed) is False
    assert xml_only_diff(_file_diff("custom_addons/m/models/x.py", "+x = 1\n")) is False
    assert xml_only_diff("") is False
