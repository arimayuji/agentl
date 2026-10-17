"""email.read with provider: msgraph against a local fake Microsoft Graph (#186)."""

import base64
import http.server
import json
import threading
import urllib.parse
from pathlib import Path
from typing import ClassVar

import pytest

from agentl.connectors import saas
from agentl.registry import get


def att(name, data, odata="#microsoft.graph.fileAttachment"):
    return {
        "@odata.type": odata,
        "name": name,
        "contentType": "application/octet-stream",
        "contentBytes": base64.b64encode(data).decode(),
    }


class Graph(http.server.BaseHTTPRequestHandler):
    log: ClassVar[list] = []
    throttled: ClassVar[set] = set()

    def log_message(self, *a):
        pass

    def _reply(self, code=200, data=None, headers=None):
        body = json.dumps(data or {}).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = urllib.parse.unquote(u.path)
        Graph.log.append((method, path, q, body))
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if path == "/tenant/oauth2/v2.0/token":
            return self._reply(data={"access_token": "ms-token", "expires_in": 3600})
        if self.headers.get("Authorization") != "Bearer ms-token":
            return self._reply(401, {"error": "bad token"})
        user = "/graph/users/reports@example.com"
        if path == f"{user}/mailFolders":
            assert q["$filter"] == "displayName eq 'Reports'"
            return self._reply(data={"value": [{"id": "F1", "displayName": "Reports"}]})
        if path == f"{user}/mailFolders/F1/childFolders":
            if q["$filter"] != "displayName eq 'Weekly'":
                return self._reply(data={"value": []})
            return self._reply(data={"value": [{"id": "F2", "displayName": "Weekly"}]})
        if path == f"{user}/mailFolders/F1/childFolders/F2/messages":
            if "page" not in q:
                assert "receivedDateTime ge " in q["$filter"]
                if "first" not in Graph.throttled:  # throttle once to exercise Retry-After
                    Graph.throttled.add("first")
                    return self._reply(429, {"error": "slow down"}, {"Retry-After": "0"})
                nxt = f"{base}{path}?page=2"
                return self._reply(data={"value": [msg("m1", True)], "@odata.nextLink": nxt})
            return self._reply(data={"value": [msg("m2", False)]})
        if path == f"{user}/messages/m1/attachments":
            if "page" not in q:
                return self._reply(
                    data={
                        "value": [att("Weekly 2026-10.xlsx", b"xlsx-bytes"), att("logo.png", b"png")],
                        "@odata.nextLink": f"{base}{path}?page=2",
                    }
                )
            return self._reply(
                data={
                    "value": [
                        att("../Weekly extra.xlsx", b"more"),
                        att("Weekly fwd.xlsx", b"", "#microsoft.graph.itemAttachment"),
                    ]
                }
            )
        if method == "PATCH" and path.startswith(f"{user}/messages/"):
            return self._reply(data={})
        return self._reply(404, {"error": path})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")


def msg(mid, has):
    return {
        "id": mid,
        "internetMessageId": f"<{mid}@example.com>",
        "from": {"emailAddress": {"address": "bot@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "reports@example.com"}}],
        "subject": f"Weekly {mid}",
        "receivedDateTime": "2026-10-14T08:00:00Z",
        "body": {"contentType": "text", "content": "see attached"},
        "hasAttachments": has,
    }


@pytest.fixture
def graph(monkeypatch):
    Graph.log, Graph.throttled = [], set()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Graph)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.delenv("MS_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("MS_TENANT_ID", "tenant")
    monkeypatch.setenv("MS_CLIENT_ID", "app")
    monkeypatch.setenv("MS_CLIENT_SECRET", "s3cret")
    monkeypatch.setattr(saas.time, "sleep", lambda s: None)
    saas._TOKENS.clear()
    yield {"api_url": f"{base}/graph", "token_url": f"{base}/tenant/oauth2/v2.0/token"}
    srv.shutdown()
    srv.server_close()


def read(graph, **kw):
    opts = {"provider": "msgraph", "mailbox": "reports@example.com", "folder": "Reports/Weekly"}
    return get("email.read")(**{**opts, "since": "7d", **graph, **kw})


def test_graph_explodes_matching_attachments_across_pages(graph):
    out = read(graph, explode=True, attachments={"name_regex": r"^Weekly .*\.xlsx$"})
    assert [r["name"] for r in out] == ["Weekly 2026-10.xlsx", "Weekly extra.xlsx"]
    first = out[0]
    assert first["message_id"] == "<m1@example.com>" and first["received_at"] == "2026-10-14T08:00:00Z"
    assert first["uid"] == "m1" and first["from"] == "bot@example.com"
    assert Path(first["path"]).read_bytes() == b"xlsx-bytes"
    assert Path(out[1]["path"]).name == "Weekly extra.xlsx"  # sender path stripped
    assert not any(m == "PATCH" for m, *_ in Graph.log)  # read-only by default


def test_graph_one_record_per_message_and_mark_seen(graph):
    out = read(graph, mark_seen=True)
    assert [r["uid"] for r in out] == ["m1", "m2"]
    assert len(out[0]["attachments"]) == 3 and out[1]["attachments"] == []
    assert out[0]["text"] == "see attached"
    patches = [(p, json.loads(b)) for m, p, _, b in Graph.log if m == "PATCH"]
    assert patches[0][1] == {"isRead": True} and len(patches) == 2


def test_graph_limit_stops_paging(graph):
    out = read(graph, limit=1, attachments=False)
    assert [r["uid"] for r in out] == ["m1"]
    assert not any("page" in q for _, p, q, _ in Graph.log if p.endswith("/messages"))


def test_graph_missing_folder_and_bad_options(graph):
    with pytest.raises(ValueError, match="not found"):
        read(graph, folder="Reports/Nope")
    with pytest.raises(ValueError, match="mailbox"):
        get("email.read")(provider="msgraph", **graph)
    with pytest.raises(ValueError, match="unknown keys"):
        read(graph, attachments={"name": "x"})
    with pytest.raises(ValueError, match="provider"):
        get("email.read")(provider="pop3")
