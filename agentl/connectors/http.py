"""HTTP connectors (stdlib only). URLs accept {field} placeholders filled
from each upstream record, so one request is made per record."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..registry import connector
from ._util import single


def _request(method: str, url: str, headers: dict, body: Any, timeout: float, retries: int) -> Any:
    data = None
    headers = {"Accept": "application/json", **headers}
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {"text": raw}
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429 or attempt == retries:
                raise
        except urllib.error.URLError:
            if attempt == retries:
                raise
        time.sleep(2 ** attempt)


def _fill(value: Any, rec: dict) -> Any:
    """Fill {field} placeholders inside string values of a JSON body."""
    if isinstance(value, str):
        return value.format(**rec)
    if isinstance(value, list):
        return [_fill(v, rec) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, rec) for k, v in value.items()}
    return value


def _as_records(data: Any, items: str | None) -> list[dict]:
    if items:
        for part in items.split("."):
            data = data[part]
    if isinstance(data, list):
        return [d if isinstance(d, dict) else {"value": d} for d in data]
    return [data if isinstance(data, dict) else {"value": data}]


def _http(method: str, input: Any, url: str, headers: dict | None, json_body: Any, items: str | None,
          concurrency: int, timeout: float, retries: int) -> list[dict]:
    headers = headers or {}
    upstream = single(input)
    if not upstream:
        return _as_records(_request(method, url, headers, json_body, timeout, retries), items)

    def one(rec: dict) -> list[dict]:
        body = _fill(json_body, rec)
        return _as_records(_request(method, url.format(**rec), headers, body, timeout, retries), items)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return [r for batch in pool.map(one, upstream) for r in batch]


@connector("http.get")
def http_get(input: Any = None, url: str = "", headers: dict | None = None, items: str | None = None,
             concurrency: int = 4, timeout: float = 30, retries: int = 2) -> list[dict]:
    return _http("GET", input, url, headers, None, items, concurrency, timeout, retries)


@connector("http.post")
def http_post(input: Any = None, url: str = "", headers: dict | None = None, json: Any = None,
              items: str | None = None, concurrency: int = 4, timeout: float = 30, retries: int = 2) -> list[dict]:
    return _http("POST", input, url, headers, json, items, concurrency, timeout, retries)
