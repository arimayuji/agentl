"""Connector registry. Third-party packages register through the
`agentl.connectors` entry point group: name = "module:function"."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from importlib.metadata import entry_points
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


def resolve_fn(ref: str) -> Callable[..., Any]:
    """'package.module:function' -> callable."""
    if ":" not in ref:
        raise ValueError(f"fn '{ref}' must look like 'module:function'")
    mod, attr = ref.split(":", 1)
    return getattr(import_module(mod), attr)


def require(module: str, extra: str) -> Any:
    try:
        return import_module(module)
    except ImportError as e:
        raise ImportError(f"this connector needs '{module}': pip install 'agentl[{extra}]'") from e
