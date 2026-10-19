"""HTTP connectors (stdlib only). URLs accept {field} placeholders filled
from each upstream record, so one request is made per record."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import netguard
from ..registry import NoChange, after_run, connector, step_state
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
            with netguard.urlopen(req, timeout=timeout) as resp:
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


def _parse(raw: bytes) -> Any:
    text = raw.decode()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


# ---------------------------------------------------------------- if_changed
_STRATEGIES = ("page_stamp", "content_length", "etag", "last_modified", "hash")


def _open(req: urllib.request.Request, timeout: float, retries: int) -> tuple[int, Any, bytes]:
    """(status, headers, body) with retries on network errors, 429 and 5xx; a 304 is returned."""
    for attempt in range(retries + 1):
        try:
            with netguard.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, e.headers, b""
            if (e.code < 500 and e.code != 429) or attempt == retries:
                raise
        except urllib.error.URLError:
            if attempt == retries:
                raise
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def _changed_get(
    url: str,
    headers: dict,
    items: str | None,
    timeout: float,
    retries: int,
    if_changed: dict,
    on_unchanged: str,
) -> list[dict]:
    body = conditional_get(
        url, {"Accept": "application/json", **headers}, timeout, retries, if_changed, on_unchanged
    )
    return [] if body is None else _as_records(_parse(body), items)


def conditional_get(
    url: str,
    headers: dict,
    timeout: float,
    retries: int,
    if_changed: dict,
    on_unchanged: str,
) -> bytes | None:
    """The body of `url`, or None (`on_unchanged: continue_empty`) when it did not change since
    the last successful run; `on_unchanged: stop` raises NoChange instead. Shared by `http.get`
    and the file readers that take a `url`.

    GET `url` only when it changed since the last successful run. Strategies run cheapest
    first: `page_stamp` (a regex on another, small page), `content_length` (a HEAD request),
    `etag` / `last_modified` (a conditional GET answered with 304), then a hash of the body,
    which is always the last resort. The validators are kept in the run store (`agentl state`)
    and saved only when the run succeeds."""
    if on_unchanged not in ("stop", "continue_empty"):
        raise ValueError("on_unchanged must be stop or continue_empty")
    by = if_changed.get("by") or ["etag", "last_modified"]
    by = [by] if isinstance(by, str) else list(by)
    stamp_cfg = if_changed.get("page_stamp")
    if stamp_cfg and "page_stamp" not in by:
        by.insert(0, "page_stamp")
    unknown = [b for b in by if b not in _STRATEGIES]
    if unknown:
        raise ValueError(f"if_changed.by: unknown strategy {unknown[0]!r}; use {', '.join(_STRATEGIES)}")
    state = step_state()
    saved: dict = dict((state.get() if state else None) or {})
    new: dict = {}

    def unchanged(reason: str) -> bytes | None:
        if on_unchanged == "stop":
            raise NoChange(f"{url}: {reason}")
        return None

    def save() -> None:
        if state is not None:
            value = {**saved, **new}
            after_run(lambda: state.set(value))

    if "page_stamp" in by:
        if not isinstance(stamp_cfg, dict) or not stamp_cfg.get("regex"):
            raise ValueError("if_changed.page_stamp needs {url, regex}")
        req = urllib.request.Request(stamp_cfg.get("url") or url, headers={"Accept": "*/*"})
        _, _, page = _open(req, timeout, retries)
        match = re.search(stamp_cfg["regex"], page.decode(errors="replace"))
        if match is None:
            raise RuntimeError(f"if_changed.page_stamp: regex found nothing on {req.full_url}")
        new["page_stamp"] = match.group(1) if match.groups() else match.group(0)
        if saved.get("page_stamp") == new["page_stamp"]:
            return unchanged(f"page stamp {new['page_stamp']!r} unchanged")
    if "content_length" in by:
        _, head, _ = _open(urllib.request.Request(url, headers=headers, method="HEAD"), timeout, retries)
        length = head.get("Content-Length")
        if length is not None:
            new["content_length"] = int(length)
            if saved.get("content_length") == new["content_length"]:
                return unchanged(f"content length {length} unchanged")
    conditional = dict(headers)
    if "etag" in by and saved.get("etag"):
        conditional["If-None-Match"] = saved["etag"]
    if "last_modified" in by and saved.get("last_modified"):
        conditional["If-Modified-Since"] = saved["last_modified"]
    status, resp_headers, body = _open(urllib.request.Request(url, headers=conditional), timeout, retries)
    if status == 304:
        return unchanged("not modified (304)")
    for key, header in (("etag", "ETag"), ("last_modified", "Last-Modified")):
        if key in by and resp_headers.get(header):
            new[key] = resp_headers[header]
    new["hash"] = hashlib.sha256(body).hexdigest()
    if saved.get("hash") == new["hash"]:
        save()  # the validators may have changed even though the content did not
        return unchanged("content hash unchanged")
    save()
    return body


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
    if_changed: dict | None = None,
    on_unchanged: str = "stop",
) -> list[dict]:
    """GET a JSON API. `items: data` picks the list inside each response. Every page:
    `paginate: { param: page, next: next_page }` (the page number goes in `param`, starting at
    `start`, 1 by default; it stops when the response's `next` key is empty, or without `next`
    when a page comes back empty). With an input step, one request per record: `{field}` in `url`
    or `params` is filled from the record.

    `if_changed: {by: [etag, last_modified, content_length], page_stamp: {url, regex}}` skips the
    download when the source did not change since the last successful run (304, an equal stamp,
    length or body hash); `on_unchanged: stop` (default) then ends the run as `no_change`,
    `continue_empty` returns no records instead."""
    if if_changed is not None:
        if single(input) or paginate:
            raise ValueError("if_changed works on a single URL: no input step and no paginate")
        return _changed_get(
            _with_query(url, params), headers or {}, items, timeout, retries, if_changed, on_unchanged
        )
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
