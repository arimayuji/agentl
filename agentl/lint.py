"""`agentl validate` and `agentl lint`: every problem at once, with file, line, column, a code and,
when there is one, the fix. Errors stop a pipeline from running; warnings are best practices.

Findings carry the YAML position of the thing they are about, so an editor, a CI annotation or
an AI agent can jump to it and fix it."""

from __future__ import annotations

import difflib
import importlib.util
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import registry
from .spec import _ENV, PipelineSpec, SpecError, _flatten_sections, _Loader, interpolate

# Writes that leave the machine or change shared state: a check before them is worth having.
_DELIVERING = {
    "postgres.write",
    "sql.write",
    "gsheets.write",
    "gdrive.upload",
    "sharepoint.upload",
    "http.post",
    "kafka.produce",
    "pubsub.publish",
    "rabbitmq.publish",
    "sqs.send",
    "discord.send",
    *(f"{s}.send" for s in ("slack", "mattermost", "teams", "telegram", "googlechat", "whatsapp")),
    *(f"{s}.send" for s in ("sms", "ntfy", "pushover", "matrix", "email")),
}
_SECRET_KEY = re.compile(r"(pass(word)?|secret|token|api[_-]?key|private[_-]?key|credential|webhook)", re.I)
_SECRET_VALUE = re.compile(
    r"^(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})$"
)
_DSN_PASSWORD = re.compile(r"^[a-z][a-z0-9+]*://[^:/@\s]+:[^@\s$]+@")
_DISABLE = re.compile(r"#\s*agentl:\s*disable=([A-Z0-9, ]+)")

RULES = {
    "AE001": "invalid YAML",
    "AE002": "invalid pipeline structure",
    "AE003": "unknown connector",
    "AE004": "unknown step referenced",
    "AE005": "unknown llm referenced",
    "AE006": "duplicate step id",
    "AE007": "cycle between steps",
    "AE008": "environment variable not set",
    "AE009": "sql step reads a table that is not an input",
    "AE010": "invalid or undeclared run parameter",
    "AL001": "agent steps without a run budget",
    "AL002": "delivery with no check before it",
    "AL003": "secret written in the pipeline",
    "AL004": "step whose output nobody uses",
    "AL005": "fn module not found",
    "AL006": "cursor never used",
    "AL007": "unknown top-level field (ignored when the pipeline runs)",
    "AL008": "agent step sees fields before they are protected",
    "AL009": "cron trigger without a time zone",
    "AL010": "cast without locale reads numbers that look en-US",
}


@dataclass
class Finding:
    code: str
    level: str  # error | warning
    message: str
    line: int = 0  # 1-based; 0 when unknown
    col: int = 0
    fix: str | None = None
    path: list[Any] = field(default_factory=list)

    def text(self, file: str) -> str:
        where = f"{file}:{self.line}:{self.col}" if self.line else file
        out = f"{where}  {self.level} {self.code}  {self.message}"
        return out + (f"\n    fix: {self.fix}" if self.fix else "")

    def github(self, file: str) -> str:
        kind = "error" if self.level == "error" else "warning"
        pos = f",line={self.line},col={self.col}" if self.line else ""
        msg = self.message + (f" Fix: {self.fix}" if self.fix else "")
        return f"::{kind} file={file}{pos},title=agentl {self.code}::{msg}"


@dataclass
class Report:
    file: str
    findings: list[Finding]
    spec: PipelineSpec | None = None

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    def render(self, fmt: str = "text") -> str:
        if fmt == "json":
            return json.dumps(
                {"file": self.file, "ok": not self.errors, "findings": [asdict(f) for f in self.findings]},
                indent=2,
                default=str,
            )
        if fmt == "github":
            return "\n".join(f.github(self.file) for f in self.findings)
        return "\n".join(f.text(self.file) for f in self.findings)


def suggest(name: str, choices: Any) -> str | None:
    close = difflib.get_close_matches(str(name), sorted({str(c) for c in choices}), n=1, cutoff=0.6)
    return close[0] if close else None


def _did_you_mean(name: str, choices: Any) -> str:
    best = suggest(name, choices)
    return f" Did you mean '{best}'?" if best else ""


# ---------------------------------------------------------------- YAML positions
class _Positions:
    """Line and column of every key and value, by path (`("steps", 2, "use")`)."""

    def __init__(self, text: str):
        self.at: dict[tuple, tuple[int, int]] = {}
        self.steps: dict[str, tuple] = {}  # step id -> path of its mapping
        self.dups: dict[str, list[tuple]] = {}  # step id -> paths of every step with that id
        self.lines = text.splitlines()
        try:
            root = yaml.compose(text, Loader=_Loader)
        except yaml.YAMLError:
            root = None
        if root is not None:
            self._walk(root, ())
        self._index_steps(root)

    def _walk(self, node: Any, path: tuple) -> None:
        self.at[path] = (node.start_mark.line + 1, node.start_mark.column + 1)
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                key = k.value
                key_path = (*path, key, "<key>")
                self.at[key_path] = (k.start_mark.line + 1, k.start_mark.column + 1)
                self._walk(v, (*path, key))
        elif isinstance(node, yaml.SequenceNode):
            for i, v in enumerate(node.value):
                self._walk(v, (*path, i))

    def _index_steps(self, root: Any) -> None:
        if not isinstance(root, yaml.MappingNode):
            return
        for k, v in root.value:
            if k.value not in ("steps", "extract", "transform", "load"):
                continue
            items = v.value if isinstance(v, yaml.SequenceNode) else [v]
            for i, item in enumerate(items):
                path = (k.value, i) if isinstance(v, yaml.SequenceNode) else (k.value,)
                sid = f"{k.value}_{i + 1}" if k.value != "steps" else None
                if isinstance(item, yaml.MappingNode):
                    for ik, iv in item.value:
                        if ik.value == "id" and isinstance(iv, yaml.ScalarNode):
                            sid = iv.value
                if sid:
                    self.steps.setdefault(sid, path)
                    self.dups.setdefault(sid, []).append(path)

    def of(self, path: tuple) -> tuple[int, int]:
        for n in range(len(path), -1, -1):
            if path[:n] in self.at:
                return self.at[path[:n]]
        return (0, 0)

    def step(self, step_id: str, *rest: Any) -> tuple[int, int]:
        base = self.steps.get(step_id)
        return self.of((*base, *rest)) if base is not None else (0, 0)

    def disabled(self, line: int) -> set[str]:
        if not 0 < line <= len(self.lines):
            return set()
        m = _DISABLE.search(self.lines[line - 1])
        return {c.strip() for c in m.group(1).split(",")} if m else set()


# ---------------------------------------------------------------- errors
def _structure_errors(raw: dict, pos: _Positions) -> list[Finding]:
    """References between steps, connectors and llms, all at once, with suggestions."""
    out: list[Finding] = []
    steps = [s for s in raw.get("steps") or [] if isinstance(s, dict)]
    ids = [str(s.get("id")) for s in steps if s.get("id") is not None]
    seen: set[str] = set()
    for sid in ids:
        if sid in seen:
            again = pos.dups.get(sid, [])
            line, col = pos.of((*again[1], "id")) if len(again) > 1 else pos.step(sid, "id")
            out.append(
                Finding(
                    "AE006", "error", f"duplicate step id '{sid}'", line, col, "give each step its own id"
                )
            )
        seen.add(sid)
    llms = raw.get("llm")
    llm_names = set(llms) if isinstance(llms, dict) else {"default"}
    names = registry.names()
    for s in steps:
        sid = str(s.get("id"))
        use = s.get("use")
        if isinstance(use, str) and use not in names:
            line, col = pos.step(sid, "use")
            best = suggest(use, names)
            out.append(
                Finding(
                    "AE003",
                    "error",
                    f"step '{sid}': unknown connector '{use}'.{_did_you_mean(use, names)}",
                    line,
                    col,
                    f"use: {best}" if best else "run `agentl connectors` to list them",
                )
            )
        refs = s.get("input")
        refs = [refs] if isinstance(refs, str) else refs if isinstance(refs, list) else []
        for key, ref in (
            [("input", r) for r in refs]
            + [("check", s.get("check"))]
            + [
                (
                    "wait_until",
                    (s.get("wait_until") or {}).get("step")
                    if isinstance(s.get("wait_until"), dict)
                    else None,
                )
            ]
        ):
            if isinstance(ref, str) and ref not in ids:
                line, col = pos.step(sid, key)
                best = suggest(ref, ids)
                out.append(
                    Finding(
                        "AE004",
                        "error",
                        f"step '{sid}': {key} '{ref}' does not exist.{_did_you_mean(ref, ids)} "
                        f"Steps: {', '.join(ids)}",
                        line,
                        col,
                        f"{key}: {best}" if best else None,
                    )
                )
        if s.get("agent") is not None:
            wanted = s.get("llm", "default")
            for name in wanted if isinstance(wanted, list) else [wanted]:
                if isinstance(name, str) and name not in llm_names:
                    line, col = pos.step(sid, "llm")
                    best = suggest(name, llm_names)
                    out.append(
                        Finding(
                            "AE005",
                            "error",
                            f"step '{sid}': unknown llm '{name}'.{_did_you_mean(name, llm_names)} "
                            f"Defined: {', '.join(sorted(llm_names))}",
                            line,
                            col,
                            f"llm: {best}" if best else "add it under `llm:`",
                        )
                    )
    return out


def _pydantic_errors(
    raw: dict, pos: _Positions, skip_refs: bool
) -> tuple[PipelineSpec | None, list[Finding]]:
    from pydantic import ValidationError

    try:
        return PipelineSpec.model_validate(raw), []
    except ValidationError as e:
        out = []
        steps = raw.get("steps") or []
        for err in e.errors():
            loc = tuple(err.get("loc") or ())
            msg = str(err.get("msg", "")).removeprefix("Value error, ")
            if skip_refs and re.search(r"unknown (input|llm)|duplicate step ids", msg):
                continue  # already reported, with positions and suggestions
            path: tuple = loc
            if len(loc) >= 2 and loc[0] == "steps" and isinstance(loc[1], int) and loc[1] < len(steps):
                sid = str(steps[loc[1]].get("id"))
                line, col = pos.step(sid, *loc[2:])
                where = f"step '{sid}'" + (f" {'.'.join(map(str, loc[2:]))}" if loc[2:] else "")
                if not msg.startswith(f"step '{sid}'"):
                    msg = f"{where}: {msg}"
            else:
                m = re.search(r"step '([^']+)'", msg)
                line, col = pos.step(m.group(1)) if m else pos.of(path)
                if loc and not m:
                    msg = f"{'.'.join(map(str, loc))}: {msg}"
            if err.get("type") == "extra_forbidden" and loc:
                parent = [str(x) for x in loc[:-1]]
                msg = f"unknown field '{loc[-1]}'" + (f" in {'.'.join(parent)}" if parent else "")
            out.append(Finding("AE002", "error", msg, line, col, path=list(loc)))
        return None, out
    except ValueError as e:
        return None, [Finding("AE002", "error", str(e), *pos.of(()))]


# ---------------------------------------------------------------- warnings
def _consumers(spec: PipelineSpec) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {s.id: [] for s in spec.steps}
    for s in spec.steps:
        for ref in s.inputs:
            out.setdefault(ref, []).append(s.id)
    return out


def _ancestors(spec: PipelineSpec, step_id: str) -> set[str]:
    by_id = {s.id: s for s in spec.steps}
    seen: set[str] = set()
    todo = list(by_id[step_id].inputs)
    while todo:
        cur = todo.pop()
        if cur in seen or cur not in by_id:
            continue
        seen.add(cur)
        todo.extend(by_id[cur].inputs)
    return seen


def _walk_values(node: Any, path: tuple = ()) -> Any:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_values(v, (*path, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_values(v, (*path, i))
    else:
        yield path, node


def _warnings(spec: PipelineSpec, raw_text_tree: Any, pos: _Positions) -> list[Finding]:
    out: list[Finding] = []
    agents = [s for s in spec.steps if s.kind == "agent" or s.review]
    paid = [s for s in agents if spec.llm.get(s.llm) and spec.llm[s.llm].provider not in ("echo", "ollama")]
    if paid and not (spec.budget and (spec.budget.max_usd or spec.budget.max_tokens)):
        line, col = pos.step(paid[0].id)
        out.append(
            Finding(
                "AL001",
                "warning",
                f"{len(paid)} agent step(s) call a paid LLM and the run has no budget",
                line,
                col,
                "add `budget: { max_usd: 5 }` at the top level",
            )
        )
    checks = {s.id for s in spec.steps if s.kind == "check"}
    for s in spec.steps:
        remote_path = any("://" in str(v) for k, v in s.with_.items() if k in ("path", "url", "dsn"))
        use = s.use or ""
        delivering = use in _DELIVERING or (use.endswith(".write") and remote_path)
        if delivering and not (_ancestors(spec, s.id) & checks):
            line, col = pos.step(s.id, "use")
            out.append(
                Finding(
                    "AL002",
                    "warning",
                    f"step '{s.id}' delivers data ({s.use}) and nothing checks it first",
                    line,
                    col,
                    f"add a `check:` step between '{s.inputs[0] if s.inputs else s.id}' and '{s.id}'",
                )
            )
    for path, value in _walk_values(raw_text_tree):
        if not isinstance(value, str) or "${" in value or not path:
            continue
        key = str(path[-1])
        env_name = key.endswith("_env") or re.fullmatch(r"[A-Z][A-Z0-9_]*", value) is not None
        literal_secret = bool(_SECRET_KEY.search(key)) and bool(value) and not env_name
        if literal_secret or _SECRET_VALUE.match(value) or _DSN_PASSWORD.match(value):
            line, col = pos.of(path)
            out.append(
                Finding(
                    "AL003",
                    "warning",
                    f"{'.'.join(map(str, path))} looks like a secret written in the pipeline",
                    line,
                    col,
                    "move it to an environment variable and write `${VAR}` (or a secret reference)",
                )
            )
    by_id = {s.id: s for s in spec.steps}
    for s in spec.steps:
        if s.use != "protect":
            continue
        fields = s.with_.get("fields")
        names = ", ".join(map(str, fields)) if isinstance(fields, dict) else "the protected fields"
        for a in sorted(_ancestors(spec, s.id)):
            if by_id[a].kind == "agent":
                line, col = pos.step(a)
                out.append(
                    Finding(
                        "AL008",
                        "warning",
                        f"agent step '{a}' runs before '{s.id}', so the LLM provider sees {names}",
                        line,
                        col,
                        f"move '{s.id}' before '{a}', or drop those fields with `select` first",
                    )
                )
    consumed = _consumers(spec)
    last = spec.steps[-1].id if spec.steps else None
    sink_verbs = ("write", "upload", "post", "produce", "publish", "send", "mark")
    for s in spec.steps:
        is_sink = s.kind in ("fn", "check", "wait") or (
            bool(s.use) and (s.use or "").rsplit(".", 1)[-1] in sink_verbs
        )
        if not consumed.get(s.id) and s.id != last and not is_sink:
            line, col = pos.step(s.id)
            out.append(
                Finding(
                    "AL004",
                    "warning",
                    f"step '{s.id}' produces records that no step uses",
                    line,
                    col,
                    f"use it as `input: {s.id}` somewhere, or remove it",
                )
            )
    for s in spec.steps:
        if s.fn and ":" in s.fn:
            mod = s.fn.split(":", 1)[0]
            base = Path(spec.base_dir or ".")
            local = base / (mod.replace(".", "/") + ".py")
            package = base / mod.replace(".", "/") / "__init__.py"
            found = (
                local.exists() or package.exists() or (Path.cwd() / (mod.replace(".", "/") + ".py")).exists()
            )
            if not found:
                try:
                    found = importlib.util.find_spec(mod.split(".")[0]) is not None
                except (ImportError, ValueError):
                    found = False
            if not found:
                line, col = pos.step(s.id, "fn")
                out.append(
                    Finding(
                        "AL005",
                        "warning",
                        f"step '{s.id}': no module '{mod}' next to the pipeline or installed",
                        line,
                        col,
                        f"create {mod.replace('.', '/')}.py next to the pipeline",
                    )
                )
    for s in spec.steps:
        if s.cursor and "{cursor}" not in json.dumps(s.with_, default=str):
            line, col = pos.step(s.id, "cursor")
            out.append(
                Finding(
                    "AL006",
                    "warning",
                    f"step '{s.id}' keeps a cursor but never uses it: no `{{cursor}}` in its `with`",
                    line,
                    col,
                    'pass it to the source, e.g. `params: { since: "{cursor}" }`',
                )
            )
    out += _locale_hints(spec, pos)
    known = set(PipelineSpec.model_fields) | {"extract", "transform", "load", "lint"}
    for key in raw_text_tree if isinstance(raw_text_tree, dict) else {}:
        if key not in known:
            line, col = pos.of((key, "<key>"))
            choices = known - {"base_dir"}
            best = suggest(key, choices)
            out.append(
                Finding(
                    "AL007",
                    "warning",
                    f"unknown top-level field '{key}' is ignored when the pipeline runs."
                    + _did_you_mean(key, choices),
                    line,
                    col,
                    f"{best}:" if best else "remove it",
                )
            )
    for i, t in enumerate(spec.trigger):
        if t.cron and not t.timezone:
            line, col = pos.of(("trigger", i, "cron"))
            out.append(
                Finding(
                    "AL009",
                    "error",
                    f"cron trigger '{t.cron}' has no time zone; it follows the server clock and shifts "
                    "with daylight saving",
                    line,
                    col,
                    "add `timezone: America/New_York` (any IANA name) next to `cron:`",
                )
            )
    return out


# ---------------------------------------------------------------- entry points
_NUMERIC_TYPES = {"int", "decimal", "float", "money", "percent"}
# 1,234.56 or 1.050 (en-US 1.05, but 1050 under the pt-BR default)
_EN_US_NUMBER = re.compile(r"^[-+(]?\D{0,4}\s*(\d{1,3}(,\d{3})+\.\d+|\d+\.\d{3})\)?$")


def _sample_csv(path: Path, limit: int = 200) -> list[dict]:
    import csv

    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            text = f.read(256_000)
        lines = text.splitlines()[: limit + 1]
        try:
            dialect: Any = csv.Sniffer().sniff(lines[0]) if lines else csv.excel
        except csv.Error:
            dialect = csv.excel
        return list(csv.DictReader(lines, dialect=dialect))
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


def _locale_hints(spec: PipelineSpec, pos: _Positions) -> list[Finding]:
    """AL010: a `cast` step with no `locale` (so pt-BR) whose numeric fields come from a local CSV
    where the first values read like en-US numbers."""
    from .connectors.transforms import _cast_spec

    out: list[Finding] = []
    by_id = {s.id: s for s in spec.steps}
    base = Path(spec.base_dir or ".")
    for s in spec.steps:
        if s.use != "cast" or "locale" in s.with_ or not isinstance(s.with_.get("fields"), dict):
            continue
        numeric = []
        for f, t in s.with_["fields"].items():
            try:
                if _cast_spec(t)[0] in _NUMERIC_TYPES:
                    numeric.append(str(f))
            except ValueError:
                continue
        if not numeric:
            continue
        hits: dict[str, str] = {}
        for a in sorted(_ancestors(spec, s.id)):
            src = by_id[a]
            path = src.with_.get("path")
            if src.use != "csv.read" or not isinstance(path, str) or "$" in path or "*" in path:
                continue
            p = Path(path) if Path(path).is_absolute() else base / path
            rows = _sample_csv(p) if p.is_file() else []
            for f in numeric:
                values = [str(r.get(f) or "").strip() for r in rows]
                example = next((v for v in values if _EN_US_NUMBER.match(v)), None)
                if example:
                    hits.setdefault(f, example)
        if hits:
            pairs = [f"{f} ({v!r})" for f, v in hits.items()]
            line, col = pos.step(s.id)
            out.append(
                Finding(
                    "AL010",
                    "warning",
                    f"step '{s.id}' casts numbers with the default locale pt-BR, but "
                    f"{', '.join(pairs)} look like en-US numbers",
                    line,
                    col,
                    "set `locale: en-US` in its `with` (or `locale: pt-BR` to keep the default)",
                )
            )
    return out


def _env_errors(text: str, pos: _Positions, env: Any) -> list[Finding]:
    """`${VAR}` with no default and not set. Only names are checked; values never reach a
    finding. Secret references are left to `--connect`, which may call cloud APIs."""
    import os

    env = os.environ if env is None else env
    out = []
    for n, line_text in enumerate(pos.lines, 1):
        if line_text.lstrip().startswith("#"):
            continue
        for m in _ENV.finditer(line_text):
            if m.group(2) is None and m.group(1) not in env:
                out.append(
                    Finding(
                        "AE008",
                        "error",
                        f"environment variable {m.group(1)} is not set",
                        n,
                        m.start() + 1,
                        f"export {m.group(1)}=... (or write ${{{m.group(1)}:-default}})",
                    )
                )
    return out


def _param_errors(flat: dict, pos: _Positions) -> list[Finding]:
    """Declarations under `params:` (type, default of that type) and `${params.x}` references
    to names that are not declared. Normalizes `flat["params"]` to the long form."""
    from .spec import PARAM_REF, _param_spec

    out: list[Finding] = []
    declared = flat.get("params") or {}
    if not isinstance(declared, dict):
        return [Finding("AE010", "error", "params: expected a mapping of name -> type", *pos.of(("params",)))]
    norm = {}
    for name, raw in declared.items():
        try:
            norm[name] = _param_spec(name, raw).model_dump(exclude_none=True)
        except SpecError as e:
            out.append(Finding("AE010", "error", str(e), *pos.of(("params", name))))
    flat["params"] = norm
    for n, line_text in enumerate(pos.lines, 1):
        if line_text.lstrip().startswith("#"):
            continue
        for m in PARAM_REF.finditer(line_text):
            if m.group(1) not in declared:
                out.append(
                    Finding(
                        "AE010",
                        "error",
                        f"{m.group(0)} is not declared under params:"
                        + _did_you_mean(m.group(1), list(declared)),
                        n,
                        m.start() + 1,
                        f"add `params: {{{m.group(1)}: str}}`",
                    )
                )
    return out


def _sql_errors(spec: PipelineSpec, pos: _Positions) -> list[Finding]:
    """A `sql` step may read only its inputs. Needs DuckDB to parse; skipped without it."""
    from .connectors import duck

    out = []
    for s in spec.steps:
        query = s.with_.get("query")
        if s.use != "sql" or not isinstance(query, str):
            continue
        used = duck.tables(query)
        if used is None:
            continue
        for name in sorted(used - set(s.inputs)):
            out.append(
                Finding(
                    "AE009",
                    "error",
                    f"step '{s.id}': sql reads table '{name}', which is not one of its inputs "
                    f"({', '.join(s.inputs) or 'none'}).{_did_you_mean(name, s.inputs)}",
                    *pos.step(s.id),
                    f"add '{name}' to input" if name in {x.id for x in spec.steps} else None,
                )
            )
    return out


def check_text(
    text: str,
    file: str = "<pipeline>",
    base_dir: str | None = None,
    warnings: bool = True,
    env: Any = None,
) -> Report:
    """Validate (and lint) pipeline YAML text. Environment variables and secret references are
    never resolved: they read as placeholders, so nothing secret is read or printed."""
    pos = _Positions(text)
    try:
        raw = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line, col = (mark.line + 1, mark.column + 1) if mark else (0, 0)
        problem = getattr(e, "problem", None) or str(e)
        fix = "optional types like `str?` need block style or quotes inside { }" if "'?'" in str(e) else None
        return Report(file, [Finding("AE001", "error", f"invalid YAML: {problem}", line, col, fix)])
    if not isinstance(raw, dict):
        return Report(
            file, [Finding("AE002", "error", "a pipeline is a YAML mapping with name and steps", 1, 1)]
        )

    from .mcp_server import _neutral, _Placeholders

    neutral = yaml.load(_neutral(text), Loader=_Loader)
    try:
        flat = _flatten_sections(interpolate(dict(neutral), _Placeholders()))
    except (SpecError, ValueError) as e:
        return Report(file, [Finding("AE002", "error", str(e), 1, 1)])
    if isinstance(flat.get("llm"), dict) and "provider" in flat["llm"]:
        flat["llm"] = {"default": flat["llm"]}
    lint_cfg = flat.pop("lint", None) or {}
    findings = _structure_errors(flat, pos) + _env_errors(text, pos, env) + _param_errors(flat, pos)
    spec, errs = _pydantic_errors(flat, pos, skip_refs=bool(findings))
    findings += errs
    if spec is not None and not findings:
        from . import graph

        try:
            graph.order(spec)
        except SpecError as e:
            findings.append(Finding("AE007", "error", str(e), *pos.of(("steps",))))
        findings += _sql_errors(spec, pos)
    if spec is not None:
        spec.base_dir = base_dir
        if warnings:
            findings += _warnings(spec, raw, pos)
    off = set((lint_cfg.get("disable") if isinstance(lint_cfg, dict) else None) or [])
    findings = [f for f in findings if f.code not in off and f.code not in pos.disabled(f.line)]
    findings.sort(key=lambda f: (f.line or 10**9, f.col, f.code))
    return Report(file, findings, spec)


def check_file(path: str | Path, warnings: bool = True, env: Any = None) -> Report:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return check_text(text, str(path), str(p.resolve().parent), warnings, env)


def schema() -> dict:
    """JSON Schema of a pipeline file, for editors (VS Code YAML extension) and agents. Also
    accepts the shortcuts `parse` accepts: a single `llm:` mapping and extract/transform/load."""
    s = PipelineSpec.model_json_schema(by_alias=True)
    s["title"] = "agentl pipeline"
    props = s.setdefault("properties", {})
    if "llm" in props:
        props["llm"] = {"anyOf": [props["llm"], {"$ref": "#/$defs/LLMSpec"}]}
    step_list = {
        "anyOf": [{"type": "array", "items": {"$ref": "#/$defs/StepSpec"}}, {"$ref": "#/$defs/StepSpec"}]
    }
    for section in ("extract", "transform", "load"):
        props[section] = step_list
    props["lint"] = {
        "type": "object",
        "properties": {"disable": {"type": "array", "items": {"type": "string", "enum": sorted(RULES)}}},
    }
    s.pop("required", None)
    s["anyOf"] = [{"required": ["name", "steps"]}, {"required": ["name", "extract"]}]
    step = s.get("$defs", {}).get("StepSpec", {})
    if "llm" in step.get("properties", {}):
        step["properties"]["llm"] = {
            "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]
        }
    return s
