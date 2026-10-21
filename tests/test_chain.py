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


def test_foreach_fans_out_and_after_all_fires_once(tmp_path, monkeypatch):
    from agentl import chain

    calls = []
    monkeypatch.setattr(chain, "call", lambda use, params, rec: calls.append((use, params, rec)))
    parent = spec(
        "plan",
        trigger=[{"webhook": "/p"}],
        on_success=[{"trigger": "load_day", "foreach": "${outputs.t}", "with": {"day": "${item.day}"}}],
        after_all={
            "when": "all_ok and records == 3",
            "do": [{"use": "email.send", "with": {"subject": "done ${run.pipeline}"}}],
        },
    )
    with Store(tmp_path / "s.db") as store:
        svc = Service([parent, spec("load_day")], store)
        seen = []
        orig = svc._run

        def run(s, records, kind):
            if s.name == "load_day":
                seen.append(records[0]["day"])
            return orig(s, records, kind)

        svc._run = run
        svc.submit(parent, [{"day": d} for d in ("a", "b", "c")], "webhook")
        wait(svc, ["plan", "load_day"])
        assert sorted(seen) == ["a", "b", "c"]
        assert calls == [("email.send", {"subject": "done plan"}, calls[0][2])]
        plan_run = store.recent_runs("plan", 1)[0]["id"]
        done = [e for e in store.events(plan_run) if e["type"] == "chain.after_all"]
        assert len(done) == 1 and done[0]["data"]["children"] == 3
        # the dashboard shows the link both ways
        from agentl.dashboard import run_detail

        d = run_detail(store, plan_run)
        assert len(d["chain"]["children"]) == 3 and d["chain"]["parent"] is None
        child = d["chain"]["children"][0]["run_id"]
        assert run_detail(store, child)["chain"]["parent"]["run_id"] == plan_run


def test_after_all_when_sees_a_failed_child(tmp_path, monkeypatch):
    from agentl import chain

    calls = []
    monkeypatch.setattr(chain, "call", lambda *a: calls.append(a))
    parent = spec(
        "plan",
        trigger=[{"webhook": "/p"}],
        on_success=[{"trigger": "kid", "foreach": "${trigger.days}"}],
        after_all={"when": "all_ok", "do": [{"use": "email.send"}]},
    )
    results = iter(["succeeded", "failed"])

    def run(s, r, k):
        return RunResult(run_id=f"{s.name}{id(r)}", status="succeeded" if s.name == "plan" else next(results))

    with Store(tmp_path / "s.db") as store:
        svc = Service([parent, spec("kid")], store, run=run)
        svc.submit(parent, [{"days": [1, 2]}], "webhook")
        wait(svc, ["plan", "kid"])
        assert calls == []
        with pytest.raises(ValueError, match="after_all needs"):
            Service(
                [spec("x", trigger=[{"webhook": "/x"}], after_all={"do": [{"use": "email.send"}]})], store
            )


def test_depends_on_waits_and_skips_when_dependency_failed(tmp_path):
    status = {"ref": "failed"}
    ran = []

    def run(s, r, k):
        ran.append(s.name)
        return RunResult(run_id=s.name, status=status.get(s.name, "succeeded"))

    ref = spec("ref", trigger=[{"webhook": "/r"}])
    load = spec("load", trigger=[{"webhook": "/l"}], depends_on=["ref"])
    with Store(tmp_path / "s.db") as store:
        svc = Service([ref, load], store, run=run)
        svc.submit(load, [], "webhook")  # ref never ran
        wait(svc, ["load"])
        svc.submit(ref, [], "webhook")
        wait(svc, ["ref"])
        svc.submit(load, [], "webhook")  # ref failed
        wait(svc, ["load"])
        assert ran == ["ref"]
        status["ref"] = "succeeded"
        svc.submit(ref, [], "webhook")
        svc.submit(load, [], "webhook")
        wait(svc, ["ref", "load"])
        assert ran == ["ref", "ref", "load"]
        with pytest.raises(ValueError, match="not served"):
            Service([load], store)
        with pytest.raises(ValueError, match="itself"):
            Service([spec("z", trigger=[{"webhook": "/z"}], depends_on=["z"])], store)


def test_emit_to_a_queue_connector_carries_the_run_id(tmp_path, monkeypatch):
    from agentl import registry

    sent = []

    def fake(input, queue_url="", dedup_field=None, **kw):
        sent.append((queue_url, dedup_field, input))
        return [{"sent": len(input)}]

    monkeypatch.setattr(registry, "get", lambda name: fake if name == "sqs.send" else None)
    p = spec(
        "load",
        trigger=[{"webhook": "/l"}],
        after=[
            {
                "emit": {
                    "use": "sqs.send",
                    "with": {"queue_url": "q", "dedup_field": "id"},
                    "topic": "load.done",
                }
            }
        ],
    )
    with Store(tmp_path / "s.db") as store:
        svc = Service([p], store, run=lambda s, r, k: RunResult(run_id="r9", status="succeeded"))
        svc.submit(p, [], "webhook")
        wait(svc, ["load"])
    assert sent == [
        ("q", "id", [{**sent[0][2][0], "id": "r9:load.done", "run_id": "r9", "topic": "load.done"}])
    ]
    for bad in (
        {"topic": "t"},
        {"url": "u", "use": "sqs.send", "topic": "t"},
        {"use": "sqs.send", "secret": "s", "topic": "t"},
    ):
        with pytest.raises(ValueError):
            spec("x", after=[{"emit": bad}])
