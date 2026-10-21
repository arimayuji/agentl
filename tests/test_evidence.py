"""Evidence store (#293): content-addressed artifacts, `when`, the run store index and the CLI."""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from typing import ClassVar

import pytest

from agentl import evidence, parse
from agentl.cli import main
from agentl.registry import get
from agentl.runner import Runner
from agentl.store import Store


class _Site(http.server.BaseHTTPRequestHandler):
    pages: ClassVar[dict[str, tuple[int, str, str]]] = {}

    def log_message(self, *a):
        pass

    def _answer(self):
        status, ctype, body = self.pages.get(self.path.split("?")[0], (404, "text/plain", "missing"))
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("ETag", '"v1"')
        self.send_header("Set-Cookie", "session=secret")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _answer


@pytest.fixture
def site(monkeypatch):
    monkeypatch.delenv("AGENTL_EVIDENCE_DIR", raising=False)
    _Site.pages = {
        "/robots.txt": (200, "text/plain", ""),
        "/": (200, "text/html", '<html><body><a href="/a">a</a></body></html>'),
        "/a": (200, "text/html", "<html><body>page a</body></html>"),
        "/api": (200, "application/json", json.dumps({"items": [{"id": 1}, {"id": 2}]})),
        "/broken": (500, "application/json", '{"error": "boom"}'),
    }
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _crawl(base: str, ev: dict) -> object:
    return parse(
        {
            "name": "evcrawl",
            "steps": [
                {"id": "pages", "use": "web.crawl", "with": {"start": base + "/", "delay": 0, "evidence": ev}}
            ],
        }
    )


def test_crawl_stores_changed_pages_once_and_show_returns_the_bytes(site, tmp_path, capsysbinary):
    db = tmp_path / "a.db"
    with Store(db) as store:
        first = Runner(_crawl(site, {"raw": True, "request": True}), store=store).run()
        assert first.status == "succeeded", first.error
        recs = first.outputs["pages"]
        assert len(recs) == 2 and all(len(r["_evidence"]) == 64 for r in recs)
        page_a = next(r for r in recs if r["url"].endswith("/a"))
        assert page_a["_evidence"] == hashlib.sha256(b"<html><body>page a</body></html>").hexdigest()
        rows = store.evidence_list(run_id=first.run_id)
        assert sorted(r["kind"] for r in rows) == ["raw", "raw", "request", "request"]
        assert all(r["step"] == "pages" and r["pipeline"] == "evcrawl" for r in rows)
        stored = tmp_path / "evidence" / "objects"
        objects = sorted(p.name for p in stored.rglob("*") if p.is_file())

        second = Runner(_crawl(site, {"raw": True, "request": True}), store=store).run()
        assert store.evidence_list(run_id=second.run_id) == []  # nothing changed, nothing new
        assert sorted(p.name for p in stored.rglob("*") if p.is_file()) == objects
        assert [r["_evidence"] for r in second.outputs["pages"]] == [r["_evidence"] for r in recs]

        _Site.pages["/a"] = (200, "text/html", "<html><body>page a, edited</body></html>")
        third = Runner(_crawl(site, {"raw": True}), store=store).run()
        assert [r["url"].endswith("/a") for r in store.evidence_list(run_id=third.run_id)] == [True]

    capsysbinary.readouterr()
    assert main(["evidence", "show", page_a["_evidence"][:16], "--db", str(db)]) == 0
    assert capsysbinary.readouterr().out == b"<html><body>page a</body></html>"
    request = next(r for r in rows if r["kind"] == "request" and r["url"].endswith("/a"))
    assert main(["evidence", "show", request["sha256"], "--db", str(db)]) == 0
    meta = json.loads(capsysbinary.readouterr().out)
    assert meta["status"] == 200 and meta["etag"] == '"v1"' and meta["method"] == "GET"
    assert meta["response_headers"]["Set-Cookie"] == "***"


def test_when_always_and_on_failure(site, tmp_path):
    with Store(tmp_path / "a.db") as store:
        for _ in range(2):
            run = Runner(_crawl(site, {"when": "always"}), store=store).run()
            assert len(store.evidence_list(run_id=run.run_id)) == 2
        run = Runner(_crawl(site, {"when": "on_failure"}), store=store).run()
        assert store.evidence_list(run_id=run.run_id) == []
        assert all("_evidence" not in r for r in run.outputs["pages"])


def test_http_get_tags_records_and_captures_failures(site, tmp_path):
    spec = parse(
        {
            "name": "evhttp",
            "steps": [
                {
                    "id": "api",
                    "use": "http.get",
                    "with": {
                        "url": site + "/api?token=abc",
                        "items": "items",
                        "headers": {"Authorization": "Bearer xyz"},
                        "evidence": {"raw": True, "request": True},
                    },
                }
            ],
        }
    )
    with Store(tmp_path / "a.db") as store:
        run = Runner(spec, store=store).run()
        assert run.status == "succeeded", run.error
        recs = run.outputs["api"]
        body = json.dumps({"items": [{"id": 1}, {"id": 2}]}).encode()
        assert {r["_evidence"] for r in recs} == {hashlib.sha256(body).hexdigest()}
        assert all(len(r["_evidence_request"]) == 64 for r in recs)
        rows = {r["kind"]: r for r in store.evidence_list(run_id=run.run_id)}
        assert "token=%2A%2A%2A" in rows["raw"]["url"] or "token=***" in rows["raw"]["url"]
        meta = json.loads(evidence.read(rows["request"]))
        assert meta["request_headers"]["Authorization"] == "***"

        failing = parse(
            {
                "name": "evhttp",
                "steps": [
                    {
                        "id": "api",
                        "use": "http.get",
                        "with": {"url": site + "/broken", "retries": 0, "evidence": {"when": "on_failure"}},
                    }
                ],
            }
        )
        bad = Runner(failing, store=store).run()
        assert bad.status == "failed"
        [row] = store.evidence_list(run_id=bad.run_id)
        assert evidence.read(row) == b'{"error": "boom"}'


def test_storage_url_and_verify(site, tmp_path, capsys):
    pytest.importorskip("fsspec")  # storage URLs need the `remote` extra
    db = tmp_path / "a.db"
    ev = {"storage": "memory://evidence-test/objects-root"}
    with Store(db) as store:
        run = Runner(_crawl(site, ev), store=store).run()
        assert run.status == "succeeded", run.error
        rows = store.evidence_list(run_id=run.run_id)
        assert {r["location"] for r in rows} == {ev["storage"]}
        assert not (tmp_path / "evidence").exists()
    assert main(["evidence", "verify", "--db", str(db)]) == 0
    assert "2 artifact(s): 0 problem(s)" in capsys.readouterr().out

    local = {"path": str(tmp_path / "ev")}
    with Store(db) as store:
        run = Runner(_crawl(site, local), store=store).run()
        row = store.evidence_list(run_id=run.run_id)[0]
    target = next(p for p in (tmp_path / "ev" / "objects").rglob(row["sha256"] + "*"))
    target.chmod(0o600)
    target.write_bytes(b"tampered")
    assert main(["evidence", "verify", run.run_id, "--db", str(db)]) == 1
    assert "hash differs" in capsys.readouterr().out
    assert main(["evidence", "ls", run.run_id, "--db", str(db), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_options_are_checked():
    with pytest.raises(ValueError, match="screenshot needs a browser"):
        evidence.options({"screenshot": True}, "web.crawl")
    with pytest.raises(ValueError, match="when must be"):
        evidence.options({"when": "sometimes"}, "http.get")
    with pytest.raises(ValueError, match="does not take"):
        evidence.options({"keep": "90d"}, "http.get")
    assert evidence.options(None, "http.get") is None
    assert evidence.options(True, "http.get").raw  # type: ignore[union-attr]


def test_outside_a_run_objects_are_kept_without_an_index(site, tmp_path):
    out = get("http.get")(None, url=site + "/api", items="items", evidence={"path": str(tmp_path)})
    sha = out[0]["_evidence"]
    assert (tmp_path / "objects" / sha[:2] / f"{sha}.body").is_file()
