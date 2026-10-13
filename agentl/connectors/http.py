"""HTTP connectors (stdlib only). URLs accept {field} placeholders filled
from each upstream record, so one request is made per record."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
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
            if (e.code < 500 and e.code != 429) or attempt == retries:
                raise
        except urllib.error.URLError:
            if attempt == retries:
                raise
        time.sleep(2**attempt)


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


def _dig(data: Any, path: str) -> Any:
    for part in path.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data


def _with_query(url: str, params: dict | None) -> str:
    if not params:
        return url
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
    return f"{url}{'&' if '?' in url else '?'}{query}" if query else url


def _pages(
    method: str,
    url: str,
    params: dict | None,
    paginate: dict | None,
    headers: dict,
    body: Any,
    items: str | None,
    timeout: float,
    retries: int,
) -> list[dict]:
    """One request, or every page: `paginate: {param: page, start: 1, next: next_page, max_pages: 1000}`.
    Stops when the `next` field of the response is empty, or when a page has no items."""
    if not paginate:
        return _as_records(_request(method, _with_query(url, params), headers, body, timeout, retries), items)
    param = paginate.get("param", "page")
    page = paginate.get("start", 1)
    out: list[dict] = []
    for _ in range(int(paginate.get("max_pages", 1000))):
        data = _request(
            method, _with_query(url, {**(params or {}), param: page}), headers, body, timeout, retries
        )
        rows = _as_records(data, items) if data else []
        out.extend(rows)
        nxt = paginate.get("next")
        if not rows or (nxt and not _dig(data, nxt)):
            return out
        page += 1
    raise RuntimeError(f"pagination stopped after max_pages={paginate.get('max_pages', 1000)}")


def _http(
    method: str,
    input: Any,
    url: str,
    headers: dict | None,
    json_body: Any,
    items: str | None,
    concurrency: int,
    timeout: float,
    retries: int,
    params: dict | None = None,
    paginate: dict | None = None,
) -> list[dict]:
    headers = headers or {}
    upstream = single(input)
    if not upstream:
        return _pages(method, url, params, paginate, headers, json_body, items, timeout, retries)

    def one(rec: dict) -> list[dict]:
        return _pages(
            method,
            url.format(**rec),
            _fill(params, rec),
            paginate,
            headers,
            _fill(json_body, rec),
            items,
            timeout,
            retries,
        )

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return [r for batch in pool.map(one, upstream) for r in batch]


@connector("http.get")
def http_get(
    input: Any = None,
    url: str = "",
    headers: dict | None = None,
    items: str | None = None,
    concurrency: int = 4,
    timeout: float = 30,
    retries: int = 2,
    params: dict | None = None,
    paginate: dict | None = None,
) -> list[dict]:
    return _http("GET", input, url, headers, None, items, concurrency, timeout, retries, params, paginate)


@connector("http.post")
def http_post(
    input: Any = None,
    url: str = "",
    headers: dict | None = None,
    json: Any = None,
    items: str | None = None,
    concurrency: int = 4,
    timeout: float = 30,
    retries: int = 2,
    params: dict | None = None,
    paginate: dict | None = None,
) -> list[dict]:
    return _http("POST", input, url, headers, json, items, concurrency, timeout, retries, params, paginate)
