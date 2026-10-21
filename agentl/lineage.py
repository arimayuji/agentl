"""OpenLineage events for data catalogs (Marquez, DataHub, OpenMetadata and others).

A listener that turns agentl events into OpenLineage RunEvents: START / COMPLETE / FAIL for the
run (job = pipeline name) and for each step (job = `<pipeline>.<step>`, with a parent facet that
points at the run). Input and output datasets come from connector parameters (file paths,
tables, topics, URLs). Output datasets carry the columnLineage facet where the pipeline knows
which input fields each output field comes from (select, compute, cast, aggregate, agent steps
and simple sql queries). Configure it with `lineage:` at the top level of the YAML, or with the
`OPENLINEAGE_URL` / `OPENLINEAGE_FILE` environment variables. Stdlib only."""

from __future__ import annotations

import ast
import copy
import json
import logging
import os
import re
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
COLUMN_FACET_URL = (
    "https://openlineage.io/spec/facets/1-2-0/ColumnLineageDatasetFacet.json#/$defs/ColumnLineageDatasetFacet"
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


# Column-level lineage. Each step gets a _Cols: the output fields it knows the origin of, plus the
# source datasets whose other fields pass through under the same name ("open"). None means the
# step's output columns cannot be traced (a `fn` step, an unknown connector), so nothing below
# it gets a facet. Transformation subtypes follow the OpenLineage spec, weakest first.
_RANK = {"IDENTITY": 0, "TRANSFORMATION": 1, "AGGREGATION": 2}
# Steps that keep every field as it is (they drop, reorder or fill records, not columns).
_PASS = ("filter", "dedupe", "sort", "limit", "fill", "cache", "concat")
_SQL_ITEM = re.compile(r"^(.*?)(?:\s+as)?\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?$", re.I | re.S)
_SQL_IDENT = re.compile(r'"([^"]+)"|\b([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*\()')
_SQL_WORDS = {
    "as", "and", "or", "not", "null", "true", "false", "case", "when", "then", "else", "end", "is",
    "in", "like", "between", "distinct", "cast", "integer", "int", "bigint", "double", "varchar",
    "text", "date", "timestamp", "decimal", "boolean", "interval",
}  # fmt: skip


class _Cols:
    def __init__(self, known: dict[str, list[dict] | None] | None = None, open_: list[dict] | None = None):
        self.known = dict(known or {})  # output field -> input fields (None: the field was dropped)
        self.open = list(open_ or [])

    def lookup(self, name: str, subtype: str = "IDENTITY") -> list[dict]:
        """The input fields behind `name`, with their transformation raised to at least `subtype`."""
        if name in self.known:
            found = self.known[name] or []
        else:
            found = [{"namespace": d["namespace"], "name": d["name"], "field": name} for d in self.open]
        out = []
        for f in found:
            old = f.get("transformations", [{}])[0].get("subtype", "IDENTITY")
            sub = old if _RANK[old] >= _RANK[subtype] else subtype
            out.append({**f, "transformations": [{"type": "DIRECT", "subtype": sub}]})
        return out

    def closed(self) -> _Cols:
        return _Cols({k: v for k, v in self.known.items() if v is not None})


def _merge(fields: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for f in fields:
        key = (f["namespace"], f["name"], f["field"])
        if (
            key not in seen
            or _RANK[f["transformations"][0]["subtype"]] > _RANK[seen[key]["transformations"][0]["subtype"]]
        ):
            seen[key] = f
    return list(seen.values())


def _expr_fields(text: str) -> list[str]:
    """Field names an agentl expression reads (names that are not functions or constants)."""
    from . import expr

    tree = expr.compile_expr(str(text))
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "field":
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.append(arg.value)
        elif isinstance(node, ast.Name) and node.id not in ("null", "None", "true", "True", "false", "False"):
            names.append(node.id)
    return list(dict.fromkeys(n for n in names if n not in calls))


def _split_top(text: str) -> list[str]:
    """Split on commas outside parentheses and quotes."""
    parts, depth, quote, cur = [], 0, "", ""
    for c in text:
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += c
    return [*parts, cur]


def _sql_cols(query: str, up: _Cols) -> _Cols | None:
    """`select a, b as c, sum(x) as y from t [where/group by ...]` over one input. Anything
    more (joins, subqueries, `*`, unions, ctes) gives None: no guess."""
    q = re.sub(r"--[^\n]*", " ", query).strip().rstrip(";")
    m = re.match(
        r"(?is)^select\s+(.*?)\s+from\s+\"?[A-Za-z_][A-Za-z0-9_]*\"?(?:\s+(?:as\s+)?\w+)?(\s.*)?$", q
    )
    if not m or re.search(r"(?i)\b(join|union|intersect|except|select)\b|\(", m.group(2) or ""):
        return None
    if re.search(r"(?i)\bselect\b", m.group(1)) or "*" in re.sub(r"\(\s*\*\s*\)", "()", m.group(1)):
        return None
    out = _Cols()
    for item in _split_top(m.group(1)):
        item = item.strip()
        plain = re.fullmatch(r'"?([A-Za-z_][A-Za-z0-9_]*)"?', item)
        if plain:
            out.known[plain.group(1)] = up.lookup(plain.group(1))
            continue
        a = _SQL_ITEM.match(item)
        if not a or not a.group(1).strip():
            return None
        body, alias = a.group(1), a.group(2)
        body = re.sub(r"'(?:[^']|'')*'", "''", body)
        refs = [x.group(1) or x.group(2) for x in _SQL_IDENT.finditer(body)]
        refs = [r for r in refs if r.lower() not in _SQL_WORDS and "." not in r]
        agg = re.search(
            r"(?i)\b(sum|count|avg|min|max|any_value|first|last|list|string_agg|array_agg)\s*\(", body
        )
        sub = "AGGREGATION" if agg else "TRANSFORMATION"
        out.known[alias] = _merge([f for r in dict.fromkeys(refs) for f in up.lookup(r, sub)])
    return out


def _step_cols(step: StepSpec, ups: list[_Cols | None], reads: list[dict]) -> _Cols | None:
    if step.use and reads:
        return _Cols(open_=reads)
    if any(u is None for u in ups) or not ups:
        return None
    up = ups[0] if len(ups) == 1 else None
    if step.kind == "check" or step.kind == "wait":
        return ups[0]
    name = step.use or ""
    if name == "concat":
        known: dict[str, list[dict] | None] = {}
        for u in ups:
            assert u is not None
            for k in u.known:
                known[k] = _merge([*(known.get(k) or []), *u.lookup(k)])
        return _Cols(known, [d for u in ups if u is not None for d in u.open])
    if up is None:
        return None
    w = step.with_
    if name in _PASS:
        return up
    if name == "select":
        fields = [w["fields"]] if isinstance(w.get("fields"), str) else list(w.get("fields") or [])
        rename = dict(w.get("rename") or {})
        if not fields:
            out = _Cols(up.known, up.open)
            for old, new in rename.items():
                out.known[new] = up.lookup(old)
                out.known[old] = None
            return out
        source = {new: old for old, new in rename.items()}
        res = _Cols()
        for n in fields:
            src = source.get(n, n)
            res.known.setdefault(rename.get(src, src), up.lookup(src))
        return res
    if name == "cast":
        out = _Cols(up.known, up.open)
        for f in w.get("fields") or {}:
            out.known[f] = up.lookup(f, "TRANSFORMATION")
        return out
    if name == "compute":
        out = _Cols(up.known, up.open)
        for f, text in (w.get("fields") or {}).items():
            out.known[f] = _merge([x for n in _expr_fields(text) for x in out.lookup(n, "TRANSFORMATION")])
        return out
    if name == "aggregate":
        from .connectors.transforms import _AGG

        group = [w["group_by"]] if isinstance(w.get("group_by"), str) else list(w.get("group_by") or [])
        res = _Cols({k: up.lookup(k) for k in group})
        window = w.get("window")
        if isinstance(window, dict) and isinstance(window.get("field"), str):
            res.known["window_start"] = up.lookup(window["field"], "TRANSFORMATION")
            if window.get("end"):
                res.known["window_end"] = up.lookup(window["field"], "TRANSFORMATION")
        for metric, text in (w.get("metrics") or {}).items():
            m = _AGG.match(str(text))
            arg = m.group(2).strip() if m else ""
            res.known[metric] = up.lookup(arg, "AGGREGATION") if arg else []
        return res
    if name == "sql" and isinstance(w.get("query"), str):
        return _sql_cols(w["query"], up)
    if step.kind == "agent" and step.output:
        # The model reads the whole record: each answer field depends on every known input field.
        every = _merge(
            [x for k, v in up.known.items() if v is not None for x in up.lookup(k, "TRANSFORMATION")]
        )
        rows = step.output_mode == "rows"
        out = _Cols() if rows else _Cols(up.known, up.open)
        for f in step.output:
            out.known[f] = every
        return out
    return None


def column_lineage(spec: PipelineSpec, datasets: dict[str, tuple[list[dict], list[dict]]]) -> dict[str, dict]:
    """step id -> the columnLineage facet of the dataset that writer step writes (only steps whose
    input columns can be traced, and only fields with a known origin)."""
    cols: dict[str, _Cols | None] = {}
    by_id = {s.id: s for s in spec.steps}

    def get(sid: str, seen: frozenset = frozenset()) -> _Cols | None:
        if sid in cols:
            return cols[sid]
        step = by_id.get(sid)
        if step is None or sid in seen:
            return None
        ups = [get(u, seen | {sid}) for u in step.inputs]
        try:
            cols[sid] = _step_cols(step, ups, datasets.get(sid, ([], []))[0])
        except Exception:  # an expression the lineage cannot read is not worth failing a run
            cols[sid] = None
        return cols[sid]

    facets: dict[str, dict] = {}
    for step in spec.steps:
        if not datasets.get(step.id, ([], []))[1] or not step.inputs:
            continue
        ups = [get(u) for u in step.inputs]
        if any(u is None for u in ups):
            continue
        fields: dict[str, dict] = {}
        for u in ups:
            assert u is not None
            for k, v in u.closed().known.items():
                found = u.lookup(k)
                if found and v:
                    fields[k] = {"inputFields": _merge([*fields.get(k, {}).get("inputFields", []), *found])}
        if fields:
            facets[step.id] = {"_producer": PRODUCER, "_schemaURL": COLUMN_FACET_URL, "fields": fields}
    return facets


class OpenLineage:
    """Listener: `Runner(spec, listeners=[OpenLineage(spec)])`. The runner adds it on its own when
    `lineage:` or `OPENLINEAGE_URL` / `OPENLINEAGE_FILE` is set. Sending never fails a run."""

    def __init__(self, spec: PipelineSpec, config: LineageSpec | None = None, sink: Any = None):
        self.spec = spec
        self.config = config or from_env(spec.lineage) or LineageSpec()
        self.sink = sink  # callable(event dict); tests pass a list.append
        self._lock = threading.Lock()
        self._datasets = {s.id: step_datasets(s, self.config.namespace) for s in spec.steps}
        for sid, facet in column_lineage(spec, self._datasets).items():
            for ds in self._datasets[sid][1]:
                ds["facets"]["columnLineage"] = copy.deepcopy(facet)

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
