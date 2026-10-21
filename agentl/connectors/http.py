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

from .. import evidence as _evidence
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
    auth: dict | None = None
    session: Session | None = None
    evidence: Any = None  # agentl.evidence.Recorder

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
        if self.auth is not None and self.session is None:
            self.session = Session(self.auth, self)

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
        """`_send` with the `auth` session: its headers on every request, and one new login and
        retry when the answer says the session expired."""
        if self.session is None:
            return self._send(req)
        return self.session.open(req)

    def _send(self, req: urllib.request.Request, cookies: Any = None) -> tuple[int, Any, bytes]:
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
                    extra: dict[str, Any] = {"context": self.context} if self.context is not None else {}
                    if cookies is not None:
                        extra["cookies"] = cookies
                    with netguard.urlopen(one, timeout=self.timeout, **extra) as resp:
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
        return self.fetch(method, url, headers, body)[1]

    def fetch(self, method: str, url: str, headers: dict, body: Any) -> tuple[Any, Any]:
        """(response headers, parsed JSON body)."""
        return self.fetch_tagged(method, url, headers, body)[:2]

    def fetch_tagged(self, method: str, url: str, headers: dict, body: Any) -> tuple[Any, Any, dict]:
        """(response headers, parsed JSON body, `_evidence*` fields of the capture)."""
        data = None
        headers = {"Accept": "application/json", **headers}
        if body is not None:
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        started = time.monotonic()
        try:
            status, resp_headers, raw = self.open(req)
        except urllib.error.HTTPError as e:
            if self.evidence is not None:
                err = e.read() if e.fp is not None else b""
                self._capture(method, url, headers, e.code, e.headers, err, started, failed=True)
            raise
        fields = (
            self._capture(method, url, headers, status, resp_headers, raw, started, status >= 400)
            if self.evidence is not None
            else {}
        )
        return resp_headers, _parse(raw), fields

    def _capture(
        self,
        method: str,
        url: str,
        headers: dict,
        status: int,
        resp_headers: Any,
        raw: bytes,
        started: float,
        failed: bool,
    ) -> dict:
        from ..evidence import request_metadata

        ctype = resp_headers.get("Content-Type") if resp_headers is not None else None
        meta = request_metadata(method, url, status, headers, resp_headers, time.monotonic() - started)
        return dict(self.evidence.capture(url, body=raw, request=meta, failed=failed, content_type=ctype))


_TOKENS: dict[str, tuple[float, dict]] = {}  # login key -> (expires, headers), for this process
_TOKEN_LOCKS: dict[str, threading.Lock] = {}
_TOKEN_LOCKS_LOCK = threading.Lock()


class SessionExpired(RuntimeError):
    pass


class Session:
    """`auth: {login: {...}, expired_if, token_cache_ttl, shared}` (#212): log in once, add the
    session (cookies, or a token from the login answer) to every request, log in again when an
    answer says the session expired. Concurrent requests, steps (and, with `shared: true`, runs
    sharing the run store) log in once: the login runs under a lock and its result is cached."""

    KEYS = frozenset({"login", "expired_if", "expired_status", "token_cache_ttl", "shared"})
    LOGIN_KEYS = frozenset({"url", "method", "form", "json", "headers", "token_key", "header", "prefix"})

    def __init__(self, auth: dict, client: Client):
        from ..checks import parse_duration

        unknown = set(auth) - self.KEYS
        if unknown:
            raise ValueError(f"auth: unknown key {sorted(unknown)[0]!r}; use {', '.join(sorted(self.KEYS))}")
        login = auth.get("login")
        if not isinstance(login, dict) or not login.get("url"):
            raise ValueError("auth.login needs a url (and form or json)")
        unknown = set(login) - self.LOGIN_KEYS
        if unknown:
            raise ValueError(f"auth.login: unknown key {sorted(unknown)[0]!r}")
        if login.get("form") is not None and login.get("json") is not None:
            raise ValueError("auth.login: set form or json, not both")
        netguard.require_http(login["url"], "auth.login.url")
        self.login_cfg = login
        self.client = client
        expired = auth.get("expired_if")
        self.expired_if = re.compile(expired) if expired else None
        status = auth.get("expired_status", [401])
        self.expired_status = {int(x) for x in ([status] if isinstance(status, (int, str)) else status)}
        self.ttl = parse_duration(auth.get("token_cache_ttl", "30m"))
        self.shared = bool(auth.get("shared", False))
        stamp = json.dumps(login, sort_keys=True, default=str)
        self.key = "http-login:" + hashlib.sha256(stamp.encode()).hexdigest()[:24]
        with _TOKEN_LOCKS_LOCK:
            self.lock = _TOKEN_LOCKS.setdefault(self.key, threading.Lock())

    def _store(self) -> Any:
        return self.client.store if self.shared else None

    def _login(self) -> dict:
        """POST the login and return the headers that carry the session."""
        import http.cookiejar

        cfg = self.login_cfg
        headers = {"Accept": "application/json, text/html;q=0.9", **(cfg.get("headers") or {})}
        data = None
        if cfg.get("json") is not None:
            data = json.dumps(cfg["json"]).encode()
            headers.setdefault("Content-Type", "application/json")
        elif cfg.get("form") is not None:
            data = urllib.parse.urlencode(cfg["form"]).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        method = cfg.get("method", "POST")
        jar = http.cookiejar.CookieJar()
        req = urllib.request.Request(cfg["url"], data=data, headers=headers, method=method)
        _, _, raw = self.client._send(req, cookies=jar)
        log.info("auth: logged in at %s", cfg["url"])
        out: dict = {}
        cookies = "; ".join(f"{c.name}={c.value}" for c in jar)
        if cookies:
            out["Cookie"] = cookies
        if cfg.get("token_key"):
            token = _dig(_parse(raw), cfg["token_key"])
            if not token:
                raise RuntimeError(f"auth.login: no {cfg['token_key']!r} in the login answer")
            out[cfg.get("header", "Authorization")] = f"{cfg.get('prefix', 'Bearer ')}{token}"
        if not out:
            raise RuntimeError("auth.login: the login answer set no cookie (set token_key for a token)")
        return out

    def credentials(self, stale: dict | None = None) -> dict:
        """The cached session headers, logging in when there are none or when they are `stale`
        (the ones a request just found expired). Under the lock, so only one caller logs in."""
        store = self._store()
        with self.lock:
            if store is not None:
                current = store.cache_get(self.key)
                if current is not None and current == stale:
                    store.cache_clear(self.key)
                return store.cache_compute(self.key, self._login, ttl=self.ttl)
            hit = _TOKENS.get(self.key)
            if hit and hit[0] > time.time() and hit[1] != stale:
                return hit[1]
            fresh = self._login()
            _TOKENS[self.key] = (time.time() + self.ttl, fresh)
            return fresh

    def _expired(self, status: int, body: bytes) -> bool:
        if status in self.expired_status:
            return True
        return bool(self.expired_if and self.expired_if.search(body.decode(errors="replace")))

    def _try(self, req: urllib.request.Request, creds: dict) -> tuple[int, Any, bytes]:
        one = urllib.request.Request(
            req.full_url,
            data=req.data,
            headers={**dict(req.header_items()), **creds},
            method=req.get_method(),
        )
        try:
            return self.client._send(one)
        except urllib.error.HTTPError as e:
            if e.code in self.expired_status:
                return e.code, e.headers, b""
            raise

    def open(self, req: urllib.request.Request) -> tuple[int, Any, bytes]:
        creds = self.credentials()
        status, headers, body = self._try(req, creds)
        if not self._expired(status, body):
            return status, headers, body
        log.info("auth: session expired on %s, logging in again", req.full_url)
        status, headers, body = self._try(req, self.credentials(stale=creds))
        if self._expired(status, body):
            raise SessionExpired(f"auth: {req.full_url} still answers as logged out right after a new login")
        return status, headers, body


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
    if body is None:
        return []
    fields = {}
    if client.evidence is not None:
        from ..evidence import request_metadata

        meta = request_metadata("GET", url, 200, headers)
        fields = dict(client.evidence.capture(url, body=body, request=meta))
    return _evidence.tag(_as_records(_parse(body), items), fields)


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


_STYLES = ("page", "offset", "cursor", "link")
_PAGINATE_KEYS = {
    "page": {"param", "start", "next", "total_key", "concurrency"},
    "offset": {"param", "start", "limit", "limit_param", "total_key"},
    "cursor": {"param", "cursor_key"},
    "link": set(),
}


def _next_link(headers: Any) -> str | None:
    """The `rel="next"` target of an RFC 8288 (RFC 5988) `Link` header."""
    value = headers.get("Link") if headers is not None else None
    for part in re.split(r",\s*(?=<)", value or ""):
        m = re.match(r"\s*<([^>]*)>(.*)", part)
        if m and re.search(r';\s*rel\s*=\s*"?([^";]*\s)?next(\s[^";]*)?"?\s*(;|$)', m.group(2)):
            return m.group(1)
    return None


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
    """One request, or every page. `paginate.style` picks how the next page is asked for:
    `page` (default: a page number in `param`), `offset` (`offset` and `limit` params),
    `cursor` (the token at `cursor_key` of each answer goes in `param`) or `link` (the
    `rel="next"` URL of the `Link` header). Every style stops on a page with no items and
    after `max_pages`; `total_key` (page: the page count, offset: the item count) stops it
    when that total is reached."""
    if not paginate:
        _, data, fields = client.fetch_tagged(method, _with_query(url, params), headers, body)
        return _evidence.tag(_as_records(data, items), fields)
    style = paginate.get("style", "page")
    if style not in _STYLES:
        raise ValueError(f"paginate.style must be one of {', '.join(_STYLES)}")
    unknown = set(paginate) - _PAGINATE_KEYS[style] - {"style", "max_pages"}
    if unknown:
        raise ValueError(f"paginate: {sorted(unknown)[0]!r} does not apply to style {style}")
    if style == "cursor" and not paginate.get("cursor_key"):
        raise ValueError("paginate style cursor needs cursor_key (the path of the next token)")
    max_pages = int(paginate.get("max_pages", 1000))
    total_key = paginate.get("total_key")
    base = dict(params or {})
    out: list[dict] = []

    def get(query: dict, target: str = url) -> tuple[Any, Any, list[dict]]:
        resp_headers, data, fields = client.fetch_tagged(method, _with_query(target, query), headers, body)
        return resp_headers, data, _evidence.tag(_as_records(data, items), fields) if data else []

    if style == "page":
        param, page = paginate.get("param", "page"), int(paginate.get("start", 1))
        nxt = paginate.get("next")
        for i in range(max_pages):
            _, data, rows = get({**base, param: page + i})
            out.extend(rows)
            total = _dig(data, total_key) if total_key and i == 0 else None
            if total is not None:
                if int(total) > max_pages:
                    raise RuntimeError(f"pagination: {total} pages is more than max_pages={max_pages}")
                rest = range(page + 1, page + int(total))  # the total counts pages from `start`
                with ThreadPoolExecutor(max_workers=int(paginate.get("concurrency", 1))) as pool:
                    for batch in pool.map(lambda p: get({**base, param: p})[2], rest):
                        out.extend(batch)
                return out
            if not rows or (nxt and not _dig(data, nxt)):
                return out
    elif style == "offset":
        param, limit_param = paginate.get("param", "offset"), paginate.get("limit_param", "limit")
        limit = int(paginate.get("limit", 100))
        offset = int(paginate.get("start", 0))
        for _ in range(max_pages):
            _, data, rows = get({**base, param: offset, limit_param: limit})
            out.extend(rows)
            offset += limit
            total = _dig(data, total_key) if total_key else None
            if len(rows) < limit or (total is not None and offset >= int(total)):
                return out
    elif style == "cursor":
        param, token, seen = paginate.get("param", "cursor"), None, set()
        for _ in range(max_pages):
            _, data, rows = get(base if token is None else {**base, param: token})
            out.extend(rows)
            token = _dig(data, paginate["cursor_key"])
            if not rows or token in (None, "") or str(token) in seen:
                return out
            seen.add(str(token))
    else:  # link
        target = _with_query(url, base)
        for _ in range(max_pages):
            resp_headers, _, rows = get({}, target)
            out.extend(rows)
            link = _next_link(resp_headers)
            if not rows or not link:
                return out
            target = urllib.parse.urljoin(target, link)
    raise RuntimeError(f"pagination stopped after max_pages={max_pages}")


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
    auth: dict | None = None,
    evidence: bool | dict | None = None,
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
    a private CA (`verify: false` turns checks off, with a warning).

    `paginate.style`: `page` (above), `offset` (`limit`, `limit_param`), `cursor` (`cursor_key`:
    the path of the next token, sent in `param`) or `link` (the `Link: rel="next"` header);
    `total_key` stops at the page count (page) or item count (offset) found in the first answer.
    `auth: {login: {url, form | json, token_key}, expired_if: "Please sign in"}` logs in once
    (cookies, or the token at `token_key` sent as `Authorization: Bearer`), caches the session for
    `token_cache_ttl` (30m) under a lock and logs in again when an answer matches `expired_if` or
    has status 401 (`expired_status`); `shared: true` shares it through the run store.

    `evidence: {raw: true, request: true, when: changed}` keeps every answer body and its request
    metadata, content-addressed, and tags the records with `_evidence` (see agentl/evidence.py)."""
    client = Client(timeout, retries, retry_after, rate_limit, list(mirrors or []), tls, auth=auth)
    client.evidence = _evidence.recorder(evidence, "http.get")
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
    auth: dict | None = None,
    evidence: bool | dict | None = None,
) -> list[dict]:
    """POST a JSON body (`json`, `{field}` filled per record); the other options are `http.get`'s."""
    client = Client(timeout, retries, retry_after, rate_limit, list(mirrors or []), tls, auth=auth)
    client.evidence = _evidence.recorder(evidence, "http.post")
    return _http("POST", input, url, headers, json, items, concurrency, client, params, paginate)
