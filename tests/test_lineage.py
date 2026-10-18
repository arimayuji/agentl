import http.server
import json
import threading

import jsonschema
import pytest

from agentl import parse
from agentl.lineage import OpenLineage, from_env, run_uuid, step_datasets
from agentl.runner import Runner
from agentl.store import Store

# The parts of the OpenLineage 2-0-2 RunEvent schema (openlineage.io/spec/2-0-2/OpenLineage.json)
# that these events use, vendored so the test needs no network.
_FACET = {
    "type": "object",
    "required": ["_producer", "_schemaURL"],
    "properties": {
        "_producer": {"type": "string", "format": "uri"},
        "_schemaURL": {"type": "string", "format": "uri"},
    },
}
_DATASET = {
    "type": "object",
    "required": ["namespace", "name"],
    "properties": {"namespace": {"type": "string"}, "name": {"type": "string"}, "facets": {"type": "object"}},
}
RUN_EVENT = {
    "type": "object",
    "required": ["eventTime", "producer", "schemaURL", "run", "job"],
    "properties": {
        "eventType": {"enum": ["START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER"]},
        "eventTime": {"type": "string", "format": "date-time"},
        "producer": {"type": "string", "format": "uri"},
        "schemaURL": {"type": "string", "format": "uri"},
        "run": {
            "type": "object",
            "required": ["runId"],
            "properties": {
                "runId": {"type": "string", "format": "uuid"},
                "facets": {
                    "type": "object",
                    "properties": {
                        "parent": {
                            "allOf": [
                                _FACET,
                                {
                                    "type": "object",
                                    "required": ["run", "job"],
                                    "properties": {
                                        "run": {
                                            "type": "object",
                                            "required": ["runId"],
                                            "properties": {"runId": {"type": "string", "format": "uuid"}},
                                        },
                                        "job": {"type": "object", "required": ["namespace", "name"]},
                                    },
                                },
                            ]
                        },
                        "errorMessage": {
                            "allOf": [
                                _FACET,
                                {"type": "object", "required": ["message", "programmingLanguage"]},
                            ]
                        },
                    },
                },
            },
        },
        "job": {
            "type": "object",
            "required": ["namespace", "name"],
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string"},
                "facets": {"type": "object"},
            },
        },
        "inputs": {"type": "array", "items": _DATASET},
        "outputs": {"type": "array", "items": _DATASET},
    },
}


def validate(event):
    jsonschema.validate(event, RUN_EVENT, format_checker=jsonschema.FormatChecker())


def rows(input=None):
    return [{"id": 1}, {"id": 2}]


def boom(input=None):
    raise RuntimeError("kaboom")


def pipeline(tmp_path, fail=False, **extra):
    src = tmp_path / "orders.csv"
    src.write_text("id\n1\n2\n", encoding="utf-8")
    steps = [
        {"id": "src", "use": "csv.read", "with": {"path": str(src)}},
        {"id": "pick", "use": "select", "input": "src", "with": {"fields": ["id"]}},
        {"id": "out", "use": "json.write", "input": "pick", "with": {"path": str(tmp_path / "orders.json")}},
    ]
    if fail:
        steps.append({"id": "boom", "fn": "tests.test_lineage:boom", "input": "out"})
    return parse({"name": "orders", "steps": steps, **extra})


def run(tmp_path, spec, listeners=()):
    with Store(tmp_path / "db.sqlite") as store:
        return Runner(spec, store=store, listeners=list(listeners)).run()


def test_events_validate_and_carry_datasets(tmp_path):
    spec = pipeline(tmp_path)
    got: list[dict] = []
    result = run(tmp_path, spec, [OpenLineage(spec, sink=got.append)])
    assert result.status == "succeeded"
    for e in got:
        validate(e)
    kinds = [(e["job"]["name"], e["eventType"]) for e in got]
    assert kinds[0] == ("orders", "START") and kinds[-1] == ("orders", "COMPLETE")
    assert ("orders.src", "START") in kinds and ("orders.out", "COMPLETE") in kinds
    run_event = got[-1]
    assert run_event["run"]["runId"] == run_uuid(result.run_id)
    assert [d["name"] for d in run_event["inputs"]] == [(tmp_path / "orders.csv").as_posix()]
    assert [d["name"] for d in run_event["outputs"]] == [(tmp_path / "orders.json").as_posix()]
    step = next(e for e in got if e["job"]["name"] == "orders.out")
    assert step["run"]["facets"]["parent"]["run"]["runId"] == run_uuid(result.run_id)
    assert step["inputs"] == [] and step["outputs"][0]["namespace"] == "file"


def test_fail_events_carry_the_error(tmp_path):
    spec = pipeline(tmp_path, fail=True)
    got: list[dict] = []
    run(tmp_path, spec, [OpenLineage(spec, sink=got.append)])
    for e in got:
        validate(e)
    fails = {e["job"]["name"]: e for e in got if e["eventType"] == "FAIL"}
    assert set(fails) == {"orders", "orders.boom"}
    assert "kaboom" in fails["orders.boom"]["run"]["facets"]["errorMessage"]["message"]


def test_yaml_block_writes_a_file(tmp_path):
    out = tmp_path / "lineage.jsonl"
    spec = pipeline(tmp_path, lineage={"file": str(out), "namespace": "shop"})
    run(tmp_path, spec)
    events = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 8  # run + 3 steps, START and COMPLETE each
    assert {e["job"]["namespace"] for e in events} == {"shop"}


def test_http_transport_and_env(tmp_path, monkeypatch):
    seen: list[tuple[str, str | None, dict]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, self.headers.get("Authorization"), json.loads(body)))
            self.send_response(201)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("OPENLINEAGE_URL", f"http://127.0.0.1:{httpd.server_port}")
        monkeypatch.setenv("OPENLINEAGE_API_KEY", "secret")
        assert run(tmp_path, pipeline(tmp_path)).status == "succeeded"
    finally:
        httpd.shutdown()
    assert len(seen) == 8
    assert {p for p, _, _ in seen} == {"/api/v1/lineage"}
    assert {a for _, a, _ in seen} == {"Bearer secret"}
    for _, _, e in seen:
        validate(e)


def test_unreachable_catalog_does_not_fail_the_run(tmp_path):
    spec = pipeline(tmp_path, lineage={"url": "http://127.0.0.1:9"})
    assert run(tmp_path, spec).status == "succeeded"


def test_no_lineage_unless_asked(monkeypatch):
    monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
    monkeypatch.delenv("OPENLINEAGE_FILE", raising=False)
    assert from_env(None) is None


@pytest.mark.parametrize(
    "step, expected",
    [
        (
            {"use": "postgres.read", "with": {"dsn": "postgresql://db.local/shop", "table": "orders"}},
            ("in", "postgresql://db.local", "orders"),
        ),
        ({"use": "kafka.produce", "with": {"topic": "events"}}, ("out", "agentl", "events")),
        (
            {"use": "http.get", "with": {"url": "https://api.example.com/v1/items"}},
            ("in", "https://api.example.com", "/v1/items"),
        ),
        ({"use": "select", "with": {"columns": ["a"]}}, None),
    ],
)
def test_step_datasets(step, expected):
    spec = parse({"name": "p", "steps": [{"id": "s", **step}]})
    ins, outs = step_datasets(spec.steps[0], "agentl")
    if expected is None:
        assert ins == outs == []
        return
    side, ns, name = expected
    ds = (ins if side == "in" else outs)[0]
    assert (ds["namespace"], ds["name"]) == (ns, name)
