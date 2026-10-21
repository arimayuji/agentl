"""Cassettes (#211): agentl run --record, agentl test --replay, eval samples from a recording."""

import http.server
import json
import threading
import urllib.error
import urllib.request

import pytest

from agentl import cassettes, netguard
from agentl.cli import main
from agentl.llm import Result, register_provider

TOKEN = "s3cr3t-token-value"


class Labeler:
    """Stands in for a real model: answers that the echo provider would not give."""

    calls = 0

    def __init__(self, spec):
        self.spec = spec

    def structured(self, instruction, record, schema):
        Labeler.calls += 1
        return Result(schema.model_validate({"label": f"label-{record['id']}"}))

    def text(self, instruction, content):
        return "summary"


register_provider("cassette-labeler", Labeler)

PIPELINE = """
name: items
llm:
  provider: cassette-labeler
  model: some-model
steps:
  - id: items
    use: http.get
    with:
      url: "{url}/items?api_key={token}&page=1"
      headers: {{Authorization: "Bearer {token}"}}
      items: data
  - id: label
    agent: Label the item.
    input: items
    output: {{label: str}}
    golden: golden.jsonl
  - id: save
    use: json.write
    input: label
    with: {{path: "{out}"}}
"""


@pytest.fixture
def server():
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            assert self.headers["Authorization"] == f"Bearer {TOKEN}"
            body = json.dumps(
                {"data": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}], "access_token": TOKEN}
            )
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"sid={TOKEN}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", hits
    srv.shutdown()


@pytest.fixture
def recorded(tmp_path, server):
    url, hits = server
    pipe = tmp_path / "p.yaml"
    out = tmp_path / "out.json"
    pipe.write_text(PIPELINE.format(url=url, token=TOKEN, out=out.as_posix()), encoding="utf-8")
    tape = tmp_path / "cassettes" / "day1"
    Labeler.calls = 0
    code = main(["run", str(pipe), "--record", str(tape), "--db", str(tmp_path / "s.db"), "--no-check"])
    assert code == 0
    assert hits and Labeler.calls == 2
    return tmp_path, pipe, tape, hits


def test_record_hides_secrets(recorded):
    _, _, tape, _ = recorded
    text = (tape / cassettes.FILE).read_text(encoding="utf-8")
    assert TOKEN not in text
    data = json.loads(text)
    (entry,) = data["http"]
    assert "api_key=***" in entry["url"] and "page=1" in entry["url"]
    assert entry["request_headers"]["Authorization"] == "***"
    assert dict(entry["headers"])["Set-Cookie"] == "***"
    assert json.loads(entry["body"])["access_token"] == "***"
    assert [e["answer"] for e in data["llm"]] == [{"label": "label-1"}, {"label": "label-2"}]
    assert {e["step"] for e in data["llm"]} == {"label"}


def test_replay_runs_offline(recorded, monkeypatch, capsys):
    tmp, pipe, tape, hits = recorded
    before = len(hits)
    Labeler.calls = 0

    def no_network(*a, **kw):
        raise AssertionError("replay reached the network")

    monkeypatch.setattr(netguard, "_urlopen", no_network)
    golden = tmp / "expect.json"
    args = ["test", str(pipe), "--replay", str(tape), "--expect", str(golden)]
    assert main([*args, "--update"]) == 0
    rows = json.loads(golden.read_text(encoding="utf-8"))
    assert [r["label"] for r in rows] == ["label-1", "label-2"]  # the recorded answers, not echo
    assert main(args) == 0
    assert len(hits) == before and Labeler.calls == 0


def test_replay_miss_fails(recorded, capsys):
    tmp, pipe, tape, _ = recorded
    data = json.loads((tape / cassettes.FILE).read_text(encoding="utf-8"))
    data["llm"] = data["llm"][:1]
    (tape / cassettes.FILE).write_text(json.dumps(data), encoding="utf-8")
    golden = tmp / "expect.json"
    golden.write_text("[]", encoding="utf-8")
    assert main(["test", str(pipe), "--replay", str(tape), "--expect", str(golden)]) == 1


def test_eval_add_from_cassette(recorded, capsys):
    tmp, pipe, tape, _ = recorded
    assert main(["eval", "add", str(pipe), "label", "--from-cassette", str(tape)]) == 0
    lines = [json.loads(x) for x in (tmp / "golden.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines == [{"id": 1, "name": "a", "label": "label-1"}, {"id": 2, "name": "b", "label": "label-2"}]
    assert main(["eval", "add", str(pipe), "label", "--from-cassette", str(tape)]) == 0
    assert "added 0 golden" in capsys.readouterr().out


def test_redact_body_keeps_data_fields():
    body = json.dumps({"author": "x", "password": "p", "nested": [{"refresh_token": "t"}]}).encode()
    assert json.loads(cassettes.redact_body(body)) == {
        "author": "x",
        "password": "***",
        "nested": [{"refresh_token": "***"}],
    }
    assert cassettes.redact_body(b"user=a&password=b") == b"user=a&password=***"
    assert cassettes.redact_body(b"plain text") == b"plain text"


def test_replay_http_error_and_repeats(tmp_path):
    with cassettes.recording(tmp_path) as tape:
        req = urllib.request.Request("http://example.invalid/x")
        tape._add_http(cassettes._request_key(req), req, 200, [], b"first")
        tape._add_http(cassettes._request_key(req), req, 404, [], b"gone")
    with cassettes.replaying(tmp_path):
        assert (
            netguard.urlopen(urllib.request.Request("http://example.invalid/x"), timeout=1).read() == b"first"
        )
        with pytest.raises(urllib.error.HTTPError) as e:
            netguard.urlopen(urllib.request.Request("http://example.invalid/x"), timeout=1)
        assert e.value.code == 404
        with pytest.raises(cassettes.CassetteMiss):
            netguard.urlopen(urllib.request.Request("http://example.invalid/y"), timeout=1)
