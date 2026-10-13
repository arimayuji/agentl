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
