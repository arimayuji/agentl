import io
import json

import pytest

from agentl.registry import get

boto3 = pytest.importorskip("boto3")
from botocore.response import StreamingBody  # noqa: E402
from botocore.stub import ANY, Stubber  # noqa: E402


@pytest.fixture
def stub(monkeypatch):
    client = boto3.client("lambda", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    stubber = Stubber(client)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    with stubber:
        yield stubber


def reply(stubber, body, expected_payload=ANY, error=None, function="fn"):
    raw = json.dumps(body).encode()
    resp = {"StatusCode": 200, "Payload": StreamingBody(io.BytesIO(raw), len(raw))}
    if error:
        resp["FunctionError"] = error
    stubber.add_response(
        "invoke",
        resp,
        {"FunctionName": function, "InvocationType": "RequestResponse", "Payload": expected_payload},
    )


def test_single_call_with_items(stub):
    reply(stub, {"data": {"rows": [{"id": 1}, {"id": 2}]}}, json.dumps({"month": "2026-10"}).encode())
    out = get("aws.lambda")(None, function="fn", payload={"month": "2026-10"}, items="data.rows")
    assert out == [{"id": 1}, {"id": 2}]


def test_per_record_templating_and_keep_input(stub):
    reply(stub, {"total": 10}, json.dumps({"customer": "c1", "note": "id c1"}).encode())
    reply(stub, {"total": 20}, json.dumps({"customer": "c2", "note": "id c2"}).encode())
    out = get("aws.lambda")(
        [{"id": "c1"}, {"id": "c2"}],
        function="fn",
        payload={"customer": "{id}", "note": "id {id}"},
        keep_input=True,
        concurrency=1,
    )
    assert out == [{"id": "c1", "total": 10}, {"id": "c2", "total": 20}]


def test_record_is_the_default_payload_and_scalars(stub):
    reply(stub, 42, json.dumps({"x": 1}).encode())
    assert get("aws.lambda")([{"x": 1}], function="fn") == [{"value": 42}]


def test_function_error_raises(stub):
    reply(stub, {"errorMessage": "bad month", "errorType": "ValueError"}, error="Unhandled")
    with pytest.raises(RuntimeError, match="bad month"):
        get("aws.lambda")(None, function="fn", payload={})


def test_async_event(stub):
    stub.add_response(
        "invoke", {"StatusCode": 202}, {"FunctionName": "fn", "InvocationType": "Event", "Payload": ANY}
    )
    assert get("aws.lambda")(None, function="fn", invocation_type="Event") == [{"status_code": 202}]
