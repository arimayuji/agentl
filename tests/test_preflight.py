"""agentl validate --connect and agentl plan (#118, part 2)."""

import http.server
import threading

import pytest

from agentl import parse, register_provider
from agentl.cli import main
from agentl.llm import Result
from agentl.preflight import connect, plan
from agentl.runner import Runner
from agentl.store import Store

CALLS = []


class Counting:
    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        CALLS.append(record)
        return Result(schema.model_validate({"tag": "x"}), 100, 10)

    def text(self, *a):
        CALLS.append("text")
        return "OK"


register_provider("plan-fake", Counting)


def ran(input=None):
    raise AssertionError("fn steps must not run in a plan")


def spec(tmp_path, **llm):
    (tmp_path / "in.csv").write_text("id,text\n1,a\n2,b\n3,c\n")
    return parse(
        {
            "name": "p",
            "llm": {"provider": "plan-fake", "model": "m", "price": {"input": 1, "output": 2}, **llm},
            "steps": [
                {"id": "rows", "use": "csv.read", "with": {"path": str(tmp_path / "in.csv")}},
                {"id": "some", "use": "select", "input": "rows", "with": {"fields": ["id", "text"]}},
                {"id": "tag", "input": "some", "agent": "Tag.", "output": {"tag": "str"}},
                {"id": "code", "fn": "tests.test_preflight:ran", "input": "tag"},
                {
                    "id": "save",
                    "use": "json.write",
                    "input": "tag",
                    "with": {"path": str(tmp_path / "out.json")},
                },
                {"id": "queue", "use": "sqs.receive", "with": {"queue_url": "https://sqs.x/q"}},
            ],
        }
    )


def test_plan_reads_sources_estimates_agents_and_changes_nothing(tmp_path):
    CALLS.clear()
    p = spec(tmp_path)
    with Store(tmp_path / "s.db") as store:
        first = plan(p, store)
        by = {s.step: s for s in first.steps}
        assert (by["rows"].status, by["rows"].records) == ("read", 3)
        assert (by["some"].status, by["some"].records) == ("ran", 3)
        assert (by["tag"].llm_calls, by["tag"].cache_hits) == (3, 0)
        low, high = by["tag"].cost_usd
        assert 0 < low < high
        assert by["code"].status == "skipped" and by["save"].status == "writes"
        assert by["queue"].status == "skipped" and "may change state" in by["queue"].detail
        assert CALLS == [] and not (tmp_path / "out.json").exists()

        p2 = parse(
            {
                **p.model_dump(by_alias=True),
                "steps": [s.model_dump(by_alias=True, exclude_none=True) for s in p.steps[:3]],
            }
        )
        Runner(p2, store=store).run()
        CALLS.clear()
        again = {s.step: s for s in plan(p, store).steps}
        assert (again["tag"].cache_hits, again["tag"].llm_calls, again["tag"].cost_usd) == (3, 0, (0.0, 0.0))
        assert CALLS == []
    assert "total: 3 LLM call(s)" in first.text()


def test_plan_without_a_price_says_cost_unknown(tmp_path):
    p = spec(tmp_path)
    p.llm["default"].price = None
    with Store(tmp_path / "s.db") as store:
        out = plan(p, store)
    assert out.cost is None and "cost unknown (no price)" in out.text()


@pytest.fixture
def site():
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(401 if "private" in self.path else 200)
            self.end_headers()
            self.wfile.write(b"x")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_connect_checks_each_system_without_printing_secrets(tmp_path, site, monkeypatch, capsys):
    monkeypatch.setenv("PG_DSN", "postgresql://user:hunter2@127.0.0.1:1/db")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "p.yaml").write_text(
        f"""name: c
llm:
  default: {{ provider: anthropic, model: claude-x }}
steps:
  - id: page
    use: http.get
    with: {{ url: "{site}/public" }}
  - id: private
    use: http.get
    with: {{ url: "{site}/private" }}
  - id: missing
    use: csv.read
    with: {{ path: nope.csv }}
  - id: tag
    input: page
    agent: Tag.
    output: {{ tag: str }}
  - id: save
    use: postgres.write
    input: tag
    with: {{ dsn: "${{PG_DSN}}", table: t }}
"""
    )
    checks = {c.what.split(" ")[0] + ":" + c.status for c in connect(tmp_path / "p.yaml", timeout=2)}
    assert f"{site}/public:ok" in checks and f"{site}/private:fail" in checks
    assert "csv.read:fail" in checks and "llm:fail" in checks
    assert main(["validate", str(tmp_path / "p.yaml"), "--connect"]) == 2
    out = capsys.readouterr().out
    assert "hunter2" not in out and "ANTHROPIC_API_KEY is not set" in out and "credentials refused" in out


def test_connect_reports_unresolved_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_VAR", raising=False)
    (tmp_path / "p.yaml").write_text(
        "name: c\nsteps:\n  - id: a\n    use: csv.read\n    with: { path: '${NOPE_VAR}' }\n"
    )
    checks = connect(tmp_path / "p.yaml")
    assert checks[0].what == "configuration" and checks[0].status == "fail"
