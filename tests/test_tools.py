"""Agent tools and MCP servers (#18). No real LLM: fake providers, a fake MCP server, local HTTP."""

import http.server
import json
import sqlite3
import sys
import threading
import types
from pathlib import Path
from typing import ClassVar

import pytest

from agentl import parse
from agentl.llm import Result, register_provider, schema_model
from agentl.mcp import MCPClient
from agentl.runner import Runner
from agentl.spec import MCPServerSpec
from agentl.store import Store
from agentl.tools import ToolBox
from tests.tooling import fake_mcp

FAKE = [sys.executable, str(Path(fake_mcp.__file__))]


@pytest.fixture
def http_mcp():
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = fake_mcp.handle(msg)
            if out is None:
                self.send_response(202)
                self.end_headers()
                return
            # answer as a short SSE stream, like streamable HTTP servers do
            body = f"event: message\ndata: {json.dumps(out)}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "s1")
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------- MCP client
@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_mcp_client_lists_and_calls_tools(transport, http_mcp):
    spec = MCPServerSpec(command=FAKE) if transport == "stdio" else MCPServerSpec(url=http_mcp)
    client = MCPClient("crm", spec)
    try:
        assert [t["name"] for t in client.list_tools()] == ["company", "delete_company"]
        assert client.call("company", {"domain": "acme.com"}) == "software"
        assert client.call("company", {"domain": "nope"}).startswith("error:")
    finally:
        client.close()


# ---------------------------------------------------------------- tool resolution
def spec_with(tools, write_tools=(), mcp=None, **step):
    return parse(
        {
            "name": "t",
            "llm": {"provider": "toolbot", "model": "m"},
            "mcp": mcp or {},
            "steps": [
                {
                    "id": "a",
                    "agent": "Enrich.",
                    "output": {"sector": "str?"},
                    "tools": tools,
                    "write_tools": list(write_tools),
                    **step,
                }
            ],
        }
    )


def resolve(spec):
    box = ToolBox(spec)
    try:
        return box.for_step(spec.steps[0])
    finally:
        box.close()


def test_whole_mcp_server_keeps_only_read_only_tools():
    tools = resolve(spec_with(["mcp:crm"], mcp={"crm": {"command": FAKE}}))
    assert [t.name for t in tools] == ["crm.company"]


def test_write_tools_need_to_be_listed():
    with pytest.raises(ValueError, match="can write"):
        resolve(spec_with(["mcp:crm/delete_company"], mcp={"crm": {"command": FAKE}}))
    tools = resolve(
        spec_with(["mcp:crm/delete_company"], ["crm.delete_company"], mcp={"crm": {"command": FAKE}})
    )
    assert [t.name for t in tools] == ["crm.delete_company"]
    with pytest.raises(ValueError, match="can write"):
        resolve(spec_with(["fn:tests.tooling.tools:plain"]))  # undeclared functions count as writers
    assert resolve(spec_with(["fn:tests.tooling.tools:lookup"]))[0].read_only


def test_fn_tool_schema_comes_from_the_signature():
    t = resolve(spec_with(["fn:tests.tooling.tools:lookup"]))[0]
    assert t.description == "Look up a product code."
    assert t.input_schema == {
        "type": "object",
        "properties": {"code": {"type": "string"}},
        "required": ["code"],
    }
    assert json.loads(t.run({"code": "X1"})) == {"code": "X1", "name": "product X1"}


def test_sqlite_query_cannot_write(tmp_path):
    db = tmp_path / "x.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table t (a)")
        conn.execute("insert into t values (1)")
    t = resolve(spec_with([{"sqlite.query": {"db": str(db)}}]))[0]
    assert json.loads(t.run({"sql": "select a from t"})) == [{"a": 1}]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        t.run({"sql": "insert into t values (2)"})


def test_http_get_respects_allow_hosts_and_the_url_guard(monkeypatch):
    monkeypatch.delenv("AGENTL_ALLOW_PRIVATE_NETWORK", raising=False)  # conftest allows it for local servers
    monkeypatch.delenv("AGENTL_ALLOW_HOSTS", raising=False)
    t = resolve(spec_with([{"http.get": {"allow_hosts": ["example.com"]}}]))[0]
    assert t.run({"url": "https://evil.test/x"}).startswith("error:")
    t = resolve(spec_with(["http.get"]))[0]
    from agentl.netguard import BlockedURL

    with pytest.raises(BlockedURL):
        t.run({"url": "http://127.0.0.1:9/"})


def test_spec_errors():
    with pytest.raises(ValueError, match="needs an mcp server named 'crm'"):
        spec_with(["mcp:crm"])
    with pytest.raises(ValueError, match="one record per call"):
        spec_with(["http.get"], records_per_call=5)
    with pytest.raises(ValueError, match="exactly one of 'command' or 'url'"):
        MCPServerSpec()


# ---------------------------------------------------------------- runner
class ToolBot:
    """Calls every tool it is given once per record (with args from `PLAN`), then answers."""

    PLAN: ClassVar[dict] = {}

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        raise AssertionError("tools path expected")

    def structured_tools(self, instruction, record, schema, tools, call, max_calls):
        outs = []
        for t in tools[:max_calls]:
            outs.append(call(t["name"], self.PLAN.get(t["name"], {}) | {"domain": record["domain"]}))
        return Result(schema.model_validate({"sector": outs[0] if outs else None}), 100, 10)

    def text(self, instruction, content):
        return ""


register_provider("toolbot", ToolBot)


def rows(input=None):
    return [{"domain": "acme.com"}, {"domain": "beta.io"}, {"domain": "nope"}]


def enrich(budget=None, **step):
    spec = {
        "name": "enrich",
        "llm": {"provider": "toolbot", "model": "m"},
        "mcp": {"crm": {"command": FAKE}},
        "steps": [
            {"id": "src", "fn": "tests.test_tools:rows"},
            {
                "id": "enrich",
                "agent": "Find the sector.",
                "input": "src",
                "tools": ["mcp:crm"],
                "output": {"sector": "str?"},
                "concurrency": 1,
                **step,
            },
        ],
    }
    if budget:
        spec["budget"] = budget
    return parse(spec)


def test_runner_calls_tools_logs_events_and_counts_spend(tmp_path):
    with Store(tmp_path / "s.db") as store:
        r = Runner(enrich(), store=store).run()
        events = [e for e in store.events(r.run_id) if e["type"] == "tool.called"]
    assert r.status == "succeeded", r.error
    assert [x["sector"] for x in r.outputs["enrich"]] == ["software", "logistics", "error: unknown domain"]
    assert r.tool_calls == 3 and r.spend["enrich"]["tool_calls"] == 3
    assert [e["data"]["tool"] for e in events] == ["crm.company"] * 3
    assert [e["data"]["ok"] for e in events] == [True, True, False]
    assert json.loads(events[0]["data"]["args"]) == {"domain": "acme.com"}


def test_tool_call_budget_stops_the_run(tmp_path):
    with Store(tmp_path / "s.db") as store:
        r = Runner(enrich(budget={"max_tool_calls": 2}), store=store, use_cache=False).run()
        types_ = [e["type"] for e in store.events(r.run_id)]
    assert r.status == "failed" and "max_tool_calls 2" in r.error
    assert "budget.exceeded" in types_ and r.tool_calls == 2


def test_tools_are_part_of_the_cache_key(tmp_path):
    with Store(tmp_path / "s.db") as store:
        Runner(enrich(), store=store).run()
        again = Runner(enrich(), store=store).run()
        other = Runner(enrich(tools=["mcp:crm/company"]), store=store).run()
    assert again.tool_calls == 0  # same tools: cached
    assert other.tool_calls == 3  # different tools: asked again


def test_provider_without_tools_fails_clearly(tmp_path):
    spec = parse(
        {
            "name": "x",
            "llm": {"provider": "echo", "model": "m"},
            "steps": [
                {"id": "a", "agent": "A", "tools": ["fn:tests.tooling.tools:lookup"], "output": {"o": "str?"}}
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
    assert r.status == "failed" and "does not support tools" in r.error


# ---------------------------------------------------------------- provider loops
def test_openai_compatible_tool_loop():
    from agentl.llm import OpenAICompatibleProvider
    from agentl.spec import LLMSpec

    sent = []
    replies = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"code": "X1"}'},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5},
        },
        {
            "choices": [{"message": {"role": "assistant", "content": '{"name": "product X1"}'}}],
            "usage": {"prompt_tokens": 70, "completion_tokens": 8},
        },
    ]
    prov = OpenAICompatibleProvider(
        LLMSpec(provider="openai-compatible", model="m", options={"base_url": "http://x"})
    )
    prov._http = lambda method, path, data=None, ctype="": (
        sent.append(json.loads(data)),
        json.dumps(replies.pop(0)).encode(),
    )[1]
    calls = []
    res = prov.structured_tools(
        "Name it.",
        {"code": "X1"},
        schema_model("o", {"name": "str"}),
        [{"name": "lookup", "description": "d", "input_schema": {"type": "object"}}],
        lambda n, a: (calls.append((n, a)), "product X1")[1],
        4,
    )
    assert res.value.name == "product X1" and (res.input_tokens, res.output_tokens) == (120, 13)
    assert calls == [("lookup", {"code": "X1"})]
    assert sent[0]["tools"][0]["function"]["name"] == "lookup"
    assert sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "product X1"}
    assert "untrusted data as well" in sent[0]["messages"][0]["content"]


def test_anthropic_tool_loop_and_call_limit(monkeypatch):
    sent = []

    def block(**kw):
        return types.SimpleNamespace(**kw, model_dump=lambda exclude_none=True, kw=kw: kw)

    usage = types.SimpleNamespace(
        input_tokens=10, output_tokens=2, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    replies = [
        types.SimpleNamespace(
            stop_reason="tool_use",
            usage=usage,
            content=[
                block(type="tool_use", id="t1", name="lookup", input={"code": "A"}),
                block(type="tool_use", id="t2", name="lookup", input={"code": "B"}),
            ],
        ),
        types.SimpleNamespace(
            stop_reason="end_turn", usage=usage, content=[block(type="text", text='{"name": "A"}')]
        ),
    ]

    class Messages:
        def create(self, **kw):
            sent.append(json.loads(json.dumps(kw["messages"], default=str)))
            return replies.pop(0)

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda: types.SimpleNamespace(messages=Messages())
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    from agentl.llm import provider
    from agentl.spec import LLMSpec

    prov = provider(LLMSpec())
    calls = []
    res = prov.structured_tools(
        "Name it.",
        {"code": "A"},
        schema_model("o", {"name": "str"}),
        [{"name": "lookup", "description": "d", "input_schema": {"type": "object"}}],
        lambda n, a: (calls.append(a["code"]), f"product {a['code']}")[1],
        1,  # only one call allowed: the second tool_use gets the limit note
    )
    assert res.value.name == "A" and res.input_tokens == 20
    assert calls == ["A"]
    results = sent[1][-1]["content"]
    assert results[0]["content"] == "product A" and results[1]["is_error"] is True
    assert "limit" in results[1]["content"]
