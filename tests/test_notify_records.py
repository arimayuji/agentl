import json
import logging

import pytest

from agentl import parse, register_channel
from agentl.connectors.console import console_send, log_write
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

SENT: list[tuple[str, dict]] = []


def alerts(records, rows=2):
    return [{"id": i, "msg": f"m{i}"} for i in range(rows)]


def ok(records):
    return [{"id": 1}]


@pytest.fixture(autouse=True)
def capture():
    SENT.clear()
    register_channel("capture", lambda cfg, text, event: SENT.append((text, event)))


def pipeline(rows=2, **rule):
    return parse(
        {
            "name": "p",
            "steps": [
                {"id": "alerts", "fn": "tests.test_notify_records:alerts", "with": {"rows": rows}},
                {"id": "other", "fn": "tests.test_notify_records:ok"},
            ],
            "notify": {
                "channels": {"cap": {"type": "capture"}},
                "rules": [{"on": "records", "step": "alerts", "to": ["cap"], **rule}],
            },
        }
    )


def run(tmp_path, spec):
    with Store(tmp_path / "db") as store:
        return Runner(spec, store=store).run()


def test_records_rule_sends_rows_with_template_and_cap(tmp_path):
    r = run(tmp_path, pipeline(rows=5, template="{id}: {msg}", max_rows=2))
    assert r.status == "succeeded", r.error
    assert len(SENT) == 1
    text, event = SENT[0]
    assert event["step"] == "alerts"
    assert "0: m0" in text and "1: m1" in text and "2: m2" not in text
    assert "3 more" in text


def test_records_rule_default_is_json(tmp_path):
    run(tmp_path, pipeline(rows=1))
    assert json.dumps({"id": 0, "msg": "m0"}) in SENT[0][0]


def test_records_rule_silent_when_step_is_empty(tmp_path):
    r = run(tmp_path, pipeline(rows=0))
    assert r.status == "succeeded"
    assert SENT == []


def test_records_rule_needs_step():
    with pytest.raises(SpecError, match="step"):
        parse({"name": "p", "steps": [], "notify": {"rules": [{"on": "records", "to": ["x"]}]}})


def test_console_send_passes_records_through(capsys):
    rows = [{"a": 1}, {"a": 2}]
    assert console_send(rows, template="a={a}") == rows
    assert capsys.readouterr().out == "a=1\na=2\n"
    console_send(rows, max_rows=1)
    out = capsys.readouterr().out
    assert out.splitlines()[0] == '{"a": 1}' and "1 more" in out


def test_log_write(caplog):
    with caplog.at_level(logging.INFO, logger="agentl.records"):
        assert log_write([{"a": 1}], level="warning", template="a={a}") == [{"a": 1}]
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [("WARNING", "a=1")]
    with pytest.raises(ValueError, match="level"):
        log_write([], level="loud")


def test_json_write_run_id_path(tmp_path):
    out = tmp_path / "out_{run_id}.json"
    spec = parse(
        {
            "name": "p",
            "steps": [
                {"id": "a", "fn": "tests.test_notify_records:ok"},
                {"id": "w", "use": "json.write", "input": "a", "with": {"path": str(out)}},
            ],
        }
    )
    r = run(tmp_path, spec)
    assert r.status == "succeeded", r.error
    assert (tmp_path / f"out_{r.run_id}.json").exists()
