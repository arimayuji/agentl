"""Outbound URL guard for connectors that fetch URLs which may come from data or from an agent.

By default only public http(s) addresses are allowed; loopback, private, link-local (cloud
metadata such as 169.254.169.254), multicast and reserved addresses are refused, including after
redirects. The connection goes to the address that was checked, so a host that answers DNS with
a public address for the check and a private one for the connection (DNS rebinding) is still
refused. Allow internal hosts with AGENTL_ALLOW_HOSTS (comma-separated host names or CIDRs) or
allow everything with AGENTL_ALLOW_PRIVATE_NETWORK=1."""

from __future__ import annotations

import http.client
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


def _resolve(host: str, port: int) -> list[Any]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise urllib.error.URLError(e) from e
    return infos


def _checked(host: str, infos: list[Any], url: str) -> None:
    ips = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    bad = [ip for ip in ips if not ip.is_global or ip.is_multicast]
    if bad and not _allowed_by_env(host, ips):
        raise BlockedURL(
            f"refusing to fetch {url}: {host} resolves to non-public address {bad[0]}. "
            "Allow it with AGENTL_ALLOW_HOSTS=<host or CIDR> or AGENTL_ALLOW_PRIVATE_NETWORK=1."
        )


def require_http(url: str, what: str) -> str:
    """For URLs from configuration (not data): only http and https, no file:// or others."""
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"{what} must be an http(s) URL, got {scheme or 'none'}: {url}")
    return url


def check_url(url: str) -> None:
    """Raise BlockedURL unless `url` is http(s) to a public address (or explicitly allowed)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"only http and https URLs are allowed, got {parts.scheme or 'none'}: {url}")
    host = parts.hostname or ""
    _checked(host, _resolve(host, parts.port or (443 if parts.scheme == "https" else 80)), url)


def _connect(conn: http.client.HTTPConnection, address: Any, timeout: Any, source: Any) -> socket.socket:
    """Resolve once, check those addresses, and connect to one of them (never resolving again)."""
    if getattr(conn, "_tunnel_host", None):  # an operator-configured proxy: the target was checked
        return socket.create_connection(address, timeout, source)
    host, port = address
    infos = _resolve(host, port)
    _checked(host, infos, f"{host}:{port}")
    last: OSError | None = None
    for family, socktype, proto, _, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                sock.settimeout(timeout)
            if source:
                sock.bind(source)
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            sock.close()
            last = e
    raise last or OSError(f"could not connect to {host}:{port}")


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._create_connection = lambda addr, timeout, source=None: _connect(self, addr, timeout, source)


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._create_connection = lambda addr, timeout, source=None: _connect(self, addr, timeout, source)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: Any) -> Any:
        if req.has_proxy():  # an operator-configured HTTP proxy: the target URL was checked
            return self.do_open(http.client.HTTPConnection, req)
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: Any) -> Any:
        return self.do_open(_HTTPSConnection, req, context=self._context)  # type: ignore[attr-defined]


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    extra: Any = None  # a further check on each redirect target, e.g. an allow list of hosts

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        check_url(newurl)
        if self.extra is not None:
            self.extra(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HTTPHandler, _HTTPSHandler, _GuardedRedirects)


def urlopen(req: urllib.request.Request, timeout: float, check: Any = None) -> Any:
    """urllib.request.urlopen with the guard applied to the URL, every redirect and the address
    actually connected to. `check(url)` runs on the URL and on every redirect target too."""
    check_url(req.full_url)
    opener = _opener
    if check is not None:
        check(req.full_url)
        redirects = type("_Redirects", (_GuardedRedirects,), {"extra": staticmethod(check)})
        opener = urllib.request.build_opener(_HTTPHandler, _HTTPSHandler, redirects)
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.URLError as e:
        if isinstance(e.reason, BlockedURL):  # refused at connect time: say so, not "URL error"
            raise e.reason from None
        raise
