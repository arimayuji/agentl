"""Connector registry. Third-party packages register through the
`agentl.connectors` entry point group: name = "module:function"."""

from __future__ import annotations

import contextvars
import hashlib
import importlib.util
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

Records = list[dict[str, Any]]
Connector = Callable[..., Records]

_CONNECTORS: dict[str, Connector] = {}
_loaded = False

# Chunked execution (`chunk_size`). A stream reader yields lists of records; a stream writer takes
# an iterator of lists and writes them in one file or transaction; a row-wise connector gives the
# same answer on each chunk as on the whole input, so the runner may call it chunk by chunk.
StreamReader = Callable[..., Iterator[Records]]
StreamWriter = Callable[..., Records]
_STREAM_READERS: dict[str, StreamReader] = {}
_STREAM_WRITERS: dict[str, StreamWriter] = {}
_ROWWISE: set[str] = set()


class Rejects(list):
    """A connector's output with the records it refused: `Rejects(good, rejected)`. The runner
    keeps the rejected ones apart (RunResult.rejected, an `item.rejected` event), as it does for
    agent steps. Each rejected record carries `_error` (the reason)."""

    def __init__(self, good: Iterable[dict] = (), rejected: Iterable[dict] = ()):
        super().__init__(good)
        self.rejected = list(rejected)


def connector(name: str) -> Callable[[Connector], Connector]:
    def deco(fn: Connector) -> Connector:
        _CONNECTORS[name] = fn
        return fn

    return deco


def stream_reader(name: str) -> Callable[[StreamReader], StreamReader]:
    """`fn(input, chunk_size, **with) -> Iterator[Records]` for connector `name`."""

    def deco(fn: StreamReader) -> StreamReader:
        _STREAM_READERS[name] = fn
        return fn

    return deco


def stream_writer(name: str) -> Callable[[StreamWriter], StreamWriter]:
    """`fn(chunks: Iterator[Records], **with) -> Records` for connector `name`."""

    def deco(fn: StreamWriter) -> StreamWriter:
        _STREAM_WRITERS[name] = fn
        return fn

    return deco


def rowwise(name: str) -> None:
    """Declare that connector `name` handles each record on its own (like `select` or `filter`)."""
    _ROWWISE.add(name)


# Columnar fast path (`agentl-etl[arrow]`). An Arrow reader returns a pyarrow Table; an Arrow
# writer takes one. The runner uses them only on edges where both ends can, and only when pyarrow
# is installed; everywhere else records stay dicts.
ArrowReader = Callable[..., Any]
ArrowWriter = Callable[..., Records]
_ARROW_READERS: dict[str, ArrowReader] = {}
_ARROW_WRITERS: dict[str, tuple[ArrowWriter, Callable[[dict], bool]]] = {}


def arrow_reader(name: str) -> Callable[[ArrowReader], ArrowReader]:
    """`fn(input, **with) -> pyarrow.Table` for connector `name`."""

    def deco(fn: ArrowReader) -> ArrowReader:
        _ARROW_READERS[name] = fn
        return fn

    return deco


def arrow_writer(
    name: str, accepts: Callable[[dict], bool] | None = None
) -> Callable[[ArrowWriter], ArrowWriter]:
    """`fn(table, **with) -> Records` for connector `name`. `accepts(with_)` says whether this step's
    settings can take a table (for example, not in upsert mode)."""

    def deco(fn: ArrowWriter) -> ArrowWriter:
        _ARROW_WRITERS[name] = (fn, accepts or (lambda _: True))
        return fn

    return deco


def arrow_available() -> bool:
    return importlib.util.find_spec("pyarrow") is not None


def arrow(name: str) -> tuple[ArrowReader | None, tuple[ArrowWriter, Callable[[dict], bool]] | None]:
    """(Arrow reader, (Arrow writer, accepts)) for connector `name`."""
    _load()
    return _ARROW_READERS.get(name), _ARROW_WRITERS.get(name)


def streaming(name: str) -> tuple[StreamReader | None, StreamWriter | None, bool]:
    """(stream reader, stream writer, row-wise) for connector `name`."""
    _load()
    return _STREAM_READERS.get(name), _STREAM_WRITERS.get(name), name in _ROWWISE


_LOAD_LOCK = threading.RLock()


def _load() -> None:
    """Imports the built-in connectors and the entry points once. Locked and marked loaded only at
    the end: steps run in parallel threads, and none may see a half-filled registry."""
    global _loaded
    if _loaded:
        return
    with _LOAD_LOCK:
        if _loaded:
            return
        import_module("agentl.connectors")
        from importlib.metadata import entry_points

        for ep in entry_points(group="agentl.connectors"):
            _CONNECTORS.setdefault(ep.name, ep.load())
        _loaded = True


def get(name: str) -> Connector:
    _load()
    if name not in _CONNECTORS:
        raise KeyError(f"unknown connector '{name}'. Installed: {', '.join(sorted(_CONNECTORS))}")
    return _CONNECTORS[name]


def describe(name: str) -> str:
    """A connector's `with:` parameters with types and defaults, and its docstring: what an agent
    needs to write the step without guessing."""
    import inspect

    fn = get(name)
    lines = [name]
    doc = inspect.getdoc(fn)
    if doc:
        lines += ["", doc]
    params = [
        p
        for p in inspect.signature(fn).parameters.values()
        if p.name != "input" and p.kind != p.VAR_POSITIONAL
    ]
    if any(p.kind == p.VAR_KEYWORD for p in params):
        lines += ["", "with: settings described above (any keys)"]
        params = [p for p in params if p.kind != p.VAR_KEYWORD]
    if params:
        lines += ["", "with:"]
        for p in params:
            kind = "" if p.annotation is p.empty else f" ({p.annotation})"
            default = "" if p.default is p.empty or p.default in ("", None) else f" = {p.default!r}"
            required = "  required" if p.default is p.empty else ""
            lines.append(f"  {p.name}{kind}{default}{required}")
    return "\n".join(lines)


def names() -> list[str]:
    _load()
    return sorted(_CONNECTORS)


_LOCAL: dict[Path, ModuleType] = {}
_LOCAL_LOCK = threading.Lock()


def _local_module(mod: str, base: Path) -> ModuleType | None:
    """Load `mod` from a file under `base` by path, under a name unique to that path, so
    two pipelines with their own `steps.py` never share a module."""
    rel = Path(*mod.split("."))
    for file in (base / rel.with_suffix(".py"), base / rel / "__init__.py"):
        if not file.is_file():
            continue
        file = file.resolve()
        with _LOCAL_LOCK:
            if file in _LOCAL:
                return _LOCAL[file]
            digest = hashlib.sha1(str(file).encode(), usedforsecurity=False).hexdigest()[:12]
            name = f"_agentl_fn_{digest}_{mod.replace('.', '_')}"
            spec = importlib.util.spec_from_file_location(name, file)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            # Sibling imports inside the module (`import helpers`) resolve from its folder.
            sys.path.insert(0, str(file.parent))
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
            finally:
                sys.path.remove(str(file.parent))
            _LOCAL[file] = module
            return module
    return None


def resolve_fn(ref: str, base_dir: str | Path | None = None) -> Callable[..., Any]:
    """'module:function' -> callable.

    Lookup order: a file next to the pipeline (`base_dir`), loaded by path under a name unique
    to that file; a normal import (installed packages, `sys.path`); a file in the working
    directory, loaded by path."""
    if ":" not in ref:
        raise ValueError(f"fn '{ref}' must look like 'module:function'")
    mod, attr = ref.split(":", 1)
    for base in [Path(base_dir)] if base_dir else []:
        if (module := _local_module(mod, base)) is not None:
            return getattr(module, attr)
    try:
        return getattr(import_module(mod), attr)
    except ModuleNotFoundError as e:
        if e.name != mod.split(".")[0] and e.name != mod:
            raise  # the module exists but one of its own imports is missing
        if (module := _local_module(mod, Path.cwd())) is not None:
            return getattr(module, attr)
        raise


def require(module: str, extra: str) -> Any:
    try:
        return import_module(module)
    except ImportError as e:
        raise ImportError(f"this connector needs '{module}': pip install 'agentl-etl[{extra}]'") from e


# The id of the run a connector is called from (`{run_id}` in writer paths); None outside a run.
_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("agentl_run_id", default=None)


def current_run_id() -> str | None:
    return _RUN_ID.get()


# ---------------------------------------------------------------- after the run
# Queue consumers acknowledge messages only once the whole run succeeded (at-least-once). A
# connector registers what to do then; the runner calls it when the run ends.
_AFTER_RUN: contextvars.ContextVar[list | None] = contextvars.ContextVar("agentl_after_run", default=None)


def after_run(on_success: Callable[[], None], on_failure: Callable[[], None] | None = None) -> None:
    """Call `on_success` when the current run succeeds, `on_failure` otherwise. Outside a run (a
    connector called directly), `on_success` runs at once: there is no later point to wait for."""
    hooks = _AFTER_RUN.get()
    if hooks is None:
        on_success()
    else:
        hooks.append((on_success, on_failure))


# ---------------------------------------------------------------- step state
# Stream readers without broker-side offsets (Kinesis, Event Hubs) keep their checkpoints in the
# run store, keyed by pipeline and step id, like incremental cursors (`agentl state`).
class StepState:
    """The stored state of the step being run: `get()` reads it, `set(value)` writes it (JSON)."""

    def __init__(self, get: Callable[[], Any], set: Callable[[Any], None]):
        self.get, self.set = get, set


_STEP_STATE: contextvars.ContextVar[StepState | None] = contextvars.ContextVar(
    "agentl_step_state", default=None
)


def step_state() -> StepState | None:
    """The current step's stored state, or None outside a run (a connector called directly).
    Write it from an `after_run` success hook so it moves only when the run succeeded."""
    return _STEP_STATE.get()


# ---------------------------------------------------------------- seen keys
# `skip_seen` remembers which files or records a pipeline already handled, in the run store.
class SeenKeys:
    """Seen keys of the current run: `filter(scope, keys)` returns those already seen, `add(scope,
    keys)` marks them (call it from an `after_run` success hook). `default_scope` is the pipeline."""

    def __init__(
        self,
        default_scope: str,
        filter: Callable[[str, list[str]], set[str]],
        add: Callable[[str, list[str]], None],
    ):
        self.default_scope, self.filter, self.add = default_scope, filter, add


_SEEN: contextvars.ContextVar[SeenKeys | None] = contextvars.ContextVar("agentl_seen", default=None)


def seen_keys() -> SeenKeys | None:
    """The run store's seen keys, or None outside a run (a connector called directly)."""
    return _SEEN.get()


class NoChange(Exception):
    """Raised by a source step that found its source unchanged since the last run (for example
    `http.get` with `if_changed`). The runner ends the run as `no_change` instead of `failed`:
    the steps after it do not run and the run is not an error."""
