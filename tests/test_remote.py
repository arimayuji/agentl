"""Remote paths through fsspec, tested with its in-memory filesystem (no network)."""

import gzip
import io
import json

import pytest

from agentl.registry import get

fsspec = pytest.importorskip("fsspec")


@pytest.fixture
def mem():
    fs = fsspec.filesystem("memory")
    fs.store.clear()
    fs.pseudo_dirs.clear()
    fs.pseudo_dirs.append("")
    yield fs
    fs.store.clear()


def test_read_glob_from_bucket(mem):
    mem.pipe("/bucket/in/a.csv", b"id;v\n1;x\n")
    mem.pipe("/bucket/in/b.csv", b"id;v\n2;y\n")
    mem.pipe("/bucket/other/c.csv", b"id;v\n3;z\n")
    rows = get("csv.read")(None, path="memory://bucket/in/*.csv")
    assert [r["id"] for r in rows] == ["1", "2"]


def test_file_read_reports_remote_origin_and_expands_archives(mem):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        f.write(b'{"id": 1}\n{"id": 2}\n')
    mem.pipe("/bucket/drop/events.jsonl.gz", buf.getvalue())
    mem.pipe("/bucket/drop/notes.txt", b"hello")
    out = get("file.read")(None, path="memory://bucket/drop/*")
    files = {r["_file"] for r in out}
    assert "memory:///bucket/drop/notes.txt" in files
    assert [r["id"] for r in out if "id" in r] == [1, 2]
    assert any(r.get("_archive") == "memory:///bucket/drop/events.jsonl.gz" for r in out)


def test_upstream_records_with_remote_paths(mem):
    mem.pipe("/b/x.json", json.dumps([{"k": 1}]).encode())
    assert get("json.read")([{"path": "memory://b/x.json"}]) == [{"k": 1}]


def test_missing_remote_file(mem):
    with pytest.raises(FileNotFoundError):
        get("txt.read")(None, path="memory://nope/file.txt")


def test_write_to_bucket(mem):
    rows = [{"id": 1, "v": "a"}]
    assert get("csv.write")(rows, path="memory://out/r.csv") == [{"path": "memory://out/r.csv", "written": 1}]
    get("json.write")(rows, path="memory://out/r.jsonl")
    assert mem.cat("/out/r.csv").decode().splitlines() == ["id,v", "1,a"]
    assert json.loads(mem.cat("/out/r.jsonl")) == {"id": 1, "v": "a"}


def test_missing_fsspec_is_a_clear_error(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "fsspec", None)
    with pytest.raises(ImportError, match=r"agentl-etl\[remote\]"):
        get("csv.read")(None, path="s3://bucket/a.csv")
