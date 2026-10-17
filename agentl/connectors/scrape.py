"""Declarative extraction from HTML (`html.select`) and list-to-detail crawling (`crawl`).

A selector that matches nothing is the usual way a scraper breaks when the site changes. By
default (`on_selector_miss: fail_with_snapshot`) the step fails naming the selector and the URL,
and keeps the HTML it received under `.agentl/evidence`, so the fix starts from what the site
actually sent. `skip` and `empty` keep going. The miss is handled in one place, `handle_miss`:
that is where a later `agent` mode (a model reading the snapshot, see #195) plugs in."""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import netguard
from ..registry import Records, connector, get
from ._util import paths, single
from .htmldom import Node, parse

log = logging.getLogger("agentl.scrape")

MISS_MODES = ("fail_with_snapshot", "skip", "empty")
MAX_PAGE_BYTES = 20 * 1024 * 1024
DETAIL_STEPS = ("html.read", "html.select")


def user_agent() -> str:
    from .. import __version__

    return f"agentl/{__version__} (+https://agentl.com.br)"


class SelectorMiss(LookupError):
    """A selector matched nothing. `snapshot` is the saved HTML, when one was kept."""

    def __init__(self, selector: str, url: str, snapshot: str | None, what: str = "selector"):
        self.selector, self.url, self.snapshot = selector, url, snapshot
        saved = f"; the HTML received is in {snapshot}" if snapshot else ""
        super().__init__(f"{what} {selector!r} matched nothing on {url}{saved}")


def check_mode(mode: str) -> None:
    if mode not in MISS_MODES:
        raise ValueError(f"on_selector_miss must be one of {', '.join(MISS_MODES)}, not {mode!r}")


def handle_miss(
    mode: str, selector: str, source: str, html: str, snapshot_dir: str | None, what: str = "selector"
) -> None:
    """Raise for `fail_with_snapshot` (after saving the page); log and return for the others."""
    if mode == "fail_with_snapshot":
        from .web import save_evidence

        saved = save_evidence(snapshot_dir or True, source, html)["_evidence_html"]
        raise SelectorMiss(selector, source, saved, what)
    log.warning("%s %r matched nothing on %s (on_selector_miss: %s)", what, selector, source, mode)


def pages(
    input: Any,
    path: str | None,
    url: str | None,
    headers: dict | None,
    storage_options: dict | None,
    field: str = "html",
    encoding: str = "utf-8",
) -> list[tuple[str, str]]:
    """(source, html) for each page: from `url` or `path`, from upstream records that carry the
    HTML in `field` (browser.scrape, crawl, http.get), or from records with a `path`."""
    rows = single(input) if isinstance(input, list) and not (url or path) else None
    if rows and any(isinstance(r.get(field), str) for r in rows):
        return [
            (str(r.get("url") or r.get("_url") or r.get("_source") or f"record {i}"), r[field])
            for i, r in enumerate(rows)
            if isinstance(r.get(field), str)
        ]
    out = []
    for p in paths(input, path, "path", url, headers, storage_options):
        source = url if url and "{" not in url else str(p)
        out.append((source, Path(p).read_text(encoding=encoding, errors="replace")))
    return out


def base_url(root: Node, source: str) -> str:
    for b in root.select("base[href]"):
        return urllib.parse.urljoin(source, b.attrs["href"])
    return source


# ---------------------------------------------------------------- html.select
def _field_spec(name: str, spec: Any) -> dict:
    if isinstance(spec, str):
        return {"select": spec}
    if not isinstance(spec, dict):
        raise ValueError(f"html.select: field {name!r} must be a selector or a mapping")
    unknown = set(spec) - {"select", "attr", "absolute", "type", "all", "required"}
    if unknown:
        raise ValueError(f"html.select: field {name!r}: unknown keys {sorted(unknown)}")
    return spec


def _convert(name: str, value: Any, spec: dict, locale: str, source: str) -> Any:
    if value is None or not spec.get("type"):
        return value
    from .. import types
    from .transforms import _cast_spec

    tname, opts = _cast_spec(spec["type"], f"html.select: field {name!r}")
    kw = {k: v for k, v in opts.items() if k != "_optional"}
    if tname in ("int", "decimal", "float", "money", "percent"):
        kw.setdefault("locale", locale)
    try:
        clean = types.TYPES[tname](value, **kw)
    except (ValueError, TypeError, ArithmeticError) as e:
        raise ValueError(f"html.select: field {name!r} on {source}: {e}") from None
    return clean.isoformat() if hasattr(clean, "isoformat") else clean


def extract(
    root: Node,
    source: str,
    html: str,
    items: str | None,
    fields: dict[str, Any],
    on_selector_miss: str,
    snapshot_dir: str | None,
    locale: str,
) -> Records:
    base = base_url(root, source)
    specs = {name: _field_spec(name, s) for name, s in fields.items()}
    nodes = root.select(items) if items else [root]
    if not nodes:
        handle_miss(on_selector_miss, str(items), source, html, snapshot_dir)
        return []
    out: Records = []
    for node in nodes:
        rec: dict[str, Any] = {}
        keep = True
        for name, spec in specs.items():
            sel = spec.get("select")
            found = node.select(sel) if sel else [node]
            if not found:
                if spec.get("required", True):
                    handle_miss(on_selector_miss, str(sel), source, html, snapshot_dir)
                    if on_selector_miss == "skip":
                        keep = False
                        break
                rec[name] = [] if spec.get("all") else None
                continue
            values: list[Any] = []
            for n in found if spec.get("all") else found[:1]:
                attr = spec.get("attr")
                v: str | None = n.attrs.get(attr) if attr else n.text()
                if v is not None:
                    v = v.strip()
                    if spec.get("absolute") and v:
                        v = urllib.parse.urljoin(base, v)
                values.append(_convert(name, v, spec, locale, source))
            rec[name] = values if spec.get("all") else values[0]
        if keep:
            out.append({**rec, "_source": source})
    return out


@connector("html.select")
def html_select(
    input: Any = None,
    fields: dict[str, Any] | None = None,
    items: str | None = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    field: str = "html",
    on_selector_miss: str = "fail_with_snapshot",
    snapshot_dir: str | None = None,
    locale: str = "pt-BR",
    encoding: str = "utf-8",
) -> Records:
    """One record per element matching the CSS selector `items` (the whole page when unset), with
    one value per entry in `fields`: a selector (its trimmed text) or `{select, attr, absolute,
    type, all, required}`. `attr` reads an attribute instead of the text, `absolute: true` resolves
    a link against the page URL, `type` converts with the `cast` types (`money`, `int`, `date`...),
    `all: true` gives every match as a list, `required: false` gives null instead of a miss. The
    HTML comes from `url`, `path` or the `field` (default `html`) of upstream records."""
    check_mode(on_selector_miss)
    if not fields:
        raise ValueError("html.select: set 'fields', e.g. {name: h2, link: {select: a, attr: href}}")
    out: Records = []
    for source, html in pages(input, path, url, headers, storage_options, field, encoding):
        out.extend(extract(parse(html), source, html, items, fields, on_selector_miss, snapshot_dir, locale))
    return out


# ---------------------------------------------------------------- crawl
class _Fetcher:
    """GETs through the SSRF guard, one at a time, with `delay` seconds between requests."""

    def __init__(self, headers: dict | None, delay: float, timeout: float, retries: int, agent: str | None):
        self.headers = {"User-Agent": agent or user_agent(), **(headers or {})}
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self._last = 0.0
        self.requests = 0

    def get(self, url: str) -> tuple[str, str]:
        """(final URL after redirects, HTML)."""
        netguard.require_http(url, "crawl")
        for attempt in range(self.retries + 1):
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            req = urllib.request.Request(url, headers=self.headers)
            try:
                with netguard.urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read(MAX_PAGE_BYTES + 1)
                    if len(data) > MAX_PAGE_BYTES:
                        raise ValueError(f"crawl: {url} is larger than {MAX_PAGE_BYTES} bytes")
                    charset = resp.headers.get_content_charset() or "utf-8"
                    return resp.geturl(), data.decode(charset, errors="replace")
            except urllib.error.HTTPError as e:
                if (e.code < 500 and e.code != 429) or attempt == self.retries:
                    raise
                after = e.headers.get("Retry-After", "") if e.headers else ""
                time.sleep(min(float(after), 60) if after.isdigit() else 2**attempt)
            except urllib.error.URLError:
                if attempt == self.retries:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")


def _detail_steps(detail: Any) -> list[tuple[str, dict]]:
    if detail is None:
        return []
    if not isinstance(detail, list):
        raise ValueError("crawl: 'detail' is a list of steps, e.g. [{use: html.read, with: {...}}]")
    out = []
    for s in detail:
        use = s.get("use") if isinstance(s, dict) else None
        if use not in DETAIL_STEPS:
            raise ValueError(f"crawl: detail steps can be {', '.join(DETAIL_STEPS)}, not {use!r}")
        out.append((use, dict(s.get("with") or {})))
    return out


@connector("crawl")
def crawl(
    input: Any = None,
    start: str | list[str] | None = None,
    list: dict | None = None,  # the YAML key
    detail: Any = None,
    limit: int | None = None,
    delay: float = 1.0,
    same_host: bool = True,
    headers: dict | None = None,
    user_agent: str | None = None,
    timeout: float = 30,
    retries: int = 2,
    on_selector_miss: str = "fail_with_snapshot",
    snapshot_dir: str | None = None,
) -> Records:
    """Open `start` (one URL, a list, or the `url` field of upstream records), take the links
    matching `list.select` (`attr`, default `href`), follow `list.next_page` up to
    `list.max_pages` list pages (default 10), and open each linked page, at most `limit` of them.
    `detail` steps (`html.read`, `html.select`) run on every detail page; each of their records
    gets `url` (the detail page) and `source_url` (the list page). Without `detail` the output is
    one record per page with its `html`. Without `list`, the start URLs are the detail pages.
    Requests go one at a time, `delay` seconds apart (default 1), with an identifying User-Agent,
    through the same private-network guard as every other connector."""
    check_mode(on_selector_miss)
    if isinstance(start, str):
        starts = [start]
    elif start:
        starts = [str(u) for u in start]
    else:
        starts = [str(r["url"]) for r in single(input) if r.get("url")]
    if not starts:
        raise ValueError("crawl: set 'start' (a URL or a list), or feed records with a 'url' field")
    steps = _detail_steps(detail)
    fetch = _Fetcher(headers, delay, timeout, retries, user_agent)
    hosts = {urllib.parse.urlsplit(u).hostname for u in starts}
    if list is not None:
        unknown = set(list) - {"select", "attr", "next_page", "max_pages"}
        if unknown or not list.get("select"):
            raise ValueError("crawl: 'list' takes select (required), attr, next_page and max_pages")

    targets: dict[str, str | None] = {}  # detail URL -> list page, in discovery order
    full = False
    for first in starts:
        if list is None:
            targets.setdefault(first, None)
            continue
        page_url: str | None = first
        seen_pages: set[str] = set()
        max_pages = int(list.get("max_pages") or 10)
        while page_url and page_url not in seen_pages and len(seen_pages) < max_pages and not full:
            seen_pages.add(page_url)
            final, html = fetch.get(page_url)
            root = parse(html)
            base = base_url(root, final)
            links = root.select(list["select"])
            if not links:
                handle_miss(on_selector_miss, list["select"], final, html, snapshot_dir, "list selector")
            attr = list.get("attr") or "href"
            for a in links:
                href = (a.attrs.get(attr) or "").strip()
                if not href or href.startswith(("#", "javascript:", "mailto:")):
                    continue
                link = urllib.parse.urldefrag(urllib.parse.urljoin(base, href))[0]
                if same_host and urllib.parse.urlsplit(link).hostname not in hosts:
                    continue
                targets.setdefault(link, final)
                if limit is not None and len(targets) >= limit:
                    full = True
                    break
            nxt = root.select(list["next_page"]) if list.get("next_page") else []
            href = nxt[0].attrs.get("href", "").strip() if nxt else ""
            page_url = urllib.parse.urljoin(base, href) if href else None
        if full:
            break

    out: Records = []
    for target, source in targets.items():
        final, html = fetch.get(target)
        page = {"html": html, "url": final}
        if not steps:
            out.append({"url": final, "source_url": source, "html": html})
            continue
        for use, params in steps:
            params.setdefault("on_selector_miss", on_selector_miss)
            if snapshot_dir:
                params.setdefault("snapshot_dir", snapshot_dir)
            for r in get(use)([page], **params):
                r.pop("_source", None)
                out.append({**r, "url": final, "source_url": source})
    return out
