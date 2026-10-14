"""Batch mode (#59): a fake batch provider, plus OpenAI and Anthropic request shapes against fakes."""

import http.server
import json
import sys
import threading
import types
from typing import ClassVar

import pytest

from agentl import parse
from agentl.llm import InvalidOutput, Result, provider, register_provider, schema_model
from agentl.runner import Runner
from agentl.spec import LLMSpec
from agentl.store import Store


class FakeBatch:
    """Finishes after `polls` checks. Record n == `bad` comes back invalid."""

    submitted: ClassVar[list[dict]] = []
    sync_calls: ClassVar[list[dict]] = []
    polls = 2
    bad = -1

    def __init__(self, spec):
        self.checks = 0

    def structured(self, instruction, record, schema):
        self.sync_calls.append(record)
        return Result(schema.model_validate({"double": record["n"] * 2}), 100, 10)

    def submit_batch(self, instruction, records, schema):
        self.submitted.append(records)
        return f"batch-{len(self.submitted)}"

    def batch_done(self, batch_id):
        self.checks += 1
        return self.checks > self.polls

    def batch_results(self, batch_id, schema):
        out = {}
        for cid, rec in self.submitted[int(batch_id.split("-")[1]) - 1].items():
            if rec["n"] == self.bad:
                out[cid] = InvalidOutput("nope", 100, 1)
            else:
                out[cid] = Result(schema.model_validate({"double": rec["n"] * 2}), 100, 10)
        return out

    def text(self, instruction, content):
        return ""


register_provider("fake-batch", FakeBatch)


def rows(input=None, n=4):
    return [{"n": i} for i in range(n)]


def pipeline(provider_name="fake-batch", timeout=60.0, budget=None):
    spec = {
        "name": "b",
        "llm": {
            "provider": provider_name,
            "model": "m",
            "price": {"input": 3, "output": 15, "batch": 0.5},
        },
        "steps": [
            {"id": "src", "fn": "tests.test_batch_mode:rows"},
            {
                "id": "a",
                "agent": "Double n.",
                "input": "src",
                "mode": "batch",
                "batch_poll_seconds": 0.01,
                "batch_timeout_seconds": timeout,
                "output": {"double": "int"},
            },
        ],
    }
    if budget:
        spec["budget"] = budget
    return parse(spec)


@pytest.fixture(autouse=True)
def reset():
    FakeBatch.submitted, FakeBatch.sync_calls, FakeBatch.polls, FakeBatch.bad = [], [], 2, -1


def events(store, run_id, type):
    return [e for e in store.events(run_id) if e["type"] == type]


def test_batch_mode_submits_once_and_prices_with_the_discount(tmp_path):
    with Store(tmp_path / "db") as store:
        r = Runner(pipeline(), store=store, use_cache=False).run()
        assert r.status == "succeeded", r.error
        ended = events(store, r.run_id, "batch.ended")[0]["data"]
    assert [x["double"] for x in r.outputs["a"]] == [0, 2, 4, 6]
    assert len(FakeBatch.submitted) == 1 and FakeBatch.sync_calls == []
    assert (ended["succeeded"], ended["failed"]) == (4, 0)
    assert r.llm_calls == 4 and r.input_tokens == 400
    assert r.cost_usd == pytest.approx((400 * 3 + 40 * 15) / 1e6 * 0.5)


def test_failed_batch_items_are_retried_as_normal_calls(tmp_path):
    FakeBatch.bad = 2
    with Store(tmp_path / "db") as store:
        r = Runner(pipeline(), store=store, use_cache=False).run()
    assert [x["double"] for x in r.outputs["a"]] == [0, 2, 4, 6]
    assert FakeBatch.sync_calls == [{"n": 2}]
    # 4 batch answers at half price (one invalid, still billed) + 1 normal call at full price
    batch = (400 * 3 + 31 * 15) / 1e6 * 0.5
    assert r.cost_usd == pytest.approx(batch + (100 * 3 + 10 * 15) / 1e6)


def test_a_stopped_run_resumes_the_same_batch(tmp_path):
    FakeBatch.polls = 10_000
    with Store(tmp_path / "db") as store:
        r1 = Runner(pipeline(timeout=0.05), store=store).run()
        assert r1.status == "failed" and "run again later" in r1.error
        FakeBatch.polls = 0
        r2 = Runner(pipeline(), store=store).run()
        assert events(store, r2.run_id, "batch.resumed")[0]["data"]["batch_id"] == "batch-1"
    assert r2.status == "succeeded" and len(FakeBatch.submitted) == 1
    assert [x["double"] for x in r2.outputs["a"]] == [0, 2, 4, 6]


def test_cached_records_are_not_sent_again(tmp_path):
    with Store(tmp_path / "db") as store:
        Runner(pipeline(), store=store).run()
        r = Runner(pipeline(), store=store).run()
    assert len(FakeBatch.submitted) == 1 and r.llm_calls == 0


def test_budget_is_checked_before_submitting(tmp_path):
    with Store(tmp_path / "db") as store:
        Runner(pipeline(), store=store, use_cache=False).run()
        r = Runner(pipeline(budget={"max_tokens": 1}), store=store, use_cache=False)
        result = r.run()
    assert result.status == "succeeded"  # nothing spent before the batch, so it may go
    assert len(FakeBatch.submitted) == 2


def test_provider_without_batch_mode_fails_clearly(tmp_path):
    with Store(tmp_path / "db") as store:
        r = Runner(pipeline("echo"), store=store, use_cache=False).run()
    assert r.status == "failed" and "has no batch mode" in r.error


def test_batch_and_records_per_call_do_not_mix():
    with pytest.raises(ValueError, match="one record per request"):
        parse(
            {
                "name": "x",
                "steps": [
                    {"id": "s", "fn": "tests.test_batch_mode:rows"},
                    {
                        "id": "a",
                        "agent": "A",
                        "input": "s",
                        "mode": "batch",
                        "records_per_call": 5,
                        "output": {"o": "str"},
                    },
                ],
            }
        )


# ------------------------------------------------------------------ OpenAI
@pytest.fixture
def openai_api(monkeypatch):
    state: dict = {"requests": [], "status": "completed"}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append({"path": self.path, "type": self.headers["Content-Type"], "body": raw})
            if self.path == "/v1/files":
                state["jsonl"] = raw.decode().split("\r\n\r\n", 2)[2].rsplit("\r\n--", 1)[0]
                self._send(b'{"id": "file-in"}')
            else:
                self._send(b'{"id": "batch-x"}')

        def do_GET(self):
            if self.path == "/v1/batches/batch-x":
                self._send(json.dumps({"status": state["status"], "output_file_id": "file-out"}).encode())
                return
            lines = []
            for line in state["jsonl"].splitlines():
                req = json.loads(line)
                rec = json.loads(
                    req["body"]["messages"][1]["content"].split("<record>\n")[1].split("\n</record>")[0]
                )
                content = json.dumps({"double": rec["n"] * 2})
                body = {
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 5},
                }
                lines.append(
                    json.dumps(
                        {"custom_id": req["custom_id"], "response": {"status_code": 200, "body": body}}
                    )
                )
            self._send("\n".join(lines).encode(), "application/jsonl")

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    yield state
    httpd.shutdown()
    httpd.server_close()


def test_openai_batch_round_trip(openai_api):
    prov = provider(LLMSpec(provider="openai", model="m", options={"base_url": openai_api["url"]}))
    schema = schema_model("o", {"double": "int"})
    batch_id = prov.submit_batch("Double n.", {"r0": {"n": 1}, "r1": {"n": 5}}, schema)
    assert batch_id == "batch-x"
    upload, create = openai_api["requests"]
    assert upload["type"].startswith("multipart/form-data") and b'name="purpose"' in upload["body"]
    assert json.loads(create["body"]) == {
        "input_file_id": "file-in",
        "endpoint": "/v1/chat/completions",
        "completion_window": "24h",
    }
    line = json.loads(openai_api["jsonl"].splitlines()[0])
    assert line["url"] == "/v1/chat/completions" and line["body"]["response_format"]["type"] == "json_schema"
    assert prov.batch_done(batch_id)
    out = prov.batch_results(batch_id, schema)
    assert {k: v.value.double for k, v in out.items()} == {"r0": 2, "r1": 10}
    openai_api["status"] = "failed"
    with pytest.raises(RuntimeError, match="failed"):
        prov.batch_done(batch_id)


# --------------------------------------------------------------- Anthropic
def test_anthropic_batch_requests_and_results(monkeypatch):
    sent: dict = {}

    def result(cid, kind, text=""):
        usage = types.SimpleNamespace(
            input_tokens=10, output_tokens=2, cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        msg = types.SimpleNamespace(
            stop_reason="end_turn", usage=usage, content=[types.SimpleNamespace(type="text", text=text)]
        )
        return types.SimpleNamespace(custom_id=cid, result=types.SimpleNamespace(type=kind, message=msg))

    class Batches:
        def create(self, requests):
            sent["requests"] = requests
            return types.SimpleNamespace(id="msgbatch_1")

        def retrieve(self, batch_id):
            return types.SimpleNamespace(processing_status="ended")

        def results(self, batch_id):
            return [
                result("r0", "succeeded", '{"ok": true}'),
                result("r1", "expired"),
                result("r2", "succeeded", "{}"),
            ]

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
        messages=types.SimpleNamespace(batches=Batches())
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    prov = provider(LLMSpec())
    schema = schema_model("o", {"ok": "bool"})
    assert prov.submit_batch("Check.", {"r0": {"x": 1}}, schema) == "msgbatch_1"
    params = sent["requests"][0]["params"]
    assert sent["requests"][0]["custom_id"] == "r0"
    assert params["output_config"]["format"]["schema"]["required"] == ["ok"]
    assert params["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert prov.batch_done("msgbatch_1")
    out = prov.batch_results("msgbatch_1", schema)
    assert out["r0"].value.ok is True
    assert isinstance(out["r1"], RuntimeError) and isinstance(out["r2"], InvalidOutput)
    assert out["r2"].input_tokens == 10
