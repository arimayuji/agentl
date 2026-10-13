import pytest

from agentl import parse
from agentl.llm import provider, schema_model
from agentl.runner import Runner
from agentl.spec import LLMSpec
from agentl.store import Store

boto3 = pytest.importorskip("boto3")
from botocore.stub import ANY, Stubber  # noqa: E402

MODEL = "us.anthropic.claude-test-v1:0"


@pytest.fixture
def stub(monkeypatch):
    client = boto3.client(
        "bedrock-runtime", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )
    stubber = Stubber(client)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    with stubber:
        yield stubber


def tool_reply(stubber, data, tokens=(11, 4)):
    stubber.add_response(
        "converse",
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"toolUse": {"toolUseId": "t1", "name": "record_output", "input": data}}],
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1], "totalTokens": sum(tokens)},
            "metrics": {"latencyMs": 5},
        },
        {
            "modelId": MODEL,
            "messages": ANY,
            "inferenceConfig": {"maxTokens": 4096, "temperature": 0},
            "system": ANY,
            "toolConfig": ANY,
        },
    )


def test_structured_output_via_forced_tool(stub):
    tool_reply(stub, {"category": "food", "flag": None})
    prov = provider(LLMSpec(provider="bedrock", model=MODEL, options={"temperature": 0}))
    out = prov.structured(
        "Classify.", {"d": "lunch"}, schema_model("o", {"category": "str", "flag": "bool?"})
    )
    assert out.value.model_dump() == {"category": "food", "flag": None}
    assert (out.input_tokens, out.output_tokens) == (11, 4)


def test_model_is_required():
    with pytest.raises(ValueError, match="Bedrock model"):
        provider(LLMSpec(provider="bedrock"))


def test_invalid_output_is_rejected_in_a_pipeline(stub, tmp_path):
    # first record: valid; second: wrong type twice (one retry) -> rejected
    tool_reply(stub, {"ok": True})
    tool_reply(stub, {"ok": "maybe"})
    tool_reply(stub, {"ok": "maybe"})
    spec = parse(
        {
            "name": "b",
            "llm": {"provider": "bedrock", "model": MODEL, "options": {"temperature": 0}},
            "steps": [
                {"id": "src", "fn": "tests.test_bedrock:rows"},
                {"id": "check", "agent": "Check.", "input": "src", "output": {"ok": "bool"}},
            ],
        }
    )
    with Store(tmp_path / "db.sqlite") as store:
        result = Runner(spec, store=store, use_cache=False).run()
    assert result.status == "succeeded", result.error
    assert result.outputs["check"] == [{"i": 0, "ok": True}]
    assert len(result.rejected["check"]) == 1
    assert result.input_tokens == 11 * 3


def rows(input=None):
    return [{"i": 0}, {"i": 1}]
