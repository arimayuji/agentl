"""Tumbling time windows in aggregate (#226)."""

import pytest

from agentl.registry import Rejects, get

agg = get("aggregate")


def test_windows_by_device_sorted_with_rejects():
    rows = [
        {"device": "b", "ts": "2026-10-17T10:00:59", "t": 20},
        {"device": "a", "ts": "2026-10-17T10:01:10", "t": 22},
        {"device": "a", "ts": "2026-10-17T10:00:05", "t": 21},
        {"device": "a", "ts": "2026-10-17T10:00:40", "t": 23},
        {"device": "a", "ts": "not a time", "t": 1},
        {"device": "a", "ts": None, "t": 1},
    ]
    out = agg(
        rows,
        metrics={"avg_t": "avg(t)", "n": "count()"},
        group_by="device",
        window={"field": "ts", "every": "1m", "end": True},
    )
    assert isinstance(out, Rejects)
    assert list(out) == [
        {
            "window_start": "2026-10-17T10:00:00",
            "window_end": "2026-10-17T10:01:00",
            "device": "a",
            "avg_t": 22,
            "n": 2,
        },
        {
            "window_start": "2026-10-17T10:00:00",
            "window_end": "2026-10-17T10:01:00",
            "device": "b",
            "avg_t": 20,
            "n": 1,
        },
        {
            "window_start": "2026-10-17T10:01:00",
            "window_end": "2026-10-17T10:02:00",
            "device": "a",
            "avg_t": 22,
            "n": 1,
        },
    ]
    assert len(out.rejected) == 2 and all("_error" in r for r in out.rejected)


def test_timezone_kept_and_epoch_numbers():
    out = agg(
        [{"ts": "2026-10-17T10:07:00-03:00"}, {"ts": 1760695620}, {"ts": 1760695620000}],
        metrics={"n": "count()"},
        window={"field": "ts", "every": "5m"},
    )
    assert list(out) == [
        {"window_start": "2025-10-17T10:05:00+00:00", "n": 2},  # epoch seconds and milliseconds
        {"window_start": "2026-10-17T10:05:00-03:00", "n": 1},
    ]
    day = agg(
        [{"ts": "2026-10-17T23:59:59Z"}], metrics={"n": "count()"}, window={"field": "ts", "every": "1d"}
    )
    assert day[0]["window_start"] == "2026-10-17T00:00:00+00:00"


@pytest.mark.parametrize("every", ["1x", "0s", "m", "", "1.5h"])
def test_bad_every_fails_early(every):
    with pytest.raises(ValueError, match="aggregate: window"):
        agg([], metrics={"n": "count()"}, window={"field": "ts", "every": every})


def test_without_window_unchanged():
    out = agg([{"g": 1}, {"g": 1}], metrics={"n": "count()"}, group_by="g")
    assert out == [{"g": 1, "n": 2}] and not isinstance(out, Rejects)
