"""Tests for webhook signature verification (app.security)."""

from __future__ import annotations

import hashlib
import hmac

from app.security import verify_signature


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_valid():
    body = b'{"action":"opened"}'
    assert verify_signature(body, _sign(body, "topsecret"), "topsecret") is True


def test_verify_signature_rejects_wrong_secret():
    body = b'{"action":"opened"}'
    assert verify_signature(body, _sign(body, "attacker"), "topsecret") is False


def test_verify_signature_rejects_unprefixed_header():
    body = b'{"action":"opened"}'
    digest = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, digest, "topsecret") is False


def test_verify_signature_rejects_empty_secret():
    """SECU-2 backstop: with an empty secret an attacker can compute
    hmac(b'', body) without any knowledge; verification must refuse rather than
    accept the forged signature, even if the startup guard were bypassed."""
    body = b'{"action":"opened","pull_request":{"number":1}}'
    forged = _sign(body, "")
    assert verify_signature(body, forged, "") is False
