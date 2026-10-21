"""`web.crawl`: cover a whole site with good conduct.

Start URLs and/or sitemaps, breadth first up to `max_depth` and `max_pages`, inside the allowed
domains and URL patterns, with URL normalisation and dedupe. By default it obeys `robots.txt`
(including `Crawl-delay`), waits `delay` seconds between requests to the same host, honours
`Retry-After` with backoff and sends an identifying User-Agent. With `incremental: true` pages
unchanged since the last successful run (ETag, Last-Modified, content hash) are not output again.
Bypassing CAPTCHAs or bot protection is out of scope."""

from __future__ import annotations

import collections
import datetime as _dt
import email.utils
import gzip
import hashlib
import io
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from typing import Any

from .. import netguard
from ..registry import Records, after_run, connector, step_state
from ._util import single
from .htmldom import parse
from .scrape import MAX_PAGE_BYTES, base_url
from .scrape import user_agent as default_agent

log = logging.getLogger("agentl.crawl")

FORMATS = ("html", "markdown", "both")
MAX_SITEMAPS = 50


def normalize(url: str) -> str:
    """Lowercase scheme and host, drop the default port and the fragment, `/` for an empty path,
    query parameters sorted. Two spellings of one page give the same string."""
    parts = urllib.parse.urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    netloc = host if port is None or (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True)))
    return urllib.parse.urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def _retry_after(value: str | None, attempt: int) -> float:
    if value:
        value = value.strip()
        if value.isdigit():
            return min(float(value), 120)
        try:
            when = email.utils.parsedate_to_datetime(value)
            return max(0.0, min((when - _dt.datetime.now(_dt.timezone.utc)).total_seconds(), 120))
        except (TypeError, ValueError):
            pass
    return float(2**attempt)


class _Response:
    def __init__(self, url: str, status: int, headers: Any, body: bytes):
        self.url, self.status, self.headers, self.body = url, status, headers, body

    def text(self) -> str:
        charset = (self.headers.get_content_charset() if self.headers else None) or "utf-8"
        return self.body.decode(charset, errors="replace")


class _Client:
    """GETs through the SSRF guard, `delay` seconds apart per host, retrying 429 and 5xx."""

    def __init__(
        self, headers: dict | None, delay: float, timeout: float, retries: int, agent: str, proxy: Any
    ):
        self.headers = {"User-Agent": agent, **(headers or {})}
        self.delay, self.timeout, self.retries, self.proxy = delay, timeout, retries, proxy
        self.host_delay: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self.requests = 0

    def _proxy_for(self, host: str) -> str | None:
        if isinstance(self.proxy, dict):
            return self.proxy.get(host) or self.proxy.get("*")
        return self.proxy or None

    def get(self, url: str, extra: dict | None = None) -> _Response:
        host = urllib.parse.urlsplit(url).hostname or ""
        for attempt in range(self.retries + 1):
            wait = self._last.get(host, -1e9) + self.host_delay.get(host, self.delay) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()
            self.requests += 1
            req = urllib.request.Request(url, headers={**self.headers, **(extra or {})})
            try:
                with netguard.urlopen(req, timeout=self.timeout, proxy=self._proxy_for(host)) as resp:
                    body = resp.read(MAX_PAGE_BYTES + 1)
                    if len(body) > MAX_PAGE_BYTES:
                        raise ValueError(f"web.crawl: {url} is larger than {MAX_PAGE_BYTES} bytes")
                    return _Response(resp.geturl(), resp.status, resp.headers, body)
            except urllib.error.HTTPError as e:
                if e.code == 304 or (e.code < 500 and e.code != 429) or attempt == self.retries:
                    return _Response(url, e.code, e.headers, b"")
                time.sleep(_retry_after(e.headers.get("Retry-After") if e.headers else None, attempt))
            except urllib.error.URLError:
                if attempt == self.retries:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")


def _float_crawl_delay(lines: list[str]) -> float | None:
    """robotparser reads whole seconds only; `Crawl-delay: 0.5` comes through here."""
    for line in lines:
        m = re.match(r"\s*crawl-delay\s*:\s*([0-9]*\.?[0-9]+)", line, re.I)
        if m:
            return float(m.group(1))
    return None


class _Robots:
    def __init__(self, client: _Client, agent: str, ignore: bool):
        self.client, self.agent, self.ignore = client, agent, ignore
        self._by_origin: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.blocked = 0

    def allowed(self, url: str) -> bool:
        if self.ignore:
            return True
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rp = self._by_origin.get(origin)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
            resp = self.client.get(origin + "/robots.txt")
            if resp.status in (401, 403):
                rp.parse(["User-agent: *", "Disallow: /"])
            elif resp.status >= 400:
                rp.parse([])
            else:
                lines = resp.text().splitlines()
                rp.parse(lines)
                cd = rp.crawl_delay(self.agent) or _float_crawl_delay(lines)
                if cd:
                    host = parts.hostname or ""
                    self.client.host_delay[host] = max(self.client.delay, float(cd))
            self._by_origin[origin] = rp
        ok = rp.can_fetch(self.agent, url)
        if not ok:
            self.blocked += 1
            log.info("web.crawl: robots.txt disallows %s", url)
        return ok


def _sitemap_urls(client: _Client, url: str, seen: set[str]) -> list[str]:
    """Page URLs of a sitemap (or sitemap index, followed), plain or gzipped."""
    if url in seen or len(seen) >= MAX_SITEMAPS:
        return []
    seen.add(url)
    resp = client.get(url)
    if resp.status >= 400:
        raise ValueError(f"web.crawl: sitemap {url} answered HTTP {resp.status}")
    body = resp.body
    if body[:2] == b"\x1f\x8b":
        body = gzip.GzipFile(fileobj=io.BytesIO(body)).read(MAX_PAGE_BYTES + 1)
        if len(body) > MAX_PAGE_BYTES:
            raise ValueError(f"web.crawl: sitemap {url} is larger than {MAX_PAGE_BYTES} bytes")
    if b"<!DOCTYPE" in body[:2048] or b"<!ENTITY" in body:
        raise ValueError(f"web.crawl: sitemap {url} declares a DTD or entities, refusing to parse it")
    root = ET.fromstring(body)
    locs = [(el.text or "").strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc"]
    if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
        out: list[str] = []
        for loc in locs:
            out.extend(_sitemap_urls(client, urllib.parse.urljoin(url, loc), seen))
        return out
    return [urllib.parse.urljoin(url, loc) for loc in locs if loc]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else [str(v) for v in value]


@connector("web.crawl")
def web_crawl(
    input: Any = None,
    start: str | list[str] | None = None,
    sitemap: str | list[str] | None = None,
    max_depth: int = 2,
    max_pages: int = 100,
    allowed_domains: list[str] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    format: str = "html",
    delay: float = 1.0,
    obey_robots: bool = True,
    user_agent: str | None = None,
    headers: dict | None = None,
    proxy: str | dict | None = None,
    incremental: bool = False,
    timeout: float = 30,
    retries: int = 2,
    evidence: bool | dict | None = None,
) -> Records:
    """Crawl breadth first from `start` (URLs, or the `url` field of upstream records) and the
    pages listed in `sitemap`, following links up to `max_depth` (0: only the start pages) and
    reading at most `max_pages` pages. Only hosts in `allowed_domains` (default: the start hosts;
    subdomains included) and URLs matching an `include` regex (if any) and no `exclude` regex are
    followed. One record per page: url, status, depth, referrer, links, and html and/or markdown
    (`format`). `obey_robots: false` skips robots.txt and logs a warning. `proxy` is one proxy URL
    or a mapping of host to proxy URL (`"*"` for the rest); pass it from a secret. `evidence: {raw:
    true, request: true, when: changed}` keeps each page as received, content-addressed, and
    tags its record with `_evidence` (see agentl/evidence.py)."""
    if format not in FORMATS:
        raise ValueError(f"web.crawl: format must be one of {', '.join(FORMATS)}, not {format!r}")
    starts = _as_list(start) or [str(r["url"]) for r in single(input) if r.get("url")]
    sitemaps = _as_list(sitemap)
    if not starts and not sitemaps:
        raise ValueError("web.crawl: set 'start' and/or 'sitemap', or feed records with a 'url' field")
    for u in starts + sitemaps:
        netguard.require_http(u, "web.crawl")
    from .. import evidence as _evidence

    recorder = _evidence.recorder(evidence, "web.crawl")
    agent = user_agent or default_agent()
    client = _Client(headers, delay, timeout, retries, agent, proxy)
    if not obey_robots:
        log.warning("web.crawl: obey_robots is false, robots.txt is NOT read (explicit override)")
    robots = _Robots(client, agent, ignore=not obey_robots)
    domains = [d.lower().lstrip(".") for d in allowed_domains] if allowed_domains else None
    if domains is None:
        domains = sorted({(urllib.parse.urlsplit(u).hostname or "").lower() for u in starts + sitemaps})
    inc = [re.compile(p) for p in include or []]
    exc = [re.compile(p) for p in exclude or []]

    def in_scope(url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        host = (parts.hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in domains):
            return False
        if inc and not any(p.search(url) for p in inc):
            return False
        return not any(p.search(url) for p in exc)

    state = step_state() if incremental else None
    old: dict[str, dict] = dict(((state.get() if state else None) or {}).get("pages") or {})
    new: dict[str, dict] = {}

    queue: collections.deque[tuple[str, int, str | None]] = collections.deque()
    seen: set[str] = set()

    def push(url: str, depth: int, referrer: str | None, check_scope: bool = True) -> None:
        url = normalize(url)
        if url in seen or (check_scope and not in_scope(url)):
            return
        seen.add(url)
        queue.append((url, depth, referrer))

    for u in starts:
        push(u, 0, None, check_scope=False)
    sm_seen: set[str] = set()
    for sm in sitemaps:
        for u in _sitemap_urls(client, sm, sm_seen):
            push(u, 0, None)

    out: Records = []
    read = unchanged = 0
    while queue and read < max_pages:
        url, depth, referrer = queue.popleft()
        if not robots.allowed(url):
            continue
        prev = old.get(url) or {}
        cond: dict[str, str] = {}
        if incremental and prev.get("etag"):
            cond["If-None-Match"] = prev["etag"]
        if incremental and prev.get("last_modified"):
            cond["If-Modified-Since"] = prev["last_modified"]
        started = time.monotonic()
        resp = client.get(url, cond)
        elapsed = time.monotonic() - started
        read += 1
        if resp.status == 304 and prev:
            new[url] = prev
            unchanged += 1
            links = list(prev.get("links") or [])
        else:
            ctype = (resp.headers.get("Content-Type", "") if resp.headers else "").lower()
            html = resp.text() if resp.status < 400 and ("html" in ctype or not ctype) else ""
            links = []
            if html:
                root = parse(html)
                base = base_url(root, resp.url)
                for a in root.select("a[href]"):
                    href = (a.attrs.get("href") or "").strip()
                    if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                        links.append(normalize(urllib.parse.urljoin(base, href)))
                links = list(dict.fromkeys(links))
            digest = hashlib.sha256(resp.body).hexdigest()
            entry = {"hash": digest, "links": links}
            if resp.headers is not None:
                if resp.headers.get("ETag"):
                    entry["etag"] = resp.headers["ETag"]
                if resp.headers.get("Last-Modified"):
                    entry["last_modified"] = resp.headers["Last-Modified"]
            if resp.status < 400:
                new[url] = entry
            if incremental and prev.get("hash") == digest:
                unchanged += 1
            else:
                rec: dict[str, Any] = {
                    "url": normalize(resp.url),
                    "status": resp.status,
                    "depth": depth,
                    "referrer": referrer,
                    "links": links,
                }
                if format in ("html", "both"):
                    rec["html"] = html
                if format in ("markdown", "both"):
                    from .web import to_markdown

                    rec["markdown"] = to_markdown(html, resp.url)["markdown"] if html else ""
                if recorder is not None:
                    meta = _evidence.request_metadata(
                        "GET", resp.url, resp.status, {**client.headers, **cond}, resp.headers, elapsed
                    )
                    ctype = resp.headers.get("Content-Type") if resp.headers else None
                    failed = resp.status >= 400
                    rec.update(recorder.capture(resp.url, resp.body, meta, failed=failed, content_type=ctype))
                out.append(rec)
        if depth < max_depth:
            for link in links:
                push(link, depth + 1, url)

    log.info(
        "web.crawl: %d pages read, %d output, %d unchanged, %d blocked by robots.txt, %d requests",
        read,
        len(out),
        unchanged,
        robots.blocked,
        client.requests,
    )
    if state is not None:
        saved = {"pages": {**old, **new}}
        after_run(lambda: state.set(saved))
    return out
