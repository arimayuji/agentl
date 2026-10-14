"""`python -m cases`: every case writes the same result both ways (fake model, no network)."""

import pytest

from cases import harness


@pytest.mark.parametrize("name", harness.CASES)
def test_case_matches_and_recovers(name):
    r = harness.run_case(name)
    if "skipped" in r:
        pytest.skip(r["skipped"])
    assert r["identical"] and r["records"] > 0
    assert r["calls_after"] == r["calls_before"]  # same work on a clean run
    assert r["recover_calls_after"] <= r["recover_calls_before"]
    assert r["rerun_calls_after"] == 0
