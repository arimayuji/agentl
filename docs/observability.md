# Observability

<video class="feature-clip" autoplay muted loop playsinline preload="none" poster="../assets/clips/observe.jpg" aria-label="Run diagram, JSON logs and traces">
  <source src="../assets/clips/observe.webm" type="video/webm">
  <source src="../assets/clips/observe.mp4" type="video/mp4">
</video>

Every run emits events (`run.started`, `step.succeeded`, `item.rejected`, `budget.exceeded`, ...).
agentl ships listeners that turn them into logs, traces, metrics and error reports.

| Want | CLI | Python | Install |
|---|---|---|---|
| JSON logs | `--log-format json` or `AGENTL_LOG_FORMAT=json` | `JsonLogs()` | built in |
| Traces and metrics | `--otel` | `OpenTelemetry()` | `agentl-etl[otel]` + your exporter |
| Error tracking | `--sentry` (reads `SENTRY_DSN`) | `Sentry(dsn)` | `agentl-etl[sentry]` |

```python
import agentl
from agentl.observability import JsonLogs, OpenTelemetry, Sentry

agentl.run("pipeline.yaml", listeners=[JsonLogs(), OpenTelemetry(), Sentry()])
```

A listener is any callable that takes an event dict, so writing your own is one function.

## JSON logs

One object per line on stderr (or a `logging.Logger` with `JsonLogs(logger=...)`):

```json
{"level": "info", "run_id": "20261013-214022-d6135f", "pipeline": "sales", "ts": 1760391622.1,
 "type": "step.succeeded", "step": "clean", "data": {"records": 120, "seconds": 0.4}}
```

`level` is `error` for `*.failed` and `budget.exceeded`, `warning` for `item.rejected`, `info`
otherwise. Ship it with whatever already collects your logs (Loki, CloudWatch, Datadog, ELK).

## OpenTelemetry

One trace per run, with a child span per step. Spans carry records, duration, tokens and cost, and
are marked as errors when a step or the run fails; rejected records and `budget.exceeded` become span
events.

Metrics:

| Name | Type | Labels |
|---|---|---|
| `agentl.runs` | counter | pipeline, status |
| `agentl.records` | counter | pipeline, step |
| `agentl.rejected` | counter | pipeline, step |
| `agentl.tokens` | counter | pipeline, step, direction |
| `agentl.cost_usd` | counter | pipeline, step |
| `agentl.step.duration` | histogram (s) | pipeline, step |

agentl uses the global OpenTelemetry providers, so configure exporters the standard way, for example:

```bash
pip install 'agentl-etl[otel]' opentelemetry-distro opentelemetry-exporter-otlp
export OTEL_SERVICE_NAME=etl OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317
opentelemetry-instrument agentl run pipeline.yaml --otel
```

## Sentry

Failed runs become Sentry issues tagged with `agentl.pipeline`, `agentl.run_id` and the failing
`agentl.step`, grouped per pipeline and step, with the run summary as context. `Sentry()` reuses an
SDK you already initialized; `Sentry(dsn=...)` initializes it.
