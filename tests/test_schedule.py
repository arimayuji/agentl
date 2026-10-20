"""Schedules by publication cycle (#201)."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from agentl import lint, parse
from agentl.cli import main
from agentl.schedule import Schedule, _br_national, _easter, _us_federal, read_exceptions
from agentl.store import Store
from agentl.triggers import Service

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


def _spec(schedule: dict):
    return parse(
        {"name": "pub", "trigger": [{"schedule": schedule}], "steps": [{"id": "a", "use": "trigger"}]}
    )


BASE = {
    "tz": "America/New_York",
    "expect": {
        "days": ["mon", "tue"],
        "at": "16:00",
        "calendar": "us_federal",
        "if_holiday": "next_business_day",
    },
    "poll": {
        "cadence": [{"for": "15m", "every": "5m"}, {"for": "30m", "every": "15m"}],
        "stop_when": "success_in_cycle",
    },
}


def test_holiday_calendars():
    assert _easter(2026) == dt.date(2026, 4, 5) and _easter(2027) == dt.date(2027, 3, 28)
    us = _us_federal(2026)
    assert dt.date(2026, 11, 26) in us  # Thanksgiving
    assert dt.date(2026, 7, 3) in us  # July 4 on Saturday, observed Friday
    assert dt.date(2026, 10, 12) in us and dt.date(2026, 10, 13) not in us
    br = _br_national(2026)
    assert dt.date(2026, 4, 3) in br and dt.date(2026, 11, 20) in br  # Good Friday, Black Consciousness
    assert dt.date(2023, 11, 20) not in _br_national(2023)


def test_cycles_holidays_and_cadence():
    sch = Schedule(_spec(BASE).trigger[0].schedule)
    fires = sch.next_firings(dt.datetime(2026, 10, 9, tzinfo=UTC), 7)
    # Monday Oct 12 is a holiday: moved to Tuesday Oct 13, which is a cycle already (one cycle)
    assert [t.strftime("%H:%M") for _, t in fires[:6]] == [
        "16:00",
        "16:05",
        "16:10",
        "16:15",
        "16:30",
        "16:45",
    ]
    assert {d for d, _ in fires[:6]} == {dt.date(2026, 10, 13)}
    assert fires[6] == (dt.date(2026, 10, 19), dt.datetime(2026, 10, 19, 16, 0, tzinfo=NY))
    assert fires[0][1].utcoffset() == dt.timedelta(hours=-4)
    winter = sch.next_firings(dt.datetime(2026, 12, 1, tzinfo=UTC), 1)[0][1]
    assert winter.utcoffset() == dt.timedelta(hours=-5)  # same wall time across daylight saving


def test_skip_previous_active_extra_and_exceptions(tmp_path):
    f = tmp_path / "closed.txt"
    f.write_text("# office closed\n2026-10-20\n", encoding="utf-8")
    assert read_exceptions(f) == {dt.date(2026, 10, 20)}
    spec = {
        "tz": "UTC",
        "expect": {"days": ["mon", "tue"], "calendar": "us_federal", "exceptions": str(f)},
        "active": {"between": ["2026-10-01", "2026-10-31"]},
        "extra_dates": ["2026-11-05"],
        "skip_dates": ["2026-10-06"],
    }
    days = [
        d
        for d, _ in Schedule(_spec(spec).trigger[0].schedule).next_firings(
            dt.datetime(2026, 10, 1, tzinfo=UTC), 20
        )
    ]
    assert days == [dt.date(2026, 10, d) for d in (5, 13, 19, 26, 27)] + [dt.date(2026, 11, 5)]
    spec["expect"]["if_holiday"] = "previous_business_day"
    days = [
        d
        for d, _ in Schedule(_spec(spec).trigger[0].schedule).next_firings(
            dt.datetime(2026, 10, 1, tzinfo=UTC), 3
        )
    ]
    assert days == [dt.date(2026, 10, 5), dt.date(2026, 10, 9), dt.date(2026, 10, 13)]
    bad = tmp_path / "bad.txt"
    bad.write_text("tomorrow\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad\.txt:1"):
        read_exceptions(bad)


def test_validation():
    with pytest.raises(ValueError, match="tz"):
        _spec({"expect": {"days": ["mon"]}})
    with pytest.raises(ValueError, match="not a known time zone"):
        _spec({"tz": "Mars/Base", "expect": {"days": ["mon"]}})
    with pytest.raises(ValueError, match="unknown day"):
        _spec({"tz": "UTC", "expect": {"days": ["monday"]}})
    with pytest.raises(ValueError, match="calendar"):
        _spec({"tz": "UTC", "expect": {"days": ["mon"], "calendar": "mars"}})
    with pytest.raises(ValueError, match=r"expect\.days"):
        _spec({"tz": "UTC"})
    assert _spec(BASE).trigger[0].kind == "schedule"


def test_lint_refuses_cron_without_timezone():
    text = "name: x\ntrigger:\n  - cron: '0 7 * * *'\nsteps:\n  - id: a\n    use: trigger\n"
    found = lint.check_text(text)
    assert [f.code for f in found.errors] == ["AL009"] and found.errors[0].line == 3
    assert not lint.check_text(text.replace("'\n", "'\n    timezone: UTC\n", 1)).errors
    assert not lint.check_text(text, warnings=False).errors  # validate and run keep working


class _Res:
    def __init__(self, ok):
        self.status = "succeeded" if ok else "failed"


def _service(tmp_path, results):
    spec = _spec(BASE)
    runs = []

    def run(spec, records, kind):
        runs.append(records[0])
        return _Res(results.pop(0) if results else False)

    svc = Service([spec], Store(tmp_path / "s.db"), run=run)
    late = []
    svc.on_late = lambda s, d, exp: late.append(d)
    sch = svc.schedules[("pub", 0)]
    svc.next_cycle[("pub", 0)] = sch.next_firings(dt.datetime(2026, 10, 13, 19, tzinfo=UTC), 1)[0]
    return svc, runs, late


def _at(svc, *times):
    for h, m in times:
        svc.tick(dt.datetime(2026, 10, 13, h, m, tzinfo=NY).timestamp())
        for q in svc.queues.values():
            q.join()


def test_stop_when_success_in_cycle(tmp_path):
    svc, runs, late = _service(tmp_path, [False, True])
    _at(svc, (16, 0), (16, 5), (16, 10), (16, 15), (16, 30), (16, 46))
    assert [r["scheduled_at"][11:16] for r in runs] == ["16:00", "16:05"]
    assert runs[0]["cycle"] == "2026-10-13" and runs[0]["expected_at"].startswith("2026-10-13T16:00")
    assert svc.cycle_succeeded("pub", 0, dt.date(2026, 10, 13)) and late == []


def test_lateness_alert_when_window_closes_empty(tmp_path):
    svc, runs, late = _service(tmp_path, [])
    _at(svc, (16, 0), (16, 5), (16, 10), (16, 15), (16, 30), (16, 45), (16, 46))
    assert len(runs) == 6 and late == [dt.date(2026, 10, 13)]
    _at(svc, (16, 50))
    assert late == [dt.date(2026, 10, 13)]  # once per cycle


def test_lateness_is_a_notify_event(tmp_path, monkeypatch):
    from agentl import notify

    sent = []
    monkeypatch.setitem(notify.CHANNELS, "console", lambda cfg, text, event: sent.append((text, event)))
    spec = parse(
        {
            "name": "pub",
            "trigger": [{"schedule": BASE}],
            "notify": {
                "channels": {"ops": {"type": "console"}},
                "rules": [{"on": "schedule.late", "to": ["ops"]}],
            },
            "steps": [{"id": "a", "use": "trigger"}],
        }
    )
    svc = Service([spec], Store(tmp_path / "s.db"), run=lambda s, r, k: _Res(False))
    sch = svc.schedules[("pub", 0)]
    svc.next_cycle[("pub", 0)] = sch.next_firings(dt.datetime(2026, 10, 13, 19, tzinfo=UTC), 1)[0]
    _at(svc, (16, 0), (16, 46), (16, 50))
    assert len(sent) == 1
    text, event = sent[0]
    assert event["type"] == "schedule.late" and event["pipeline"] == "pub" and event["step"] is None
    assert event["data"]["cycle"] == "2026-10-13"
    assert event["data"]["expected_at"].startswith("2026-10-13T16:00")
    assert "schedule.late" in text and "cycle=2026-10-13" in text


def test_cli_schedule_next(tmp_path, capsys):
    p = tmp_path / "p.yaml"
    p.write_text(
        "name: pub\ntrigger:\n  - schedule: { tz: UTC, expect: { days: [mon], at: '09:00' } }\n"
        "  - cron: '0 7 * * *'\n    timezone: UTC\nsteps:\n  - id: a\n    use: trigger\n",
        encoding="utf-8",
    )
    assert main(["schedule", "next", str(p), "--n", "3"]) == 0
    out = capsys.readouterr().out
    assert out.count("T09:00:00+00:00  cycle") == 3 and out.count("T07:00:00+00:00") == 3


def test_month_rules_nth_weekday():
    spec = {
        "tz": "Europe/London",
        "expect": {"days": ["sun"], "nth": [1], "months": ["jan", "mar", "may", "jul", "sep", "nov"]},
    }
    sch = Schedule(_spec(spec).trigger[0].schedule)
    days = [d for d, _ in sch.next_firings(dt.datetime(2026, 10, 20, tzinfo=UTC), 4)]
    assert days == [dt.date(2026, 11, 1), dt.date(2027, 1, 3), dt.date(2027, 3, 7), dt.date(2027, 5, 2)]
    spec = {"tz": "UTC", "expect": {"days": ["fri"], "nth": [-1], "months": [12]}}
    sch = Schedule(_spec(spec).trigger[0].schedule)
    assert [d for d, _ in sch.next_firings(dt.datetime(2026, 1, 1, tzinfo=UTC), 2)] == [
        dt.date(2026, 12, 25),
        dt.date(2027, 12, 31),
    ]
    with pytest.raises(ValueError, match=r"expect\.nth"):
        _spec({"tz": "UTC", "expect": {"days": ["sun"], "nth": [6]}})
    with pytest.raises(ValueError, match=r"expect\.months"):
        _spec({"tz": "UTC", "expect": {"days": ["sun"], "months": ["smarch"]}})


def test_more_calendars():
    from agentl.schedule import CALENDARS

    uk = CALENDARS["uk_england"](2027)
    assert dt.date(2027, 3, 26) in uk and dt.date(2027, 3, 29) in uk  # Good Friday, Easter Monday
    assert dt.date(2027, 12, 27) in uk and dt.date(2027, 12, 28) in uk  # Christmas on Saturday
    assert dt.date(2027, 5, 31) in uk and dt.date(2027, 8, 30) in uk
    assert dt.date(2022, 1, 3) in CALENDARS["uk_england"](2022)  # New Year on Saturday
    de = CALENDARS["de_national"](2026)
    assert {dt.date(2026, 5, 14), dt.date(2026, 5, 25), dt.date(2026, 10, 3)} <= de  # Ascension, Whit Monday
    ca = CALENDARS["ca_federal"](2026)
    assert {dt.date(2026, 5, 18), dt.date(2026, 7, 1), dt.date(2026, 10, 12), dt.date(2026, 9, 30)} <= ca
    assert dt.date(2026, 12, 28) in ca and dt.date(2026, 12, 25) in ca  # Boxing Day on Saturday
    spec = {"tz": "Europe/London", "expect": {"days": ["mon"], "calendar": "uk_england"}}
    days = [
        d
        for d, _ in Schedule(_spec(spec).trigger[0].schedule).next_firings(
            dt.datetime(2027, 3, 27, tzinfo=UTC), 1
        )
    ]
    assert days == [dt.date(2027, 4, 5)]  # Easter Monday skipped
