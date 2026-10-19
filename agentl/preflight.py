"""`agentl validate --connect` and `agentl plan`: what would go wrong, and what a run would do and
cost, before running it.

`--connect` resolves the configuration and touches every external system once (an HTTP request,
`select 1`, a file lookup) without reading or writing data. `plan` reads the sources, runs the
steps that cannot change anything (readers and transforms), counts cache hits for agent steps and
estimates their calls, tokens and cost: from the tokens per record that past runs of the step and
model really used when the run store has them, from a 4-characters-per-token guess otherwise.
Nothing is written, no LLM is called, no message is taken from a queue and no `fn:` step runs."""

from __future__ import annotations

import glob
import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import graph, llm
from .spec import PipelineSpec, StepSpec

# Connectors that only read: safe to run in `plan`. Queue consumers and `email.read` are left out
# on purpose: reading takes or marks messages.
SAFE = {
    "csv.read",
    "json.read",
    "excel.read",
    "parquet.read",
    "txt.read",
    "xml.read",
    "nfe.read",
    "html.read",
    "html.select",
    "pdf.read",
    "doc.read",
    "docx.read",
    "file.read",
    "sql.read",
    "sqlite.read",
    "postgres.read",
    "http.get",
    "graphql.query",
    "gsheets.read",
    "gdrive.files",
    "sharepoint.files",
    "select",
    "filter",
    "dedupe",
    "join",
    "concat",
}
_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "azure-openai": "AZURE_OPENAI_API_KEY",
}


# ---------------------------------------------------------------- --connect
@dataclass
class Check:
    what: str
    status: str  # ok | fail | skip
    detail: str = ""


def _http(url: str, timeout: float) -> Check:
    import urllib.error
    import urllib.request

    from . import netguard

    if "{" in url:
        return Check(url, "skip", "the URL is filled per record")
    req = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-0"})
    try:
        with netguard.urlopen(req, timeout=timeout) as resp:
            return Check(url, "ok", f"HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return Check(url, "fail", f"HTTP {e.code}: credentials refused")
        return Check(url, "ok", f"reachable (HTTP {e.code})")
    except Exception as e:
        return Check(url, "fail", netguard.describe(e))


def _redact(dsn: str) -> str:
    import re

    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", dsn)


def _database(use: str, dsn: str, timeout: float) -> Check:
    what = f"{use} {_redact(dsn)}"
    try:
        if use.startswith("postgres"):
            try:
                import psycopg
            except ImportError:
                return Check(what, "skip", "psycopg not installed (agentl-etl[postgres])")
            with psycopg.connect(dsn, connect_timeout=int(timeout)) as conn:
                conn.execute("select 1")
        elif use.startswith("sql."):
            try:
                import sqlalchemy
            except ImportError:
                return Check(what, "skip", "sqlalchemy not installed (agentl-etl[sql])")
            engine = sqlalchemy.create_engine(dsn)
            with engine.connect() as conn:
                conn.execute(sqlalchemy.text("select 1"))
            engine.dispose()
        return Check(what, "ok", "select 1")
    except Exception as e:
        return Check(what, "fail", _redact(f"{type(e).__name__}: {e}")[:300])


def _file(use: str, path: str, base: Path) -> Check:
    if "://" in path:
        return Check(f"{use} {path}", "skip", "remote files are not checked")
    if "{" in path:
        return Check(f"{use} {path}", "skip", "the path is filled per record")
    full = path if Path(path).is_absolute() else str(base / path)
    found = glob.glob(full)
    if found:
        return Check(f"{use} {path}", "ok", f"{len(found)} file(s)")
    return Check(f"{use} {path}", "fail", "no such file")


def _llm(name: str, spec: Any, call: bool, timeout: float) -> Check:
    what = f"llm {name} ({spec.provider} {spec.model})"
    env = _KEY_ENV.get(spec.provider) or spec.options.get("api_key_env")
    if env and not os.environ.get(env):
        return Check(what, "fail", f"{env} is not set")
    try:
        prov = llm.provider(spec)
    except Exception as e:
        return Check(what, "fail", str(e))
    if not call:
        return Check(what, "ok", "configured" + (f" ({env} set)" if env else ""))
    try:
        prov.text("Answer with the single word OK.", "")
        return Check(what, "ok", "answered a one-word test call")
    except Exception as e:
        from . import netguard

        return Check(what, "fail", netguard.describe(e)[:300])


def connect(path: str | Path, call_llm: bool = False, timeout: float = 10) -> list[Check]:
    """One check per external system. Secrets and env values are resolved but never printed."""
    from .spec import load

    try:
        spec = load(path)
    except Exception as e:
        return [Check("configuration", "fail", str(e))]
    out = [Check("configuration", "ok", "environment variables and secret references resolve")]
    base = Path(spec.base_dir or ".")
    for s in spec.steps:
        use, w = s.use or "", s.with_
        if "url" in w and isinstance(w["url"], str):
            out.append(_http(w["url"], timeout))
        if "dsn" in w and isinstance(w["dsn"], str):
            out.append(_database(use, w["dsn"], timeout))
        if "path" in w and isinstance(w["path"], str) and use.endswith(".read"):
            out.append(_file(use, w["path"], base))
    used = {s.llm for s in spec.steps if s.kind == "agent" or s.review}
    used |= {n for s in spec.steps if s.kind == "agent" for n in s.escalate}
    used |= {f for n in list(used) if n in spec.llm for f in spec.llm[n].fallback}
    for name in sorted(used):
        if name in spec.llm:
            out.append(_llm(name, spec.llm[name], call_llm, timeout))
    return out


# ---------------------------------------------------------------- plan
@dataclass
class StepPlan:
    step: str
    kind: str
    status: str  # read | ran | estimated | skipped | writes
    records: int | None = None
    detail: str = ""
    cache_hits: int | None = None
    llm_calls: int | None = None
    tokens: tuple[int, int] | None = None  # low, high (input + output)
    cost_usd: tuple[float, float] | None = None  # low, high; None when no price is known
    basis: str | None = None  # agent steps: "history" (past runs) or "guess" (characters / 4)


@dataclass
class Plan:
    pipeline: str
    steps: list[StepPlan] = field(default_factory=list)

    @property
    def llm_calls(self) -> int:
        return sum(s.llm_calls or 0 for s in self.steps)

    @property
    def cost(self) -> tuple[float, float] | None:
        costs = [s.cost_usd for s in self.steps if s.llm_calls]
        if not costs:
            return (0.0, 0.0)
        if any(c is None for c in costs):
            return None
        return (sum(c[0] for c in costs if c), sum(c[1] for c in costs if c))

    def to_dict(self) -> dict:
        return {
            "pipeline": self.pipeline,
            "llm_calls": self.llm_calls,
            "cost_usd": self.cost,
            "steps": [asdict(s) for s in self.steps],
        }

    def text(self) -> str:
        lines = [f"plan for {self.pipeline} (nothing is written, no LLM is called)", ""]
        for s in self.steps:
            n = "?" if s.records is None else f"{s.records:,}"
            line = f"  {s.step:<18} {s.kind:<6} {s.status:<9} {n:>8} rec"
            if s.llm_calls is not None:
                line += f"   cache {s.cache_hits or 0:,}, calls {s.llm_calls:,}"
                if s.tokens:
                    line += f", ~{s.tokens[0]:,}-{s.tokens[1]:,} tokens"
                line += ", " + _money(s.cost_usd)
                if s.basis:
                    line += f" ({s.basis})"
            if s.detail:
                line += f"   {s.detail}"
            lines.append(line)
        lines += ["", f"total: {self.llm_calls:,} LLM call(s), {_money(self.cost)}"]
        return "\n".join(lines)


def _money(c: tuple[float, float] | None) -> str:
    if c is None:
        return "cost unknown (no price)"
    if c == (0.0, 0.0):
        return "$0"
    return f"${c[0]:.4f}-${c[1]:.4f}"


CHARS_PER_TOKEN = 4


def estimate_tokens(*parts: Any) -> int:
    """Rough input tokens of a call (4 characters per token); dicts and lists count as JSON."""
    chars = sum(
        len(p) if isinstance(p, str) else len(json.dumps(p, default=str, ensure_ascii=False)) for p in parts
    )
    return math.ceil(chars / CHARS_PER_TOKEN)


def _tokens(step: StepSpec, records: list[dict]) -> tuple[int, int]:
    """Rough token count (4 characters per token) of the calls an agent step would make, low and
    high: the output length is the part nobody can know before the model answers."""
    from .llm import SYSTEM, schema_model

    schema = json.dumps(schema_model(f"{step.id}_output", step.output or {}).model_json_schema())
    prefix = (len(SYSTEM) + len(step.agent or "") + len(schema)) / CHARS_PER_TOKEN
    body = sum(len(json.dumps(r, default=str, ensure_ascii=False)) for r in records) / CHARS_PER_TOKEN
    calls = math.ceil(len(records) / step.records_per_call) if records else 0
    tin = prefix * calls + body
    per_out = 6 + 8 * len(step.output or {})
    return int(tin * 0.9 + len(records) * per_out * 0.7), int(tin * 1.2 + len(records) * per_out * 2.5)


def _cost(
    spec: PipelineSpec, step: StepSpec, records: list[dict], tokens: tuple[int, int]
) -> tuple[float, float] | None:
    names = step.llms
    prices = [spec.llm[n] for n in names]
    if any(p.price is None for p in prices):
        free = all(p.provider in ("echo", "ollama") for p in prices)
        return (0.0, 0.0) if free else None
    share_in = 0.75  # input share of the tokens, for pricing a total

    def usd(p: Any, total: int) -> float:
        cost = p.cost(int(total * share_in), int(total * (1 - share_in)))
        return cost * (p.price.batch if step.mode == "batch" else 1)

    low = usd(prices[0], tokens[0])
    high = usd(prices[0], tokens[1]) + (usd(prices[-1], tokens[1]) if len(prices) > 1 else 0)
    return (low, high)


HISTORY_DAYS = 90  # how far back `plan` looks for the tokens a step really used


def token_history(store: Any, pipeline: str, since: float | None = None) -> dict[tuple[str, str], dict]:
    """Per (agent step, model): the tokens a record really took in past runs of the pipeline
    (the last `HISTORY_DAYS` by default). A record is one the LLM answered (cache hits cost
    nothing); the tokens of failed calls and retries count, so the average is on the safe side.
    `per_record` is the low and high of the per-run averages, input plus output; `models` splits
    the average by the model paid for, so fallbacks and escalations are priced at their own model."""
    if not hasattr(store, "events_of"):
        return {}
    since = time.time() - HISTORY_DAYS * 86400 if since is None else since
    ids = {r["id"] for r in store.history(since, pipeline)}
    rows: dict[tuple[str, str], dict] = {}
    for e in store.events_of(since, ("step.succeeded",)):
        d = e["data"] or {}
        if e["run_id"] not in ids or "llm_calls" not in d:
            continue
        answered = sum((d.get("answered_by") or {}).values())
        tin, tout = int(d.get("input_tokens") or 0), int(d.get("output_tokens") or 0)
        if not answered or not tin + tout:
            continue
        model = str(d.get("model") or "")
        row = rows.setdefault(
            (e["step"], model),
            {"runs": 0, "records": 0, "input_tokens": 0, "output_tokens": 0, "models": {}, "_per": []},
        )
        row["runs"] += 1
        row["records"] += answered
        row["input_tokens"] += tin
        row["output_tokens"] += tout
        row["_per"].append((tin + tout) / answered)
        for name, m in (d.get("models") or {model: {"input_tokens": tin, "output_tokens": tout}}).items():
            t = row["models"].setdefault(name, {"input_tokens": 0, "output_tokens": 0})
            t["input_tokens"] += int(m.get("input_tokens") or 0)
            t["output_tokens"] += int(m.get("output_tokens") or 0)
    for row in rows.values():
        per = row.pop("_per")
        n = row["records"]
        row["input_per_record"] = row["input_tokens"] / n
        row["output_per_record"] = row["output_tokens"] / n
        row["per_record"] = (min(per), max(per))
        for t in row["models"].values():
            t["input_per_record"] = t["input_tokens"] / n
            t["output_per_record"] = t["output_tokens"] / n
    return rows


def _from_history(
    spec: PipelineSpec, step: StepSpec, n: int, h: dict
) -> tuple[tuple[int, int], tuple[float, float] | None]:
    """Tokens and cost of `n` records at what a record took in past runs: the low and high are
    the cheapest and the dearest run's average."""
    avg = h["input_per_record"] + h["output_per_record"]
    low, high = (x / avg for x in h["per_record"])
    tokens = (round(n * avg * low), round(n * avg * high))
    llms = [spec.llm[name] for name in step.llms]
    cost = 0.0
    for model, t in h["models"].items():
        p = next((x for x in llms if x.model == model), llms[0])
        if p.price is None:
            if p.provider in ("echo", "ollama"):
                continue
            return tokens, None
        usd = p.cost(round(n * t["input_per_record"]), round(n * t["output_per_record"]))
        cost += usd * (p.price.batch if step.mode == "batch" else 1)
    return tokens, (cost * low, cost * high)


def plan(spec: PipelineSpec, store: Any, records: dict | None = None) -> Plan:
    """`records`, when given, receives the records of every step that ran (None when unknown)."""
    from .runner import Runner, RunResult, agent_cache_key

    spec = spec.model_copy(update={"arrow": False, "chunk_size": None, "notify": None})
    out = Plan(spec.name)
    history = token_history(store, spec.name)
    with tempfile.TemporaryDirectory() as tmp:
        from .store import Store

        scratch = Store(Path(tmp) / "plan.db")
        runner = Runner(spec, store=scratch, use_cache=False)
        known: dict[str, list[dict] | None] = {}
        result = RunResult(run_id="plan", status="running")
        for step_id in graph.order(spec):
            s = next(x for x in spec.steps if x.id == step_id)
            inputs = [known.get(r) for r in s.inputs]
            if any(i is None for i in inputs):
                out.steps.append(
                    StepPlan(s.id, s.kind, "skipped", None, "its input is not known before running")
                )
                known[s.id] = None
                continue
            if s.kind == "agent":
                recs = (inputs[0] if inputs else [{}]) or []
                hits = (
                    sum(store.cache_get(agent_cache_key(spec, s, r)) is not None for r in recs)
                    if s.cached
                    else 0
                )
                todo = (
                    [r for r in recs if store.cache_get(agent_cache_key(spec, s, r)) is None]
                    if s.cached
                    else recs
                )
                calls = math.ceil(len(todo) / s.records_per_call) if todo else 0
                past = history.get((s.id, spec.llm[s.llm].model))
                if past:
                    tokens, cost = _from_history(spec, s, len(todo), past)
                    basis = f"history: {past['records']:,} records in {past['runs']} runs"
                else:
                    tokens = _tokens(s, todo)
                    cost, basis = _cost(spec, s, todo, tokens), "guess"
                out.steps.append(
                    StepPlan(
                        s.id,
                        "agent",
                        "estimated",
                        len(recs),
                        "high: every record also goes to the last llm"
                        if s.escalate
                        else "router: each target is planned with every record (upper bound)"
                        if s.routes is not None
                        else "",
                        hits,
                        calls,
                        tokens,
                        cost,
                        basis,
                    )
                )
                fields = {} if s.routes is not None else dict.fromkeys(s.output or {})
                known[s.id] = [{**r, **fields} for r in recs]
                continue
            if s.kind == "check":
                known[s.id] = inputs[0] if inputs else []
                out.steps.append(
                    StepPlan(s.id, "check", "skipped", len(known[s.id] or []), "checks run with the data")
                )
                continue
            if s.kind == "use" and s.use not in SAFE:
                n = len(inputs[0] or []) if inputs else None
                where = s.with_.get("path") or s.with_.get("table") or s.with_.get("url") or ""
                verb = "writes" if s.inputs else "skipped"
                detail = (
                    f"would write to {where}"
                    if s.inputs
                    else f"{s.use} is not read in a plan (it may change state)"
                )
                out.steps.append(StepPlan(s.id, "use", verb, n, detail))
                known[s.id] = None if not s.inputs else (inputs[0] if inputs else None)
                continue
            if s.kind != "use":
                out.steps.append(
                    StepPlan(s.id, s.kind, "skipped", None, f"{s.kind} steps do not run in a plan")
                )
                known[s.id] = None
                continue
            try:
                step = s
                if s.cursor:
                    runner.store = store  # the cursor the next run would start from
                    step = runner._with_cursor(s)
                    runner.store = scratch
                outputs = {r: known[r] or [] for r in s.inputs}
                recs = runner._run_step("plan", step, outputs, result)
                recs = list(recs.to_pylist() if hasattr(recs, "to_pylist") else recs)  # type: ignore[attr-defined]
                known[s.id] = recs
                detail = f"cursor {runner._cursor_start.get(s.id)!r}" if s.cursor else ""
                out.steps.append(StepPlan(s.id, "use", "read" if not s.inputs else "ran", len(recs), detail))
            except Exception as e:
                from .runner import _describe

                known[s.id] = None
                out.steps.append(StepPlan(s.id, "use", "skipped", None, f"failed: {_describe(e)[:200]}"))
        scratch.close()
    if records is not None:
        records.update(known)
    return out


def preview(spec: PipelineSpec, store: Any, step_id: str, n: int = 5) -> dict:
    """The first `n` records going into and coming out of a step, computed like `plan`: only
    readers and transforms run, so agent, fn and writer steps show their input only."""
    if step_id not in {s.id for s in spec.steps}:
        from .lint import suggest

        best = suggest(step_id, [s.id for s in spec.steps])
        raise KeyError(f"no step '{step_id}'" + (f". Did you mean '{best}'?" if best else ""))
    known: dict[str, Any] = {}
    planned = plan(spec, store, known)
    step = next(s for s in spec.steps if s.id == step_id)
    sp = next(x for x in planned.steps if x.step == step_id)
    out: dict[str, Any] = {"step": step_id, "kind": step.kind, "status": sp.status}
    inputs = {r: known.get(r) for r in step.inputs}
    out["input"] = {
        r: (None if v is None else {"records": len(v), "first": v[:n]}) for r, v in inputs.items()
    }
    if sp.status in ("read", "ran") and known.get(step_id) is not None:
        rows = known[step_id]
        out["output"] = {"records": len(rows), "first": rows[:n]}
    else:
        out["output"] = None
        out["why"] = sp.detail or f"{step.kind} steps do not run in a preview"
    return out
