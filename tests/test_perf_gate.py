"""The performance regression gate (#157): this agentl against the base branch's on one machine
(--against), and by hand, ratios to a reference variant against a saved file (--check)."""

import subprocess
import sys
from pathlib import Path

import pytest

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


def test_cli_flags_exist():
    out = subprocess.run(
        [sys.executable, "-m", "benchmarks", "--help"], capture_output=True, text=True, cwd=ROOT
    )
    assert "--check" in out.stdout and "--write-baseline" in out.stdout and "--against" in out.stdout


def _ab(head_s: float, base_s: float, head_mb: float = 100, base_mb: float = 100) -> list[dict]:
    case, mine, _, _ = compare.GATES[0]
    return [
        {
            "case": case,
            "variants": [
                {"variant": mine, "seconds": head_s, "peak_mb": 0, "max_rss_mb": head_mb},
                {"variant": mine + compare.BASE, "seconds": base_s, "peak_mb": 0, "max_rss_mb": base_mb},
            ],
        }
    ]


def test_against_the_base_passes_within_tolerance_and_fails_beyond():
    case = compare.GATES[0][0]
    rows, problems = compare.check_against(_ab(1.1, 1.0))  # +10%, under the 20% tolerance
    assert problems == [] and rows[case]["time_ratio"] == 1.1
    _, problems = compare.check_against(_ab(1.3, 1.0))
    assert len(problems) == 1 and "1.30x, limit 1.20x" in problems[0]


def test_against_the_base_catches_memory_growth():
    assert compare.check_against(_ab(1.0, 1.0, head_mb=130))[1] == []  # 100 * 1.25 + 10 = 135
    assert "peak RSS 140 MB" in compare.check_against(_ab(1.0, 1.0, head_mb=140))[1][0]


def test_against_the_base_skips_cases_left_out_and_notes_new_ones():
    assert compare.check_against([]) == ({}, [])  # --only left every gated case out
    results = _ab(1.0, 1.0)
    results[0]["variants"][1] = {
        "variant": results[0]["variants"][1]["variant"],
        "skipped": "the base failed",
    }
    rows, problems = compare.check_against(results)
    assert problems == [] and "new in this change" in rows[compare.GATES[0][0]]["note"]
    del results[0]["variants"][0]  # this agentl did not run it: that is a failure
    assert "not measured" in compare.check_against(results)[1][0]


@pytest.mark.skipif(sys.platform == "win32", reason="--compare needs the resource module (Linux or macOS)")
def test_peak_memory_starts_fresh_in_the_child():
    # ru_maxrss carries over from the parent on Linux; VmHWM does not. A child that allocates
    # nothing must not report the parent's peak.
    big = bytearray(300 * 2**20)  # noqa: F841  (raise this process's peak)
    code = "from benchmarks.compare import _hwm_mb; print(_hwm_mb())"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
    assert float(out.stdout) < 200


@pytest.mark.skipif(sys.platform == "win32", reason="--compare needs the resource module (Linux or macOS)")
def test_against_loads_agentl_from_the_given_tree(tmp_path):
    # The child checks agentl.__file__ against the tree and raises otherwise; this repo is a valid tree.
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks",
            "--compare",
            "--quick",
            "--against",
            str(ROOT),
            "--only",
            "LLM call per record",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    # The verdict itself is noise here (one quick run of ~0.2 s each side): only check that both
    # agentls ran and the gate reported. A wrong tree raises before any output, with exit 1 too.
    assert out.returncode in (0, 1), out.stderr[-2000:]
    assert "agentl (concurrency 8) @ base" in out.stdout, out.stderr[-2000:]
    assert "LLM call per record: " in out.stdout.split("regression gate against")[1]
