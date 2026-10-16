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
    kept = tmp_path / "kept"
    args = ["run", "--arm", "both", "--runs", "1", "--agent", agent, "--out", str(out), "--keep", str(kept)]
    assert agent_bench.main(args) == 0
    run = kept / "run1-01-filter-paid-orders-agentl"
    assert (run / "workspace" / "out" / "result.json").is_file() and "num_turns" in (
        run / "agent-stdout.txt"
    ).read_text()
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 26 and all(r["ok"] for r in rows), [r for r in rows if not r["ok"]]
    assert rows[0]["turns"] == 1 and rows[0]["tokens"] == 15
    assert "| plain | 13 | 13/13 (100%)" in agent_bench.report(rows)


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
        "print(json.dumps({'is_error': True, 'num_turns': 1, 'result': 'Invalid API key',"
        " 'modelUsage': {'claude-x': {}}}))\n"
    )
    out = tmp_path / "r.jsonl"
    agent = subprocess.list2cmdline([sys.executable, str(broken)])
    assert agent_bench.main(["run", "--runs", "3", "--agent", agent, "--out", str(out)]) == 2
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["agent_error"] and rows[0]["agent_says"] == "Invalid API key"
    assert rows[0]["models"] == ["claude-x"]
    printed = capsys.readouterr().out
    assert "Invalid API key" in printed and "stopped: the agent did not work" in printed


def test_the_python_api_solves_every_task_too(tmp_path):
    """reference_agentl.py: the ten tasks with al.read / al.write / al.http_get_all. Also checks the
    harness lets agentl reach the local API (its SSRF guard blocks 127.0.0.1 by default)."""
    pytest.importorskip("openpyxl")
    pytest.importorskip("pypdf")
    out = tmp_path / "r.jsonl"
    script = REFERENCE.parent / "reference_agentl.py"
    agent = subprocess.list2cmdline([sys.executable, str(script)])
    args = ["run", "--arm", "agentl-python", "--runs", "1", "--agent", agent, "--out", str(out)]
    assert agent_bench.main(args) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 13 and all(r["ok"] for r in rows), [r for r in rows if not r["ok"]]


def test_a_pipeline_pays_only_for_what_is_new_on_the_second_run(tmp_path):
    """Tasks 11-13 run twice. reference_pipeline.py (agentl pipelines: per-record cache, cursor,
    upsert) reaches each ideal; the plain script pays again for everything. All offline: the LLM
    and the API are local fakes."""
    tasks = ["11-classify-reruns", "12-resume-after-outage", "13-incremental-api"]
    rows = {}
    for arm, script in (("agentl-pipeline", "reference_pipeline.py"), ("plain", "reference.py")):
        out = tmp_path / f"{arm}.jsonl"
        agent = subprocess.list2cmdline([sys.executable, str(REFERENCE.parent / script)])
        args = ["run", "--arm", arm, "--runs", "1", "--tasks", *tasks, "--agent", agent, "--out", str(out)]
        assert agent_bench.main(args) == 0
        rows[arm] = {r["task"]: r for r in map(json.loads, out.read_text().splitlines())}
    for task in tasks:
        pipe, plain = rows["agentl-pipeline"][task], rows["plain"][task]
        assert pipe["ok"] and plain["ok"], (pipe, plain)
        assert pipe["phase2"] == pipe["phase2_ideal"], pipe
        assert plain["phase2"] > plain["phase2_ideal"] * 2, plain
    report = agent_bench.report([*rows["agentl-pipeline"].values(), *rows["plain"].values()])
    assert "| 11-classify-reruns | 10 | 10 (10-10) | 60 (60-60) |" in report


def test_fake_llm_answers_in_the_requested_schema():
    import urllib.error
    import urllib.request

    llm = agent_bench._Llm({"T-001": {"category": "billing"}}, fail_after=1)
    try:
        body = {
            "messages": [{"role": "user", "content": 'Classify: {"id": "T-001", "text": "charged twice"}'}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "schema": {"properties": {"category": {"type": "string"}, "urgent": {"type": "boolean"}}}
                },
            },
        }
        req = urllib.request.Request(
            llm.url + "/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        answer = json.load(urllib.request.urlopen(req))["choices"][0]["message"]["content"]
        assert json.loads(answer) == {"category": "billing", "urgent": False}
        with pytest.raises(urllib.error.HTTPError, match="503"):
            urllib.request.urlopen(req)  # after `fail_after` calls the provider is down
        assert llm.calls == 2
    finally:
        llm.close()


def test_an_unknown_task_stops_the_run(tmp_path, capsys):
    out = tmp_path / "r.jsonl"
    args = ["run", "--tasks", "01-filter-paid-orders", "99-nope", "--agent", "true", "--out", str(out)]
    assert agent_bench.main(args) == 2
    assert "unknown task(s): 99-nope" in capsys.readouterr().out and not out.exists()
