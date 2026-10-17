"""time.normalize (#213): hour ending / beginning, accumulated windows, DST gaps and overlaps."""

from __future__ import annotations

import pytest

from agentl.connectors.timeconv import normalize

try:
    from zoneinfo import ZoneInfo

    ZoneInfo("America/Sao_Paulo")
    ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - Windows without tzdata
    pytest.skip("IANA time zone data not available", allow_module_level=True)

SP = "America/Sao_Paulo"
NY = "America/New_York"


def run(rows, src, to=None, **kw):
    return normalize(rows, **{"from": src}, to=to or {"column": "ts"}, **kw)


def day_rows(day, labels):
    return [{"date": day, "hour": h} for h in labels]


def test_hour_ending_24_is_next_midnight():
    out = run(
        day_rows("2026-03-10", [1, 24]),
        {"column": "hour", "kind": "hour_ending", "date_column": "date", "tz": "UTC"},
        {"column": "ts", "convention": "interval_end", "end_column": "start"},
    )
    assert out[0]["ts"] == "2026-03-10T01:00:00+00:00"
    assert out[0]["start"] == "2026-03-10T00:00:00+00:00"
    assert out[1]["ts"] == "2026-03-11T00:00:00+00:00"


def test_clock_24_00_label_without_date_column():
    out = run(
        [{"t": "2026-03-10 24:00"}],
        {"column": "t", "kind": "hour_ending", "tz": SP},
        {"column": "ts", "convention": "interval_start", "tz": SP},
    )
    assert out[0]["ts"] == "2026-03-10T23:00:00-03:00"


def test_hour_beginning_and_half_hours():
    out = run(
        day_rows("2026-03-10", [0, 13.5]),
        {"column": "hour", "kind": "hour_beginning", "date_column": "date", "tz": "UTC"},
    )
    assert [r["ts"] for r in out] == ["2026-03-10T00:00:00+00:00", "2026-03-10T13:30:00+00:00"]
    half = run(
        day_rows("2026-03-10", [1, 48]),
        {"column": "hour", "date_column": "date", "tz": "UTC", "interval": 30},
        {"column": "ts", "convention": "interval_start"},
    )
    assert [r["ts"] for r in half] == ["2026-03-10T00:00:00+00:00", "2026-03-10T23:30:00+00:00"]


def test_accumulated_window():
    out = run(
        day_rows("2026-03-10", ["06:00", 18]),
        {"column": "hour", "kind": "accumulated", "date_column": "date", "tz": "UTC", "since": "06:00"},
        {"column": "start", "convention": "interval_start", "end_column": "end"},
    )
    assert out[1]["start"] == "2026-03-10T06:00:00+00:00"
    assert out[1]["end"] == "2026-03-10T18:00:00+00:00"


def test_new_york_spring_forward_gap_fails_by_default():
    src = {"column": "hour", "kind": "hour_beginning", "date_column": "date", "tz": NY}
    with pytest.raises(ValueError, match="DST gap"):
        run(day_rows("2026-03-08", [2]), src)
    out = run(day_rows("2026-03-08", [2]), src, on_missing="reject")
    assert list(out) == [] and "DST gap" in out.rejected[0]["_error"]
    shifted = run(day_rows("2026-03-08", [2]), src, on_missing="shift")
    assert shifted[0]["ts"] == "2026-03-08T07:00:00+00:00"  # 03:00 EDT


def test_new_york_fall_back_overlap():
    src = {"column": "hour", "kind": "hour_beginning", "date_column": "date", "tz": NY}
    with pytest.raises(ValueError, match="DST overlap"):
        run(day_rows("2026-11-01", [1]), src)
    assert run(day_rows("2026-11-01", [1]), src, on_ambiguous="first")[0]["ts"] == (
        "2026-11-01T05:00:00+00:00"
    )
    assert run(day_rows("2026-11-01", [1]), src, on_ambiguous="last")[0]["ts"] == (
        "2026-11-01T06:00:00+00:00"
    )


def test_new_york_23_and_25_hour_days_elapsed():
    src = {"column": "hour", "date_column": "date", "tz": NY, "clock": "elapsed"}
    short = run(day_rows("2026-03-08", range(1, 24)), src)
    assert len({r["ts"] for r in short}) == 23
    assert short[-1]["ts"] == "2026-03-09T03:00:00+00:00"  # 23:00 EDT
    with pytest.raises(ValueError, match="past the end"):
        run(day_rows("2026-03-08", [24]), src)
    long = run(day_rows("2026-11-01", range(1, 26)), src)
    stamps = [r["ts"] for r in long]
    assert len(set(stamps)) == 25 and stamps == sorted(stamps)
    assert stamps[0] == "2026-11-01T04:00:00+00:00"
    assert stamps[-1] == "2026-11-02T04:00:00+00:00"


def test_sao_paulo_historical_gap_at_midnight():
    # 2018-11-04: clocks went from 00:00 to 01:00, so the day had 23 hours.
    src = {"column": "hour", "kind": "hour_beginning", "date_column": "date", "tz": SP}
    with pytest.raises(ValueError, match="DST gap"):
        run(day_rows("2018-11-04", [0]), src)
    elapsed = run(
        day_rows("2018-11-04", range(1, 24)),
        {"column": "hour", "date_column": "date", "tz": SP, "clock": "elapsed"},
        {"column": "ts", "convention": "interval_start", "tz": SP},
    )
    assert elapsed[0]["ts"] == "2018-11-04T01:00:00-02:00"
    assert elapsed[-1]["ts"] == "2018-11-04T23:00:00-02:00"


def test_sao_paulo_historical_fall_back_overlap():
    # 2018-02-18 00:00 -02 became 2018-02-17 23:00 -03: the 17th had 25 hours.
    src = {"column": "hour", "kind": "hour_ending", "date_column": "date", "tz": SP}
    with pytest.raises(ValueError, match="DST overlap"):
        run(day_rows("2018-02-17", [23]), src)
    out = run(day_rows("2018-02-17", [24]), src, {"column": "ts", "tz": SP})
    assert out[0]["ts"] == "2018-02-17T23:00:00-03:00"
    long = run(
        day_rows("2018-02-17", range(1, 26)),
        {**src, "clock": "elapsed"},
        {"column": "ts", "convention": "interval_end"},
    )
    assert long[-1]["ts"] == "2018-02-18T03:00:00+00:00"
    assert len({r["ts"] for r in long}) == 25


def test_bad_options():
    with pytest.raises(ValueError, match="kind"):
        run([], {"column": "h", "kind": "nope"})
    with pytest.raises(ValueError, match="on_ambiguous"):
        run([], {"column": "h"}, on_ambiguous="guess")
    with pytest.raises(ValueError, match="time of day"):
        run(day_rows("2026-01-01", ["24:30"]), {"column": "hour", "date_column": "date"})
