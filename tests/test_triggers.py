"""agentl serve: schedules, webhooks and new files (#20)."""

import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from agentl import parse
from agentl.runner import Runner
from agentl.store import Store
from agentl.triggers import Cron, Service, authorized, new_files

D = dt.datetime


def test_cron_matching():
    c = Cron("0 7 * * 1-5")
    assert c.matches(D(2026, 10, 14, 7, 0)) and not c.matches(D(2026, 10, 18, 7, 0))  # Wed yes, Sun no
    assert c.next_after(D(2026, 10, 16, 7, 0)) == D(2026, 10, 19, 7, 0)  # Fri 07:00 -> Mon 07:00
    assert Cron("*/15 9-10 * * *").next_after(D(2026, 1, 1, 10, 50)) == D(2026, 1, 2, 9, 0)
    assert Cron("30 6 1,15 * *").matches(D(2026, 3, 15, 6, 30))
    assert Cron("0 0 1 * mon").matches(D(2026, 10, 19, 0, 0))  # both day fields: either one
    assert Cron("0 12 * jan-mar sun").matches(D(2026, 2, 1, 12, 0)) and Cron("0 0 * * 7").matches(
        D(2026, 2, 1)
    )
    assert Cron("@daily").next_after(D(2026, 1, 1, 0, 0)) == D(2026, 1, 2)
    for bad in ("* * *", "61 * * * *", "0 7 * * funday", "*/0 * * * *"):
        with pytest.raises(ValueError):
            Cron(bad)


def test_spec_validation():
    with pytest.raises(
        ValueError, match="exactly one of cron, schedule, webhook, files, after, gather or every"
    ):
        parse(
            {
                "name": "x",
                "trigger": [{"cron": "0 * * * *", "webhook": "/a"}],
                "steps": [{"id": "a", "use": "trigger"}],
            }
        )
    with pytest.raises(ValueError, match="five fields"):
        parse({"name": "x", "trigger": [{"cron": "daily"}], "steps": [{"id": "a", "use": "trigger"}]})
    with pytest.raises(ValueError, match="secret belongs to webhook"):
        parse(
            {
                "name": "x",
                "trigger": [{"cron": "@daily", "secret": "s"}],
                "steps": [{"id": "a", "use": "trigger"}],
            }
        )


def test_webhook_auth():
    body = b'{"id": 1}'
    sig = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert authorized(None, {}, body)
    assert authorized("k", {"Authorization": "Bearer k"}, body)
    assert authorized("k", {"X-Hub-Signature-256": sig}, body)
    assert not authorized("k", {"Authorization": "Bearer nope"}, body)
    assert not authorized("k", {"X-Signature-256": sig}, b'{"id": 2}')


# ---------------------------------------------------------------- service
SEEN: list = []


def record(input):
    SEEN.append(input)
    if any(r.get("fail") for r in input):
        raise RuntimeError("downstream failed")
    return input


def spec(trigger, name="p"):
    return parse(
        {
            "name": name,
            "trigger": trigger,
            "steps": [
                {"id": "got", "use": "trigger"},
                {"id": "save", "fn": "tests.test_triggers:record", "input": "got"},
            ],
        }
    )


@pytest.fixture(autouse=True)
def reset():
    SEEN.clear()


def drain(service):
    for q in service.queues.values():
        q.join()


def test_files_are_handed_once_and_retried_after_a_failure(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.csv").write_text("x\n1\n")
    with Store(tmp_path / "s.db") as store:
        svc = Service([spec([{"files": str(inbox / "*.csv"), "every": "1m"}])], store)
        svc.tick(now=1000)
        drain(svc)
        assert [r["name"] for r in SEEN[0]] == ["a.csv"] and SEEN[0][0]["path"] == str(inbox / "a.csv")
        svc.tick(now=1010)  # within `every`: not even looked at
        (inbox / "b.csv").write_text("x\n2\n")
        svc.tick(now=1061)
        drain(svc)
        assert [r["name"] for r in SEEN[1]] == ["b.csv"]  # only the new one
        (inbox / "a.csv").write_text("x\n1\n2\n")  # changed: size differs
        svc.tick(now=1122)
        drain(svc)
        assert [r["name"] for r in SEEN[2]] == ["a.csv"]
        assert new_files(store, "p", str(inbox / "*.csv")) == []
        # a restart keeps the cursor
        svc2 = Service([spec([{"files": str(inbox / "*.csv")}])], store)
        svc2.tick(now=5000)
        drain(svc2)
        assert len(SEEN) == 3


def test_failed_run_sees_the_files_again(tmp_path):
    (tmp_path / "bad.json").write_text("{}")

    def run(spec_, records, kind):
        SEEN.append(records)
        return type("R", (), {"status": "failed" if len(SEEN) == 1 else "succeeded"})()

    with Store(tmp_path / "s.db") as store:
        svc = Service([spec([{"files": str(tmp_path / "*.json"), "every": "1s"}])], store, run=run)
        svc.tick(now=0)
        drain(svc)
        svc.tick(now=2)
        drain(svc)
        svc.tick(now=4)
        drain(svc)
    assert len(SEEN) == 2  # failed once, handed again, then marked seen


def test_cron_fires_when_due(tmp_path):
    with Store(tmp_path / "s.db") as store:
        svc = Service([spec([{"cron": "* * * * *"}])], store)
        due = svc.next_cron[("p", 0)]
        svc.tick(now=due.timestamp() - 1)
        assert svc.queues == {}
        svc.tick(now=due.timestamp() + 1)
        drain(svc)
        svc.tick(now=due.timestamp() + 2)  # same minute: not again
        drain(svc)
    assert [[{"scheduled_at": due.isoformat()}]] == SEEN


def test_webhook_end_to_end_with_a_real_run(tmp_path):
    with Store(tmp_path / "s.db") as store:
        svc = Service([spec([{"webhook": "/hooks/orders", "secret": "k"}], name="orders")], store)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), svc.handler())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def post(path, body, headers):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", path, body=body, headers=headers)
            r = c.getresponse()
            return r.status, json.loads(r.read())

        try:
            assert post("/hooks/orders", b'[{"id": 1}]', {})[0] == 401
            assert post("/nope", b"{}", {"Authorization": "Bearer k"})[0] == 404
            assert post("/hooks/orders", b"not json", {"Authorization": "Bearer k"})[0] == 400
            status, data = post("/hooks/orders", b'[{"id": 1}, {"id": 2}]', {"Authorization": "Bearer k"})
            assert status == 202 and data == {"queued": "orders", "records": 2}
            drain(svc)
        finally:
            srv.shutdown()
            srv.server_close()
        assert SEEN == [[{"id": 1}, {"id": 2}]]
        [run] = store.runs()
        assert run["pipeline"] == "orders" and run["status"] == "succeeded"


def test_runs_of_one_pipeline_never_overlap(tmp_path):
    active, peak = [0], [0]
    lock = threading.Lock()

    def run(spec_, records, kind):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1

    with Store(tmp_path / "s.db") as store:
        svc = Service([spec([{"webhook": "/h"}])], store, run=run)
        for _ in range(4):
            svc.submit(svc.specs["p"], [], "webhook")
        drain(svc)
    assert peak[0] == 1


def test_trigger_step_is_empty_outside_serve(tmp_path):
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec([{"webhook": "/h"}]), store=store).run()
    assert r.status == "succeeded" and r.outputs["got"] == []
    assert os.path.exists(tmp_path / "s.db")
