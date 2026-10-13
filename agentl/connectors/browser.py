"""Pages that need a real browser (JavaScript, logins, download buttons), through Playwright.

Install with `pip install 'agentl-etl[browser]'` and `playwright install chromium`."""

from __future__ import annotations

import os
from typing import Any

from ..registry import connector, require
from ._util import tmpdir
from .files import html_tables

EXTRACT = ("table", "text", "html", "links")
ACTIONS = ("goto", "fill", "click", "press", "select", "check", "wait_for", "wait")


def _do(page: Any, action: dict, rec: dict, timeout: float) -> None:
    """Run one step: {goto: url} {fill: sel, value: v} {click: sel} {press: sel, key: Enter}
    {select: sel, value: v} {check: sel} {wait_for: sel} {wait: ms}. Strings take {field} from the record."""
    kind = next((k for k in ACTIONS if k in action), None)
    if kind is None:
        raise ValueError(f"unknown browser step {action}; use one of {ACTIONS}")
    arg = str(action[kind]).format(**rec) if kind != "wait" else action[kind]
    value = str(action.get("value", "")).format(**rec)
    if kind == "goto":
        page.goto(arg, timeout=timeout)
    elif kind == "fill":
        page.fill(arg, value, timeout=timeout)
    elif kind == "click":
        page.click(arg, timeout=timeout)
    elif kind == "press":
        page.press(arg, action.get("key", "Enter"), timeout=timeout)
    elif kind == "select":
        page.select_option(arg, value, timeout=timeout)
    elif kind == "check":
        page.check(arg, timeout=timeout)
    elif kind == "wait_for":
        page.wait_for_selector(arg, timeout=timeout)
    elif kind == "wait":
        page.wait_for_timeout(float(arg))


def _extract(page: Any, mode: str, selector: str | None, table: int | None) -> list[dict]:
    if mode == "table":
        html = "".join(el.evaluate("e => e.outerHTML") for el in page.query_selector_all(selector or "table"))
        return html_tables(html, table)
    if mode == "links":
        return page.eval_on_selector_all(
            f"{selector or 'body'} a[href]", "els => els.map(a => ({text: a.innerText.trim(), href: a.href}))"
        )
    els = page.query_selector_all(selector or "body")
    if mode == "text":
        return [{"text": el.inner_text()} for el in els]
    return [{"html": el.inner_html()} for el in els]


@connector("browser.scrape")
def browser_scrape(
    input: Any = None,
    url: str | None = None,
    steps: list[dict] | None = None,
    wait_for: str | None = None,
    selector: str | None = None,
    extract: str = "table",
    table: int | None = None,
    download: str | None = None,
    timeout: float = 30,
    headless: bool = True,
    executable_path: str | None = None,
    user_agent: str | None = None,
) -> list[dict]:
    """Open `url` (or run `steps`), then extract `table` rows, `text`, `html` or `links` matching
    `selector`, or click `download` and return the saved file as `{path}` for `file.read`.

    With upstream records, runs once per record and fills `{field}` placeholders in `url` and
    steps. Every output record gets `_url` (the page it came from)."""
    if extract not in EXTRACT:
        raise ValueError(f"extract must be one of {EXTRACT}")
    sync_api = require("playwright.sync_api", "browser")
    records = input if isinstance(input, list) and input else [{}]
    ms = timeout * 1000
    out: list[dict] = []
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless, executable_path=executable_path or os.environ.get("AGENTL_BROWSER_EXECUTABLE")
        )
        try:
            context = browser.new_context(accept_downloads=True, user_agent=user_agent)
            page = context.new_page()
            for rec in records:
                if url:
                    page.goto(url.format(**rec), timeout=ms)
                for action in steps or []:
                    _do(page, action, rec, ms)
                if wait_for:
                    page.wait_for_selector(wait_for.format(**rec), timeout=ms)
                if download:
                    with page.expect_download(timeout=ms) as info:
                        page.click(download.format(**rec), timeout=ms)
                    dl = info.value
                    path = tmpdir("browser-") / dl.suggested_filename.replace("/", "_").replace("\\", "_")
                    dl.save_as(str(path))
                    out.append({"path": str(path), "filename": path.name, "_url": page.url})
                    continue
                out.extend({**r, "_url": page.url} for r in _extract(page, extract, selector, table))
            context.close()
        finally:
            browser.close()
    return out
