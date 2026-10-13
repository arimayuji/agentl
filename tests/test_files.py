import gzip
import http.server
import json
import sys
import threading
import types
import zipfile
from functools import partial

import pytest

from agentl.registry import get


@pytest.fixture
def server(tmp_path):
    """Serve tmp_path over HTTP on localhost."""
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a: None  # type: ignore[attr-defined]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_csv_and_json_from_url(tmp_path, server):
    (tmp_path / "sales.csv").write_text("id;total\n1;10\n2;20\n")
    (tmp_path / "data.json").write_text(json.dumps({"data": {"items": [{"id": 1}, {"id": 2}]}}))

    rows = get("csv.read")(None, url=f"{server}/sales.csv")
    assert rows == [{"id": "1", "total": "10"}, {"id": "2", "total": "20"}]  # delimiter sniffed

    items = get("json.read")(None, url=f"{server}/data.json", key="data.items")
    assert [r["id"] for r in items] == [1, 2]


def test_url_from_upstream_records(tmp_path, server):
    (tmp_path / "a.txt").write_text("hello")
    out = get("txt.read")([{"path": f"{server}/a.txt"}])
    assert out[0]["text"] == "hello"


def test_txt_lines(tmp_path):
    (tmp_path / "notes.txt").write_text("first\n\nsecond\n")
    out = get("txt.read")(None, path=str(tmp_path / "notes.txt"), lines=True)
    assert [(r["line"], r["text"]) for r in out] == [(1, "first"), (3, "second")]


def test_xml_items_and_attributes(tmp_path):
    (tmp_path / "f.xml").write_text(
        '<nfe xmlns="http://x"><item code="A"><qty>2</qty><price>9.5</price></item>'
        '<item code="B"><qty>1</qty><price>3</price></item></nfe>'
    )
    out = get("xml.read")(None, path=str(tmp_path / "f.xml"), items="item")
    assert out == [{"code": "A", "qty": "2", "price": "9.5"}, {"code": "B", "qty": "1", "price": "3"}]


def test_html_tables(tmp_path):
    (tmp_path / "p.html").write_text(
        "<html><body><table><tr><th>Name</th><th>Age</th></tr>"
        "<tr><td>Ana</td><td> 30 </td></tr></table>"
        "<table><tr><th>x</th></tr><tr><td>1</td></tr></table></body></html>"
    )
    out = get("html.read")(None, path=str(tmp_path / "p.html"))
    assert out == [{"table": 0, "Name": "Ana", "Age": "30"}, {"table": 1, "x": "1"}]
    assert get("html.read")(None, path=str(tmp_path / "p.html"), table=1) == [{"table": 1, "x": "1"}]


def test_parquet(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    pq.write_table(pa.table({"id": [1, 2], "name": ["a", "b"]}), tmp_path / "t.parquet")
    out = get("parquet.read")(None, path=str(tmp_path / "t.parquet"), columns=["name"])
    assert out == [{"name": "a"}, {"name": "b"}]


def test_xls_uses_xlrd(tmp_path, monkeypatch):
    sheet = types.SimpleNamespace(
        nrows=3,
        row_values=lambda i: [["id", "name"], [1.0, "Ana"], ["", ""]][i],
    )
    book = types.SimpleNamespace(sheet_by_index=lambda i: sheet, sheet_by_name=lambda n: sheet)
    monkeypatch.setitem(sys.modules, "xlrd", types.SimpleNamespace(open_workbook=lambda p: book))
    (tmp_path / "old.xls").write_bytes(b"fake")
    assert get("excel.read")(None, path=str(tmp_path / "old.xls")) == [{"id": 1.0, "name": "Ana"}]


def test_doc_without_libreoffice_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    (tmp_path / "old.doc").write_bytes(b"fake")
    with pytest.raises(RuntimeError, match="LibreOffice"):
        get("doc.read")(None, path=str(tmp_path / "old.doc"))


def test_file_read_mixed_folder_zip_and_gz(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.csv").write_text("id,v\n1,x\n")
    (inbox / "b.json").write_text('[{"id": 2}]')
    (inbox / "notes.txt").write_text("hi")
    (inbox / "image.png").write_bytes(b"\x89PNG")
    with zipfile.ZipFile(inbox / "batch.zip", "w") as z:
        z.writestr("inner/c.csv", "id,v\n3,y\n")
        z.writestr("readme.md", "doc")
    with gzip.open(inbox / "d.jsonl.gz", "wt") as f:
        f.write('{"id": 4}\n{"id": 5}\n')

    out = get("file.read")(None, path=str(inbox / "*"))
    by_file = {}
    for r in out:
        by_file.setdefault(r["_file"].split("/")[-1], []).append(r)
    assert by_file["a.csv"][0]["id"] == "1"
    assert by_file["b.json"][0]["id"] == 2
    assert by_file["notes.txt"][0]["text"] == "hi"
    assert by_file["batch.zip!c.csv"][0]["id"] == "3"
    assert by_file["batch.zip!c.csv"][0]["_archive"].endswith("batch.zip")
    assert by_file["batch.zip!readme.md"][0]["text"] == "doc"
    assert [r["id"] for r in by_file["d.jsonl.gz!d.jsonl"]] == [4, 5]
    assert not any("image.png" in k for k in by_file)  # skipped

    with pytest.raises(ValueError, match="unsupported"):
        get("file.read")(None, path=str(inbox / "image.png"), on_unsupported="error")


def test_zip_slip_is_rejected(tmp_path):
    with zipfile.ZipFile(tmp_path / "evil.zip", "w") as z:
        z.writestr("../../escape.csv", "id\n1\n")
    with pytest.raises(ValueError, match="unsafe path"):
        get("file.read")(None, path=str(tmp_path / "evil.zip"))


def test_zip_bomb_limit(tmp_path):
    with zipfile.ZipFile(tmp_path / "big.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.csv", "a" * 10_000)
    with pytest.raises(ValueError, match="expands beyond"):
        get("file.read")(None, path=str(tmp_path / "big.zip"), max_extract_bytes=1000)
