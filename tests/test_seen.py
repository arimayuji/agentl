"""skip_seen (#184): files already handled by a successful run are not emitted again."""

from pathlib import Path

from agentl import parse
from agentl.cli import main
from agentl.connectors.seen import skip_seen
from agentl.runner import Runner
from agentl.store import Store

FAIL = {"on": False}


def listing(input=None, folder=""):
    from pathlib import Path

    return [{"path": str(p)} for p in sorted(Path(folder).iterdir())]


def sink(input):
    if FAIL["on"]:
        raise RuntimeError("load failed")
    return input


def spec(folder, key="sha256", scope=None):
    with_ = {"key": key, **({"scope": scope} if scope else {})}
    return parse(
        {
            "name": "reports",
            "steps": [
                {"id": "files", "fn": "tests.test_seen:listing", "with": {"folder": str(folder)}},
                {"id": "new", "use": "skip_seen", "input": "files", "with": with_},
                {"id": "load", "fn": "tests.test_seen:sink", "input": "new"},
            ],
        }
    )


def test_second_run_emits_nothing_and_failed_run_marks_nothing(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.csv").write_text("1")
    (folder / "b.csv").write_text("2")
    (folder / "copy.csv").write_text("1")  # same content as a.csv: one of them goes through
    with Store(tmp_path / "s.db") as store:
        FAIL["on"] = True
        try:
            assert Runner(spec(folder), store=store).run().status == "failed"
        finally:
            FAIL["on"] = False
        assert store.seen_list() == []
        r1 = Runner(spec(folder), store=store).run()
        assert r1.status == "succeeded"
        assert [Path(r["path"]).name for r in r1.outputs["new"]] == ["a.csv", "b.csv"]
        assert Runner(spec(folder), store=store).run().outputs["new"] == []
        (folder / "c.csv").write_text("3")
        assert [Path(r["path"]).name for r in Runner(spec(folder), store=store).run().outputs["new"]] == [
            "c.csv"
        ]
        # another scope keeps its own keys
        assert len(Runner(spec(folder, key="path", scope="other"), store=store).run().outputs["new"]) == 4


def test_cli_lists_and_forgets(tmp_path, capsys):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.csv").write_text("1")
    db = tmp_path / "s.db"
    with Store(db) as store:
        Runner(spec(folder, key="path"), store=store).run()
    assert main(["seen", "list", "--db", str(db)]) == 0
    assert "reports  " in capsys.readouterr().out
    assert main(["seen", "forget", "--db", str(db)]) == 2
    assert main(["seen", "forget", "--scope", "reports", "--db", str(db)]) == 0
    assert "forgot 1 key(s)" in capsys.readouterr().out
    with Store(db) as store:
        assert len(Runner(spec(folder, key="path"), store=store).run().outputs["new"]) == 1


def test_direct_call_passes_records_through():
    rows = [{"id": 1}, {"id": 1}]
    assert skip_seen(rows, key="id") == rows
