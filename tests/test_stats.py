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
