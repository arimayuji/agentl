import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from agentl.registry import get


@pytest.fixture
def api():
    seen: list[dict] = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            seen.append(q)
            page = int(q.get("page", 1))
            body = {"items": [{"n": (page - 1) * 2 + i} for i in (1, 2)] if page <= 3 else []}
            body["next_page"] = page + 1 if page < 3 else None
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()
    srv.server_close()


def test_params_and_pagination_by_next_field(api):
    url, seen = api
    rows = get("http.get")(
        None,
        url=f"{url}/invoices",
        params={"per_page": 2, "updated_since": "2026-10-01", "skip": None},
        paginate={"param": "page", "next": "next_page"},
        items="items",
    )
    assert [r["n"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert seen[0] == {"per_page": "2", "updated_since": "2026-10-01", "page": "1"}
    assert len(seen) == 3


def test_pagination_stops_on_empty_page_and_max_pages(api):
    url, seen = api
    rows = get("http.get")(None, url=f"{url}/x", paginate={"param": "page"}, items="items")
    assert len(rows) == 6 and len(seen) == 4  # 4th page is empty
    with pytest.raises(RuntimeError, match="max_pages"):
        get("http.get")(None, url=f"{url}/x", paginate={"param": "page", "max_pages": 2}, items="items")


def test_params_filled_per_record(api):
    url, seen = api
    get("http.get")([{"d": "2026-10-13"}], url=f"{url}/x", params={"day": "{d}"}, items="items")
    assert seen[0]["day"] == "2026-10-13"


ROWS = [{"n": i} for i in range(1, 8)]  # 7 rows


@pytest.fixture
def styles():
    """One API per pagination style over the same 7 rows; records every query."""
    seen: list[tuple[str, dict]] = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = urlparse(self.path).path
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            seen.append((path, q))
            extra = {}
            if path == "/page":  # 3 per page, total pages in meta
                p = int(q["page"])
                body = {"data": ROWS[(p - 1) * 3 : p * 3], "meta": {"totalPages": 3}}
            elif path == "/offset":
                o, n = int(q["offset"]), int(q["limit"])
                body = {"data": ROWS[o : o + n], "total": len(ROWS)}
            elif path == "/cursor":
                o = int(q.get("after", 0))
                body = {"data": ROWS[o : o + 3], "paging": {"next": str(o + 3) if o + 3 < 7 else None}}
            else:  # /link: RFC 5988 Link header, relative next URL
                o = int(q.get("from", 0))
                body = {"data": ROWS[o : o + 3]}
                if o + 3 < 7:
                    extra["Link"] = (
                        f'</link?from={o + 3}&k={q["k"]}>; rel="next", </link?from=0>; rel="first"'
                    )
            data = json.dumps(body).encode()
            self.send_response(200)
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()
    srv.server_close()


def _ns(rows):
    return [r["n"] for r in rows]


def test_page_style_stops_on_total_key_and_fetches_the_rest_concurrently(styles):
    url, seen = styles
    pg = {"style": "page", "param": "page", "total_key": "meta.totalPages", "concurrency": 3}
    rows = get("http.get")(None, url=f"{url}/page", paginate=pg, items="data")
    assert _ns(rows) == list(range(1, 8))
    assert sorted(q["page"] for _, q in seen) == ["1", "2", "3"]  # no 4th, empty page
    with pytest.raises(RuntimeError, match="max_pages"):
        get("http.get")(None, url=f"{url}/page", paginate={**pg, "max_pages": 2}, items="data")


def test_offset_style_with_and_without_total_key(styles):
    url, seen = styles
    rows = get("http.get")(
        None,
        url=f"{url}/offset",
        paginate={"style": "offset", "limit": 3, "total_key": "total"},
        items="data",
    )
    assert _ns(rows) == list(range(1, 8))
    assert [q["offset"] for _, q in seen] == ["0", "3", "6"]
    seen.clear()
    rows = get("http.get")(None, url=f"{url}/offset", paginate={"style": "offset", "limit": 7}, items="data")
    assert _ns(rows) == list(range(1, 8)) and len(seen) == 2  # a full page, then a short one


def test_cursor_style_follows_the_token(styles):
    url, seen = styles
    rows = get("http.get")(
        None,
        url=f"{url}/cursor",
        params={"q": "x"},
        paginate={"style": "cursor", "param": "after", "cursor_key": "paging.next"},
        items="data",
    )
    assert _ns(rows) == list(range(1, 8))
    assert [q.get("after") for _, q in seen] == [None, "3", "6"]
    assert all(q["q"] == "x" for _, q in seen)
    with pytest.raises(ValueError, match="cursor_key"):
        get("http.get")(None, url=f"{url}/cursor", paginate={"style": "cursor"})


def test_link_style_follows_rel_next(styles):
    url, seen = styles
    rows = get("http.get")(
        None, url=f"{url}/link", params={"k": "v"}, paginate={"style": "link"}, items="data"
    )
    assert _ns(rows) == list(range(1, 8))
    assert [q.get("from") for _, q in seen] == [None, "3", "6"]


def test_paginate_rejects_unknown_style_and_keys(styles):
    url, _ = styles
    with pytest.raises(ValueError, match="style"):
        get("http.get")(None, url=f"{url}/page", paginate={"style": "scroll"})
    with pytest.raises(ValueError, match="cursor_key"):
        get("http.get")(None, url=f"{url}/page", paginate={"style": "page", "cursor_key": "x"})


def test_next_link_parsing():
    from agentl.connectors.http import _next_link

    h = {"Link": '<https://a/x?page=2>; rel="next", <https://a/x?page=9>; rel="last"'}
    assert _next_link(h) == "https://a/x?page=2"
    assert _next_link({"Link": '<https://a/1>; rel="prev first"'}) is None
    assert _next_link({"Link": "<https://a/2>; rel=next"}) == "https://a/2"
    assert _next_link({}) is None
