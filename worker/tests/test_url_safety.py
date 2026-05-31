"""Tests for reva.url_safety.assert_safe_url."""

from __future__ import annotations

import pytest

from reva.url_safety import assert_safe_url


def test_allows_https_host_in_allowlist():
    assert_safe_url(
        "https://chat.googleapis.com/v1/spaces/AAA/messages?key=x",
        allowed_hosts={"chat.googleapis.com"},
    )  # no raise


def test_rejects_host_not_in_allowlist():
    with pytest.raises(ValueError, match="allowlist"):
        assert_safe_url("https://evil.example.com/hook", allowed_hosts={"chat.googleapis.com"})


def test_rejects_non_http_scheme():
    with pytest.raises(ValueError, match="scheme"):
        assert_safe_url("file:///etc/passwd")


def test_rejects_cloud_metadata_ip():
    with pytest.raises(ValueError, match="metadata|blocked"):
        assert_safe_url("http://169.254.169.254/latest/meta-data/")


def test_rejects_metadata_hostname():
    with pytest.raises(ValueError, match="blocked"):
        assert_safe_url("http://metadata.google.internal/computeMetadata/v1/")


def test_allows_internal_rfc1918_host_without_allowlist():
    # Odoo legitimately runs on an internal network — must not be blocked.
    assert_safe_url("https://10.0.0.5/odoo/write-field")  # no raise
    assert_safe_url("http://192.168.1.20:8069/reva/write-field")  # no raise
