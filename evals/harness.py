"""Evals: is the answer right, what does it cost, and how much does the cache save.

Small labeled datasets (evals/data) run through three variants with the same model and prompt:
agentl, agentl with `records_per_call: 10`, and a plain-Python loop calling the model directly
(the baseline). Then the cache: the same pipeline again with 0%, 10% and 50% of the records
changed.

    python -m evals --provider anthropic --model claude-haiku-5-5 --price-in IN --price-out OUT
    python -m evals --provider fake        # offline: exercises the harness, the numbers mean nothing

Prices are USD per million tokens and are not guessed: without them, cost is reported as n/a.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

from agentl import parse, register_provider
from agentl.llm import Result, provider, schema_model
from agentl.runner import Runner
from agentl.spec import LLMSpec
from agentl.store import Store

DATA = Path(__file__).parent / "data"

TASKS: dict[str, dict] = {
    "tickets": {
        "instruction": (
            "Classify the support ticket. category is one of: billing, bug, account, "
            "feature_request, other. urgent is true only when something critical is blocked right "
            "now or the customer needs it fixed today."
        ),
        "output": {"category": "str", "urgent": "bool"},
    },
    "invoices": {
        "instruction": (
            "Extract from the invoice: tax_id (the CNPJ exactly as written, like 12.345.678/0001-90), "
            "total (the total amount due as a number, like 1234.56) and due_date (the payment due "
            "date as YYYY-MM-DD)."
        ),
        "output": {"tax_id": "str", "total": "float", "due_date": "str"},
    },
}

CONCURRENCY = 8


def load(task: str) -> list[dict]:
    return [json.loads(line) for line in (DATA / f"{task}.jsonl").read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- scoring
def _norm(field: str, v: Any) -> Any:
    if v is None:
        return None
    if field == "tax_id":
        return re.sub(r"\D", "", str(v))
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    return str(v).strip().casefold()


def score(rows: list[dict], answers: dict[int, dict], fields: list[str]) -> dict:
    """Share of records where each field matches the label, and where all of them do."""
    per = {f: 0 for f in fields}
    exact = 0
    for r in rows:
        a = answers.get(r["id"]) or {}
        ok = [_norm(f, a.get(f)) == _norm(f, r["label"][f]) for f in fields]
        for f, hit in zip(fields, ok, strict=True):
            per[f] += hit
        exact += all(ok)
    n = len(rows) or 1
    return {"all_fields": exact / n, **{f: per[f] / n for f in fields}}


# ---------------------------------------------------------------- fake model (offline)
class FakeModel:
    """Answers from the labels with a fixed share of deterministic mistakes and 5 ms per call.
    It exists so the harness can be tested without a key; its accuracy is set, not measured."""

    labels: ClassVar[dict[str, dict]] = {}
    error_rate = 0.1
    latency = 0.005

    def __init__(self, spec: LLMSpec):
        pass

    def _answer(self, text: str, schema: Any) -> dict:
        label = dict(self.labels.get(text.removesuffix(" (sent again)"), {}))
        if sum(map(ord, text)) % 100 < self.error_rate * 100:
            for k, v in label.items():
                label[k] = (not v) if isinstance(v, bool) else (v + 1 if isinstance(v, float) else "wrong")
                break
        return label

    def structured(self, instruction: str, record: dict, schema: Any) -> Result:
        time.sleep(self.latency)
        recs = record.get("records")
        tin = 300 + len(json.dumps(record)) // 4
        if recs is None:
            ans = self._answer(record["text"], schema)
            return Result(schema.model_validate(ans), tin, 20)
        items = [{"index": r["index"], **self._answer(r["record"]["text"], schema)} for r in recs]
        return Result(schema.model_validate({"items": items}), tin, 20 * len(recs))

    def text(self, instruction: str, content: str) -> str:
        return ""


register_provider("fake", FakeModel)


# ---------------------------------------------------------------- variants
_SOURCE: dict[str, list[dict]] = {"rows": []}


def _rows(input: Any = None) -> list[dict]:
    return [{"id": r["id"], "text": r["text"]} for r in _SOURCE["rows"]]


def _llm(a: argparse.Namespace) -> dict:
    llm: dict = {"provider": a.provider, "model": a.model}
    if a.price_in is not None and a.price_out is not None:
        llm["price"] = {"input": a.price_in, "output": a.price_out}
    return llm


def run_agentl(
    a: argparse.Namespace, task: str, rows: list[dict], store: Store, per_call: int = 1, cache: bool = False
) -> dict:
    _SOURCE["rows"] = rows
    t = TASKS[task]
    spec = parse(
        {
            "name": f"eval-{task}",
            "llm": _llm(a),
            "steps": [
                {"id": "src", "fn": "evals.harness:_rows"},
                {
                    "id": "answer",
                    "agent": t["instruction"],
                    "input": "src",
                    "concurrency": CONCURRENCY,
                    "records_per_call": per_call,
                    "output": t["output"],
                },
            ],
        }
    )
    t0 = time.perf_counter()
    r = Runner(spec, store=store, use_cache=cache).run()
    secs = time.perf_counter() - t0
    if r.status != "succeeded":
        raise RuntimeError(r.error)
    answers = {rec["id"]: rec for rec in r.outputs["answer"]}
    return {
        "answers": answers,
        "seconds": secs,
        "calls": r.llm_calls,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cost_usd": r.cost_usd if "price" in _llm(a) else None,
    }


def run_plain(a: argparse.Namespace, task: str, rows: list[dict]) -> dict:
    """The baseline: the same client, prompt and schema in a thread pool, no pipeline."""
    t = TASKS[task]
    spec = LLMSpec(**_llm(a))
    model = provider(spec)
    schema = schema_model("Answer", t["output"])
    latencies: list[float] = []

    def one(r: dict) -> tuple[int, Result]:
        t0 = time.perf_counter()
        res = model.structured(t["instruction"], {"id": r["id"], "text": r["text"]}, schema)
        latencies.append(time.perf_counter() - t0)
        return r["id"], res

    t0 = time.perf_counter()
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        results = list(pool.map(one, rows))
    secs = time.perf_counter() - t0
    tin = sum(res.input_tokens for _, res in results)
    tout = sum(res.output_tokens for _, res in results)
    lat = sorted(latencies)
    return {
        "answers": {rid: res.value.model_dump() for rid, res in results},
        "seconds": secs,
        "calls": len(results),
        "input_tokens": tin,
        "output_tokens": tout,
        "cost_usd": spec.cost(tin, tout) if spec.price else None,
        "p50_ms": round(statistics.median(lat) * 1000, 1),
        "p95_ms": round(lat[max(0, int(len(lat) * 0.95) - 1)] * 1000, 1),
    }


# ---------------------------------------------------------------- report
def _summary(runs: list[dict], rows: list[dict], fields: list[str]) -> dict:
    scores = [score(rows, r["answers"], fields) for r in runs]
    n = len(rows)

    def stat(key: str) -> dict:
        vals = [s[key] for s in scores]
        return {"mean": round(statistics.mean(vals), 4), "stdev": round(statistics.pstdev(vals), 4)}

    costs = [r["cost_usd"] for r in runs if r["cost_usd"] is not None]
    out = {
        "accuracy": {k: stat(k) for k in ["all_fields", *fields]},
        "calls": runs[-1]["calls"],
        "tokens_per_record": round((runs[-1]["input_tokens"] + runs[-1]["output_tokens"]) / n, 1),
        "usd_per_1k_records": round(statistics.mean(costs) / n * 1000, 4) if costs else None,
        "seconds": round(statistics.median(r["seconds"] for r in runs), 2),
    }
    for k in ("p50_ms", "p95_ms"):
        if k in runs[-1]:
            out[k] = runs[-1][k]
    return out


def evaluate(a: argparse.Namespace) -> dict:
    report: dict = {
        "meta": {
            "provider": a.provider,
            "model": a.model,
            "price_per_mtok": {"input": a.price_in, "output": a.price_out},
            "runs": a.runs,
            "date": time.strftime("%Y-%m-%d"),
            "python": platform.python_version(),
            "concurrency": CONCURRENCY,
        },
        "tasks": {},
    }
    tmp = Path(tempfile.mkdtemp(prefix="agentl-evals-"))
    for task in a.tasks:
        rows = load(task)
        if a.limit:
            rows = rows[: a.limit]
        fields = list(TASKS[task]["output"])
        FakeModel.labels = {r["text"]: r["label"] for r in rows}
        variants: dict[str, dict] = {}
        for name, per_call in (
            ("plain Python loop", None),
            ("agentl", 1),
            ("agentl, records_per_call 10", 10),
        ):
            print(f"… {task} / {name}", file=sys.stderr, flush=True)
            runs = []
            for _ in range(a.runs):
                if per_call is None:
                    runs.append(run_plain(a, task, rows))
                else:
                    with Store(tmp / f"{task}-{time.time_ns()}.db") as store:
                        runs.append(run_agentl(a, task, rows, store, per_call=per_call))
            variants[name] = _summary(runs, rows, fields)
        report["tasks"][task] = {
            "records": len(rows),
            "variants": variants,
            "cache": cache_savings(a, task, rows, tmp),
        }
    return report


def cache_savings(a: argparse.Namespace, task: str, rows: list[dict], tmp: Path) -> list[dict]:
    """First run with the cache on, then reruns where a share of the records changed."""
    print(f"… {task} / cache", file=sys.stderr, flush=True)
    out = []
    for share in (0.0, 0.1, 0.5):
        with Store(tmp / f"cache-{task}-{share}.db") as store:
            first = run_agentl(a, task, rows, store, cache=True)
            k = round(len(rows) * share)
            changed = [{**r, "text": r["text"] + " (sent again)"} if i < k else r for i, r in enumerate(rows)]
            again = run_agentl(a, task, changed, store, cache=True)
        saved = None
        if first["cost_usd"]:
            saved = round(1 - (again["cost_usd"] or 0) / first["cost_usd"], 4)
        out.append(
            {
                "changed": share,
                "calls_first": first["calls"],
                "calls_rerun": again["calls"],
                "calls_avoided": round(1 - again["calls"] / max(1, first["calls"]), 4),
                "cost_saved": saved,
                "seconds_first": round(first["seconds"], 2),
                "seconds_rerun": round(again["seconds"], 2),
            }
        )
    return out


def table(report: dict) -> str:
    m = report["meta"]
    lines = [f"{m['provider']} / {m['model']} · {m['runs']} run(s) · {m['date']}"]
    if m["provider"] == "fake":
        lines.append("fake provider: these numbers test the harness, they are not results")
    for task, t in report["tasks"].items():
        lines += ["", f"{task} ({t['records']} records)"]
        lines.append(
            f"  {'variant':<30} {'all fields':>11} {'calls':>6} {'tok/rec':>8} {'USD/1k':>8} {'seconds':>8}"
        )
        for name, v in t["variants"].items():
            acc = v["accuracy"]["all_fields"]
            usd = "n/a" if v["usd_per_1k_records"] is None else f"{v['usd_per_1k_records']:.4f}"
            lines.append(
                f"  {name:<30} {acc['mean']:>6.1%} ±{acc['stdev']:.1%} {v['calls']:>6} "
                f"{v['tokens_per_record']:>8} {usd:>8} {v['seconds']:>8}"
            )
        for c in t["cache"]:
            saved = "n/a" if c["cost_saved"] is None else f"{c['cost_saved']:.0%}"
            lines.append(
                f"  rerun with {c['changed']:.0%} changed: {c['calls_rerun']}/{c['calls_first']} calls "
                f"({c['calls_avoided']:.0%} avoided), cost saved {saved}, "
                f"{c['seconds_first']}s -> {c['seconds_rerun']}s"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals")
    p.add_argument("--provider", default="fake")
    p.add_argument("--model", default="fake")
    p.add_argument("--price-in", type=float, help="USD per million input tokens")
    p.add_argument("--price-out", type=float, help="USD per million output tokens")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--tasks", nargs="*", default=list(TASKS), choices=list(TASKS))
    p.add_argument("--limit", type=int, help="first N records of each dataset")
    p.add_argument("--json", metavar="PATH")
    a = p.parse_args(argv)
    report = evaluate(a)
    print(table(report))
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
