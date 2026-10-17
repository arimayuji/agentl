import gzip
import http.server
import threading
import time
from typing import ClassVar

import pytest

from agentl import registry
from agentl.connectors.webcrawl import normalize
from agentl.registry import get


def _page(*links: str) -> str:
    return "<html><body>" + "".join(f'<a href="{h}">{h}</a>' for h in links) + "</body></html>"


class _Site(http.server.BaseHTTPRequestHandler):
    pages: ClassVar[dict] = {}
    hits: ClassVar[list] = []
    headers_seen: ClassVar[list] = []
    flaky: ClassVar[int] = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        cls.hits.append(self.path)
        cls.headers_seen.append(dict(self.headers))
        if self.path == "/busy" and cls.flaky > 0:
            cls.flaky -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        page = cls.pages.get(self.path.split("://", 1)[-1].split("/", 1)[-1].join(["/", ""]))
        if page is None:
            page = cls.pages.get(self.path)
        if page is None:
            self.send_response(404)
            self.end_headers()
            return
        body, ctype, etag = page if isinstance(page, tuple) else (page, "text/html; charset=utf-8", None)
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def site():
    _Site.pages, _Site.hits, _Site.headers_seen, _Site.flaky = {}, [], [], 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def crawl(**kw):
    kw.setdefault("delay", 0)
    return get("web.crawl")(None, **kw)


TREE = {
    "/": _page("/a", "/b", "/a#frag", "/a?", "http://elsewhere.invalid/x"),
    "/a": _page("/a/1", "/"),
    "/b": _page("/b/1"),
    "/a/1": _page("/a/1/deep"),
    "/b/1": _page(),
    "/a/1/deep": _page(),
}


def test_normalize():
    assert normalize("HTTP://Example.ORG:80/p?b=2&a=1#x") == "http://example.org/p?a=1&b=2"
    assert normalize("https://example.org") == "https://example.org/"
    assert normalize("https://example.org:8443/") == "https://example.org:8443/"


def test_depth_limit_dedupe_and_domains(site):
    _Site.pages = dict(TREE)
    out = crawl(start=site + "/", max_depth=1)
    assert [(r["url"].removeprefix(site), r["depth"]) for r in out] == [("/", 0), ("/a", 1), ("/b", 1)]
    assert out[1]["referrer"] == site + "/" and out[0]["status"] == 200 and "<a" in out[0]["html"]
    assert _Site.hits.count("/a") == 1  # /a, /a#frag and /a? are one page
    assert all("elsewhere" not in h for h in _Site.hits)
    ua = _Site.headers_seen[0]["User-Agent"]
    assert ua.startswith("agentl/")


def test_page_limit_and_patterns(site):
    _Site.pages = dict(TREE)
    assert len(crawl(start=site + "/", max_depth=5, max_pages=2)) == 2
    out = crawl(start=site + "/", max_depth=5, include=[r"/$", r"/a"], exclude=[r"deep"])
    assert [r["url"].removeprefix(site) for r in out] == ["/", "/a", "/a/1"]


def test_robots_txt_and_explicit_override(site, caplog):
    _Site.pages = {**TREE, "/robots.txt": ("User-agent: *\nDisallow: /b\n", "text/plain", None)}
    out = crawl(start=site + "/", max_depth=2)
    urls = [r["url"].removeprefix(site) for r in out]
    assert "/b" not in urls and "/b/1" not in urls and "/a/1" in urls
    assert "/b" not in _Site.hits
    with caplog.at_level("WARNING", logger="agentl.crawl"):
        out = crawl(start=site + "/", max_depth=1, obey_robots=False)
    assert "/b" in [r["url"].removeprefix(site) for r in out]
    assert "NOT read" in caplog.text


def test_rate_limit_per_host_and_crawl_delay(site):
    _Site.pages = {"/": _page("/a", "/b"), "/a": _page(), "/b": _page()}
    t0 = time.monotonic()
    crawl(start=site + "/", delay=0.2, obey_robots=False)
    assert time.monotonic() - t0 >= 0.4  # three requests, 0.2 s apart
    _Site.pages["/robots.txt"] = ("User-agent: *\nCrawl-delay: 0.3\n", "text/plain", None)
    _Site.hits = []
    t0 = time.monotonic()
    crawl(start=site + "/", delay=0)
    assert time.monotonic() - t0 >= 0.6 and _Site.hits[0] == "/robots.txt"


def test_retry_after_on_429(site):
    _Site.pages = {"/busy": _page()}
    _Site.flaky = 2
    out = crawl(start=site + "/busy", obey_robots=False, retries=2)
    assert out[0]["status"] == 200 and _Site.hits.count("/busy") == 3


def test_sitemap_index_and_gzip(site):
    sm = (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{site}/a</loc></url><url><loc>{site}/b</loc></url></urlset>"
    )
    idx = (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{site}/sm.xml.gz</loc></sitemap></sitemapindex>"
    )
    _Site.pages = {
        **TREE,
        "/sitemap.xml": (idx, "application/xml", None),
        "/sm.xml.gz": (gzip.compress(sm.encode()), "application/gzip", None),
    }
    out = crawl(sitemap=site + "/sitemap.xml", max_depth=0)
    assert [r["url"].removeprefix(site) for r in out] == ["/a", "/b"]
    _Site.pages["/bad.xml"] = ('<!DOCTYPE x [<!ENTITY a "b">]><urlset/>', "application/xml", None)
    with pytest.raises(ValueError, match="DTD"):
        crawl(sitemap=site + "/bad.xml")


def test_incremental_rerun_skips_unchanged(site):
    _Site.pages = {
        "/": (_page("/a", "/b"), "text/html", '"v1"'),
        "/a": _page(),
        "/b": _page(),
    }
    stored: dict = {}
    state = registry.StepState(lambda: stored.get("v"), lambda v: stored.update(v=v))
    token = registry._STEP_STATE.set(state)
    try:
        first = crawl(start=site + "/", obey_robots=False, incremental=True)
        assert len(first) == 3
        _Site.pages["/b"] = _page("/a")  # changed content
        _Site.headers_seen = []
        second = crawl(start=site + "/", obey_robots=False, incremental=True)
    finally:
        registry._STEP_STATE.reset(token)
    assert [r["url"].removeprefix(site) for r in second] == ["/b"]
    assert _Site.headers_seen[0].get("If-None-Match") == '"v1"'  # / answered 304, links kept


def test_proxy_per_domain(site, monkeypatch):
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    _Site.pages = {"/p": _page()}
    out = crawl(start="http://localhost:9/p", obey_robots=False, proxy={"localhost": site})
    assert out[0]["status"] == 200 and _Site.hits == ["http://localhost:9/p"]


def test_bad_arguments():
    with pytest.raises(ValueError, match="start"):
        crawl()
    with pytest.raises(ValueError, match="format"):
        crawl(start="https://example.org", format="pdf")
    with pytest.raises(ValueError, match="http"):
        crawl(start="file:///etc/passwd")
