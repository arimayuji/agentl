"""Cloud integrations (#29): secret references and the Azure OpenAI provider."""

import base64
import http.server
import json
import threading
import types
from typing import ClassVar

import pytest

from agentl import parse, registry, secrets
from agentl.llm import provider, schema_model
from agentl.spec import LLMSpec, SpecError, load


class Cloud(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list] = []

    def log_message(self, *a):
        pass

    def _reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Cloud.seen.append((self.path, dict(self.headers)))
        if self.path == "/v1/projects/acme/secrets/pg_dsn/versions/latest:access":
            assert self.headers["Authorization"] == "Bearer g-token"
            data = base64.b64encode(b"postgresql://gcp").decode()
            return self._reply(200, {"payload": {"data": data}})
        if self.path == "/kv/secrets/pg-dsn?api-version=7.4":
            assert self.headers["Authorization"] == "Bearer az-token"
            return self._reply(200, {"value": '{"user": "etl", "password": "p@ss"}'})
        self._reply(404, {"error": "nope"})

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(n))
        Cloud.seen.append((self.path, dict(self.headers), body))
        if self.path == "/openai/v1/chat/completions":
            if self.headers.get("api-key") != "az-key":
                return self._reply(401, {"error": "bad key"})
            content = json.dumps({"category": "billing"})
            return self._reply(
                200,
                {
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 5},
                },
            )
        self._reply(404, {"error": "nope"})


@pytest.fixture
def cloud(monkeypatch):
    Cloud.seen = []
    secrets._CACHE.clear()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Cloud)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "g-token")
    monkeypatch.setenv("AGENTL_GCP_SECRETS_API", f"{base}/v1")
    monkeypatch.setenv("AZURE_ACCESS_TOKEN", "az-token")
    monkeypatch.setenv("AGENTL_AZURE_KV_URL", base + "/{vault}")
    yield base
    srv.shutdown()
    srv.server_close()


class FakeBoto:
    calls: ClassVar[list] = []

    @staticmethod
    def client(name):
        FakeBoto.calls.append(name)
        if name == "secretsmanager":
            return types.SimpleNamespace(
                get_secret_value=lambda SecretId: {
                    "SecretString": {"prod/pg_dsn": "postgresql://aws", "prod/db": '{"password": "s3"}'}[
                        SecretId
                    ]
                }
            )
        return types.SimpleNamespace(
            get_parameter=lambda Name, WithDecryption: {
                "Parameter": {"Value": f"ssm:{Name}:{WithDecryption}"}
            }
        )


@pytest.fixture
def aws(monkeypatch):
    FakeBoto.calls = []
    real = registry.require
    monkeypatch.setattr(registry, "require", lambda m, extra: FakeBoto if m == "boto3" else real(m, extra))


def spec_with(value):
    return parse(
        {
            "name": "x",
            "steps": [{"id": "a", "use": "sqlite.read", "with": {"db": value, "query": "select 1"}}],
        }
    )


def test_every_secret_kind_resolves(cloud, aws, monkeypatch):
    monkeypatch.setenv("AGENTL_TEST_PLAIN", "plain")
    s = parse(
        {
            "name": "x",
            "steps": [
                {
                    "id": "a",
                    "use": "sqlite.read",
                    "with": {
                        "aws": "${aws-sm:prod/pg_dsn}",
                        "aws_key": "${aws-sm:prod/db#password}",
                        "ssm": "${ssm:/prod/pg}",
                        "gcp": "${gcp-sm:acme/pg_dsn}",
                        "az": "postgresql://etl:${azure-kv:kv/pg-dsn#password}@db/x",
                        "env": "${AGENTL_TEST_PLAIN}",
                    },
                }
            ],
        }
    )
    w = s.steps[0].with_
    assert w["aws"] == "postgresql://aws" and w["aws_key"] == "s3" and w["ssm"] == "ssm:/prod/pg:True"
    assert w["gcp"] == "postgresql://gcp" and w["az"] == "postgresql://etl:p@ss@db/x" and w["env"] == "plain"


def test_secrets_are_fetched_once_per_process(cloud, aws):
    for _ in range(3):
        spec_with("${aws-sm:prod/pg_dsn}")
    assert FakeBoto.calls == ["secretsmanager"]


def test_secret_errors_name_the_reference(cloud, aws):
    with pytest.raises(SpecError, match=r"could not read secret '\$\{aws-sm:prod/db#user\}'.*no key 'user'"):
        spec_with("${aws-sm:prod/db#user}")
    with pytest.raises(SpecError, match="use project/secret"):
        spec_with("${gcp-sm:just-a-name}")


def test_secret_value_never_reaches_the_run_store(cloud, aws, tmp_path):
    from agentl.runner import Runner
    from agentl.store import Store

    db = tmp_path / "d.db"
    import sqlite3

    sqlite3.connect(db).execute("create table t (v)").connection.commit()
    (tmp_path / "p.yaml").write_text(
        "name: x\nsteps:\n  - id: a\n    use: sqlite.read\n"
        f"    with: {{db: '{db}', query: \"select '${{aws-sm:prod/pg_dsn}}' as v\"}}\n"
    )
    with Store(tmp_path / "s.db") as store:
        r = Runner(load(tmp_path / "p.yaml"), store=store).run()
    assert r.status == "succeeded" and r.outputs["a"] == [{"v": "postgresql://aws"}]
    stored = b"".join(p.read_bytes() for p in tmp_path.glob("s.db*"))
    assert b"postgresql://aws" not in stored  # events and run rows never carry step settings


def test_azure_openai_provider(cloud, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    p = provider(LLMSpec(provider="azure-openai", model="gpt-prod", options={"endpoint": cloud}))
    res = p.structured("Classify.", {"text": "refund"}, schema_model("Out", {"category": "str"}))
    assert res.value.category == "billing" and res.input_tokens == 50
    path, headers, body = Cloud.seen[-1]
    assert (
        path == "/openai/v1/chat/completions"
        and body["model"] == "gpt-prod"
        and "Authorization" not in headers
    )
    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    with pytest.raises(ValueError, match="AZURE_OPENAI_API_KEY"):
        provider(LLMSpec(provider="azure-openai", model="gpt-prod", options={"endpoint": cloud}))
