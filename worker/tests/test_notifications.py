"""Tests for reva.notifications.post_to_chat (SECU-15 SSRF guard)."""

from __future__ import annotations

from reva.notifications import post_to_chat


def test_post_to_chat_blocks_metadata_url():
    # SSRF guard: the cloud-metadata endpoint must never be posted to.
    assert post_to_chat("http://169.254.169.254/latest/", "hi") is False


def test_post_to_chat_rejects_non_allowlisted_host():
    assert post_to_chat("https://evil.example.com/hook", "hi") is False
