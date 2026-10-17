import datetime as dt

import pytest

from agentl import parse
from agentl.cli import main
from agentl.spec import SpecError, WindowSpec
from agentl.store import Store
from agentl.windows import bind, plan, run_windows

D = dt.date


def test_plan_splits_by_span_and_alignment():
    w = WindowSpec(max_span="31d", align="calendar_month")
    assert plan(D(2026, 1, 15), D(2026, 3, 10), w) == [
        (D(2026, 1, 15), D(2026, 2, 1)),
        (D(2026, 2, 1), D(2026, 3, 1)),
        (D(2026, 3, 1), D(2026, 3, 10)),
    ]
    assert plan(D(2026, 12, 20), D(2027, 1, 2), w)[0] == (D(2026, 12, 20), D(2027, 1, 1))
    weeks = plan(D(2026, 10, 14), D(2026, 10, 27), WindowSpec(align="week", order="newest_first"))
    assert weeks == [
        (D(2026, 10, 26), D(2026, 10, 27)),
        (D(2026, 10, 19), D(2026, 10, 26)),
        (D(2026, 10, 14), D(2026, 10, 19)),
    ]
    assert len(plan(D(2026, 1, 1), D(2026, 1, 11), WindowSpec(max_span="3d"))) == 4


def test_bad_duration_fails_at_parse():
    with pytest.raises(SpecError, match="not a duration"):
        parse({"name": "x", "window": {"max_span": "31 days"}, "steps": [{"id": "a", "use": "echo"}]})


def _pipeline(tmp_path, fail_on=None):
    (tmp_path / "series.py").write_text(
        "import pathlib\n"
        "def fetch(input, start, end):\n"
        f"    with open({str(tmp_path / 'calls.txt')!r}, 'a', encoding='utf-8') as f:\n"
        "        f.write(start + '\\n')\n"
        f"    if start == {fail_on!r}:\n"
        "        raise RuntimeError('source down')\n"
        "    return [{'start': start, 'end': end}]\n",
        encoding="utf-8",
    )
    yaml = tmp_path / "p.yaml"
    yaml.write_text(
        "name: series\n"
        "window: {lookback: 10d, max_span: 4d}\n"
        "steps:\n"
        "  - id: s\n"
        "    fn: series:fetch\n"
        '    with: {start: "${window.start}", end: "${window.end}"}\n',
        encoding="utf-8",
    )
    return yaml


def test_bind_fills_placeholders(tmp_path):
    from agentl.spec import load

    spec = load(_pipeline(tmp_path))
    assert bind(spec, D(2026, 1, 1), D(2026, 1, 5)).steps[0].with_ == {
        "start": "2026-01-01",
        "end": "2026-01-05",
    }
    assert spec.steps[0].with_["start"] == "${window.start}"


def test_live_run_rereads_lookback_tail(tmp_path):
    from agentl.spec import load

    with Store(tmp_path / "db.sqlite") as store:
        report = run_windows(load(_pipeline(tmp_path)), store, today=D(2026, 10, 17))
    assert report.status == "succeeded"
    assert report.done == ["2026-10-08/2026-10-12", "2026-10-12/2026-10-16", "2026-10-16/2026-10-18"]


def test_backfill_resume_skips_done_and_lists_pending(tmp_path, capsys):
    yaml = _pipeline(tmp_path, fail_on="2026-01-05")
    db = ["--db", str(tmp_path / "db.sqlite")]
    args = ["backfill", str(yaml), "--from", "2026-01-01", "--to", "2026-01-12", *db]
    assert main(args) == 1
    out = capsys.readouterr()
    assert "2 done, 0 skipped, 1 failed" in out.out
    assert "pending 2026-01-05/2026-01-09" in out.err

    calls = tmp_path / "calls.txt"
    calls.unlink()
    assert main(args) == 1  # still failing: only the pending chunk runs again
    assert calls.read_text(encoding="utf-8").split() == ["2026-01-05"]
    assert "0 done, 2 skipped, 1 failed" in capsys.readouterr().out

    assert main([*args, "--no-resume"]) == 1
    assert len(calls.read_text(encoding="utf-8").split()) == 4


def test_backfill_window_mode_rejects_params_mode_flags(tmp_path, capsys):
    yaml = _pipeline(tmp_path)
    db = ["--db", str(tmp_path / "db.sqlite")]
    args = ["backfill", str(yaml), "--from", "2026-01-01", "--to", "2026-01-12", "--concurrency", "2", *db]
    assert main(args) == 2
    assert "window:" in capsys.readouterr().err
