"""Outbound URL guard for connectors that fetch URLs which may come from data or from an agent.

By default only public http(s) addresses are allowed; loopback, private, link-local (cloud
metadata such as 169.254.169.254), multicast and reserved addresses are refused, including after
redirects. Allow internal hosts with AGENTL_ALLOW_HOSTS (comma-separated host names or CIDRs) or
allow everything with AGENTL_ALLOW_PRIVATE_NETWORK=1."""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class BlockedURL(PermissionError):
    pass


def _allowed_by_env(host: str, ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address]) -> bool:
    if os.environ.get("AGENTL_ALLOW_PRIVATE_NETWORK", "").lower() in ("1", "true", "yes"):
        return True
    for item in filter(None, (x.strip() for x in os.environ.get("AGENTL_ALLOW_HOSTS", "").split(","))):
        if item.lower() == host.lower():
            return True
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if any(ip in net for ip in ips):
            return True
    return False


def check_url(url: str) -> None:
    """Raise BlockedURL unless `url` is http(s) to a public address (or explicitly allowed)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"only http and https URLs are allowed, got {parts.scheme or 'none'}: {url}")
    host = parts.hostname or ""
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise urllib.error.URLError(e) from e
    ips = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    bad = [ip for ip in ips if not ip.is_global or ip.is_multicast]
    if bad and not _allowed_by_env(host, ips):
        raise BlockedURL(
            f"refusing to fetch {url}: {host} resolves to non-public address {bad[0]}. "
            "Allow it with AGENTL_ALLOW_HOSTS=<host or CIDR> or AGENTL_ALLOW_PRIVATE_NETWORK=1."
        )


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_GuardedRedirects)


def urlopen(req: urllib.request.Request, timeout: float) -> Any:
    """urllib.request.urlopen with the guard applied to the URL and every redirect."""
    check_url(req.full_url)
    return _opener.open(req, timeout=timeout)
