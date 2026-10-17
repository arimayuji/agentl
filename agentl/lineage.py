"""OpenLineage events for data catalogs (Marquez, DataHub, OpenMetadata and others).

A listener that turns agentl events into OpenLineage RunEvents: START / COMPLETE / FAIL for the
run (job = pipeline name) and for each step (job = `<pipeline>.<step>`, with a parent facet that
points at the run). Input and output datasets come from connector parameters (file paths,
tables, topics, URLs). Configure it with `lineage:` at the top level of the YAML, or with the
`OPENLINEAGE_URL` / `OPENLINEAGE_FILE` environment variables. Stdlib only."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from .spec import LineageSpec, PipelineSpec, StepSpec

SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
PARENT_FACET_URL = "https://openlineage.io/spec/facets/1-0-1/ParentRunFacet.json#/$defs/ParentRunFacet"
ERROR_FACET_URL = (
    "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet"
)
PRODUCER = "https://github.com/arimayuji/agentl"
_RUN_NS = uuid.UUID("6f1d3c0e-2b8a-4c9e-9a51-1f0c7a3e5b42")  # fixed: same agentl run -> same uuid

# Connector names whose last part means "this step writes" (its datasets are outputs).
_WRITES = ("write", "post", "send", "produce", "publish", "upload", "mark")
# Parameters that name a dataset, most specific first.
_NAME_KEYS = ("table", "topic", "queue", "stream", "path", "file", "url", "sheet", "folder")

log = logging.getLogger("agentl.lineage")


def from_env(spec: LineageSpec | None) -> LineageSpec | None:
    """The YAML block, filled in from OPENLINEAGE_URL, OPENLINEAGE_FILE, OPENLINEAGE_NAMESPACE
    and OPENLINEAGE_API_KEY. None when neither the YAML nor the environment asks for lineage."""
    env = os.environ
    if spec is None and not (env.get("OPENLINEAGE_URL") or env.get("OPENLINEAGE_FILE")):
        return None
    base = spec or LineageSpec(namespace=env.get("OPENLINEAGE_NAMESPACE") or "agentl")
    return base.model_copy(
        update={
            "url": base.url or env.get("OPENLINEAGE_URL"),
            "file": base.file or env.get("OPENLINEAGE_FILE"),
            "api_key": base.api_key or env.get("OPENLINEAGE_API_KEY"),
        }
    )


def run_uuid(run_id: str, step: str | None = None) -> str:
    """OpenLineage wants UUID run ids: derived from the agentl run id (and step), so it is stable."""
    return str(uuid.uuid5(_RUN_NS, run_id if step is None else f"{run_id}/{step}"))


def _dataset(value: str, default_ns: str) -> dict[str, Any]:
    parts = urllib.parse.urlsplit(value)
    if parts.scheme and parts.netloc:
        name = parts.path or "/"
        return {"namespace": f"{parts.scheme}://{parts.netloc}", "name": name, "facets": {}}
    if any(c in value for c in "/\\") or Path(value).suffix:
        return {"namespace": "file", "name": Path(value).as_posix(), "facets": {}}
    return {"namespace": default_ns, "name": value, "facets": {}}


def step_datasets(step: StepSpec, namespace: str) -> tuple[list[dict], list[dict]]:
    """(inputs, outputs) datasets named by a connector step's parameters. Other steps: none."""
    if not step.use:
        return [], []
    params = step.with_
    value = next((params[k] for k in _NAME_KEYS if isinstance(params.get(k), str)), None)
    if value is None:
        return [], []
    # A database table lives in the database's namespace (its dsn host), not the default one.
    ns = namespace
    dsn = next((params[k] for k in ("dsn", "url", "database") if isinstance(params.get(k), str)), None)
    if dsn and value is not dsn:
        parts = urllib.parse.urlsplit(dsn)
        ns = f"{parts.scheme}://{parts.hostname or ''}" if parts.scheme else ns
    ds = _dataset(value, ns)
    writes = step.use.rsplit(".", 1)[-1] in _WRITES
    return ([], [ds]) if writes else ([ds], [])


class OpenLineage:
    """Listener: `Runner(spec, listeners=[OpenLineage(spec)])`. The runner adds it on its own when
    `lineage:` or `OPENLINEAGE_URL` / `OPENLINEAGE_FILE` is set. Sending never fails a run."""

    def __init__(self, spec: PipelineSpec, config: LineageSpec | None = None, sink: Any = None):
        self.spec = spec
        self.config = config or from_env(spec.lineage) or LineageSpec()
        self.sink = sink  # callable(event dict); tests pass a list.append
        self._lock = threading.Lock()
        self._datasets = {s.id: step_datasets(s, self.config.namespace) for s in spec.steps}

    def _event(self, kind: str, run_id: str, ts: float, step: str | None, error: str | None) -> dict:
        ns = self.config.namespace
        run: dict[str, Any] = {"runId": run_uuid(run_id, step), "facets": {}}
        if step is None:
            job = self.spec.name
            inputs = [d for ins, _ in self._datasets.values() for d in ins]
            outputs = [d for _, outs in self._datasets.values() for d in outs]
        else:
            job = f"{self.spec.name}.{step}"
            inputs, outputs = self._datasets.get(step, ([], []))
            run["facets"]["parent"] = {
                "_producer": PRODUCER,
                "_schemaURL": PARENT_FACET_URL,
                "run": {"runId": run_uuid(run_id)},
                "job": {"namespace": ns, "name": self.spec.name},
            }
        if error:
            run["facets"]["errorMessage"] = {
                "_producer": PRODUCER,
                "_schemaURL": ERROR_FACET_URL,
                "message": error,
                "programmingLanguage": "python",
            }
        return {
            "eventType": kind,
            "eventTime": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z",
            "run": run,
            "job": {"namespace": ns, "name": job, "facets": {}},
            "inputs": inputs,
            "outputs": outputs,
            "producer": PRODUCER,
            "schemaURL": SCHEMA_URL,
        }

    def __call__(self, event: dict) -> None:
        kinds = {"started": "START", "succeeded": "COMPLETE", "failed": "FAIL"}
        scope, _, what = event["type"].partition(".")
        if scope not in ("run", "step") or what not in kinds:
            return
        step = event.get("step") if scope == "step" else None
        if scope == "step" and step is None:
            return
        data = event.get("data") or {}
        out = self._event(kinds[what], event["run_id"], event["ts"], step, data.get("error"))
        try:
            self._send(out)
        except Exception as e:  # a catalog that is down must not stop the pipeline
            log.warning("openlineage: could not send %s event: %s", out["eventType"], e)

    def _send(self, event: dict) -> None:
        if self.sink is not None:
            self.sink(event)
        if self.config.file:
            line = json.dumps(event, ensure_ascii=False)
            with self._lock, open(self.config.file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.config.url:
            import urllib.request

            from .netguard import require_http, ssl_context

            url = (
                require_http(self.config.url, "lineage url").rstrip("/")
                + "/"
                + self.config.endpoint.lstrip("/")
            )
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            req = urllib.request.Request(url, data=json.dumps(event).encode(), headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=10, context=ssl_context()).close()
