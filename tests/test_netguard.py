import http.server
import threading
import urllib.request

import pytest

from agentl import netguard
from agentl.registry import get


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.delenv("AGENTL_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("AGENTL_ALLOW_HOSTS", raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://localhost:8080/x",
        "http://10.0.0.5/x",
        "http://192.168.1.10/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x",
        "http://[fd00::1]/x",
        "http://0.0.0.0/x",
    ],
)
def test_blocks_non_public_addresses(guarded, url):
    with pytest.raises(netguard.BlockedURL, match="AGENTL_ALLOW_HOSTS"):
        netguard.check_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x"])
def test_blocks_other_schemes(guarded, url):
    with pytest.raises(netguard.BlockedURL, match="only http"):
        netguard.check_url(url)


def test_public_address_is_allowed(guarded, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))])
    netguard.check_url("https://example.com/data.csv")


def test_dns_name_resolving_to_private_ip_is_blocked(guarded, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 80))])
    with pytest.raises(netguard.BlockedURL, match=r"10\.1\.2\.3"):
        netguard.check_url("http://innocent.example.com/")


def test_allowlist_by_host_and_cidr(guarded, monkeypatch):
    monkeypatch.setenv("AGENTL_ALLOW_HOSTS", "localhost, 10.0.0.0/8")
    netguard.check_url("http://localhost/x")
    netguard.check_url("http://10.2.3.4/x")
    with pytest.raises(netguard.BlockedURL):
        netguard.check_url("http://192.168.0.1/x")


@pytest.fixture
def redirector():
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_redirect_to_metadata_is_blocked(guarded, monkeypatch, redirector):
    monkeypatch.setenv("AGENTL_ALLOW_HOSTS", "127.0.0.1")  # the test server itself
    with pytest.raises(netguard.BlockedURL, match=r"169\.254\.169\.254"):
        netguard.urlopen(urllib.request.Request(redirector), timeout=5)


def test_connectors_use_the_guard(guarded):
    with pytest.raises(netguard.BlockedURL):
        get("http.get")(None, url="http://169.254.169.254/latest/meta-data/")
    with pytest.raises(netguard.BlockedURL):
        get("csv.read")(None, url="http://127.0.0.1/data.csv")
    with pytest.raises(netguard.BlockedURL):
        get("http.get")([{"host": "10.0.0.1"}], url="http://{host}/x")


def test_dns_rebinding_is_refused_at_connect_time(guarded, monkeypatch):
    """The name resolves to a public address for the check, then to the metadata address: the
    connection must use what was checked, so the second answer is refused, not connected to."""
    answers = iter(["93.184.216.34", "169.254.169.254"])
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (next(answers), 80))])
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    with pytest.raises(netguard.BlockedURL, match=r"169\.254\.169\.254"):
        netguard.urlopen(urllib.request.Request("http://rebind.example.com/"), timeout=5)


def test_pinned_connection_still_fetches(guarded, monkeypatch, redirector):
    monkeypatch.setenv("AGENTL_ALLOW_HOSTS", "127.0.0.1")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    with pytest.raises(netguard.BlockedURL):  # the redirect target is still refused
        netguard.urlopen(urllib.request.Request(redirector + "/x"), timeout=5)
