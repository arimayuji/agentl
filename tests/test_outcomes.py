import urllib.error

import pytest

from agentl import PipelineFailed, connector, parse, run
from agentl.cli import _exit_code
from agentl.runner import Runner, RunResult, is_transport_error
from agentl.stats import compute
from agentl.store import Store

STATE = {"rows": [], "error": None}


@connector("test.outcome_src")
def _src(input):
    if STATE["error"] is not None:
        raise STATE["error"]
    return list(STATE["rows"])


def _spec(**outcomes):
    raw = {"name": "outcomes-test", "steps": [{"id": "src", "use": "test.outcome_src"}]}
    if outcomes:
        raw["outcomes"] = outcomes
    return parse(raw)


@pytest.fixture
def store(tmp_path):
    STATE.update(rows=[], error=None)
    with Store(tmp_path / "o.db") as s:
        yield s


def _run(spec, store):
    events = []
    result = Runner(spec, store=store, listeners=[events.append]).run()
    return result, [e["type"] for e in events]


def test_success_and_no_change(store):
    STATE["rows"] = [{"n": 1}]
    result, _ = _run(_spec(), store)
    assert (result.status, result.outcome) == ("succeeded", "success")
    STATE["rows"] = []
    result, types = _run(_spec(), store)
    assert (result.status, result.outcome) == ("succeeded", "no_change")
    assert "run.alert" not in types
    result, _ = _run(_spec(on_empty="success"), store)
    assert result.outcome == "success"
    assert store.run(result.run_id)["outcome"] == "success"


def test_transport_error_is_failed_by_default(store):
    STATE["error"] = TimeoutError("timed out")
    result, types = _run(_spec(), store)
    assert (result.status, result.outcome) == ("failed", "failed")
    assert "run.failed" in types and "run.alert" in types


def test_transport_error_skips_until_the_streak_fails(store):
    STATE["error"] = urllib.error.URLError(ConnectionRefusedError("refused"))
    spec = _spec(on_transport_error="skipped_upstream", max_skipped=3)
    for _ in range(2):
        result, types = _run(spec, store)
        assert (result.status, result.outcome) == ("skipped", "skipped")
        assert "run.skipped" in types and "run.alert" not in types
        assert result.reason and "source unreachable" in result.reason
    result, types = _run(spec, store)
    assert (result.status, result.outcome) == ("failed", "failed")
    assert "3 skipped runs in a row" in (result.error or "")
    assert "run.alert" in types
    assert store.run(result.run_id)["status"] == "failed"
    STATE["error"] = None
    assert _run(spec, store)[0].status == "succeeded"  # a good run resets the streak


def test_raise_on_failure_still_raises(store):
    STATE["error"] = RuntimeError("boom")
    with pytest.raises(PipelineFailed):
        run(_spec(), store=store, raise_on_failure=True)


def test_skipped_does_not_raise_for_orchestrators(store):
    STATE["error"] = ConnectionResetError("reset")
    spec = _spec(on_transport_error="skipped_upstream", max_skipped=None)
    for _ in range(4):
        assert run(spec, store=store, raise_on_failure=True).status == "skipped"
    report = compute(store, 0)["pipelines"][0]
    assert report["skipped"] == 4 and report["outcomes"] == {"skipped": 4}


def test_http_status_errors_stay_failed(store):
    STATE["error"] = urllib.error.HTTPError("http://x", 503, "unavailable", {}, None)  # type: ignore[arg-type]
    result, _ = _run(_spec(on_transport_error="skipped_upstream"), store)
    assert result.outcome == "failed"
    assert not is_transport_error(ValueError("bad json"))


def test_partial_and_exit_codes(store):
    STATE["rows"] = [{"n": 1}]
    spec = _spec(exit_codes={"partial": 3, "no_change": 4})
    runner = Runner(spec, store=store)
    result = RunResult(run_id="x", status="succeeded", rejected={"src": [{"n": 2}]})
    runner._source_records = {"src": 1}
    runner._classify(result)
    assert result.outcome == "partial"
    assert _exit_code(spec, result) == 3
    assert _exit_code(spec, RunResult(run_id="y", status="succeeded", outcome="no_change")) == 4
    assert _exit_code(spec, RunResult(run_id="z", status="failed", outcome="failed")) == 1
    assert _exit_code(spec, RunResult(run_id="w", status="skipped", outcome="skipped")) == 0
