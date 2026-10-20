"""agentl resume (#339): completed steps are skipped, sinks are not written twice."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentl import parse
from agentl.cli import main
from agentl.registry import connector
from agentl.resume import ResumeRefused
from agentl.runner import Runner
from agentl.store import Store

CALLS: dict[str, int] = {}
WRITTEN: list[dict] = []
FAIL = {"on": True}


def _count(name: str) -> None:
    CALLS[name] = CALLS.get(name, 0) + 1


@connector("test_resume.source")
def source(input=None):
    _count("source")
    return [{"n": 1}, {"n": 2}, {"n": 3}]


@connector("test_resume.double")
def double(input, factor=2):
    _count("double")
    return [{"n": r["n"] * factor} for r in input]


@connector("test_resume.write")
def write(input):
    _count("write")
    WRITTEN.extend(input)
    return [{"written": len(input)}]


@connector("test_resume.flaky")
def flaky(input):
    _count("flaky")
    if FAIL["on"]:
        raise RuntimeError("downstream down")
    return list(input)


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    CALLS.clear()
    WRITTEN.clear()
    FAIL["on"] = True
    monkeypatch.delenv("AGENTL_CHECKPOINT_DIR", raising=False)
    monkeypatch.delenv("AGENTL_CHECKPOINTS", raising=False)


def _spec(factor: int = 2, after_write: bool = True):
    steps = [
        {"id": "src", "use": "test_resume.source"},
        {"id": "dbl", "use": "test_resume.double", "input": "src", "with": {"factor": factor}},
    ]
    if after_write:
        steps += [
            {"id": "save", "use": "test_resume.write", "input": "dbl"},
            {"id": "notify", "use": "test_resume.flaky", "input": "save"},
        ]
    else:
        steps += [{"id": "notify", "use": "test_resume.flaky", "input": "dbl"}]
    return parse({"name": "resumable", "steps": steps})


def _failed(store: Store, spec) -> str:
    result = Runner(spec, store=store).run()
    assert result.status == "failed"
    return result.run_id


def test_resume_skips_completed_steps_and_does_not_write_twice(tmp_path):
    with Store(tmp_path / "a.db") as store:
        first = _failed(store, _spec())
        assert CALLS == {"source": 1, "double": 1, "write": 1, "flaky": 1}
        FAIL["on"] = False
        result = Runner(_spec(), store=store, resume_from=first).run()
        assert result.status == "succeeded"
        assert CALLS == {"source": 1, "double": 1, "write": 1, "flaky": 2}
        assert WRITTEN == [{"n": 2}, {"n": 4}, {"n": 6}]  # the sink wrote once
        assert result.outputs["notify"] == [{"written": 3}]
        events = store.events(result.run_id)
        started = next(e for e in events if e["type"] == "run.started")
        assert started["data"]["resumed_from"] == first
        resumed = next(e for e in events if e["type"] == "run.resumed")
        assert resumed["data"]["skipped"] == ["dbl", "save", "src"]
        assert not (tmp_path / "checkpoints" / first).exists()  # cleaned up on success


def test_changed_spec_is_refused(tmp_path):
    with Store(tmp_path / "a.db") as store:
        first = _failed(store, _spec())
        with pytest.raises(ResumeRefused, match="pipeline changed"):
            Runner(_spec(factor=3), store=store, resume_from=first).run()
        assert len(store.runs()) == 1  # nothing recorded for the refused resume


def test_only_failed_runs_of_the_same_pipeline(tmp_path):
    FAIL["on"] = False
    with Store(tmp_path / "a.db") as store:
        ok = Runner(_spec(), store=store).run()
        with pytest.raises(ResumeRefused, match="only a failed run"):
            Runner(_spec(), store=store, resume_from=ok.run_id).run()
        with pytest.raises(ResumeRefused, match="no run"):
            Runner(_spec(), store=store, resume_from="nope").run()


def _drop_checkpoint(store: Store, run_id: str, step: str) -> None:
    data = next(e["data"] for e in store.events(run_id) if e["type"] == "checkpoint" and e["step"] == step)
    os.remove(data["path"])


def test_missing_output_reruns_that_step(tmp_path):
    with Store(tmp_path / "a.db") as store:
        first = _failed(store, _spec(after_write=False))
        _drop_checkpoint(store, first, "dbl")
        FAIL["on"] = False
        result = Runner(_spec(after_write=False), store=store, resume_from=first).run()
        assert result.status == "succeeded"
        assert CALLS == {"source": 1, "double": 2, "flaky": 2}  # src restored, dbl ran again
        resumed = next(e for e in store.events(result.run_id) if e["type"] == "run.resumed")
        assert "dbl" in resumed["data"]["rerun"]


def test_missing_output_of_a_writer_is_refused(tmp_path):
    with Store(tmp_path / "a.db") as store:
        first = _failed(store, _spec())
        _drop_checkpoint(store, first, "save")
        with pytest.raises(ResumeRefused, match="writer"):
            Runner(_spec(), store=store, resume_from=first).run()
        assert CALLS["write"] == 1


def test_checkpoints_off_reruns_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTL_CHECKPOINTS", "0")
    with Store(tmp_path / "a.db") as store:
        first = _failed(store, _spec(after_write=False))
        assert not (tmp_path / "checkpoints").exists()
        FAIL["on"] = False
        result = Runner(_spec(after_write=False), store=store, resume_from=first).run()
        assert result.status == "succeeded"
        assert CALLS == {"source": 2, "double": 2, "flaky": 2}


def test_oversized_output_is_not_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTL_CHECKPOINT_MAX_BYTES", "10")
    with Store(tmp_path / "a.db") as store:
        first = _failed(store, _spec(after_write=False))
        cps = [e["data"] for e in store.events(first) if e["type"] == "checkpoint"]
        assert cps and all(c["path"] is None and "limit" in c["reason"] for c in cps)


def test_cli_resume(tmp_path, capsys):
    yaml = tmp_path / "p.yaml"
    yaml.write_text(
        "name: resumable\nsteps:\n"
        "  - {id: src, use: test_resume.source}\n"
        "  - {id: save, use: test_resume.write, input: src}\n"
        "  - {id: notify, use: test_resume.flaky, input: save}\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "a.db")
    assert main(["run", str(yaml), "--db", db, "--no-check"]) != 0
    with Store(db) as store:
        first = store.runs()[0]["id"]
    FAIL["on"] = False
    assert main(["resume", first, str(yaml), "--db", db]) == 0
    assert CALLS["write"] == 1
    yaml.write_text(Path(yaml).read_text().replace("notify", "alert"), encoding="utf-8")
    assert main(["resume", first, str(yaml), "--db", db]) == 2
    assert "cannot resume" in capsys.readouterr().err
