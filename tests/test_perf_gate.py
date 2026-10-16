"""The performance regression gate (#157): ratios to a reference variant of the same run, so a slower
CI machine does not fail it, and a slower agentl does."""

import json
import subprocess
import sys
from pathlib import Path

from benchmarks import compare

ROOT = Path(__file__).parent.parent


def _results(agentl_s: float, ref_s: float, mb: float = 60) -> list[dict]:
    case, mine, ref, _ = compare.GATES[0]
    return [
        {
            "case": case,
            "variants": [
                {"variant": mine, "seconds": agentl_s, "peak_mb": mb},
                {"variant": ref, "seconds": ref_s, "peak_mb": 0},
            ],
        }
    ]


def test_ratio_cancels_the_machine_speed():
    fast = compare.ratios(_results(1.4, 1.0))
    slow_machine = compare.ratios(_results(2.8, 2.0))  # everything twice as slow
    case = compare.GATES[0][0]
    assert fast[case]["time_ratio"] == slow_machine[case]["time_ratio"] == 1.4


def test_gate_passes_within_tolerance_and_fails_beyond():
    baseline = {"ratios": compare.ratios(_results(1.4, 1.0))}
    assert compare.check(_results(1.6, 1.0), baseline) == []  # +14%, under the 20% tolerance
    problems = compare.check(_results(1.8, 1.0), baseline)  # +29%
    assert len(problems) == 1 and "1.80x the reference" in problems[0]


def test_gate_catches_memory_growth_and_a_missing_case():
    baseline = {"ratios": compare.ratios(_results(1.4, 1.0, mb=60))}
    assert compare.check(_results(1.4, 1.0, mb=80), baseline) == []  # 60 * 1.25 + 10 = 85
    assert "peak 120 MB" in compare.check(_results(1.4, 1.0, mb=120), baseline)[0]
    assert "not measured" in compare.check([], baseline)[0]


def test_the_committed_baseline_covers_every_gate():
    baseline = json.loads((ROOT / "benchmarks" / "baseline.json").read_text())
    assert set(baseline["ratios"]) == {g[0] for g in compare.GATES}
    assert baseline["quick"] is False  # quick runs are too small: start-up cost swamps the ratio


def test_cli_flags_exist():
    out = subprocess.run(
        [sys.executable, "-m", "benchmarks", "--help"], capture_output=True, text=True, cwd=ROOT
    )
    assert "--check" in out.stdout and "--write-baseline" in out.stdout
