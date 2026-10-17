import http.server
import threading
from pathlib import Path
from typing import ClassVar

import pytest

from agentl import parse
from agentl.connectors.htmldom import SelectorError
from agentl.connectors.htmldom import parse as parse_html
from agentl.connectors.scrape import SelectorMiss
from agentl.netguard import BlockedURL
from agentl.registry import get
from agentl.runner import Runner
from agentl.store import Store


def _list_page(n: int, links: list[str], next_page: str | None) -> str:
    items = "".join(f'<div class="item"><a href="{h}">Report {h}</a></div>' for h in links)
    nxt = f'<a class="next" href="{next_page}">Next</a>' if next_page else ""
    return f"<html><body><h1>Reports page {n}</h1>{items}<a href='#top'>top</a>{nxt}</body></html>"


def _detail(code: str, price: str) -> str:
    return (
        f"<html><body><h2 class='title'> Report {code} </h2>"
        f"<span class='price'>{price}</span><a class='pdf' href='/files/{code}.pdf'>PDF</a>"
        "<table class='data'><tr><th rowspan='2'>Region</th><th colspan='2'>Sales</th></tr>"
        "<tr><th>Q1</th><th>Q2</th></tr><tr><td>North</td><td>10</td><td>20</td></tr></table>"
        "</body></html>"
    )


PAGES = {
    "/reports": _list_page(1, ["/r/1", "/r/2"], "/reports?page=2"),
    "/reports?page=2": _list_page(2, ["/r/3", "http://other.example.org/r/9"], "/reports?page=3"),
    "/reports?page=3": _list_page(3, ["/r/4"], None),
    "/r/1": _detail("A1", "R$ 1.234,50"),
    "/r/2": _detail("A2", "R$ 10,00"),
    "/r/3": _detail("A3", "R$ 3,00"),
    "/r/4": _detail("A4", "R$ 4,00"),
    "/changed": "<html><body><p>New layout</p></body></html>",
}


class _Site(http.server.BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []
    agents: ClassVar[list[str]] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).hits.append(self.path)
        type(self).agents.append(self.headers.get("User-Agent", ""))
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def site():
    _Site.hits, _Site.agents = [], []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------- selectors
def test_css_selectors():
    root = parse_html(
        "<div id='main'><ul class='list big'><li><a href='/a' data-x='1'>A</a></li>"
        "<li><a href='/b'>B</a></li><li><span><a href='/c'>C</a></span></li></ul><p>x<p>y</div>"
    )
    text = lambda sel: [n.text() for n in root.select(sel)]  # noqa: E731
    assert text("ul.list.big > li > a") == ["A", "B"]
    assert text("#main a") == ["A", "B", "C"]
    assert text("a[data-x]") == ["A"] and text("a[href$='c']") == ["C"] and text("a[href^=/b]") == ["B"]
    assert text("li:first-child a") == ["A"] and text("li:nth-child(2) a, li:last-child a") == ["B", "C"]
    assert text("p") == ["x", "y"]  # an open <p> is closed by the next one
    with pytest.raises(SelectorError, match="not supported"):
        root.select("li + li")


def test_html_select_fields_attrs_absolute_and_types(site):
    out = get("html.select")(
        None,
        url=site + "/r/1",
        fields={
            "title": "h2.title",
            "price": {"select": ".price", "type": "money"},
            "pdf": {"select": "a.pdf", "attr": "href", "absolute": True},
            "raw": {"select": "a.pdf", "attr": "href"},
            "cells": {"select": "td", "all": True},
            "note": {"select": ".note", "required": False},
        },
    )
    assert out == [
        {
            "title": "Report A1",
            "price": 1234.5,
            "pdf": site + "/files/A1.pdf",
            "raw": "/files/A1.pdf",
            "cells": ["North", "10", "20"],
            "note": None,
            "_source": site + "/r/1",
        }
    ]


def test_html_select_items_from_upstream_html_records():
    html = "<div class='card'><h2> One </h2><b>3</b></div><div class='card'><h2>Two</h2><b>4</b></div>"
    out = get("html.select")(
        [{"html": html, "url": "https://example.org/list"}],
        items="div.card",
        fields={"name": "h2", "qty": {"select": "b", "type": "int"}},
    )
    assert [(r["name"], r["qty"], r["_source"]) for r in out] == [
        ("One", 3, "https://example.org/list"),
        ("Two", 4, "https://example.org/list"),
    ]


@pytest.mark.parametrize("items", [None, "div.card"])
def test_html_select_preserves_upstream_page_urls(items):
    url = "https://example.org/detail"
    source_url = "https://example.org/list"
    html = "<h1>Page</h1><div class='card'><b>One</b></div><div class='card'><b>Two</b></div>"
    out = get("html.select")(
        [{"html": html, "url": url, "source_url": source_url}],
        items=items,
        fields={"title": "b" if items else "h1"},
    )

    assert [record["title"] for record in out] == (["Page"] if items is None else ["One", "Two"])
    assert [record["url"] for record in out] == [url] * len(out)
    assert [record["source_url"] for record in out] == [source_url] * len(out)
    assert all(record["_source"] == url for record in out)


def test_html_select_keeps_source_url_absent_when_upstream_omits_it():
    out = get("html.select")(
        [{"html": "<h1>Page</h1>", "url": "https://example.org/detail"}],
        fields={"title": "h1"},
    )

    assert "source_url" not in out[0]


def test_bad_type_names_the_field():
    with pytest.raises(ValueError, match="'qty'"):
        get("html.select")([{"html": "<b>many</b>"}], fields={"qty": {"select": "b", "type": "int"}})


# ---------------------------------------------------------------- tables
def test_header_rows_and_spans_are_denormalised(tmp_path):
    (tmp_path / "t.html").write_text(
        "<table class='skip'><tr><th>x</th></tr><tr><td>1</td></tr></table>"
        "<table class='data'>"
        "<tr><th rowspan='2'>Region</th><th colspan='2'>Sales</th><th rowspan='2'>Note</th></tr>"
        "<tr><th>Q1</th><th>Q2</th></tr>"
        "<tr><td rowspan='2'>North</td><td>1</td><td>2</td><td>a</td></tr>"
        "<tr><td colspan='2'>3</td><td>b</td></tr>"
        "<tr><td>South</td><td>5</td></tr>"
        "</table>"
    )
    out = get("html.read")(None, path=str(tmp_path / "t.html"), select="table.data", header_rows=2)
    assert out == [
        {"table": 0, "Region": "North", "Sales Q1": "1", "Sales Q2": "2", "Note": "a"},
        {"table": 0, "Region": "North", "Sales Q1": "3", "Sales Q2": "3", "Note": "b"},
        {"table": 0, "Region": "South", "Sales Q1": "5"},
    ]


def test_html_read_select_miss_saves_a_snapshot(tmp_path):
    page = "<html><body><p>no tables today</p></body></html>"
    with pytest.raises(SelectorMiss, match=r"'table\.data' matched nothing on https://example\.org/x") as e:
        get("html.read")(
            [{"html": page, "url": "https://example.org/x"}], select="table.data", snapshot_dir=str(tmp_path)
        )
    assert e.value.snapshot and Path(e.value.snapshot).read_text() == page
    assert str(tmp_path) in str(e.value)
    assert get("html.read")([{"html": page}], select="table.data", on_selector_miss="skip") == []


# ---------------------------------------------------------------- crawl
DETAIL = [
    {
        "use": "html.select",
        "with": {"fields": {"title": "h2.title", "price": {"select": ".price", "type": "money"}}},
    }
]


def test_crawl_list_pagination_and_detail(site):
    out = get("crawl")(
        None,
        start=site + "/reports",
        list={"select": "div.item a", "next_page": "a.next"},
        detail=DETAIL,
        delay=0,
    )
    assert [(r["title"], r["price"]) for r in out] == [
        ("Report A1", 1234.5),
        ("Report A2", 10.0),
        ("Report A3", 3.0),
        ("Report A4", 4.0),
    ]
    assert out[0]["url"] == site + "/r/1" and out[0]["source_url"] == site + "/reports"
    assert out[3]["source_url"] == site + "/reports?page=3"
    assert all("other.example.org" not in h for h in _Site.hits)  # other hosts are not followed
    assert all(a.startswith("agentl/") for a in _Site.agents)


def test_crawl_max_pages_limit_and_html_records(site):
    out = get("crawl")(
        None,
        start=site + "/reports",
        list={"select": "div.item a", "next_page": "a.next", "max_pages": 1},
        delay=0,
    )
    assert [r["url"] for r in out] == [site + "/r/1", site + "/r/2"]
    assert "Report A1" in out[0]["html"] and "/reports?page=2" not in _Site.hits
    _Site.hits = []
    out = get("crawl")(
        None, start=site + "/reports", list={"select": "div.item a", "next_page": "a.next"}, limit=3, delay=0
    )
    assert len(out) == 3 and "/reports?page=3" not in _Site.hits and "/r/4" not in _Site.hits


def test_crawl_detail_html_read_tables(site):
    out = get("crawl")(
        None,
        start=[site + "/r/2"],
        detail=[{"use": "html.read", "with": {"select": "table.data", "header_rows": 2}}],
        delay=0,
    )
    assert out == [
        {
            "table": 0,
            "Region": "North",
            "Sales Q1": "10",
            "Sales Q2": "20",
            "url": site + "/r/2",
            "source_url": None,
        }
    ]


def test_crawl_delay_between_requests(site):
    import time

    t0 = time.monotonic()
    get("crawl")(None, start=[site + "/r/1", site + "/r/2"], delay=0.2)
    assert time.monotonic() - t0 >= 0.2


@pytest.mark.parametrize("mode", ["fail_with_snapshot", "skip", "empty"])
def test_crawl_selector_miss_modes(site, tmp_path, mode):
    run = lambda: get("crawl")(  # noqa: E731
        None,
        start=[site + "/r/1", site + "/changed"],
        detail=DETAIL,
        delay=0,
        on_selector_miss=mode,
        snapshot_dir=str(tmp_path),
    )
    if mode == "fail_with_snapshot":
        with pytest.raises(SelectorMiss, match=rf"'h2.title' matched nothing on {site}/changed") as e:
            run()
        assert Path(e.value.snapshot).read_text() == PAGES["/changed"]
        return
    out = run()
    if mode == "skip":
        assert [r["title"] for r in out] == ["Report A1"]
    else:
        assert [r["title"] for r in out] == ["Report A1", None]
    assert not list(tmp_path.iterdir())  # no snapshot unless failing


def test_crawl_list_selector_miss(site, tmp_path):
    with pytest.raises(SelectorMiss, match=r"list selector 'div\.gone a'"):
        get("crawl")(
            None, start=site + "/reports", list={"select": "div.gone a"}, delay=0, snapshot_dir=str(tmp_path)
        )


def test_crawl_rejects_unknown_detail_steps():
    with pytest.raises(ValueError, match="detail steps can be"):
        get("crawl")(None, start="https://example.org", detail=[{"use": "http.post"}])


def test_crawl_keeps_the_private_network_guard(site, monkeypatch):
    monkeypatch.delenv("AGENTL_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("AGENTL_ALLOW_HOSTS", raising=False)
    with pytest.raises(BlockedURL):
        get("crawl")(None, start=site + "/r/1", delay=0)
    assert _Site.hits == []


def test_selector_miss_event_in_a_run(site, tmp_path):
    spec = parse(
        {
            "name": "reports",
            "steps": [
                {
                    "id": "reports",
                    "use": "crawl",
                    "with": {
                        "start": site + "/changed",
                        "detail": DETAIL,
                        "delay": 0,
                        "snapshot_dir": str(tmp_path),
                    },
                }
            ],
        }
    )
    with Store(tmp_path / "db.sqlite") as store:
        result = Runner(spec, store=store).run()
        assert result.status == "failed" and "h2.title" in result.error
        [ev] = [e["data"] for e in store.events(result.run_id) if e["type"] == "selector.missed"]
    assert ev["selector"] == "h2.title" and ev["url"] == site + "/changed"
    assert Path(ev["snapshot"]).read_text() == PAGES["/changed"]
