"""The agent benchmark harness (#128): every task is solvable (the reference solution scores 10/10
through the harness, as a stand-in agent) and the scorer rejects wrong answers."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks import agent_bench

REFERENCE = Path(agent_bench.__file__).parent / "agent_tasks" / "reference.py"


def test_reference_solves_every_task_through_the_harness(tmp_path):
    pytest.importorskip("openpyxl")
    pytest.importorskip("pypdf")
    out = tmp_path / "r.jsonl"
    agent = subprocess.list2cmdline([sys.executable, str(REFERENCE)])
    assert agent_bench.main(["run", "--arm", "both", "--runs", "1", "--agent", agent, "--out", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 20 and all(r["ok"] for r in rows), [r for r in rows if not r["ok"]]
    assert rows[0]["turns"] == 1 and rows[0]["tokens"] == 15
    assert "| plain | 10 | 10/10 (100%)" in agent_bench.report(rows)


def test_scorer_rejects_wrong_missing_and_extra_rows(tmp_path):
    task = agent_bench.TASKS / "01-filter-paid-orders"
    (tmp_path / "out").mkdir()
    result = tmp_path / "out" / "result.json"
    assert agent_bench.score(task, tmp_path)["detail"] == "no out/result.json"
    good = json.loads((task / "expected.json").read_text())["records"]
    result.write_text(json.dumps(good))
    assert agent_bench.score(task, tmp_path)["ok"]
    result.write_text(json.dumps([{**good[0], "total": 1}, *good[1:]]))
    assert not agent_bench.score(task, tmp_path)["ok"]
    result.write_text(json.dumps([*good, {"id": 99, "customer": "x", "total": 1}]))
    assert not agent_bench.score(task, tmp_path)["ok"]
    result.write_text(json.dumps([{**r, "total": str(r["total"])} for r in good]))  # "150.0" == 150.0
    assert agent_bench.score(task, tmp_path)["ok"]


def test_stops_early_and_shows_why_when_the_agent_itself_fails(tmp_path, capsys):
    broken = tmp_path / "broken.py"
    broken.write_text(
        "import json, sys\nsys.stdin.read()\n"
        "print(json.dumps({'is_error': True, 'num_turns': 1, 'result': 'Invalid API key'}))\n"
    )
    out = tmp_path / "r.jsonl"
    agent = subprocess.list2cmdline([sys.executable, str(broken)])
    assert agent_bench.main(["run", "--runs", "3", "--agent", agent, "--out", str(out)]) == 2
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["agent_error"] and rows[0]["agent_says"] == "Invalid API key"
    printed = capsys.readouterr().out
    assert "Invalid API key" in printed and "stopped: the agent did not work" in printed
