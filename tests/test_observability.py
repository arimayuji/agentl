import io
import json
import sys
import types

import pytest

from agentl import parse
from agentl.observability import JsonLogs
from agentl.runner import Runner
from agentl.store import Store


def pipeline(fail=False):
    steps = [
        {"id": "src", "fn": "tests.test_observability:rows"},
        {"id": "out", "use": "select", "input": "src"},
    ]
    if fail:
        steps.append({"id": "boom", "fn": "tests.test_observability:boom", "input": "out"})
    return parse({"name": "obs", "steps": steps})


def rows(input=None):
    return [{"id": 1}, {"id": 2}]


def boom(input=None):
    raise RuntimeError("kaboom")


def run(tmp_path, spec, listener):
    with Store(tmp_path / "db.sqlite") as store:
        return Runner(spec, store=store, listeners=[listener]).run()


def test_json_logs(tmp_path):
    buf = io.StringIO()
    run(tmp_path, pipeline(fail=True), JsonLogs(stream=buf))
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert lines[0]["type"] == "run.started" and lines[0]["pipeline"] == "obs" and lines[0]["level"] == "info"
    failed = next(x for x in lines if x["type"] == "step.failed")
    assert failed["level"] == "error" and failed["step"] == "boom" and "kaboom" in failed["data"]["error"]
    assert all({"ts", "run_id"} <= set(x) for x in lines)


def test_cli_json_logs(tmp_path, monkeypatch, capsys):
    from agentl.cli import main

    (tmp_path / "p.yaml").write_text("name: c\nsteps:\n  - {id: a, use: concat}\n")
    assert (
        main(["run", str(tmp_path / "p.yaml"), "--db", str(tmp_path / "d.db"), "--log-format", "json"]) == 0
    )
    err = capsys.readouterr().err.splitlines()
    assert json.loads(err[-1])["type"] == "run.succeeded"


def test_opentelemetry_spans_and_metrics(tmp_path):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from agentl.observability import OpenTelemetry

    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    otel = OpenTelemetry(tracer_provider=tp, meter_provider=MeterProvider(metric_readers=[reader]))
    run(tmp_path, pipeline(fail=True), otel)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    root = spans["agentl run obs"]
    assert root.status.status_code.name == "ERROR"
    assert spans["step src"].parent.span_id == root.context.span_id
    assert spans["step out"].attributes["agentl.records"] == 2
    assert spans["step boom"].status.status_code.name == "ERROR"

    names = {
        m.name
        for rm in reader.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert {"agentl.runs", "agentl.records", "agentl.step.duration"} <= names


def test_sentry_reports_failed_runs(tmp_path, monkeypatch):
    captured = []

    class Scope:
        def __enter__(self):
            self.tags, self.ctx = {}, {}
            return self

        def __exit__(self, *a):
            pass

        def set_tag(self, k, v):
            self.tags[k] = v

        def set_context(self, k, v):
            self.ctx[k] = v

    scope = Scope()
    fake = types.SimpleNamespace(
        init=lambda **kw: captured.append(("init", kw)),
        new_scope=lambda: scope,
        capture_message=lambda msg, level: captured.append((msg, level, dict(scope.tags))),
    )
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    from agentl.observability import Sentry

    listener = Sentry(dsn="https://k@sentry.example/1")
    run(tmp_path, pipeline(), listener)  # success: nothing reported
    run(tmp_path, pipeline(fail=True), listener)
    assert captured[0][0] == "init"
    msg, level, tags = captured[1]
    assert "kaboom" in msg and level == "error"
    assert tags["agentl.step"] == "boom" and tags["agentl.pipeline"] == "obs"
    assert len(captured) == 2
