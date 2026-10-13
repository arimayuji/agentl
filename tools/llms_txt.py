"""MkDocs hook: write llms.txt (index) and llms-full.txt (all pages) at build time.

Format follows https://llmstxt.org: a title, a summary, then sections of links."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SKIP = {"index.md", "changelog.md", "reference.md"}  # HTML landing, history, autodoc stubs


def _walk(nav: list[Any], section: str = "Docs") -> list[tuple[str, str, str]]:
    out = []
    for item in nav:
        for title, value in item.items():
            if isinstance(value, list):
                out += _walk(value, title)
            elif isinstance(value, str) and value.endswith(".md"):
                out.append((section, title, value))
    return out


def _clean(md: str) -> str:
    md = re.sub(r"<[^>]+>", "", md)  # raw HTML blocks
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def on_post_build(config: Any, **kwargs: Any) -> None:
    base = config["site_url"].rstrip("/")
    docs = Path(config["docs_dir"])
    pages = [p for p in _walk(config["nav"]) if p[2] not in SKIP]

    index = [f"# {config['site_name']}", "", f"> {config['site_description']}", ""]
    index += [
        "Install: `pip install agentl-etl`. Import and CLI: `agentl`.",
        f"Full docs as one file: {base}/llms-full.txt",
    ]
    full = [index[0], "", index[2], ""]
    section = None
    for sec, title, path in pages:
        if sec != section:
            index += ["", f"## {sec}", ""]
            section = sec
        url = f"{base}/{path[:-3]}/".replace("/index/", "/")
        index.append(f"- [{title}]({url})")
        full += [f"\n---\n\nSource: {url}\n", _clean((docs / path).read_text(encoding="utf-8"))]

    site = Path(config["site_dir"])
    (site / "llms.txt").write_text("\n".join(index).strip() + "\n", encoding="utf-8")
    (site / "llms-full.txt").write_text("\n".join(full).strip() + "\n", encoding="utf-8")
