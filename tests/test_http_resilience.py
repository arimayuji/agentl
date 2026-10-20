"""http.get / http.post resilience (#212): Retry-After, shared request budgets, mirrors, TLS."""

import datetime
import http.server
import json
import ssl
import threading
import time
import urllib.error

import pytest

from agentl import lint
from agentl.connectors import http as http_mod
from agentl.registry import get
from agentl.runner import Runner
from agentl.spec import PipelineSpec
from agentl.store import open_store


def _serve(handler, context=None):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if context is not None:
        srv.socket = context.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.fixture
def api():
    """A JSON API whose answers are queued per path: (status, headers) or a body list."""
    state: dict = {"hits": [], "plan": {}}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self):
            state["hits"].append((self.command, self.path, time.monotonic()))
            queue = state["plan"].get(self.path.split("?")[0], [])
            status, headers = queue.pop(0) if queue else (200, {})
            data = json.dumps([{"path": self.path}]).encode() if status == 200 else b"{}"
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._answer()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._answer()

    srv, port = _serve(H)
    yield f"http://127.0.0.1:{port}", state
    srv.shutdown()
    srv.server_close()


def test_retry_after_seconds_is_waited_instead_of_backoff(api, monkeypatch):
    url, state = api
    state["plan"]["/a"] = [(429, {"Retry-After": "0"}), (503, {"Retry-After": "0"})]
    sleeps: list[float] = []
    monkeypatch.setattr(http_mod.time, "sleep", sleeps.append)
    rows = get("http.get")(None, url=f"{url}/a", retries=2)
    assert rows == [{"path": "/a"}]
    assert sleeps == [0.0, 0.0]  # without the header these would be 1 and 2 seconds


def test_retry_after_http_date_and_cap(api, monkeypatch):
    url, state = api
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
    state["plan"]["/b"] = [(429, {"Retry-After": later.strftime("%a, %d %b %Y %H:%M:%S GMT")})]
    sleeps: list[float] = []
    monkeypatch.setattr(http_mod.time, "sleep", sleeps.append)
    get("http.post")(None, url=f"{url}/b", json={"x": 1}, retries=1)
    assert sleeps == [60.0]  # capped


def test_retry_after_ignore_keeps_backoff(api, monkeypatch):
    url, state = api
    state["plan"]["/c"] = [(429, {"Retry-After": "30"})]
    sleeps: list[float] = []
    monkeypatch.setattr(http_mod.time, "sleep", sleeps.append)
    get("http.get")(None, url=f"{url}/c", retries=1, retry_after="ignore")
    assert sleeps == [1.0]


def test_store_budget_is_shared_across_store_handles(tmp_path):
    with open_store(tmp_path / "s.db") as a, open_store(tmp_path / "s.db") as b:
        assert a.budget_take("api.example.org", 2, 3600) == 0
        assert b.budget_take("api.example.org", 2, 3600) == 0
        wait = a.budget_take("api.example.org", 2, 3600)
        assert 0 < wait <= 3600
        assert b.budget_take("other.example.org", 2, 3600) == 0


def test_store_budget_counts_once_per_request_under_threads(tmp_path):
    with open_store(tmp_path / "s.db") as store:
        got: list[float] = []
        threads = [
            threading.Thread(target=lambda: got.append(store.budget_take("h", 5, 3600))) for _ in range(12)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(1 for w in got if w == 0) == 5


def _spec(url: str, **with_) -> PipelineSpec:
    return PipelineSpec.model_validate(
        {"name": "budget", "steps": [{"id": "fetch", "use": "http.get", "with": {"url": url, **with_}}]}
    )


def test_budget_is_shared_between_runs_and_pauses(api, tmp_path):
    url, state = api
    limit = {"per_hour": 2, "max_wait": 0}
    with open_store(tmp_path / "s.db") as store:
        first = Runner(_spec(f"{url}/r", rate_limit=limit), store=store, use_cache=False).run()
        second = Runner(_spec(f"{url}/r", rate_limit=limit), store=store, use_cache=False).run()
        third = Runner(_spec(f"{url}/r", rate_limit=limit), store=store, use_cache=False).run()
    assert first.status == "succeeded" and second.status == "succeeded"
    assert third.status == "failed"  # max_wait 0: the hourly budget of 127.0.0.1 is spent
    assert len(state["hits"]) == 2


def test_budget_pauses_until_the_next_window(api, monkeypatch):
    url, _ = api
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(http_mod.time, "time", lambda: clock["now"])

    def sleep(s: float) -> None:
        clock["now"] += s

    monkeypatch.setattr(http_mod.time, "sleep", sleep)
    rows = get("http.get")(
        [{"i": i} for i in range(3)],
        url=f"{url}/p?i={{i}}",
        rate_limit={"per_minute": 2, "key": "paused-test"},
        concurrency=1,
    )
    assert len(rows) == 3
    assert clock["now"] >= 1_000_000.0 + 20  # the third request waited for the next minute


def test_rate_limit_rejects_bad_config():
    with pytest.raises(ValueError, match="rate_limit"):
        get("http.get")(None, url="http://127.0.0.1:1/", rate_limit={"per_hour": 1, "per_minute": 1})
    with pytest.raises(ValueError, match="rate_limit"):
        get("http.get")(None, url="http://127.0.0.1:1/", rate_limit={"every": 3})


def test_mirror_is_tried_after_the_primary_keeps_failing(api, monkeypatch):
    url, _ = api
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: None)
    dead = "http://127.0.0.1:1"  # nothing listens there
    rows = get("http.get")(None, url=f"{dead}/v1/items?x=1", mirrors=[f"{url}/mirror"], retries=1)
    assert rows == [{"path": "/mirror/v1/items?x=1"}]


def test_mirror_after_5xx_but_not_after_4xx(api, monkeypatch):
    url, state = api
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: None)
    state["plan"]["/m"] = [(500, {}), (500, {})]
    rows = get("http.get")(None, url=f"{url}/m", mirrors=[f"{url}/alt"], retries=1)
    assert rows == [{"path": "/alt/m"}]
    state["plan"]["/n"] = [(404, {})]
    with pytest.raises(urllib.error.HTTPError):
        get("http.get")(None, url=f"{url}/n", mirrors=[f"{url}/alt"], retries=1)
    assert not any(p == "/alt/n" for _, p, _ in state["hits"])


def test_mirrors_must_be_http_urls():
    with pytest.raises(ValueError, match="mirrors"):
        get("http.get")(None, url="http://127.0.0.1:1/", mirrors=["ftp://x"])


@pytest.fixture
def tls_site(tmp_path):
    pytest.importorskip("cryptography")
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    san = x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(san, False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    pem = tmp_path / "corp.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile = tmp_path / "key.pem"
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(pem, keyfile)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            data = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv, port = _serve(H, ctx)
    yield f"https://127.0.0.1:{port}/", pem
    srv.shutdown()
    srv.server_close()


def test_tls_ca_bundle_trusts_a_private_ca(tls_site):
    url, pem = tls_site
    with pytest.raises(urllib.error.URLError, match="CERTIFICATE_VERIFY_FAILED"):
        get("http.get")(None, url=url, retries=0)
    assert get("http.get")(None, url=url, tls={"ca_bundle": str(pem)}) == [{"ok": True}]


def test_tls_verify_false_works_and_warns(tls_site, caplog):
    url, _ = tls_site
    with caplog.at_level("WARNING", logger="agentl.http"):
        assert get("http.get")(None, url=url, tls={"verify": False}) == [{"ok": True}]
    assert "NOT checked" in caplog.text


def test_tls_rejects_bad_config(tmp_path):
    with pytest.raises(FileNotFoundError):
        get("http.get")(None, url="https://127.0.0.1:1/", tls={"ca_bundle": str(tmp_path / "none.pem")})
    with pytest.raises(ValueError, match="not both"):
        get("http.get")(None, url="https://127.0.0.1:1/", tls={"verify": False, "ca_bundle": "x.pem"})
    with pytest.raises(ValueError, match="unknown key"):
        get("http.get")(None, url="https://127.0.0.1:1/", tls={"insecure": True})


def test_lint_warns_on_tls_verify_false():
    text = """
name: p
steps:
  - id: fetch
    use: http.get
    with:
      url: https://example.org/data
      tls: { verify: false }
"""
    report = lint.check_text(text)
    assert "AL013" in [f.code for f in report.warnings]
