"""Runs every case both ways and compares them.

For each case: the hand-written script (`before.py`) and the agentl pipeline (`pipeline.yaml`)
run on the same data with the same fake model, and must write the same result. Then both run
again with the output location broken on the first try (a full disk, a locked file, a share
that is down), fixed, and run once more: what matters is how many model calls each one pays to
recover.

    python -m cases [--only tickets invoices] [--json out.json]
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from agentl.runner import Runner
from agentl.spec import load
from agentl.store import Store

from . import fake_llm

HERE = Path(__file__).parent
CASES = ["tickets", "invoices", "sales_report", "web_summaries"]


def code_lines(path: Path) -> int:
    """Lines that do something: no blanks, comments or the module docstring."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py" and text.lstrip().startswith('"""'):
        text = text.split('"""', 2)[2]
    return sum(1 for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))


def _fresh(work: Path) -> None:
    out = work / "out"
    if out.is_file():
        out.unlink()
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()


def _break(work: Path) -> None:
    shutil.rmtree(work / "out", ignore_errors=True)
    (work / "out").write_text("not a folder")  # writing there fails, in both versions


def _timed(fn) -> tuple[float, int, str | None]:
    fake_llm.CALLS[0] = 0
    t0 = time.perf_counter()
    err = None
    try:
        fn()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return time.perf_counter() - t0, fake_llm.CALLS[0], err


def run_case(name: str) -> dict:
    case = importlib.import_module(f"cases.{name}.case")
    missing = [m for m in case.NEEDS if importlib.util.find_spec(m) is None]
    if missing:
        return {"case": name, "title": case.TITLE, "skipped": f"needs {', '.join(missing)}"}
    before = importlib.import_module(f"cases.{name}.before")
    work = Path(tempfile.mkdtemp(prefix=f"agentl-case-{name}-"))
    (work / "data").mkdir()
    case.make_data(work)
    os.environ["WORK"] = str(work)
    spec = load(HERE / name / "pipeline.yaml")

    def agentl_run(store: Store) -> None:
        r = Runner(spec, store=store, listeners=[]).run()
        if r.status != "succeeded":
            raise RuntimeError(r.error)

    out: dict = {"case": name, "title": case.TITLE}
    # 1. clean run, same result
    _fresh(work)
    b_secs, b_calls, b_err = _timed(lambda: before.main(str(work)))
    if b_err:
        raise RuntimeError(f"{name}: before.py failed: {b_err}")
    b_result = case.result(work)
    _fresh(work)
    with Store(work / "a.db") as store:
        a_secs, a_calls, a_err = _timed(lambda: agentl_run(store))
    if a_err:
        raise RuntimeError(f"{name}: pipeline failed: {a_err}")
    if case.result(work) != b_result:
        raise AssertionError(f"{name}: the two versions wrote different results")
    extra = [p for p in (HERE / name).glob("*.py") if p.name not in ("before.py", "case.py", "__init__.py")]
    out.update(
        identical=True,
        records=len(b_result),
        lines_before=code_lines(HERE / name / "before.py"),
        lines_after=code_lines(HERE / name / "pipeline.yaml") + sum(code_lines(p) for p in extra),
        seconds_before=round(b_secs, 2),
        seconds_after=round(a_secs, 2),
        calls_before=b_calls,
        calls_after=a_calls,
    )
    # 2. the write fails once, then the job runs again
    _break(work)
    _, c1, err1 = _timed(lambda: before.main(str(work)))
    _fresh(work)
    _, c2, _ = _timed(lambda: before.main(str(work)))
    _break(work)
    with Store(work / "b.db") as store:
        _, d1, err2 = _timed(lambda: agentl_run(store))
        _fresh(work)
        _, d2, _ = _timed(lambda: agentl_run(store))
    assert err1 and err2, "the broken output should fail both versions"
    out.update(recover_calls_before=c1 + c2, recover_calls_after=d1 + d2)
    # 3. same data next day: run again with nothing changed
    with Store(work / "b.db") as store:
        _, d3, _ = _timed(lambda: agentl_run(store))
    _, c3, _ = _timed(lambda: before.main(str(work)))
    out.update(rerun_calls_before=c3, rerun_calls_after=d3)
    shutil.rmtree(work, ignore_errors=True)
    return out


def table(results: list[dict]) -> str:
    lines = []
    for r in results:
        if "skipped" in r:
            lines.append(f"\n{r['title']}: skipped ({r['skipped']})")
            continue
        lines.append(f"\n{r['title']} ({r['records']} records, identical output)")
        lines.append(f"  {'':<34}{'before':>10}{'agentl':>10}")
        rows = [
            ("lines of code", r["lines_before"], r["lines_after"]),
            ("seconds", r["seconds_before"], r["seconds_after"]),
            ("model calls", r["calls_before"], r["calls_after"]),
            ("calls to recover from a failed write", r["recover_calls_before"], r["recover_calls_after"]),
            ("calls to re-run unchanged data", r["rerun_calls_before"], r["rerun_calls_after"]),
        ]
        lines += [f"  {k:<34}{b:>10}{a:>10}" for k, b, a in rows]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m cases")
    p.add_argument("--only", nargs="*", choices=CASES)
    p.add_argument("--json", metavar="PATH")
    a = p.parse_args(argv)
    results = []
    for name in a.only or CASES:
        print(f"… {name}", file=sys.stderr, flush=True)
        results.append(run_case(name))
    print(table(results))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0
