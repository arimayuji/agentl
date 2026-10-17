import http.server
import json
import threading

import pytest

from agentl.registry import NoChange, get
from agentl.runner import Runner
from agentl.spec import PipelineSpec
from agentl.store import open_store


@pytest.fixture
def site():
    state = {"body": [{"n": 1}], "etag": '"v1"', "stamp": "01/10/2026", "hits": []}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, with_body: bool):
            self.state = state
            if self.path == "/stamp":
                data = f"<p>Updated on {state['stamp']}</p>".encode()
            else:
                data = json.dumps(state["body"]).encode()
                if state["etag"] and self.headers.get("If-None-Match") == state["etag"]:
                    self.send_response(304)
                    self.end_headers()
                    return
            self.send_response(200)
            if state["etag"]:
                self.send_header("ETag", state["etag"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if with_body:
                self.wfile.write(data)

        def do_GET(self):
            state["hits"].append(("GET", self.path, self.headers.get("If-None-Match")))
            self._send(True)

        def do_HEAD(self):
            state["hits"].append(("HEAD", self.path, None))
            self._send(False)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()
    srv.server_close()


def _spec(url: str, **with_) -> PipelineSpec:
    return PipelineSpec.model_validate(
        {
            "name": "report",
            "steps": [
                {"id": "fetch", "use": "http.get", "with": {"url": f"{url}/data", **with_}},
                {"id": "out", "fn": "builtins:list", "input": "fetch"},
            ],
        }
    )


def _run(tmp_path, spec):
    with open_store(tmp_path / "s.db") as store:
        result = Runner(spec, store=store, use_cache=False).run()
        return result, store.state_get("report", "fetch")


def test_etag_304_ends_run_as_no_change(site, tmp_path):
    url, state = site
    spec = _spec(url, if_changed={"by": ["etag"]})
    first, saved = _run(tmp_path, spec)
    assert first.status == "succeeded", first.error
    assert first.outputs["fetch"] == [{"n": 1}]
    assert saved["etag"] == '"v1"'
    second, _ = _run(tmp_path, spec)
    assert second.status == "no_change" and "out" not in second.outputs
    assert state["hits"][-1] == ("GET", "/data", '"v1"')
    state["body"], state["etag"] = [{"n": 2}], '"v2"'
    third, saved = _run(tmp_path, spec)
    assert third.status == "succeeded" and third.outputs["fetch"] == [{"n": 2}]
    assert saved["etag"] == '"v2"'


def test_page_stamp_skips_the_download(site, tmp_path):
    url, state = site
    stamp = {"url": f"{url}/stamp", "regex": r"Updated on (\d{2}/\d{2}/\d{4})"}
    spec = _spec(url, if_changed={"by": ["page_stamp"], "page_stamp": stamp})
    assert _run(tmp_path, spec)[0].status == "succeeded"
    state["hits"].clear()
    result, saved = _run(tmp_path, spec)
    assert result.status == "no_change"
    assert state["hits"] == [("GET", "/stamp", None)]
    assert saved["page_stamp"] == "01/10/2026"


def test_hash_fallback_and_continue_empty(site, tmp_path):
    url, state = site
    state["etag"] = None  # server without validators: the body hash decides
    spec = _spec(url, if_changed={"by": ["etag"]}, on_unchanged="continue_empty")
    assert _run(tmp_path, spec)[0].outputs["fetch"] == [{"n": 1}]
    result, saved = _run(tmp_path, spec)
    assert result.status == "succeeded" and result.outputs["fetch"] == []
    assert "hash" in saved


def test_content_length_by_head(site, tmp_path):
    url, state = site
    state["etag"] = None
    spec = _spec(url, if_changed={"by": ["content_length"]})
    _run(tmp_path, spec)
    state["hits"].clear()
    assert _run(tmp_path, spec)[0].status == "no_change"
    assert state["hits"] == [("HEAD", "/data", None)]


def test_direct_call_without_run_store_downloads(site):
    url, _ = site
    rows = get("http.get")(url=f"{url}/data", if_changed={"by": ["etag"]})
    assert rows == [{"n": 1}]
    with pytest.raises(ValueError):
        get("http.get")(url=f"{url}/data", if_changed={"by": ["size"]})
    assert issubclass(NoChange, Exception)
