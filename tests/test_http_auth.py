"""http.get auth (#212): form login sessions, expired_if, and a token cache under a lock."""

import http.server
import json
import threading
import time
from urllib.parse import parse_qs

import pytest

from agentl.connectors import http as http_mod
from agentl.registry import get
from agentl.store import open_store


@pytest.fixture(autouse=True)
def _fresh_tokens():
    http_mod._TOKENS.clear()
    yield
    http_mod._TOKENS.clear()


@pytest.fixture
def site():
    """A site with a form login (302 + Set-Cookie), a token login and pages that answer
    'Please sign in' (200) or 401 when the session is unknown."""
    state: dict = {"logins": 0, "sessions": set(), "forms": [], "hits": 0}
    lock = threading.Lock()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body, headers=None):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _new_session(self):
            time.sleep(0.05)  # a slow login widens the race the lock must close
            with lock:
                state["logins"] += 1
                sid = f"s{state['logins']}"
                state["sessions"].add(sid)
            return sid

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            if self.path == "/login":
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                state["forms"].append(form)
                if form != {"user": "ann", "pass": "pw"}:
                    return self._send(403, {"error": "bad credentials"})
                sid = self._new_session()
                return self._send(302, "", {"Set-Cookie": f"sid={sid}; Path=/", "Location": "/home"})
            if self.path == "/token":
                return self._send(200, {"access_token": self._new_session(), "expires_in": 3600})
            self._send(404, {})

        def do_GET(self):
            state["hits"] += 1
            if self.path == "/home":
                return self._send(200, "<h1>Welcome</h1>")
            cookie = self.headers.get("Cookie", "")
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            sid = cookie.removeprefix("sid=") if cookie else bearer
            ok = sid in state["sessions"]
            if self.path.startswith("/api"):
                return self._send(200, [{"sid": sid}]) if ok else self._send(401, {"error": "unauthorized"})
            if not ok:
                return self._send(200, "<form>Please sign in</form>")
            return self._send(200, [{"sid": sid, "path": self.path}])

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()
    srv.server_close()


def _form(url):
    return {
        "login": {"url": f"{url}/login", "form": {"user": "ann", "pass": "pw"}},
        "expired_if": "Please sign in",
    }


def test_form_login_keeps_the_cookie_across_the_redirect(site):
    url, state = site
    rows = get("http.get")(None, url=f"{url}/report", auth=_form(url))
    assert rows == [{"sid": "s1", "path": "/report"}]
    assert state["forms"] == [{"user": "ann", "pass": "pw"}]


def test_expired_session_logs_in_once_and_retries(site):
    url, state = site
    get("http.get")(None, url=f"{url}/a", auth=_form(url))
    state["sessions"].clear()  # the server drops every session
    rows = get("http.get")(None, url=f"{url}/b", auth=_form(url))
    assert rows == [{"sid": "s2", "path": "/b"}] and state["logins"] == 2


def test_session_that_stays_expired_fails_after_one_new_login(site):
    url, state = site
    auth = _form(url)
    auth["login"]["url"] = f"{url}/token"  # a token the pages ignore: a cookie never comes
    auth["login"]["token_key"] = "access_token"
    auth["login"]["header"] = "X-Ignored"
    with pytest.raises(http_mod.SessionExpired, match="right after a new login"):
        get("http.get")(None, url=f"{url}/c", auth=auth)
    assert state["logins"] == 2


def test_bad_credentials_raise(site):
    url, _ = site
    auth = _form(url)
    auth["login"]["form"] = {"user": "ann", "pass": "wrong"}
    with pytest.raises(Exception, match="403"):
        get("http.get")(None, url=f"{url}/a", auth=auth)


def test_token_login_on_401_and_concurrent_requests_log_in_once(site):
    url, state = site
    auth = {"login": {"url": f"{url}/token", "json": {"client": "c"}, "token_key": "access_token"}}
    ids = [{"i": i} for i in range(12)]
    rows = get("http.get")(ids, url=f"{url}/api/{{i}}", concurrency=6, auth=auth)
    assert {r["sid"] for r in rows} == {"s1"} and state["logins"] == 1
    state["sessions"].clear()  # every worker now gets a 401 with the same stale token
    rows = get("http.get")(ids, url=f"{url}/api/{{i}}", concurrency=6, auth=auth)
    assert {r["sid"] for r in rows} == {"s2"} and state["logins"] == 2


def test_token_cache_ttl_and_shared_store(site, tmp_path, monkeypatch):
    url, state = site
    auth = {"login": {"url": f"{url}/token", "token_key": "access_token"}, "token_cache_ttl": "1s"}
    get("http.get")(None, url=f"{url}/api/x", auth=auth)
    get("http.get")(None, url=f"{url}/api/x", auth=auth)
    assert state["logins"] == 1
    time.sleep(1.1)
    get("http.get")(None, url=f"{url}/api/x", auth=auth)
    assert state["logins"] == 2

    store = open_store(str(tmp_path / "s.db"))
    shared = {**auth, "token_cache_ttl": "10m", "shared": True}
    http_mod._TOKENS.clear()
    for _ in range(2):  # two clients (as two runs would) on the same store
        client = http_mod.Client(store=store, auth=shared)
        assert client.request("GET", f"{url}/api/x", {}, None) == [{"sid": "s3"}]
    assert state["logins"] == 3
    assert any(
        k.startswith("http-login:") for k in [r["key"] for r in store._query("SELECT key FROM {cache}")]
    )


def test_auth_validation(site):
    url, _ = site
    with pytest.raises(ValueError, match="unknown key"):
        get("http.get")(None, url=f"{url}/a", auth={"login": {"url": f"{url}/login"}, "cookie": 1})
    with pytest.raises(ValueError, match="needs a url"):
        get("http.get")(None, url=f"{url}/a", auth={"login": {}})
    with pytest.raises(ValueError, match="not both"):
        get("http.get")(None, url=f"{url}/a", auth={"login": {"url": f"{url}/login", "form": {}, "json": {}}})
