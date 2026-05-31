"""Outbound-URL validation for operator-configured webhook/callback targets.

REVA POSTs to a couple of URLs that come from configuration (the Google Chat
webhook, the Odoo callback). This guards against a mistyped or tampered value
being used as an SSRF vector — most importantly the cloud-metadata endpoint.

Note: private/RFC1918 hosts are intentionally NOT blocked, because the Odoo
callback legitimately lives on an internal network. Only link-local /
metadata addresses and non-HTTP schemes are rejected, plus an optional exact
host allowlist for targets whose host is known and fixed (Google Chat).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

# Cloud-metadata / well-known SSRF sinks, blocked regardless of allowlist.
_BLOCKED_HOSTS = frozenset({"metadata.google.internal", "metadata"})


def _is_link_local_ip(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_link_local
    except ValueError:
        return False  # not an IP literal — a hostname, which is fine


def assert_safe_url(url: str, *, allowed_hosts: frozenset[str] | set[str] | None = None) -> None:
    """Raise ValueError if `url` is unsafe to POST to.

    - scheme must be http or https
    - host must be present and not a cloud-metadata / link-local address
    - if `allowed_hosts` is given, host must be one of them (exact match)
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("URL has no host")
    if host in _BLOCKED_HOSTS or _is_link_local_ip(host):
        raise ValueError(f"URL host is blocked (metadata/link-local): {host}")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise ValueError(f"URL host {host!r} is not in the allowlist")
