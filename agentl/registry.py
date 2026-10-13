"""Connector registry. Third-party packages register through the
`agentl.connectors` entry point group: name = "module:function"."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
from collections.abc import Callable
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType
from typing import Any

Records = list[dict[str, Any]]
Connector = Callable[..., Records]

_CONNECTORS: dict[str, Connector] = {}
_loaded = False


def connector(name: str) -> Callable[[Connector], Connector]:
    def deco(fn: Connector) -> Connector:
        _CONNECTORS[name] = fn
        return fn

    return deco


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    import_module("agentl.connectors")
    for ep in entry_points(group="agentl.connectors"):
        _CONNECTORS.setdefault(ep.name, ep.load())


def get(name: str) -> Connector:
    _load()
    if name not in _CONNECTORS:
        raise KeyError(f"unknown connector '{name}'. Installed: {', '.join(sorted(_CONNECTORS))}")
    return _CONNECTORS[name]


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
            name = f"_agentl_fn_{hashlib.sha1(str(file).encode()).hexdigest()[:12]}_{mod.replace('.', '_')}"
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
