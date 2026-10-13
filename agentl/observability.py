"""Event listeners for logs, traces, metrics and error tracking.

Pass them to `Runner(listeners=[...])` / `agentl.run(..., listeners=[...])`, or use the CLI flags
`--log-format json`, `--otel` and `--sentry` (see the Observability docs)."""

from __future__ import annotations

import json
import logging
import sys
import threading
from typing import IO, Any

from .registry import require


class JsonLogs:
    """One JSON object per event (ts, level, run_id, pipeline, type, step, data), to a stream
    (stderr by default) or a `logging.Logger`. Works with any log shipper (Loki, CloudWatch,
    Datadog, ELK)."""

    def __init__(self, stream: IO[str] | None = None, logger: logging.Logger | None = None):
        self.stream = stream
        self.logger = logger
        self._lock = threading.Lock()

    def __call__(self, event: dict) -> None:
        failed = event["type"].endswith(".failed") or event["type"] == "budget.exceeded"
        level = "error" if failed else "warning" if event["type"] == "item.rejected" else "info"
        line = json.dumps({"level": level, **event}, default=str, ensure_ascii=False)
        if self.logger is not None:
            self.logger.log(getattr(logging, level.upper()), line)
            return
        with self._lock:
            print(line, file=self.stream or sys.stderr, flush=True)


class OpenTelemetry:
    """A trace per run with a child span per step, plus metrics. Uses the global OpenTelemetry
    providers, so configure exporters as usual (e.g. `opentelemetry-instrument` or the SDK).
    Needs `agentl-etl[otel]`.

    Metrics: `agentl.runs`, `agentl.records`, `agentl.rejected`, `agentl.tokens`,
    `agentl.cost_usd` (counters) and `agentl.step.duration` (histogram, seconds)."""

    def __init__(self, tracer_provider: Any = None, meter_provider: Any = None):
        trace = require("opentelemetry.trace", "otel")
        metrics = require("opentelemetry.metrics", "otel")
        self._trace = trace
        self.tracer = trace.get_tracer("agentl", tracer_provider=tracer_provider)
        meter = metrics.get_meter("agentl", meter_provider=meter_provider)
        self.runs = meter.create_counter("agentl.runs", description="finished runs")
        self.records = meter.create_counter("agentl.records", description="records out of steps")
        self.rejected = meter.create_counter("agentl.rejected", description="records rejected by agent steps")
        self.tokens = meter.create_counter("agentl.tokens", description="LLM tokens")
        self.cost = meter.create_counter("agentl.cost_usd", unit="USD", description="LLM cost")
        self.duration = meter.create_histogram("agentl.step.duration", unit="s", description="step duration")
        self._runs: dict[str, Any] = {}
        self._steps: dict[tuple[str, str | None], Any] = {}
        self._lock = threading.Lock()

    def __call__(self, event: dict) -> None:
        with self._lock:
            self._handle(event)

    def _handle(self, e: dict) -> None:
        run_id, kind, step, data = e["run_id"], e["type"], e.get("step"), e.get("data", {})
        base = {"agentl.pipeline": e.get("pipeline", ""), "agentl.run_id": run_id}
        ns = int(e.get("ts", 0) * 1e9) or None
        if kind == "run.started":
            self._runs[run_id] = self.tracer.start_span(
                f"agentl run {e.get('pipeline', '')}", attributes=base, start_time=ns
            )
        elif kind == "step.started" and run_id in self._runs:
            ctx = self._trace.set_span_in_context(self._runs[run_id])
            attrs = {**base, "agentl.step": step, "agentl.step.kind": data.get("kind", "")}
            self._steps[(run_id, step)] = self.tracer.start_span(
                f"step {step}", context=ctx, attributes=attrs, start_time=ns
            )
        elif kind in ("step.succeeded", "step.failed"):
            span = self._steps.pop((run_id, step), None)
            labels = {"pipeline": e.get("pipeline", ""), "step": step or ""}
            if "records" in data:
                self.records.add(data["records"], labels)
            if "seconds" in data:
                self.duration.record(data["seconds"], labels)
            self._spend(data, labels)
            if span is not None:
                for k in ("records", "seconds", "input_tokens", "output_tokens", "cost_usd"):
                    if k in data:
                        span.set_attribute(f"agentl.{k}", data[k])
                if kind == "step.failed":
                    span.set_status(
                        self._trace.Status(self._trace.StatusCode.ERROR, str(data.get("error", "")))
                    )
                span.end(end_time=ns)
        elif kind == "item.rejected":
            self.rejected.add(data.get("count", 0), {"pipeline": e.get("pipeline", ""), "step": step or ""})
            if (span := self._steps.get((run_id, step))) is not None:
                span.add_event(
                    "item.rejected", {"count": data.get("count", 0), "error": str(data.get("error", ""))}
                )
        elif kind == "budget.exceeded" and run_id in self._runs:
            self._runs[run_id].add_event("budget.exceeded", {"reason": str(data.get("reason", ""))})
        elif kind in ("run.succeeded", "run.failed"):
            self.runs.add(1, {"pipeline": e.get("pipeline", ""), "status": kind.split(".")[1]})
            span = self._runs.pop(run_id, None)
            if span is not None:
                for k in ("seconds", "input_tokens", "output_tokens", "cost_usd", "rejected"):
                    if k in data:
                        span.set_attribute(f"agentl.{k}", data[k])
                if kind == "run.failed":
                    span.set_status(
                        self._trace.Status(self._trace.StatusCode.ERROR, str(data.get("error", "")))
                    )
                span.end(end_time=ns)

    def _spend(self, data: dict, labels: dict) -> None:
        if "input_tokens" in data:
            self.tokens.add(data["input_tokens"], {**labels, "direction": "input"})
            self.tokens.add(data.get("output_tokens", 0), {**labels, "direction": "output"})
        if data.get("cost_usd"):
            self.cost.add(data["cost_usd"], labels)


class Sentry:
    """Report failed runs to Sentry, tagged with pipeline, run id and failing step.
    Initializes the SDK when `dsn` is given; otherwise uses an already initialized SDK.
    Needs `agentl-etl[sentry]`."""

    def __init__(self, dsn: str | None = None, **init_options: Any):
        self.sdk = require("sentry_sdk", "sentry")
        if dsn:
            self.sdk.init(dsn=dsn, **init_options)
        self._failed_step: dict[str, str] = {}

    def __call__(self, event: dict) -> None:
        if event["type"] == "step.failed" and event.get("step"):
            self._failed_step[event["run_id"]] = event["step"]
        if event["type"] != "run.failed":
            return
        step = self._failed_step.pop(event["run_id"], None)
        with self.sdk.new_scope() as scope:
            scope.set_tag("agentl.pipeline", event.get("pipeline", ""))
            scope.set_tag("agentl.run_id", event["run_id"])
            if step:
                scope.set_tag("agentl.step", step)
            scope.set_context("agentl", event.get("data", {}))
            scope.fingerprint = ["agentl", event.get("pipeline", ""), step or ""]
            self.sdk.capture_message(
                f"agentl run failed: {event.get('pipeline', '')}: {event['data'].get('error', '')}",
                level="error",
            )
