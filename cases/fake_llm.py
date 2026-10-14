"""The model both versions of every case call: fixed 20 ms latency and deterministic answers, so
the outputs can be compared exactly and every call is counted. It stands in for a real API call
such as `client.messages.create(...)`; the cost of a real model scales with `CALLS`."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from agentl import register_provider
from agentl.llm import Result

LATENCY = 0.02
CALLS = [0]
_LOCK = threading.Lock()
_DATA = Path(__file__).resolve().parent.parent / "evals" / "data"
_LABELS: dict[str, dict] = {}


def _labels() -> dict[str, dict]:
    if not _LABELS:
        for name in ("tickets", "invoices"):
            for line in (_DATA / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                _LABELS[row["text"].strip()] = row["label"]
    return _LABELS


def complete(task: str, text: str) -> dict[str, Any]:
    """One model call: the answer for `text` under `task`."""
    time.sleep(LATENCY)
    with _LOCK:
        CALLS[0] += 1
    if task == "summary":
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        return {"summary": first.lstrip("# ").strip()[:120]}
    label = _labels().get(text.strip())
    if label is None:
        raise ValueError(f"fake model has no answer for: {text[:60]!r}")
    return dict(label)


class FakeProvider:
    """agentl's side of the same model: `llm: {provider: case-fake, options: {task: ...}}`."""

    def __init__(self, spec: Any):
        self.task = spec.options["task"]

    def structured(self, instruction: str, record: dict, schema: Any) -> Result:
        text = record.get("text") or record.get("markdown") or ""
        return Result(schema.model_validate(complete(self.task, text)), 300, 30)

    def text(self, instruction: str, content: str) -> str:
        return ""


register_provider("case-fake", FakeProvider)
