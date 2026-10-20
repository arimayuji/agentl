"""HTTP connectors (stdlib only). URLs accept {field} placeholders filled
from each upstream record, so one request is made per record."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .. import llm, netguard
from ..registry import NoChange, after_run, connector, run_store, step_state
from ._util import single

log = logging.getLogger("agentl.http")

_WINDOWS = {"per_second": 1.0, "per_minute": 60.0, "per_hour": 3600.0, "per_day": 86400.0}
_LOCAL_BUDGETS: dict[str, int] = {}
_LOCAL_LOCK = threading.Lock()


def _local_take(key: str, limit: int, window: float) -> float:
    """`Store.budget_take` for a connector called outside a run: shared by this process only."""
    with _LOCAL_LOCK:
        now = time.time()
        start = now - now % window
        slot = f"{key}:{int(start)}"
        used = _LOCAL_BUDGETS.get(slot, 0)
        if used >= limit:
            return start + window - now
        _LOCAL_BUDGETS[slot] = used + 1
        return 0.0


@dataclass
class Client:
    """How requests of one `http.*` step are made: retries with backoff and `Retry-After`,
    a request budget, mirror hosts and the TLS context (#212)."""

    timeout: float = 30
    retries: int = 2
    retry_after: str = "respect"
    rate_limit: dict | None = None
    mirrors: list[str] = field(default_factory=list)
    tls: dict | None = None
    store: Any = None
    context: Any = None

    def __post_init__(self) -> None:
        if self.retry_after not in ("respect", "ignore"):
            raise ValueError("retry_after must be respect or ignore")
        if isinstance(self.mirrors, str):
            self.mirrors = [self.mirrors]
        for m in self.mirrors:
            if urllib.parse.urlsplit(m).scheme not in ("http", "https"):
                raise ValueError(f"mirrors: {m!r} is not an http(s) URL")
        if self.rate_limit is not None:
            per = [k for k in _WINDOWS if k in self.rate_limit]
            unknown = set(self.rate_limit) - set(_WINDOWS) - {"shared", "key", "max_wait"}
            if len(per) != 1 or unknown:
                raise ValueError(
                    f"rate_limit takes one of {', '.join(_WINDOWS)} (plus shared, key, max_wait)"
                )
            if int(self.rate_limit[per[0]]) < 1:
                raise ValueError("rate_limit: the budget must be at least 1 request")
        if self.tls is not None and self.context is None:
            self.context = _tls_context(self.tls)
        if self.store is None:
            self.store = run_store()

    def _budget(self, url: str) -> None:
        """Wait for a request of the budget; the budget pauses the step, it never fails it."""
        cfg = self.rate_limit
        if not cfg:
            return
        per = next(k for k in _WINDOWS if k in cfg)
        limit, window = int(cfg[per]), _WINDOWS[per]
        key = str(cfg.get("key") or urllib.parse.urlsplit(url).hostname or url)
        shared = cfg.get("shared", True) and self.store is not None
        max_wait = float(cfg.get("max_wait", 86400))
        waited = 0.0
        while True:
            wait = self.store.budget_take(key, limit, window) if shared else _local_take(key, limit, window)
            if wait <= 0:
                return
            if waited + wait > max_wait:
                raise RuntimeError(
                    f"rate_limit: budget of {key!r} still spent after {waited:.0f}s (max_wait)"
                )
            log.info("rate_limit: budget of %r spent, waiting %.1fs", key, wait)
            time.sleep(wait + 0.01)
            waited += wait

    def _pause(self, attempt: int, e: BaseException | None) -> float:
        headers = getattr(e, "headers", None)
        if self.retry_after == "respect" and headers is not None:
            asked = llm.parse_retry_after(headers.get("Retry-After"))
            if asked is not None:
                return min(asked, llm.RETRY_AFTER_CAP)
        return float(2**attempt)

    def _candidates(self, url: str) -> list[str]:
        """`url`, then the same path and query on each mirror."""
        parts = urllib.parse.urlsplit(url)
        out = [url]
        for m in self.mirrors:
            base = urllib.parse.urlsplit(m.rstrip("/"))
            out.append(
                urllib.parse.urlunsplit((base.scheme, base.netloc, base.path + parts.path, parts.query, ""))
            )
        return out

    def open(self, req: urllib.request.Request) -> tuple[int, Any, bytes]:
        """(status, headers, body) with retries on network errors, 429 and 5xx; a 304 is returned.
        When a host keeps failing that way, the next mirror is tried with the same retries."""
        candidates = self._candidates(req.full_url)
        for i, target in enumerate(candidates):
            last = i == len(candidates) - 1
            for attempt in range(self.retries + 1):
                one = urllib.request.Request(
                    target, data=req.data, headers=dict(req.header_items()), method=req.get_method()
                )
                self._budget(target)
                try:
                    tls = {"context": self.context} if self.context is not None else {}
                    with netguard.urlopen(one, timeout=self.timeout, **tls) as resp:
                        return getattr(resp, "status", 200), getattr(resp, "headers", {}), resp.read()
                except urllib.error.HTTPError as e:
                    if e.code == 304:
                        return 304, e.headers, b""
                    if e.code < 500 and e.code != 429:
                        raise
                    if attempt == self.retries:
                        if last:
                            raise
                        break
                    time.sleep(self._pause(attempt, e))
                except urllib.error.URLError:
                    if attempt == self.retries:
                        if last:
                            raise
                        break
                    time.sleep(self._pause(attempt, None))
            log.warning("http: %s kept failing, trying mirror %s", target, candidates[i + 1])
        raise AssertionError("unreachable")

    def request(self, method: str, url: str, headers: dict, body: Any) -> Any:
        data = None
        headers = {"Accept": "application/json", **headers}
        if body is not None:
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        _, _, raw = self.open(urllib.request.Request(url, data=data, headers=headers, method=method))
        return _parse(raw)


def _tls_context(tls: dict) -> Any:
    """`tls: {ca_bundle: corp.pem}` trusts the CAs in that bundle instead of the default store;
    `tls: {verify: false}` turns verification off, with a warning each time the step runs."""
    import ssl

    unknown = set(tls) - {"verify", "ca_bundle"}
    if unknown:
        raise ValueError(f"tls: unknown key {sorted(unknown)[0]!r}; use verify, ca_bundle")
    verify = tls.get("verify", True)
    if verify is False:
        if tls.get("ca_bundle"):
            raise ValueError("tls: set ca_bundle or verify: false, not both")
        log.warning("tls.verify is false: HTTPS certificates are NOT checked for this step")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if verify is not True:
        raise ValueError("tls.verify must be true or false")
    bundle = tls.get("ca_bundle")
    if not bundle:
        return None
    if not os.path.isfile(bundle):
        raise FileNotFoundError(f"tls.ca_bundle: {bundle} not found")
    return ssl.create_default_context(cafile=bundle)


def _parse(raw: bytes) -> Any:
    text = raw.decode()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


# ---------------------------------------------------------------- if_changed
_STRATEGIES = ("page_stamp", "content_length", "etag", "last_modified", "hash")


def _changed_get(
    url: str,
    headers: dict,
    items: str | None,
    client: Client,
    if_changed: dict,
    on_unchanged: str,
) -> list[dict]:
    body = conditional_get(
        url,
        {"Accept": "application/json", **headers},
        client.timeout,
        client.retries,
        if_changed,
        on_unchanged,
        client=client,
    )
    return [] if body is None else _as_records(_parse(body), items)


def conditional_get(
    url: str,
    headers: dict,
    timeout: float,
    retries: int,
    if_changed: dict,
    on_unchanged: str,
    client: Client | None = None,
) -> bytes | None:
    """The body of `url`, or None (`on_unchanged: continue_empty`) when it did not change since
    the last successful run; `on_unchanged: stop` raises NoChange instead. Shared by `http.get`
    and the file readers that take a `url`.

    GET `url` only when it changed since the last successful run. Strategies run cheapest
    first: `page_stamp` (a regex on another, small page), `content_length` (a HEAD request),
    `etag` / `last_modified` (a conditional GET answered with 304), then a hash of the body,
    which is always the last resort. The validators are kept in the run store (`agentl state`)
    and saved only when the run succeeds."""
    client = client or Client(timeout, retries)
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
        _, _, page = client.open(req)
        match = re.search(stamp_cfg["regex"], page.decode(errors="replace"))
        if match is None:
            raise RuntimeError(f"if_changed.page_stamp: regex found nothing on {req.full_url}")
        new["page_stamp"] = match.group(1) if match.groups() else match.group(0)
        if saved.get("page_stamp") == new["page_stamp"]:
            return unchanged(f"page stamp {new['page_stamp']!r} unchanged")
    if "content_length" in by:
        _, head, _ = client.open(urllib.request.Request(url, headers=headers, method="HEAD"))
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
    status, resp_headers, body = client.open(urllib.request.Request(url, headers=conditional))
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
    client: Client,
) -> list[dict]:
    """One request, or every page: `paginate: {param: page, start: 1, next: next_page, max_pages: 1000}`.
    Stops when the `next` field of the response is empty, or when a page has no items."""
    if not paginate:
        return _as_records(client.request(method, _with_query(url, params), headers, body), items)
    param = paginate.get("param", "page")
    page = paginate.get("start", 1)
    out: list[dict] = []
    for _ in range(int(paginate.get("max_pages", 1000))):
        data = client.request(method, _with_query(url, {**(params or {}), param: page}), headers, body)
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
    client: Client,
    params: dict | None = None,
    paginate: dict | None = None,
) -> list[dict]:
    headers = headers or {}
    upstream = single(input)
    if not upstream:
        return _pages(method, url, params, paginate, headers, json_body, items, client)

    def one(rec: dict) -> list[dict]:
        return _pages(
            method,
            url.format(**rec),
            _fill(params, rec),
            paginate,
            headers,
            _fill(json_body, rec),
            items,
            client,
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
    retry_after: str = "respect",
    rate_limit: dict | None = None,
    mirrors: list[str] | None = None,
    tls: dict | None = None,
) -> list[dict]:
    """GET a JSON API. `items: data` picks the list inside each response. Every page:
    `paginate: { param: page, next: next_page }` (the page number goes in `param`, starting at
    `start`, 1 by default; it stops when the response's `next` key is empty, or without `next`
    when a page comes back empty). With an input step, one request per record: `{field}` in `url`
    or `params` is filled from the record.

    `if_changed: {by: [etag, last_modified, content_length], page_stamp: {url, regex}}` skips the
    download when the source did not change since the last successful run (304, an equal stamp,
    length or body hash); `on_unchanged: stop` (default) then ends the run as `no_change`,
    `continue_empty` returns no records instead.

    `retry_after: respect` (default) waits what a 429/5xx `Retry-After` asks (at most 60 s);
    `rate_limit: {per_hour: 1000}` pauses when the budget of the host is spent, shared with
    other steps and runs through the run store; `mirrors: [https://b.example.org]` tries the
    same path on other hosts once the URL keeps failing; `tls: {ca_bundle: corp.pem}` trusts
    a private CA (`verify: false` turns checks off, with a warning)."""
    client = Client(timeout, retries, retry_after, rate_limit, list(mirrors or []), tls)
    if if_changed is not None:
        if single(input) or paginate:
            raise ValueError("if_changed works on a single URL: no input step and no paginate")
        return _changed_get(_with_query(url, params), headers or {}, items, client, if_changed, on_unchanged)
    return _http("GET", input, url, headers, None, items, concurrency, client, params, paginate)


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
    retry_after: str = "respect",
    rate_limit: dict | None = None,
    mirrors: list[str] | None = None,
    tls: dict | None = None,
) -> list[dict]:
    """POST a JSON body (`json`, `{field}` filled per record); the other options are `http.get`'s."""
    client = Client(timeout, retries, retry_after, rate_limit, list(mirrors or []), tls)
    return _http("POST", input, url, headers, json, items, concurrency, client, params, paginate)
