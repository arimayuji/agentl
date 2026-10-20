"""MCP resources, prompts and the streamable HTTP transport (#165). In-process; HTTP only on
127.0.0.1 with an ephemeral port."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agentl.mcp import PROTOCOL_VERSION
from agentl.mcp_server import PROMPTS, Server, http_server, parse_bind


def rpc(s: Server, method: str, **params):
    return s.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


@pytest.fixture
def root(tmp_path):
    (tmp_path / "in.csv").write_text("a\n1\n")
    (tmp_path / "p.yaml").write_text(
        f"name: p\nsteps:\n  - id: r\n    use: csv.read\n    with: {{path: '{tmp_path / 'in.csv'}'}}\n"
    )
    (tmp_path / "notes.yaml").write_text("just: data\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "q.yaml").write_text("name: q\nsteps: []\n")
    return tmp_path


def test_initialize_announces_resources_and_prompts():
    caps = rpc(Server(":memory:"), "initialize")["result"]["capabilities"]
    assert {"tools", "resources", "prompts"} <= set(caps)


def test_list_and_read_resources(root):
    s = Server(str(root / "runs.db"), root=str(root))
    uris = {r["uri"] for r in rpc(s, "resources/list")["result"]["resources"]}
    assert {
        "agentl://docs/skill",
        "agentl://docs/connectors",
        "agentl://docs/schema",
        "agentl://runs",
    } <= uris
    assert "agentl://templates/incremental-api-load" in uris
    assert "agentl://pipelines/p.yaml" in uris
    assert "agentl://pipelines/notes.yaml" not in uris  # not a pipeline
    assert not any(".hidden" in u for u in uris)
    templates = {t["uriTemplate"] for t in rpc(s, "resources/templates/list")["result"]["resourceTemplates"]}
    assert "agentl://runs/{run_id}" in templates

    def read(uri):
        return rpc(s, "resources/read", uri=uri)

    for uri in sorted(uris):
        c = read(uri)["result"]["contents"][0]
        assert c["uri"] == uri and c["text"]
    assert "csv.read" in read("agentl://docs/connectors")["result"]["contents"][0]["text"]
    assert "properties" in json.loads(read("agentl://docs/schema")["result"]["contents"][0]["text"])
    assert "steps:" in read("agentl://templates/incremental-api-load")["result"]["contents"][0]["text"]
    assert "with:" in read("agentl://connectors/csv.read")["result"]["contents"][0]["text"]
    assert read("agentl://templates/nope")["error"]["code"] == -32002
    assert read("https://example.com")["error"]["code"] == -32002

    # a run summary after a run
    res = rpc(s, "tools/call", name="run_pipeline", arguments={"pipeline": str(root / "p.yaml")})
    run_id = json.loads(res["result"]["content"][0]["text"])["run_id"]
    summary = json.loads(read(f"agentl://runs/{run_id}")["result"]["contents"][0]["text"])
    assert summary["status"] == "succeeded" and summary["steps_done"] == ["r"]
    assert json.loads(read("agentl://runs")["result"]["contents"][0]["text"])[0]["id"] == run_id
    assert read("agentl://runs/nope")["error"]["code"] == -32002


def test_pipeline_resources_stay_under_root(root):
    (root / "sub").mkdir()
    s = Server(None, root=str(root / "sub"))
    for uri in (
        "agentl://pipelines/../p.yaml",
        "agentl://pipelines/%2E%2E/p.yaml",
        f"agentl://pipelines/{root}/p.yaml",
    ):
        err = rpc(s, "resources/read", uri=uri)["error"]
        assert err["code"] == -32602 and "only files under" in err["message"]


def test_prompts(root):
    s = Server(None)
    prompts = {p["name"]: p for p in rpc(s, "prompts/list")["result"]["prompts"]}
    assert set(prompts) == set(PROMPTS) == {"build_pipeline", "explain_failed_run", "estimate_cost"}
    got = rpc(
        s, "prompts/get", name="build_pipeline", arguments={"sources": "a CSV", "destination": "Postgres"}
    )
    text = got["result"]["messages"][0]["content"]["text"]
    assert "a CSV" in text and "Postgres" in text and "new_from_template" in text
    assert (
        "list_failures"
        in rpc(s, "prompts/get", name="explain_failed_run")["result"]["messages"][0]["content"]["text"]
    )
    est = rpc(s, "prompts/get", name="estimate_cost", arguments={"pipeline": "p.yaml"})
    assert "plan" in est["result"]["messages"][0]["content"]["text"]
    assert rpc(s, "prompts/get", name="estimate_cost")["error"]["code"] == -32602
    assert rpc(s, "prompts/get", name="nope")["error"]["code"] == -32602


def test_parse_bind():
    assert parse_bind(":7072") == ("127.0.0.1", 7072)
    assert parse_bind("7072") == ("127.0.0.1", 7072)
    assert parse_bind("0.0.0.0:80") == ("0.0.0.0", 80)
    assert parse_bind("[::1]:7072") == ("::1", 7072)
    with pytest.raises(ValueError):
        parse_bind("localhost:http")


@pytest.fixture
def http(root):
    httpd = http_server(Server(str(root / "runs.db"), root=str(root)), "127.0.0.1", 0, token="t0ken")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    httpd.shutdown()
    httpd.server_close()


def post(url, body, token="t0ken", method="POST", **headers):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data if method == "POST" else None, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


def test_http_round_trip(http, root):
    status, body = post(http, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert status == 200 and body["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert post(http, {"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
    status, body = post(
        http,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        **{"MCP-Protocol-Version": PROTOCOL_VERSION},
    )
    assert status == 200 and any(t["name"] == "validate" for t in body["result"]["tools"])
    status, body = post(
        http,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "agentl://pipelines/p.yaml"},
        },
    )
    assert "csv.read" in body["result"]["contents"][0]["text"]
    status, body = post(
        http,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "run_pipeline", "arguments": {"pipeline": str(root / "p.yaml")}},
        },
    )
    assert '"succeeded"' in body["result"]["content"][0]["text"]
    assert post(http, b"{not json")[0] == 400
    assert post(http, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])[0] == 400
    assert post(http, {}, method="GET")[0] == 405


def test_http_refuses_without_the_token_and_outside_root(http, root, tmp_path_factory):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    status, body = post(http, ping, token=None)
    assert status == 401 and "token" in body["error"]["message"]
    assert post(http, ping, token="wrong")[0] == 401
    assert post(http, ping, method="GET", token=None)[0] == 401
    assert post(http, ping, Origin="https://evil.example")[0] == 403
    assert post(http, ping, Origin="http://localhost:3000")[0] == 200
    assert post(http, ping, **{"MCP-Protocol-Version": "1999-01-01"})[0] == 400
    assert post(http.replace("/mcp", "/other"), ping)[0] == 404
    outside = tmp_path_factory.mktemp("outside") / "o.yaml"
    outside.write_text((root / "p.yaml").read_text())
    _, body = post(
        http,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "run_pipeline", "arguments": {"pipeline": str(outside)}},
        },
    )
    assert body["result"]["isError"] and "only runs files under" in body["result"]["content"][0]["text"]
    _, body = post(
        http,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "agentl://pipelines/../x.yaml"},
        },
    )
    assert "only files under" in body["error"]["message"]


def test_serve_refuses_public_bind_without_token(monkeypatch):
    from agentl.mcp_server import serve

    monkeypatch.delenv("AGENTL_MCP_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="without a token"):
        serve(None, http="0.0.0.0:0")
