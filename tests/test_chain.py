"""Pipeline chaining under agentl serve: on_success / on_failure / after actions (#203)."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentl import parse
from agentl.chain import evaluate, metrics
from agentl.runner import RunResult
from agentl.store import Store
from agentl.triggers import Service


def spec(name, **kw):
    return parse({"name": name, "steps": [{"id": "t", "use": "trigger"}], **kw})


def wait(service, names):
    for _ in range(200):
        for n in names:
            if n in service.queues:
                service.queues[n].join()
        if all(n in service.queues and not service.queues[n].unfinished_tasks for n in names):
            return
        time.sleep(0.01)


def test_when_reads_run_metrics():
    ok = RunResult(run_id="r", status="succeeded", writes={"db": {"inserted": 2, "updated": 1}})
    m = metrics(ok)
    assert m["rows_changed"] == 3 and m["all_ok"] and m["outcome"] == "succeeded"
    assert evaluate("all_ok and rows_changed > 0", m)
    assert not evaluate("rows_changed > 5 or outcome == 'failed'", m)
    assert evaluate("not failed", m) and metrics(None)["outcome"] == "failed"
    for bad in ("__import__('os')", "rows_changed.real > 1", "unknown > 1"):
        with pytest.raises(ValueError):
            parse(
                {
                    "name": "x",
                    "steps": [{"id": "t", "use": "trigger"}],
                    "after": [{"trigger": "y", "when": bad}],
                }
            )


def test_cycles_and_unknown_names_fail_at_start(tmp_path):
    a = spec("a", trigger=[{"cron": "@daily"}], on_success=[{"trigger": "b"}])
    b = spec("b", trigger=[{"after": "a"}], on_failure=[{"trigger": "a"}])
    with Store(tmp_path / "s.db") as store:
        with pytest.raises(ValueError, match="cycle"):
            Service([a, b], store)
        with pytest.raises(ValueError, match="not served"):
            Service([a], store)


def test_success_starts_child_with_params_and_links_runs(tmp_path):
    parent = spec(
        "extract",
        trigger=[{"webhook": "/x"}],
        on_success=[{"trigger": "process", "with": {"rows": "${outputs.t}", "day": "${trigger.day}"}}],
        on_failure=[{"trigger": "cleanup"}],
    )
    child, cleanup = spec("process"), spec("cleanup")
    late = spec("report", trigger=[{"after": "process", "status": "any"}])
    with Store(tmp_path / "s.db") as store:
        svc = Service([parent, child, cleanup, late], store)
        seen = {}
        orig = svc._run

        def run(s, records, kind):
            seen[s.name] = records
            return orig(s, records, kind)

        svc._run = run
        svc.submit(parent, [{"day": "2026-01-02"}], "webhook")
        wait(svc, ["extract", "process", "report"])
        assert seen["process"] == [{"rows": [{"day": "2026-01-02"}], "day": "2026-01-02"}]
        assert seen["report"][0]["pipeline"] == "process" and seen["report"][0]["status"] == "succeeded"
        assert "cleanup" not in seen
        child_run = seen["report"][0]["run_id"]
        link = [e for e in store.events(child_run) if e["type"] == "chain.parent"]
        assert link and link[0]["data"]["pipeline"] == "extract"
        parent_run = link[0]["data"]["run_id"]
        assert any(e["type"] == "chain.child" for e in store.events(parent_run))


def test_failure_runs_on_failure_and_emit_is_idempotent_by_run(tmp_path):
    got = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append((self.headers["Idempotency-Key"], body))
            self.send_response(204)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/events"
    p = spec(
        "load",
        trigger=[{"webhook": "/l"}],
        on_success=[{"trigger": "notify_ok"}],
        on_failure=[{"emit": {"url": url, "topic": "load.failed", "payload": {"run": "${run.id}"}}}],
        after=[{"emit": {"url": url, "topic": "load.done"}, "when": "rows_changed > 0"}],
    )
    try:
        with Store(tmp_path / "s.db") as store:
            svc = Service(
                [p, spec("notify_ok")], store, run=lambda s, r, k: RunResult(run_id="r1", status="failed")
            )
            svc.submit(p, [], "webhook")
            wait(svc, ["load"])
    finally:
        srv.shutdown()
    assert got == [
        (
            "r1:load.failed",
            {
                "id": "r1:load.failed",
                "topic": "load.failed",
                "pipeline": "load",
                "run_id": "r1",
                "status": "failed",
                "payload": {"run": "r1"},
            },
        )
    ]
