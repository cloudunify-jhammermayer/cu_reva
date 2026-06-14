"""Tests for the pure reva.odoo_manifest structural validator."""

from __future__ import annotations

from reva.odoo_manifest import audit_manifest, check_version_format, parse_manifest


# --- parse_manifest -----------------------------------------------------------


def test_parse_manifest_normal():
    text = (
        "{'name': 'M', 'version': '19.0.1.0.0', 'depends': ['base', 'sale'], "
        "'data': ['security/ir.model.access.csv', 'views/m_views.xml'], "
        "'demo': ['demo/d.xml']}"
    )
    m = parse_manifest(text)
    assert m is not None
    assert m.version == "19.0.1.0.0"
    assert m.depends == ["base", "sale"]
    assert m.data == ["security/ir.model.access.csv", "views/m_views.xml"]
    assert m.demo == ["demo/d.xml"]


def test_parse_manifest_with_comment_header():
    m = parse_manifest("# -*- coding: utf-8 -*-\n{'name': 'M', 'depends': ['base']}")
    assert m is not None
    assert m.depends == ["base"]
    assert m.version is None


def test_parse_manifest_non_literal_returns_none():
    # computed value (name reference) -> literal_eval raises -> None, no crash
    assert parse_manifest("{'version': SERIES + '.1.0', 'depends': ['base']}") is None


def test_parse_manifest_non_dict_returns_none():
    assert parse_manifest("['not', 'a', 'dict']") is None


def test_parse_manifest_tolerates_missing_keys_and_non_list_data():
    m = parse_manifest("{'name': 'M', 'data': 'security.csv'}")  # data not a list
    assert m is not None
    assert m.data == []
    assert m.demo == []
    assert m.version is None


# --- audit_manifest -----------------------------------------------------------


def test_audit_manifest_flags_missing_file():
    existing = {"mod/security/ir.model.access.csv"}
    issues = audit_manifest(
        "mod",
        ["security/ir.model.access.csv", "views/missing.xml"],
        [],
        lambda p: p in existing,
    )
    assert any(i.kind == "missing_file" and "views/missing.xml" in i.message for i in issues)


def test_audit_manifest_skips_glob_entry():
    issues = audit_manifest("mod", ["data/*.xml"], [], lambda p: False)
    assert all(i.kind != "missing_file" for i in issues)


def test_audit_manifest_flags_views_before_security():
    issues = audit_manifest(
        "mod", ["views/m_views.xml", "security/ir.model.access.csv"], [], lambda p: True
    )
    assert any(i.kind == "data_order" for i in issues)


def test_audit_manifest_no_order_issue_when_security_first():
    issues = audit_manifest(
        "mod", ["security/ir.model.access.csv", "views/m_views.xml"], [], lambda p: True
    )
    assert all(i.kind != "data_order" for i in issues)


def test_audit_manifest_file_exists_receives_joined_path():
    seen: list[str] = []
    audit_manifest("mod", ["data/x.xml"], [], lambda p: seen.append(p) or True)
    assert seen == ["mod/data/x.xml"]


def test_audit_manifest_no_module_dir_uses_bare_path():
    seen: list[str] = []
    audit_manifest("", ["data/x.xml"], [], lambda p: seen.append(p) or True)
    assert seen == ["data/x.xml"]


# --- check_version_format -----------------------------------------------------


def test_check_version_format_accepts_5_part():
    assert check_version_format("19.0.1.0.0") is None


def test_check_version_format_ignores_missing():
    assert check_version_format(None) is None
    assert check_version_format("") is None


def test_check_version_format_flags_short_version():
    issue = check_version_format("1.0")
    assert issue is not None and issue.kind == "version_format"
