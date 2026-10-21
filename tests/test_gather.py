"""gather: fan in parts from independent runs, fire once per group (#217)."""

import threading
import time

import pytest

import agentl
from agentl import gather, parse
from agentl.cli import main
from agentl.runner import Runner, RunResult
from agentl.store import Store
from agentl.triggers import Service

CAPTURE = """
name: capture
params:
  date: { type: str }
  part: { type: str }
steps:
  - id: mark
    use: gather.add
    with: { group: "batch-${params.date}", item: "${params.part}" }
"""


def consumer(**g):
    return parse(
        {
            "name": "report",
            "trigger": [{"gather": {"group": "batch-{date}", **g}}],
            "steps": [{"id": "t", "use": "trigger"}],
            "notify": {
                "channels": {"c": {"type": "console"}},
                "rules": [{"on": ["gather.partial", "gather.failed"], "to": ["c"]}],
            },
        }
    )


def service(store, spec):
    runs = []

    def run(s, records, kind):
        runs.append(records[0])
        return RunResult(run_id=f"r{len(runs)}", status="succeeded")

    return Service([spec], store, run=run), runs


def test_capture_runs_add_parts_and_the_group_fires_once(tmp_path):
    with Store(tmp_path / "s.db") as store:
        path = tmp_path / "capture.yaml"
        path.write_text(CAPTURE)
        for part in ("a", "b", "b", "c"):
            r = agentl.run(path, store=store, params={"date": "2026-10-01", "part": part})
            assert r.status == "succeeded"
        svc, runs = service(store, consumer(expect={"count": 3}))
        svc.tick(time.time())
        svc.queues["report"].join()
        assert runs == [
            {
                "group": "batch-2026-10-01",
                "date": "2026-10-01",
                "items": ["a", "b", "c"],
                "count": 3,
                "missing": [],
                "missing_count": 0,
                "partial": False,
            }
        ]
        svc.next_files.clear()
        svc.tick(time.time())
        svc.queues["report"].join()
        assert len(runs) == 1
        g = gather.groups(store)[0]
        assert g["fired"] == {"report": "complete"}


def test_a_failed_capture_run_adds_nothing(tmp_path):
    spec = parse(
        {
            "name": "cap",
            "steps": [
                {"id": "mark", "use": "gather.add", "with": {"group": "g", "item": "x"}},
                {"id": "boom", "use": "python", "with": {"code": "raise ValueError('no')"}, "input": "mark"},
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        r = Runner(spec, store=store).run()
        assert r.status == "failed"
        assert gather.groups(store) == []


def test_concurrent_additions_and_services_fire_exactly_once(tmp_path):
    with Store(tmp_path / "s.db") as store:
        spec = consumer(expect={"count": 20})
        services = [service(store, spec) for _ in range(4)]
        stop = threading.Event()

        def poll(svc):
            while not stop.is_set():
                svc.next_files.clear()
                svc.tick(time.time())

        pollers = [threading.Thread(target=poll, args=(s,)) for s, _ in services]
        adders = [
            threading.Thread(target=gather.add_part, args=(store, "batch-d", f"p{i}", f"run{i}"))
            for i in range(20)
        ]
        for t in pollers + adders:
            t.start()
        for t in adders:
            t.join()
        time.sleep(0.2)
        stop.set()
        for t in pollers:
            t.join()
        for s, _ in services:
            if "report" in s.queues:
                s.queues["report"].join()
        fired = [r for _, runs in services for r in runs]
        assert len(fired) == 1 and fired[0]["count"] == 20


def test_deadline_partial_runs_with_what_arrived_and_lists_missing(tmp_path, capsys):
    with Store(tmp_path / "s.db") as store:
        gather.add_part(store, "batch-d", "east", "r1")
        spec = consumer(
            expect={"items": ["east", "west", "north"]}, deadline={"after": "1h", "fire": "partial"}
        )
        svc, runs = service(store, spec)
        svc.tick(time.time())
        assert "report" not in svc.queues  # not complete, deadline not reached
        svc.next_files.clear()
        svc.tick(time.time() + 3601)
        svc.queues["report"].join()
        assert runs[0]["partial"] and runs[0]["missing"] == ["north", "west"]
        out = capsys.readouterr()
        assert "gather.partial" in out.out + out.err and "north" in out.out + out.err


def test_deadline_fail_notifies_without_a_run(tmp_path, capsys):
    with Store(tmp_path / "s.db") as store:
        gather.add_part(store, "batch-d", "a", "r1")
        svc, runs = service(store, consumer(expect={"count": 3}, deadline={"after": "1h"}))
        svc.tick(time.time() + 3601)
        assert runs == [] and "report" not in svc.queues
        out = capsys.readouterr()
        assert "gather.failed" in out.out + out.err and "missing_count=2" in out.out + out.err
        assert gather.groups(store)[0]["fired"] == {"report": "failed"}


def test_ttl_drops_the_group(tmp_path):
    with Store(tmp_path / "s.db") as store:
        gather.add_part(store, "batch-d", "a", "r1")
        svc, runs = service(store, consumer(expect={"count": 1}, ttl="1h"))
        svc.tick(time.time())
        svc.queues["report"].join()
        svc.next_files.clear()
        svc.tick(time.time() + 3601)
        assert gather.groups(store) == [] and len(runs) == 1


def test_spec_validation():
    with pytest.raises(ValueError, match="count"):
        consumer(expect={})
    with pytest.raises(ValueError, match="ttl"):
        consumer(expect={"count": 1}, ttl="1h", deadline={"after": "2h"})
    with pytest.raises(ValueError, match="exactly one"):
        parse(
            {
                "name": "x",
                "steps": [],
                "trigger": [{"cron": "@daily", "gather": {"group": "g", "expect": {"count": 1}}}],
            }
        )
    assert gather.pattern("batch-{region}-{date}").match("batch-eu-2026-10-01").groupdict() == {
        "date": "2026-10-01",
        "region": "eu",
    }


def test_cli_ls_and_forget(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    with Store(db) as store:
        gather.add_part(store, "batch-d", "a", "r1")
        gather.add_part(store, "batch-d", "b", "r2")
    assert main(["gather", "ls", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "batch-d  2 part(s)" in out and "not fired" in out and "items: a, b" in out
    assert main(["gather", "forget", "batch-d", "--db", db]) == 0
    assert main(["gather", "ls", "--db", db]) == 0
    assert "no gather groups stored" in capsys.readouterr().out
