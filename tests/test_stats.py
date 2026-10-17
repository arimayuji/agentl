"""Track record per pipeline (#95): success rate, durations, cost, failures by reason, trend."""

import datetime as dt
import json

from agentl import stats
from agentl.cli import main
from agentl.mcp_server import Server
from agentl.store import Store

DAY = 86400
NOW = dt.datetime(2026, 10, 14, 12).timestamp()


def seed(store):
    """sales: 8 runs over 4 days, 3 failed (2 with the same timeout on different hosts and
    ports, 1 check). crm: 1 run that is still running."""
    runs = [
        ("sales", "succeeded", 4, 10, None, None),
        ("sales", "succeeded", 4, 12, None, None),
        ("sales", "failed", 3, 3, "load", "TimeoutError: db.internal:5432 timed out after 30s"),
        ("sales", "succeeded", 3, 11, None, None),
        ("sales", "failed", 2, 4, "load", "TimeoutError: db.internal:6543 timed out after 31s"),
        ("sales", "succeeded", 2, 9, None, None),
        ("sales", "failed", 1, 2, "verify", "check failed: 3 rows, expected at least 50"),
        ("sales", "succeeded", 0, 40, None, None),
        ("crm", "running", 0, 0, None, None),
        ("sales", "failed", 60, 5, "load", "too old to count"),
    ]
    for i, (name, status, days_ago, secs, step, error) in enumerate(runs):
        rid = f"r{i}"
        started = NOW - days_ago * DAY - 3600 + i
        store._exec(
            "INSERT INTO {runs} (id, pipeline, status, started, finished, error) VALUES (?, ?, ?, ?, ?, ?)",
            (rid, name, status, started, started + secs if status != "running" else None, error),
        )
        if step:
            store._exec(
                "INSERT INTO {events} (run_id, ts, type, step, data) VALUES (?, ?, ?, ?, ?)",
                (rid, started + 1, "check.failed" if step == "verify" else "step.failed", step, "{}"),
            )
        if status in ("succeeded", "failed"):
            store._exec(
                "INSERT INTO {events} (run_id, ts, type, step, data) VALUES (?, ?, ?, ?, ?)",
                (rid, started + secs, f"run.{status}", None, json.dumps({"cost_usd": 0.25})),
            )


def test_reason_strips_what_changes_between_runs():
    a = stats.reason("TimeoutError: db.internal:5432 timed out after 30s")
    b = stats.reason("TimeoutError: db.internal:6543 timed out after 31s")
    assert a == b == "TimeoutError: db.internal:<n> timed out after <n>s"
    assert stats.reason("open /data/in/2026-10-14.csv failed: run 9f8e7d6c") == "open <path> failed: run <id>"
    assert stats.reason("GET https://api.example.com/v1/x?id=7 -> 503") == "GET <url> -> <n>"


def test_track_record(tmp_path):
    with Store(tmp_path / "s.db") as store:
        seed(store)
        report = stats.compute(store, NOW - 30 * DAY, now=NOW)
    sales, crm = report["pipelines"][1], report["pipelines"][0]
    assert (sales["runs"], sales["succeeded"], sales["failed"]) == (8, 5, 3)
    assert sales["success_rate"] == 0.625 and sales["cost_usd"] == 2.0
    assert sales["median_s"] == 9.5 and sales["p95_s"] > 30
    top = sales["failures"][0]
    assert (top["step"], top["count"], top["last_run"]) == ("load", 2, "r4")
    assert top["reason"] == "TimeoutError: db.internal:<n> timed out after <n>s"
    assert sales["failures"][1]["step"] == "verify"
    assert [(d["succeeded"], d["failed"]) for d in sales["daily"]] == [(2, 0), (1, 1), (1, 1), (0, 1), (1, 0)]
    assert crm["other"] == 1 and crm["success_rate"] is None


def test_cli_and_mcp(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    with Store(db) as store:
        seed(store)
    assert main(["stats", "sales", "--since", "3650d", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "sales: 9 runs, 5 succeeded, 4 failed" in out and "2x [load] TimeoutError" in out
    assert main(["stats", "--since", "soon", "--db", db]) == 2
    res = Server(db).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "pipeline_stats", "arguments": {}},
        }
    )
    assert res["result"]["isError"] is False and '"pipeline": "sales"' in res["result"]["content"][0]["text"]


def test_agent_cost_per_record_accepted_and_cache_hit(tmp_path, capsys):
    """#156: per agent step and model, cost per record, per accepted answer and per cache hit."""
    from agentl import parse
    from agentl.runner import Runner
    from tests import test_budget  # noqa: F401  (registers the `metered` fake provider)

    spec = parse(
        {
            "name": "tagging",
            "llm": {
                "default": {"provider": "metered", "model": "m1", "price": {"input": 3.0, "output": 15.0}}
            },
            "steps": [
                {"id": "src", "fn": "tests.test_budget:rows", "with": {"n": 4}},
                {"id": "ask", "agent": "Check.", "input": "src", "output": {"ok": "bool"}},
            ],
        }
    )
    db = tmp_path / "s.db"
    with Store(db) as store:
        Runner(spec, store=store).run()  # 4 calls at 0.0105 USD
        Runner(spec, store=store).run()  # 4 cache hits, no calls
        report = stats.compute(store, 0)
    [row] = report["pipelines"][0]["agents"]
    assert (row["step"], row["model"], row["runs"], row["records"], row["accepted"]) == ("ask", "m1", 2, 8, 8)
    assert (row["answered"], row["cache_hits"]) == (4, 4)
    assert row["cost_usd"] == 0.042
    assert row["cost_per_record"] == 0.00525 and row["cost_per_accepted"] == 0.00525
    assert row["cost_per_answer"] == 0.0105 and row["cache_saved_usd"] == 0.042
    assert main(["stats", "--since", "3650d", "--db", str(db)]) == 0
    assert "[ask] m1: 0.005250/record, 0.005250/accepted (8 of 8), 4 cache hits" in capsys.readouterr().out


def test_agent_cost_counts_rejected_records():
    done = [
        {
            "run_id": "r1",
            "step": "ask",
            "data": {
                "records": 3,
                "llm_calls": 5,
                "cost_usd": 0.5,
                "model": "m",
                "answered_by": {"default": 5},
            },
        }
    ]
    [row] = stats._agent_costs(done, {("r1", "ask"): 2}, {"r1": "p"})["p"]
    assert (row["records"], row["accepted"], row["rejected"]) == (5, 3, 2)
    assert row["cost_per_record"] == 0.1 and row["cost_per_accepted"] == 0.166667
    assert row["cache_saved_usd"] == 0.0
