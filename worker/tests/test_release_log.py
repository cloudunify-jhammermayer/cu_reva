"""Tests for reva.release_log and the release-log types (spec 2026-09-04, R2/R4)."""

from __future__ import annotations

import pytest

from reva import config, release_log
from reva.types import ReleaseNoteJobParams, ReleaseRef, RepoConfig, TicketIssueJobParams


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Lollipop", "lollipop"),
        ("  lollipop  ", "lollipop"),
        ("Big Bang 2", "big-bang-2"),
        ("Tab\tand  double  space", "tab-and-double-space"),
        ("Ünïcode", "ünïcode"),
    ],
)
def test_release_slug(name, slug):
    assert release_log.release_slug(name) == slug


def test_release_log_path():
    assert release_log.release_log_path("lollipop") == "docs/releases/lollipop.html"


@pytest.mark.parametrize(
    ("slug", "safe"),
    [
        ("lollipop", True),
        ("big-bang-2", True),
        ("../x", False),
        ("a/b", False),
        ("a\\b", False),
        (".hidden", False),
        ("", False),
    ],
)
def test_is_safe_slug(slug, safe):
    assert release_log.is_safe_slug(slug) is safe


def test_release_log_path_rejects_unsafe_slug():
    with pytest.raises(ValueError):
        release_log.release_log_path("../x")


def test_docs_site_page_url_uses_configured_base(monkeypatch):
    monkeypatch.setattr(config, "DOCS_SITE_URL", "https://reva.example.com")
    assert (
        release_log.docs_site_page_url(7, "docs/releases/lollipop.html")
        == "https://reva.example.com/docs/?repo=7&path=docs/releases/lollipop.html"
    )


def test_docs_site_page_url_is_relative_when_unset(monkeypatch):
    monkeypatch.setattr(config, "DOCS_SITE_URL", "")
    assert (
        release_log.docs_site_page_url(7, "docs/releases/lollipop.html")
        == "/docs/?repo=7&path=docs/releases/lollipop.html"
    )


def test_theme_css_is_packaged():
    css = release_log.theme_css()
    assert ".rl-page" in css
    # dart-sass unquotes identifier-valued attribute selectors in its output.
    assert ".rl-ticket[data-status=umgesetzt]" in css
    assert "<style" not in css


def test_repo_config_odoo_instance_key():
    assert RepoConfig().odoo_instance is None
    assert RepoConfig.model_validate({"odoo_instance": "wenatex"}).odoo_instance == "wenatex"


def test_release_ref_rides_ticket_issue_params():
    params = TicketIssueJobParams(
        run_id=1, odoo_instance_id=1, ticket_id=42, model_name="project.task",
        github_url="https://github.com/acme/widgets", name="Login", description="",
        analysis_html="", priority="1", ticket_url="https://odoo.example.com/web#id=42",
        release={"id": 3275, "name": "Lollipop", "date": "2026-09-30 00:00:00"},
    )
    assert params.release == ReleaseRef(id=3275, name="Lollipop", date="2026-09-30 00:00:00")
    again = TicketIssueJobParams.model_validate(params.model_dump())
    assert again.release == params.release
    assert TicketIssueJobParams.model_validate({**params.model_dump(), "release": None}).release is None


def test_release_note_job_params_roundtrip():
    p = ReleaseNoteJobParams(
        note_id=1, odoo_instance_id=1, release_id=3275, release_name="Lollipop", slug="lollipop"
    )
    assert ReleaseNoteJobParams.model_validate(p.model_dump()) == p
    assert p.github_url is None


def test_release_note_job_params_roundtrip_with_github_url():
    p = ReleaseNoteJobParams(
        note_id=1, odoo_instance_id=1, release_id=3275, release_name="Lollipop", slug="lollipop",
        github_url="https://github.com/acme/widgets",
    )
    assert ReleaseNoteJobParams.model_validate(p.model_dump()) == p
    assert p.github_url == "https://github.com/acme/widgets"
