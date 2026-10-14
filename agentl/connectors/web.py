"""Web pages as clean Markdown for agent steps, and evidence of what each run saw.

Markdown comes from trafilatura (`pip install 'agentl-etl[web]'`): the main content of the page,
without menus, footers or scripts, keeping titles, lists, tables and links."""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path
from typing import Any

from ..registry import require

DEFAULT_EVIDENCE_DIR = ".agentl/evidence"


def to_markdown(html: str, url: str | None = None) -> dict[str, Any]:
    """{title, markdown} for one page. `markdown` is empty when no main content was found."""
    trafilatura = require("trafilatura", "web")
    md = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=True,
        include_formatting=True,
        favor_recall=True,
    )
    meta = trafilatura.extract_metadata(html)
    return {"title": (meta.title if meta else None) or "", "markdown": (md or "").strip()}


def _slug(source: str) -> str:
    name = re.sub(r"^[a-z]+://", "", source)
    name = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-")[:60] or "page"
    return f"{name}-{hashlib.sha256(source.encode()).hexdigest()[:8]}"


def save_evidence(
    target: bool | str,
    source: str,
    html: str,
    markdown: str | None = None,
    screenshot: bytes | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, str]:
    """Keep what a run saw: `<dir>/<date>/<time>-<page>.html` (+ `.md`, `.png`). Returns the paths
    as `_evidence_*` fields for the output record."""
    now = now or _dt.datetime.now()
    folder = Path(DEFAULT_EVIDENCE_DIR if target is True else str(target)) / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"{now.strftime('%H%M%S')}-{_slug(source)}"
    out = {"_evidence_html": str(stem) + ".html"}
    Path(out["_evidence_html"]).write_text(html, encoding="utf-8")
    if markdown is not None:
        out["_evidence_markdown"] = str(stem) + ".md"
        Path(out["_evidence_markdown"]).write_text(markdown, encoding="utf-8")
    if screenshot is not None:
        out["_evidence_screenshot"] = str(stem) + ".png"
        Path(out["_evidence_screenshot"]).write_bytes(screenshot)
    return out


def prune_evidence(target: bool | str, keep_days: int, today: _dt.date | None = None) -> int:
    """Delete evidence folders older than `keep_days`. Returns how many files were removed."""
    root = Path(DEFAULT_EVIDENCE_DIR if target is True else str(target))
    if not root.is_dir():
        return 0
    cutoff = (today or _dt.date.today()) - _dt.timedelta(days=keep_days)
    removed = 0
    for day in root.iterdir():
        try:
            when = _dt.date.fromisoformat(day.name)
        except ValueError:
            continue  # not ours
        if day.is_dir() and when < cutoff:
            for f in day.iterdir():
                f.unlink()
                removed += 1
            day.rmdir()
    return removed
