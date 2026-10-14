"""`python -m benchmarks --compare` runs end to end and every variant writes the same data."""

import pytest

pytest.importorskip("resource")

from benchmarks import compare


def test_compare_runs_and_outputs_match():
    results = compare.run_all(scale=0.002, repeat=1)
    ran = [c for c in results if "skipped" not in c]
    assert ran and all(c["identical_output"] for c in ran)
    plain = {v["variant"]: v for c in ran for v in c["variants"] if "skipped" not in v}
    assert plain["plain Python (loop)"]["imports_agentl"] is False
    assert plain["agentl (concurrency 8)"]["imports_agentl"] is True
    assert "seconds" in compare.table(results)
