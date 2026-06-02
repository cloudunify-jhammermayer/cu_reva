"""Webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(body: bytes, signature_header: str, secret: str) -> bool:
    """Verify X-Hub-Signature-256: sha256=<hex> using constant-time comparison."""
    # Backstop to the startup guard (SECU-2): an empty secret makes the HMAC
    # forgeable (anyone can compute hmac(b"", body)), so refuse outright.
    if not secret:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    received = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, received)
