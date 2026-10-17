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

## OpenLineage

agentl sends [OpenLineage](https://openlineage.io) events to the data catalog your team uses
(Marquez, DataHub, OpenMetadata, or any other that reads OpenLineage). Every run sends `START` and
then `COMPLETE` or `FAIL` for the run (the job is the pipeline name) and for each step (the job is
`<pipeline>.<step>`, with a `parent` facet that points at the run). A `FAIL` carries the error in an
`errorMessage` facet. Run ids are UUIDs derived from the agentl run id, so the same run always maps
to the same id.

Input and output datasets come from the parameters of connector steps: `table`, `topic`, `queue`,
`stream`, `path`, `file`, `url`, `sheet` or `folder`. Steps whose connector writes (`*.write`,
`*.post`, `*.send`, `*.produce`, `*.publish`, `*.upload`) have outputs, the others have inputs.
Files use the `file` namespace, URLs their `scheme://host`, tables the host of the step's `dsn`.

```yaml
name: orders
lineage:
  url: http://localhost:5000   # events go to <url>/api/v1/lineage
  namespace: shop              # job namespace, default "agentl"
  api_key: ${OPENLINEAGE_API_KEY:-}   # optional, sent as a Bearer token
  # file: lineage.jsonl        # or: append one JSON event per line
steps:
  - id: src
    use: csv.read
    with: {path: data/orders.csv}
  - id: out
    use: json.write
    input: src
    with: {path: out/orders.json}
```

Without `lineage:` in the YAML, set `OPENLINEAGE_URL` (or `OPENLINEAGE_FILE`), and optionally
`OPENLINEAGE_NAMESPACE` and `OPENLINEAGE_API_KEY`. A catalog that is down only logs a warning on
the `agentl.lineage` logger; the run carries on.

### Recipe: Marquez on your machine

```bash
git clone https://github.com/MarquezProject/marquez && cd marquez
./docker/up.sh                       # API on :5000, web UI on :3000
OPENLINEAGE_URL=http://localhost:5000 agentl run orders.yaml
```

Open http://localhost:3000, pick the namespace (`agentl` or yours) and the `orders` job shows the
graph `data/orders.csv -> orders -> out/orders.json`, with one run per `agentl run`.

Not yet: column-level lineage (for `select`, `compute`, `cast`, `aggregate`) and per-record
provenance (#209).
