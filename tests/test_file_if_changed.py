import http.server
import io
import threading
import zipfile

import pytest

from agentl.registry import get
from agentl.runner import Runner
from agentl.spec import PipelineSpec
from agentl.store import open_store


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("psd/data.csv", text)
    return buf.getvalue()


@pytest.fixture
def files():
    state = {
        "csv": b"id,name\n1,alpha\n",
        "etag": '"v1"',
        "last_modified": "Thu, 01 Oct 2026 00:00:00 GMT",
        "hits": [],
    }

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            inm, ims = self.headers.get("If-None-Match"), self.headers.get("If-Modified-Since")
            state["hits"].append((self.path, inm, ims))
            data = _zip(state["csv"].decode()) if self.path.endswith(".zip") else state["csv"]
            if (state["etag"] and inm == state["etag"]) or (
                state["last_modified"] and ims == state["last_modified"]
            ):
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            if state["etag"]:
                self.send_header("ETag", state["etag"])
            if state["last_modified"]:
                self.send_header("Last-Modified", state["last_modified"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()
    srv.server_close()


def _run(tmp_path, steps):
    spec = PipelineSpec.model_validate({"name": "psd", "steps": steps})
    with open_store(tmp_path / "s.db") as store:
        result = Runner(spec, store=store, use_cache=False).run()
        return result, store.state_get("psd", steps[0]["id"])


def test_csv_read_etag_ends_run_as_no_change(files, tmp_path):
    url, state = files
    steps = [
        {"id": "src", "use": "csv.read", "with": {"url": f"{url}/psd.csv", "if_changed": {"by": ["etag"]}}},
        {"id": "out", "fn": "builtins:list", "input": "src"},
    ]
    first, saved = _run(tmp_path, steps)
    assert first.status == "succeeded", first.error
    assert first.outputs["src"] == [{"id": "1", "name": "alpha"}]
    assert saved["etag"] == '"v1"'
    second, _ = _run(tmp_path, steps)
    assert second.status == "no_change" and "out" not in second.outputs
    assert state["hits"][-1] == ("/psd.csv", '"v1"', None)
    state["csv"], state["etag"] = b"id,name\n2,beta\n", '"v2"'
    third, saved = _run(tmp_path, steps)
    assert third.status == "succeeded" and third.outputs["src"] == [{"id": "2", "name": "beta"}]
    assert saved["etag"] == '"v2"'


def test_archive_read_last_modified(files, tmp_path):
    url, state = files
    state["etag"] = None
    cond = {"by": ["last_modified"]}
    steps = [
        {
            "id": "zip",
            "use": "archive.read",
            "with": {"url": f"{url}/psd.zip", "pattern": "csv$", "if_changed": cond},
        },
        {"id": "rows", "use": "csv.read", "input": "zip"},
    ]
    first, saved = _run(tmp_path, steps)
    assert first.status == "succeeded", first.error
    assert first.outputs["rows"] == [{"id": "1", "name": "alpha"}]
    assert saved["last_modified"] == state["last_modified"]
    second, _ = _run(tmp_path, steps)
    assert second.status == "no_change"
    assert state["hits"][-1] == ("/psd.zip", None, state["last_modified"])


def test_file_read_hash_fallback_continue_empty(files, tmp_path):
    url, state = files
    state["etag"] = state["last_modified"] = None  # no validators: the body hash decides
    steps = [
        {
            "id": "src",
            "use": "file.read",
            "with": {"url": f"{url}/psd.csv", "if_changed": {}, "on_unchanged": "continue_empty"},
        },
    ]
    assert len(_run(tmp_path, steps)[0].outputs["src"]) == 1
    result, saved = _run(tmp_path, steps)
    assert result.status == "succeeded" and result.outputs["src"] == []
    assert "hash" in saved
    state["csv"] = b"id,name\n1,alpha\n2,beta\n"
    assert len(_run(tmp_path, steps)[0].outputs["src"]) == 2


def test_if_changed_needs_url_and_works_without_store(files, tmp_path):
    url, _ = files
    rows = get("csv.read")(url=f"{url}/psd.csv", if_changed={"by": ["etag"]})
    assert rows == [{"id": "1", "name": "alpha"}]
    local = tmp_path / "a.csv"
    local.write_text("id\n1\n")
    with pytest.raises(ValueError, match="only with 'url'"):
        get("csv.read")(path=str(local), if_changed={})
