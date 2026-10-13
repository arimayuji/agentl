import http.server
import json
import threading

import pytest

from agentl import parse
from agentl.llm import provider, schema_model
from agentl.runner import Runner
from agentl.spec import LLMSpec
from agentl.store import Store


@pytest.fixture
def fake_api():
    """A local OpenAI-style /chat/completions endpoint. Set `state["reply"]` to a
    content string, or `state["status"]` to fail the next calls."""
    state: dict = {"requests": [], "reply": "{}", "status": [], "refusal": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "headers": dict(self.headers), "body": body})
            if state["status"]:
                self.send_response(state["status"].pop(0))
                self.end_headers()
                self.wfile.write(b'{"error": "boom"}')
                return
            msg = {"role": "assistant", "content": state["reply"], "refusal": state["refusal"]}
            out = {"choices": [{"message": msg}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    yield state
    httpd.shutdown()
    httpd.server_close()


def spec(**kw):
    return LLMSpec(**{"provider": "openai-compatible", "model": "m", **kw})


def test_structured_output_request_and_parse(fake_api):
    fake_api["reply"] = '{"category": "food", "flag": null}'
    prov = provider(spec(options={"base_url": fake_api["url"], "temperature": 0}))
    out = prov.structured(
        "Classify.", {"desc": "lunch"}, schema_model("o", {"category": "str", "flag": "bool?"})
    )
    assert out.value.model_dump() == {"category": "food", "flag": None}
    assert (out.input_tokens, out.output_tokens) == (7, 3)

    req = fake_api["requests"][0]
    assert req["path"] == "/v1/chat/completions"
    assert "Authorization" not in req["headers"]
    body = req["body"]
    assert body["model"] == "m" and body["temperature"] == 0
    js = body["response_format"]["json_schema"]
    assert js["strict"] is True
    assert js["schema"]["required"] == ["category", "flag"]
    assert js["schema"]["additionalProperties"] is False
    assert '"desc": "lunch"' in body["messages"][1]["content"]


def test_openai_needs_key_and_sends_it(fake_api, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        provider(LLMSpec(provider="openai", model="gpt-x"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    prov = provider(LLMSpec(provider="openai", model="gpt-x", options={"base_url": fake_api["url"]}))
    fake_api["reply"] = "hello"
    assert prov.text("Say hi.", "") == "hello"
    assert fake_api["requests"][0]["headers"]["Authorization"] == "Bearer sk-test"


def test_ollama_host_and_model_required(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11500")
    assert provider(LLMSpec(provider="ollama", model="llama3.2")).base_url == "http://0.0.0.0:11500/v1"
    with pytest.raises(ValueError, match=r"set llm\.model"):
        provider(LLMSpec(provider="ollama"))


def test_retries_then_fails_with_detail(fake_api, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    prov = provider(spec(options={"base_url": fake_api["url"], "retries": 1}))
    fake_api["status"] = [503]
    fake_api["reply"] = "ok"
    assert prov.text("x", "") == "ok"
    fake_api["status"] = [400]
    with pytest.raises(RuntimeError, match="HTTP 400"):
        prov.text("x", "")


def test_refusal_raises(fake_api):
    fake_api["refusal"] = "no"
    prov = provider(spec(options={"base_url": fake_api["url"]}))
    with pytest.raises(RuntimeError, match="refused"):
        prov.structured("x", {}, schema_model("o", {"a": "str"}))


def test_in_pipeline(fake_api, tmp_path):
    fake_api["reply"] = '{"label": "ok"}'
    p = parse(
        {
            "name": "x",
            "llm": {"provider": "openai-compatible", "model": "m", "options": {"base_url": fake_api["url"]}},
            "steps": [
                {"id": "src", "use": "txt.read", "with": {"path": str(tmp_path / "a.txt")}},
                {"id": "tag", "agent": "Label it.", "input": "src", "output": {"label": "str"}},
            ],
        }
    )
    (tmp_path / "a.txt").write_text("hello")
    with Store(tmp_path / "db.sqlite") as store:
        result = Runner(p, store=store).run()
    assert result.status == "succeeded", result.error
    assert result.outputs["tag"][0]["label"] == "ok"
    assert result.input_tokens == 7


def test_invalid_json_keeps_token_usage(fake_api):
    from agentl.llm import InvalidOutput

    fake_api["reply"] = '{"category": 5}'
    prov = provider(spec(options={"base_url": fake_api["url"]}))
    with pytest.raises(InvalidOutput) as e:
        prov.structured("x", {}, schema_model("o", {"category": "str"}))
    assert (e.value.input_tokens, e.value.output_tokens) == (7, 3)
