"""AWS Lambda: invoke a function once, or once per upstream record. Needs `agentl-etl[aws]`;
credentials come from the standard AWS chain (env vars, profile, role)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..registry import connector, require
from .http import _as_records, _fill

INVOCATION_TYPES = ("RequestResponse", "Event")


@connector("aws.lambda")
def aws_lambda(
    input: Any = None,
    function: str = "",
    payload: Any = None,
    items: str | None = None,
    invocation_type: str = "RequestResponse",
    qualifier: str | None = None,
    region: str | None = None,
    endpoint_url: str | None = None,
    concurrency: int = 4,
    keep_input: bool = False,
) -> list[dict]:
    """Invoke `function` with `payload` (JSON). With upstream records it runs once per record and
    fills `{field}` placeholders in the payload strings (`payload` defaults to the record itself).

    The JSON response becomes records: a list as is, an object as one record (or the list under
    `items`, a dotted path), anything else as `{"value": value}`. With `keep_input`, each output
    record also carries the input record's fields. `invocation_type: Event` fires and forgets and
    returns `{status_code}`. A function error raises with its message."""
    if invocation_type not in INVOCATION_TYPES:
        raise ValueError(f"invocation_type must be one of {INVOCATION_TYPES}")
    boto3 = require("boto3", "aws")
    client = boto3.client("lambda", region_name=region, endpoint_url=endpoint_url)

    def call(rec: dict | None) -> list[dict]:
        body = payload if rec is None else (_fill(payload, rec) if payload is not None else rec)
        args: dict[str, Any] = {
            "FunctionName": function,
            "InvocationType": invocation_type,
            "Payload": json.dumps(body if body is not None else {}, default=str).encode(),
        }
        if qualifier:
            args["Qualifier"] = qualifier
        resp = client.invoke(**args)
        if invocation_type == "Event":
            out = [{"status_code": resp.get("StatusCode")}]
        else:
            raw = resp["Payload"].read()
            data = json.loads(raw) if raw else None
            if resp.get("FunctionError"):
                msg = data.get("errorMessage", data) if isinstance(data, dict) else data
                raise RuntimeError(f"lambda {function} failed ({resp['FunctionError']}): {msg}")
            if data is None or isinstance(data, (str, int, float, bool)):
                out = [{"value": data}]
            else:
                out = _as_records(data, items)
        return [{**rec, **r} for r in out] if keep_input and rec else out

    if not isinstance(input, list):
        return call(None)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        return [r for batch in pool.map(call, input) for r in batch]
