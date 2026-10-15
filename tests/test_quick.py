"""agentl.read / write / http_get_all: the short Python API for one-off jobs (#137)."""

import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

import agentl as al

ROWS = [{"id": 1, "name": "Ana", "total": 10.5}, {"id": 2, "name": "Bruno", "total": 20.0}]


@pytest.mark.parametrize("ext", ["csv", "tsv", "json", "jsonl", "xlsx", "parquet"])
def test_write_then_read_every_table_format(tmp_path, ext):
    if ext == "xlsx":
        pytest.importorskip("openpyxl")
    if ext == "parquet":
        pytest.importorskip("pyarrow")
    target = tmp_path / "nested" / f"rows.{ext}"
    done = al.write(ROWS, str(target))
    assert done["written"] == 2 and target.is_file()
    back = al.read(str(target))
    assert [str(r["name"]) for r in back] == ["Ana", "Bruno"]
    assert [float(r["total"]) for r in back] == [10.5, 20.0]


def test_globs_text_and_errors(tmp_path):
    for day in ("13", "14"):
        al.write([{"day": day, "n": 1}], str(tmp_path / f"sales-{day}.csv"))
    assert [r["day"] for r in al.read(str(tmp_path / "sales-*.csv"))] == ["13", "14"]
    (tmp_path / "note.txt").write_text("hello")
    assert al.read(str(tmp_path / "note.txt"))[0]["text"] == "hello"
    with pytest.raises(FileNotFoundError):
        al.read(str(tmp_path / "missing.csv"))
    with pytest.raises(ValueError, match=r"no writer for '\.pdf'"):
        al.write(ROWS, str(tmp_path / "x.pdf"))


def test_sqlite_without_sqlalchemy(tmp_path):
    url = f"sqlite:///{tmp_path / 'db' / 'shop.db'}"
    assert al.write(ROWS, url, table="customers")["written"] == 2
    rows = al.read(url, query="select name from customers order by id")
    assert rows == [{"name": "Ana"}, {"name": "Bruno"}]
    with pytest.raises(ValueError, match="needs query="):
        al.read(url)
    with pytest.raises(ValueError, match="needs table="):
        al.write(ROWS, url)


@pytest.fixture
def api():
    pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}], 3: []}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            page = int(parse_qs(url.query).get("page", ["1"])[0])
            if url.path == "/plain":
                body = pages[page]
            elif url.path == "/rows.json":
                body = ROWS
            else:
                body = {"data": pages[page], "next_page": page + 1 if page < 2 else None}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_http_get_all_and_urls(api):
    assert [r["id"] for r in al.http_get_all(f"{api}/items", items="data", next="next_page")] == [1, 2, 3]
    assert [r["id"] for r in al.http_get_all(f"{api}/plain")] == [1, 2, 3]  # stops at the empty page
    assert al.read(f"{api}/rows.json")[0]["name"] == "Ana"
    assert al.read(f"{api}/items", items="data")[0] == {"id": 1}  # one JSON response
