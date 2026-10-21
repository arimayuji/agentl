"""Derived views of a record for agent steps (#214): `views:` turns a field into what the llm
actually needs (some pages, the tables of a PDF, compact HTML) before the call, so the prompt is
smaller and the model is not distracted by the rest of the document.

Each view is cached by the hash of its source (the file's bytes, or the field's value) and its
settings, so a second run, or a second step with the same view, does not read the PDF again.
PDF views run with the PDF library's module settings reset to their import-time values (and put
back afterwards), so a global set by one step's code cannot change how another step reads."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .registry import require
from .spec import StepSpec, ViewSpec

# Part of every view's cache key: bump it when a view's output changes for the same input.
VIEW_VERSION = 1

_SCALARS = (bool, int, float, str, bytes, tuple, frozenset, type(None))
_pdf_lock = threading.RLock()
_baseline: dict[str, dict[str, Any]] = {}


def _settings(prefix: str) -> dict[str, dict[str, Any]]:
    """Module-level scalar settings of the loaded modules of package `prefix` (`pypdf`, `pypdf._page`)."""
    out: dict[str, dict[str, Any]] = {}
    for name, mod in list(sys.modules.items()):
        if mod is None or not (name == prefix or name.startswith(prefix + ".")):
            continue
        out[name] = {k: v for k, v in vars(mod).items() if not k.startswith("__") and isinstance(v, _SCALARS)}
    return out


def _apply(state: dict[str, dict[str, Any]]) -> None:
    for name, values in state.items():
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for k, v in values.items():
            setattr(mod, k, v)


@contextlib.contextmanager
def isolated(prefix: str) -> Iterator[None]:
    """Runs the block with the scalar module settings of package `prefix` as they were when agentl
    first used it, then restores the values the block found. One block at a time per process."""
    with _pdf_lock:
        if prefix not in _baseline:
            _baseline[prefix] = _settings(prefix)
        found = _settings(prefix)
        _apply({n: v for n, v in _baseline[prefix].items()})
        try:
            yield
        finally:
            _apply(found)


def page_numbers(selection: list[int | str] | None, total: int) -> list[int]:
    """0-based page indexes of a `pages:` selection, in the order given, without repeats; numbers
    outside the document are skipped. None selects every page."""
    if selection is None:
        return list(range(total))

    def index(n: int) -> int:
        return n - 1 if n > 0 else total + n

    out: list[int] = []
    for item in selection:
        if isinstance(item, int):
            span = [index(item)]
        else:
            m = re.fullmatch(r"(-?\d+)-(-?\d+)?", item)
            assert m is not None  # checked by ViewSpec
            first, last = m.group(1), m.group(2)
            a = index(int(first))
            b = index(int(last)) if last else total - 1
            span = list(range(a, b + 1))
        out.extend(i for i in span if 0 <= i < total and i not in out)
    return out


def _source_bytes(view: ViewSpec, value: Any) -> bytes:
    if view.use.startswith("pdf."):
        if isinstance(value, bytes | bytearray):
            return bytes(value)
        if isinstance(value, str | Path):
            return Path(value).read_bytes()
        raise ValueError(f"view needs a PDF path or bytes in '{view.source}', got {type(value).__name__}")
    return json.dumps(value, sort_keys=True, default=str).encode()


def cache_key(view: ViewSpec, data: bytes) -> str:
    """The view's cache key: its settings and the hash of its source (the document's bytes)."""
    settings = view.model_dump(exclude={"keep_source", "from_"})
    doc = hashlib.sha256(data).hexdigest()
    return hashlib.sha256(
        json.dumps(["view", VIEW_VERSION, settings, doc], sort_keys=True).encode()
    ).hexdigest()


def _pages_view(view: ViewSpec, value: Any) -> tuple[Any, int, int]:
    if isinstance(value, str):
        pages: list = value.split("\f")
    elif isinstance(value, list):
        pages = value
    else:
        raise ValueError(f"pages view needs a text or a list in '{view.source}'")
    picked = [pages[i] for i in page_numbers(view.pages, len(pages))]
    return ("\f".join(map(str, picked)) if isinstance(value, str) else picked), len(pages), len(picked)


def _reader(data: bytes) -> Any:
    pypdf = require("pypdf", "pdf")
    return pypdf.PdfReader(io.BytesIO(data))


def _pdf_text(view: ViewSpec, data: bytes) -> tuple[Any, int, int]:
    with isolated("pypdf"):
        reader = _reader(data)
        picked = page_numbers(view.pages, len(reader.pages))
        text = "\f".join(reader.pages[i].extract_text() or "" for i in picked)
    return text, len(reader.pages), len(picked)


def layout_tables(text: str) -> list[list[list[str]]]:
    """Tables in layout text: runs of two or more lines whose cells are separated by two or more
    spaces. A heuristic for the text layer of digital PDFs, not for scans."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        cells = [c for c in re.split(r"\s{2,}", line.strip()) if c]
        if len(cells) >= 2:
            current.append(cells)
            continue
        if len(current) >= 2:
            tables.append(current)
        current = []
    if len(current) >= 2:
        tables.append(current)
    return tables


def _pdf_tables(view: ViewSpec, data: bytes) -> tuple[Any, int, int]:
    with isolated("pypdf"):
        reader = _reader(data)
        picked = page_numbers(view.pages, len(reader.pages))
        out: list[dict] = []
        for i in picked:
            text = reader.pages[i].extract_text(extraction_mode="layout") or ""
            out.extend({"page": i + 1, "rows": rows} for rows in layout_tables(text))
    return out, len(reader.pages), len(picked)


def compact_html(html: str, drop: list[str], keep_attrs: list[str]) -> str:
    """`html` without the `drop` tags (and their content), comments and attributes other than
    `keep_attrs`, with whitespace collapsed."""
    from .connectors.htmldom import VOID, Node, parse

    dropped = {t.lower() for t in drop}
    kept = {a.lower() for a in keep_attrs}

    def out(n: Node | str) -> str:
        if isinstance(n, str):
            text = re.sub(r"\s+", " ", n)
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if n.tag in dropped:
            return ""
        inner = "".join(out(c) for c in n.children)
        attrs = "".join(
            f' {k}="{v.replace(chr(34), "&quot;")}"' for k, v in n.attrs.items() if k.lower() in kept
        )
        if n.tag in VOID:
            return f"<{n.tag}{attrs}>"
        return f"<{n.tag}{attrs}>{inner}</{n.tag}>"

    root = parse(html)
    return re.sub(r">\s+<", "><", "".join(out(c) for c in root.children)).strip()


def _html_view(view: ViewSpec, value: Any) -> tuple[Any, int, int]:
    if not isinstance(value, str):
        raise ValueError(f"html.compact needs an HTML string in '{view.source}'")
    return compact_html(value, view.drop, view.keep_attrs), 0, 0


_BUILDERS: dict[str, Callable[[ViewSpec, Any], tuple[Any, int, int]]] = {
    "pages": _pages_view,
    "pdf.text": _pdf_text,
    "pdf.tables": _pdf_tables,
    "html.compact": _html_view,
}


def _size(value: Any) -> int:
    return len(value) if isinstance(value, str) else len(json.dumps(value, default=str))


def apply(
    step: StepSpec,
    rec: dict,
    cache_get: Callable[[str], Any] | None,
    cache_set: Callable[[str, Any], None] | None,
    stats: dict[str, int],
) -> dict:
    """The record the llm receives: `rec` with each view added under its name and its source
    fields removed, unless a view on that field says `keep_source`.
    Raises ValueError on a missing or bad source."""
    sent = dict(rec)
    sources: set[str] = set()
    kept_sources: set[str] = set()
    for name, view in step.views.items():
        if view.source not in rec:
            raise ValueError(f"view '{name}': the record has no field '{view.source}'")
        value = rec[view.source]
        data = _source_bytes(view, value)
        key = cache_key(view, data)
        hit = cache_get(key) if cache_get is not None else None
        if hit is not None:
            stats["cache_hits"] += 1
            built, total, kept = hit["value"], hit["pages"], hit["kept"]
        else:
            built, total, kept = _BUILDERS[view.use](view, data if view.use.startswith("pdf.") else value)
            if cache_set is not None:
                cache_set(key, {"value": built, "pages": total, "kept": kept})
        stats["pages_total"] += total
        stats["pages_sent"] += kept
        if not view.use.startswith("pdf."):
            stats["source_chars"] += _size(value)
        stats["view_chars"] += _size(built)
        sent[name] = built
        (kept_sources if view.keep_source else sources).add(view.source)
    for f in sources - kept_sources - set(step.views):
        sent.pop(f, None)
    return sent
