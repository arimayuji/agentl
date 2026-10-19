"""GraphQL, Google Sheets, Google Drive and SharePoint/OneDrive against a local fake API (#26)."""

import http.server
import json
import threading
import urllib.parse
from pathlib import Path
from typing import ClassVar

import pytest

from agentl.connectors import saas
from agentl.registry import get


class Api(http.server.BaseHTTPRequestHandler):
    """One server playing every service. Records each request as (method, path, query, headers, body)."""

    log: ClassVar[list] = []
    sheet: ClassVar[dict] = {}
    drive: ClassVar[dict] = {}
    graph: ClassVar[dict] = {}

    def log_message(self, *a):
        pass

    def _reply(self, code=200, data=None, raw=None):
        body = raw if raw is not None else json.dumps(data or {}).encode()
        self.send_response(code)
        self.send_header(
            "Content-Type", "application/octet-stream" if raw is not None else "application/json"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = urllib.parse.unquote(u.path)
        Api.log.append((method, path, q, dict(self.headers), body))
        port = self.server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        # --- auth
        if path == "/tenant/oauth2/v2.0/token":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            assert form["grant_type"] == "client_credentials" and form["client_secret"] == "s3cret"
            return self._reply(data={"access_token": "ms-token", "expires_in": 3600})
        signed = path.startswith(("/graph/dl/", "/graph/session/"))  # pre-signed URLs: no token
        if path.startswith(("/sheets", "/drive", "/upload", "/graph/")) and not signed:
            want = "Bearer ms-token" if path.startswith("/graph/") else "Bearer g-token"
            if self.headers.get("Authorization") != want:
                return self._reply(401, {"error": "bad token"})
        # --- GraphQL: two pages of issues
        if path == "/graphql":
            req = json.loads(body)
            if req["variables"].get("repo") == "broken":
                return self._reply(data={"errors": [{"message": "Could not resolve repository"}]})
            after = req["variables"].get("after")
            nodes = [{"title": "a"}, {"title": "b"}] if after is None else [{"title": "c"}]
            info = {"hasNextPage": after is None, "endCursor": "CUR1"}
            return self._reply(data={"data": {"repo": {"issues": {"nodes": nodes, "pageInfo": info}}}})
        # --- Sheets
        if path == "/sheets/spreadsheets/S1/values/Sales!A:C":
            return self._reply(data={"values": Api.sheet.get("values", [])})
        if path == "/sheets/spreadsheets/S1/values/Out:clear":
            Api.sheet["out"] = []
            return self._reply(data={})
        if path == "/sheets/spreadsheets/S1/values/Out" and method == "PUT":
            Api.sheet["out"] = json.loads(body)["values"]
            return self._reply(data={"updatedRange": "Out!A1:B3"})
        if path == "/sheets/spreadsheets/S1/values/Out:append":
            Api.sheet["out"] += json.loads(body)["values"]
            return self._reply(data={"updates": {"updatedRange": "Out!A4:B4"}})
        # --- Drive
        if path == "/drive/v3/files" and method == "GET":
            if "name = " in q["q"]:
                name = q["q"].split("name = '")[1].split("'")[0]
                return self._reply(data={"files": [f for f in Api.drive["files"] if f["name"] == name]})
            if q.get("pageToken") is None:
                return self._reply(data={"files": Api.drive["files"][:2], "nextPageToken": "P2"})
            return self._reply(data={"files": Api.drive["files"][2:]})
        if path.startswith("/drive/v3/files/") and path.endswith("/export"):
            return self._reply(raw=b"xlsx-bytes-of-" + path.split("/")[4].encode())
        if path.startswith("/drive/v3/files/") and q.get("alt") == "media":
            return self._reply(raw=b"csv-bytes-of-" + path.split("/")[4].encode())
        if path == "/upload/drive/v3/files":
            Api.drive["uploaded"] = body
            return self._reply(data={"id": "NEW"})
        if path.startswith("/upload/drive/v3/files/"):
            Api.drive["patched"] = (path.split("/")[-1], body)
            return self._reply(data={"id": path.split("/")[-1]})
        # --- Graph
        if path == "/graph/sites/contoso.sharepoint.com:/sites/Finance":
            return self._reply(data={"id": "SITE1"})
        if path == "/graph/sites/SITE1/drive/root:/Reports:/children":
            if "skip" not in q:
                items = Api.graph["items"][:2]
                return self._reply(
                    data={
                        "value": items,
                        "@odata.nextLink": f"{base}/graph/sites/SITE1/drive/root:/Reports:/children?skip=2",
                    }
                )
            return self._reply(data={"value": Api.graph["items"][2:]})
        if path.startswith("/graph/dl/"):
            assert "Authorization" not in self.headers  # pre-signed URL: no token sent
            return self._reply(raw=b"bytes-of-" + path.split("/")[-1].encode())
        if path.startswith("/graph/sites/SITE1/drive/root:/Reports/") and path.endswith(":/content"):
            Api.graph["put"] = (path, body)
            return self._reply(data={"id": "F1", "webUrl": "https://contoso/x"})
        if path.endswith(":/createUploadSession"):
            return self._reply(data={"uploadUrl": f"{base}/graph/session/1"})
        if path == "/graph/session/1":
            assert "Authorization" not in self.headers
            Api.graph.setdefault("chunks", []).append((self.headers["Content-Range"], len(body)))
            done = self.headers["Content-Range"].split("-")[1].split("/")
            if int(done[0]) + 1 == int(done[1]):
                return self._reply(201, {"id": "BIG", "webUrl": "https://contoso/big"})
            return self._reply(202, {"nextExpectedRanges": [f"{int(done[0]) + 1}-"]})
        return self._reply(404, {"error": f"no route {method} {path}"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")


@pytest.fixture
def api(monkeypatch):
    Api.log, Api.sheet, Api.graph = [], {}, {}
    Api.drive = {
        "files": [
            {"id": "f1", "name": "sales.csv", "mimeType": "text/csv", "modifiedTime": "2026-10-14T08:00:00Z"},
            {"id": "f2", "name": "notes.txt", "mimeType": "text/plain"},
            {"id": "f3", "name": "Budget", "mimeType": "application/vnd.google-apps.spreadsheet"},
        ]
    }
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    Api.graph["items"] = [
        {
            "id": "i1",
            "name": "sales_2026-10-14.xlsx",
            "file": {},
            "@microsoft.graph.downloadUrl": f"{base}/graph/dl/i1",
        },
        {"id": "d1", "name": "archive", "folder": {}},
        {"id": "i2", "name": "readme.txt", "file": {}, "@microsoft.graph.downloadUrl": f"{base}/graph/dl/i2"},
    ]
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "g-token")
    for k in ("MS_ACCESS_TOKEN",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MS_TENANT_ID", "tenant")
    monkeypatch.setenv("MS_CLIENT_ID", "app")
    monkeypatch.setenv("MS_CLIENT_SECRET", "s3cret")
    saas._TOKENS.clear()
    yield base
    srv.shutdown()
    srv.server_close()


def ms(base):
    return {"api_url": f"{base}/graph", "token_url": f"{base}/tenant/oauth2/v2.0/token"}


# ---------------------------------------------------------------- GraphQL
def test_graphql_follows_cursors_and_fills_placeholders(api):
    q = (
        "query($repo: String!, $after: String) "
        "{ repo(name: $repo) { issues(after: $after) { nodes { title } } } }"
    )
    rows = get("graphql.query")(
        [{"name": "agentl"}],
        url=f"{api}/graphql",
        query=q,
        variables={"repo": "{name}"},
        items="repo.issues.nodes",
        page_info="repo.issues.pageInfo",
    )
    assert [r["title"] for r in rows] == ["a", "b", "c"]
    second = json.loads(Api.log[1][4])
    assert second["variables"] == {"repo": "agentl", "after": "CUR1"}


def test_graphql_errors_are_raised(api):
    with pytest.raises(RuntimeError, match="Could not resolve repository"):
        get("graphql.query")(None, url=f"{api}/graphql", query="{x}", variables={"repo": "broken"})


# ---------------------------------------------------------------- Sheets
def test_sheets_read_and_write(api):
    Api.sheet["values"] = [["region", "total", "note"], ["south", 10.5, "ok"], ["north", 3]]
    rows = get("gsheets.read")(None, spreadsheet_id="S1", range="Sales!A:C", api_url=f"{api}/sheets")
    assert rows == [
        {"region": "south", "total": 10.5, "note": "ok"},
        {"region": "north", "total": 3, "note": ""},
    ]
    assert Api.log[0][2]["valueRenderOption"] == "UNFORMATTED_VALUE"

    out = get("gsheets.write")(
        [{"a": 1, "b": None}, {"a": 2, "b": {"x": 1}}],
        spreadsheet_id="S1",
        range="Out",
        api_url=f"{api}/sheets",
    )
    assert out == [{"written": 2, "range": "Out!A1:B3"}]
    assert Api.sheet["out"] == [["a", "b"], [1, ""], [2, '{"x": 1}']]
    get("gsheets.write")(
        [{"a": 3, "b": "z"}], spreadsheet_id="S1", range="Out", mode="append", api_url=f"{api}/sheets"
    )
    assert Api.sheet["out"][-1] == [3, "z"]


def test_google_needs_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    with pytest.raises(ValueError, match="GOOGLE_ACCESS_TOKEN"):
        get("gsheets.read")(None, spreadsheet_id="S1")


# ---------------------------------------------------------------- Drive
def test_drive_lists_pages_filters_exports_and_feeds_file_read(api):
    rows = get("gdrive.files")(None, folder_id="FOLDER", pattern="*.[cx]*", api_url=f"{api}/drive/v3")
    assert [r["name"] for r in rows] == ["sales.csv", "Budget.xlsx"]
    assert Path(rows[0]["path"]).read_bytes() == b"csv-bytes-of-f1"
    assert Path(rows[1]["path"]).read_bytes() == b"xlsx-bytes-of-f3"
    assert "'FOLDER' in parents" in Api.log[0][2]["q"] and Api.log[1][2]["pageToken"] == "P2"


def test_drive_upload_creates_or_replaces(api, tmp_path):
    new, same = tmp_path / "report.xlsx", tmp_path / "sales.csv"
    new.write_bytes(b"NEWFILE")
    same.write_bytes(b"UPDATED")
    out = get("gdrive.upload")(
        [{"path": str(new)}, {"path": str(same)}], folder_id="FOLDER", api_url=f"{api}/drive/v3"
    )
    assert out[0] == {"name": "report.xlsx", "id": "NEW", "bytes": 7, "replaced": False}
    assert b'"parents": ["FOLDER"]' in Api.drive["uploaded"] and b"NEWFILE" in Api.drive["uploaded"]
    assert out[1]["replaced"] and Api.drive["patched"] == ("f1", b"UPDATED")


# ---------------------------------------------------------------- SharePoint / OneDrive
def test_sharepoint_files_with_client_credentials(api):
    rows = get("sharepoint.files")(
        None, site="contoso.sharepoint.com:/sites/Finance", folder="/Reports", pattern="*.xlsx", **ms(api)
    )
    assert [r["name"] for r in rows] == ["sales_2026-10-14.xlsx"]
    assert Path(rows[0]["path"]).read_bytes() == b"bytes-of-i1"
    token_calls = [e for e in Api.log if e[1].endswith("/token")]
    assert len(token_calls) == 1  # cached for the next calls
    get("sharepoint.files")(None, site="contoso.sharepoint.com:/sites/Finance", folder="/Reports", **ms(api))
    assert len([e for e in Api.log if e[1].endswith("/token")]) == 1


def test_sharepoint_upload_small_and_chunked(api, tmp_path, monkeypatch):
    small = tmp_path / "sales.xlsx"
    small.write_bytes(b"x" * 100)
    out = get("sharepoint.upload")(
        [{"path": str(small)}], site="contoso.sharepoint.com:/sites/Finance", folder="Reports", **ms(api)
    )
    assert out == [{"name": "sales.xlsx", "id": "F1", "bytes": 100, "web_url": "https://contoso/x"}]
    assert Api.graph["put"] == ("/graph/sites/SITE1/drive/root:/Reports/sales.xlsx:/content", b"x" * 100)

    monkeypatch.setattr(saas, "SIMPLE_UPLOAD_MAX", 10)
    monkeypatch.setattr(saas, "CHUNK", 40)
    big = tmp_path / "big.xlsx"
    big.write_bytes(b"y" * 100)
    out = get("sharepoint.upload")(
        [{"path": str(big)}], site="contoso.sharepoint.com:/sites/Finance", folder="Reports", **ms(api)
    )
    assert out[0]["id"] == "BIG"
    assert Api.graph["chunks"] == [("bytes 0-39/100", 40), ("bytes 40-79/100", 40), ("bytes 80-99/100", 20)]


def test_excel_to_a_shared_folder_in_one_pipeline(api, tmp_path):
    pytest.importorskip("openpyxl")
    from agentl import parse
    from agentl.runner import Runner
    from agentl.store import Store

    spec = parse(
        {
            "name": "to-sharepoint",
            "steps": [
                {"id": "rows", "fn": "tests.test_saas:rows"},
                {
                    "id": "xlsx",
                    "use": "excel.write",
                    "input": "rows",
                    "with": {"path": str(tmp_path / "sales.xlsx")},
                },
                {
                    "id": "share",
                    "use": "sharepoint.upload",
                    "input": "xlsx",
                    "with": {"site": "contoso.sharepoint.com:/sites/Finance", "folder": "Reports", **ms(api)},
                },
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
    assert r.status == "succeeded", r.error
    assert Api.graph["put"][1][:2] == b"PK"  # an xlsx is a zip


def rows(input=None):
    return [{"region": "south", "total": 10}]


# ---------------------------------------------------------------- google-auth transport
def test_google_transport_uses_urllib_without_requests(api, monkeypatch):
    pytest.importorskip("google.auth")
    import builtins

    real = builtins.__import__

    def no_requests(name, *args, **kwargs):
        if name == "requests" or name.startswith("requests."):
            raise ImportError("blocked for the test")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_requests)
    req = saas._google_request()
    ok = req(f"{api}/drive/v3/files?q=x", headers={"Authorization": "Bearer g-token"})
    assert ok.status == 200 and isinstance(ok.data, bytes)
    missing = req(f"{api}/nowhere", method="POST", body=b"{}", headers={"X": "1"})
    assert missing.status == 404
