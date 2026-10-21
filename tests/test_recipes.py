"""Every recipe in examples/recipes runs offline: a local server stands in for the APIs and sites,
and a stand-in model answers for the LLMs (#128). Each recipe must also lint without errors."""

import http.server
import json
import shutil
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from agentl import lint, load, register_provider
from agentl.llm import Result
from agentl.runner import Runner
from agentl.store import Store

RECIPES = sorted(p for p in (Path(__file__).parent.parent / "examples" / "recipes").iterdir() if p.is_dir())
NEEDS = {
    "03-pdf-invoices-to-excel": ["pypdf", "openpyxl"],
    "04-contracts-summary": ["docx"],
    "05-api-pages-to-database": ["sqlalchemy"],
    "06-incremental-api-load": ["sqlalchemy"],
    "07-web-page-to-markdown": ["trafilatura"],
    "09-excel-cleanup": ["openpyxl"],
    "16-daily-report": ["openpyxl"],
    "21-nfe-to-excel": ["openpyxl"],
}
_SAMPLE = {"str": "example", "int": 1, "float": 0.9, "bool": False, "list": [], "dict": {}}


class StandIn:
    """Answers every field with a value of its type; never calls a tool."""

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        fields = schema.model_json_schema().get("properties", {})
        if "items" in fields:  # several records per call
            items = [{"index": r["index"], **self._one(schema, r)} for r in record["records"]]
            return Result(schema.model_validate({"items": items}), 50, 10)
        return Result(schema.model_validate(self._one(schema, record)), 50, 10)

    def _one(self, schema, record):
        props = schema.model_json_schema().get("properties", {})
        if "items" in props:
            item = props["items"].get("items", {}).get("properties", {})
            props = {k: v for k, v in item.items() if k != "index"}
        out = {}
        for name, prop in props.items():
            kinds = [prop.get("type")] + [a.get("type") for a in prop.get("anyOf", [])]
            kind = next((k for k in kinds if k and k != "null"), "string")
            out[name] = {"string": "example", "integer": 1, "number": 0.9, "boolean": False}.get(
                kind, [] if kind == "array" else {}
            )
        return out

    def structured_tools(self, instruction, record, schema, tools, call, max_calls):
        return self.structured(instruction, record, schema)

    def text(self, instruction, content):
        return "ok"


register_provider("recipe-stand-in", StandIn)


@pytest.fixture
def server():
    root: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            name = url.path.strip("/")
            base = root["dir"]
            files = [
                base / "pages" / name,
                base / "api" / f"{name}.page{q.get('page', 1)}.json",
                base / "api" / f"{name}.json",
            ]
            path = next((f for f in files if f.is_file()), None)
            if path is None:
                self.send_response(404)
                self.end_headers()
                return
            body = path.read_bytes()
            if "since" in q and path.suffix == ".json":
                rows = [r for r in json.loads(body) if str(r.get("updated_at", "")) > q["since"]]
                body = json.dumps(rows).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if path.suffix == ".html" else "application/json")
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield root
    srv.shutdown()
    srv.server_close()


def _stand_in(spec):
    for llm in spec.llm.values():
        llm.provider, llm.options = "recipe-stand-in", {}
    return spec


@pytest.mark.parametrize("recipe", RECIPES, ids=[r.name for r in RECIPES])
def test_recipe_runs_offline(recipe, tmp_path, server, monkeypatch):
    for module in NEEDS.get(recipe.name, []):
        pytest.importorskip(module)
    work = tmp_path / recipe.name
    shutil.copytree(recipe, work)
    monkeypatch.chdir(work)
    server["dir"] = work
    monkeypatch.setenv("API_URL", server["url"])
    monkeypatch.setenv("SITE_URL", server["url"])
    monkeypatch.delenv("DATABASE_URL", raising=False)

    report = lint.check_file(work / "pipeline.yaml")
    assert not report.errors, report.render()

    spec = _stand_in(load(work / "pipeline.yaml"))
    trigger = (
        [{"name": "Ana", "email": "ana@example.com"}] if spec.trigger and spec.trigger[0].webhook else None
    )
    with Store(tmp_path / "runs.db") as store:
        result = Runner(spec, store=store, trigger=trigger).run()
        assert result.status == "succeeded", result.error
        last = spec.steps[-1].id
        assert result.outputs[last], f"{recipe.name}: last step '{last}' produced nothing"
        if recipe.name == "06-incremental-api-load":
            again = Runner(_stand_in(load(work / "pipeline.yaml")), store=store).run()
            assert again.status == "succeeded" and len(again.outputs["customers"]) == 0


def test_every_recipe_is_listed_in_the_docs():
    page = (Path(__file__).parent.parent / "docs" / "recipes.md").read_text()
    for recipe in RECIPES:
        assert f"recipes/{recipe.name}/" in page, recipe.name
