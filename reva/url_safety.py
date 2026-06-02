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
# Cloud metadata IPs (the prime SSRF target). 169.254.169.254 is already
# link-local, but list explicitly so an IPv6 form is covered too.
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})


def _literal_ip(host: str) -> ipaddress._BaseAddress | None:
    """Return the IP for `host` if it is *any* IP-literal form, else None.

    Covers obfuscated forms used to dodge SSRF filters (SECU-20): decimal/hex/
    octal integer literals (e.g. 2852039166, 0xA9FEA9FE for 169.254.169.254) and
    IPv4-mapped IPv6 (::ffff:169.254.169.254)."""
    candidates: list[str] = [host]
    try:
        # int(_, 0) auto-detects 0x (hex) / 0o (octal) / decimal integer hosts.
        candidates.append(str(ipaddress.ip_address(int(host, 0))))
    except (ValueError, OverflowError):
        pass
    for c in candidates:
        try:
            ip = ipaddress.ip_address(c)
        except ValueError:
            continue
        return ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip
    return None


def _is_blocked_ip(host: str) -> bool:
    ip = _literal_ip(host)
    if ip is None:
        return False  # a real hostname — fine (DNS-based SSRF is out of scope here)
    return ip.is_link_local or str(ip) in _METADATA_IPS


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
    if host in _BLOCKED_HOSTS or _is_blocked_ip(host):
        raise ValueError(f"URL host is blocked (metadata/link-local): {host}")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise ValueError(f"URL host {host!r} is not in the allowlist")
