"""Build the agentl social cards (1200x675) in the brand palette: blue with the shade, white text,
yellow accent. Cards are plain data at the bottom of this file; add one and rerun.

    pip install playwright
    python media/social/cards.py            # every card into media/social/cards/
    python media/social/cards.py release    # only cards whose name starts with "release"
"""

from __future__ import annotations

import base64
import glob
import html
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
BRAND = HERE.parent / "brand"
OUT = HERE / "cards"
W, H = 1200, 675

BLUE, BLUE_DEEP, SUN, WHITE, NAVY = "#0070c9", "#005ea8", "#ffd400", "#ffffff", "#0b1b2b"


def _font(name: str) -> str:
    return base64.b64encode((BRAND / "font" / name).read_bytes()).decode()


CSS = f"""
@font-face {{ font-family: Outfit; font-weight: 700; src: url(data:font/woff2;base64,{_font("outfit-latin-700-normal.woff2")}); }}
@font-face {{ font-family: Inter; font-weight: 400; src: url(data:font/woff2;base64,{_font("inter-latin-400-normal.woff2")}); }}
* {{ box-sizing: border-box; margin: 0; }}
body {{ width: {W}px; height: {H}px; overflow: hidden; background: {BLUE}; color: {WHITE};
  font: 26px/1.4 Inter, system-ui, sans-serif; position: relative; }}
.shade {{ position: absolute; right: -{H * 0.62:.0f}px; top: -{H * 0.62:.0f}px; width: {H * 1.24:.0f}px;
  height: {H * 1.24:.0f}px; border-radius: 50%; background: {BLUE_DEEP}; opacity: .55; }}
.wrap {{ position: relative; height: 100%; padding: 64px 72px; display: flex; flex-direction: column; gap: 28px; }}
.kicker {{ color: {SUN}; font: 700 22px Outfit, sans-serif; letter-spacing: .12em; text-transform: uppercase; }}
h1 {{ font: 700 64px/1.05 Outfit, sans-serif; letter-spacing: -.02em; max-width: 1000px; }}
h1 em {{ font-style: normal; color: {SUN}; }}
.sub {{ color: rgba(255,255,255,.85); max-width: 960px; }}
.row {{ display: flex; gap: 28px; flex: 1; min-height: 0; }}
pre {{ flex: 1; background: {NAVY}; border-radius: 18px; padding: 26px 30px; color: #e1eaf3; overflow: hidden;
  font: 21px/1.5 "IBM Plex Mono", "DejaVu Sans Mono", ui-monospace, monospace; white-space: pre; }}
pre .k {{ color: #7cc4ff; }} pre .s {{ color: {SUN}; }} pre .c {{ color: #6e8193; }}
.label {{ font: 700 18px Outfit, sans-serif; letter-spacing: .1em; text-transform: uppercase; color: rgba(255,255,255,.7);
  margin-bottom: 10px; }}
.col {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
ul {{ list-style: none; padding: 0; display: grid; gap: 14px; }}
li {{ padding-left: 34px; position: relative; }}
li::before {{ content: ""; position: absolute; left: 4px; top: .55em; width: 14px; height: 14px; border-radius: 50%; background: {SUN}; }}
.row .col:first-child:not(:only-child) {{ flex: 1.6; }}
.foot {{ display: flex; align-items: center; justify-content: space-between; margin-top: auto; color: rgba(255,255,255,.85); font-size: 22px; }}
.word {{ font: 700 40px Outfit, sans-serif; letter-spacing: -.03em; }}
.word::after {{ content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: {SUN}; margin-left: 3px; }}
"""


def _yaml(src: str) -> str:
    """Escape and lightly highlight YAML: keys blue, strings yellow, comments grey."""
    out = []
    for line in src.strip("\n").splitlines():
        code, _, comment = line.partition(" #")
        esc = html.escape(code)
        if ":" in esc:
            key, sep, rest = esc.partition(":")
            esc = f'<span class="k">{key}</span>{sep}{rest}'
        esc = re.sub(r"&quot;.*?&quot;", lambda m: f'<span class="s">{m.group(0)}</span>', esc)
        out.append(esc + (f'<span class="c"> #{html.escape(comment)}</span>' if comment else ""))
    return "\n".join(out)


def _foot(right: str = "pip install agentl-etl") -> str:
    return f'<div class="foot"><span class="word">agentl</span><span>{html.escape(right)}</span></div>'


def feature(kicker: str, title: str, yaml: str, result: str | None = None, sub: str | None = None) -> str:
    """A feature: title, the YAML that does it and, optionally, what comes out."""
    right = (
        f'<div class="col"><div class="label">Result</div><pre>{html.escape(result)}</pre></div>'
        if result
        else ""
    )
    return (
        f'<div class="kicker">{html.escape(kicker)}</div><h1>{title}</h1>'
        + (f'<p class="sub">{html.escape(sub)}</p>' if sub else "")
        + f'<div class="row"><div class="col"><div class="label">pipeline.yaml</div><pre>{_yaml(yaml)}</pre></div>{right}</div>'
        + _foot()
    )


def release(version: str, title: str, items: list[str]) -> str:
    """A release: version, headline and up to six highlights."""
    lis = "".join(f"<li>{i}</li>" for i in items[:6])
    return f'<div class="kicker">{html.escape(version)}</div><h1>{title}</h1><ul>{lis}</ul>' + _foot(
        "agentl.com.br"
    )


def before_after(kicker: str, title: str, before: str, after: str) -> str:
    """A tip: what people write today next to the agentl version."""
    return (
        f'<div class="kicker">{html.escape(kicker)}</div><h1>{title}</h1>'
        f'<div class="row"><div class="col"><div class="label">Before</div><pre>{html.escape(before)}</pre></div>'
        f'<div class="col"><div class="label">With agentl</div><pre>{_yaml(after)}</pre></div></div>'
        + _foot()
    )


def page(body: str) -> str:
    return (
        f'<html><head><meta charset="utf-8"><style>{CSS}</style></head><body><div class="shade"></div>'
        f'<div class="wrap">{body}</div><script>{FIT}</script></body></html>'
    )


# shrink each code block until it fits its box, so long lines are never cut
FIT = """for (const pre of document.querySelectorAll('pre')) {
  let size = 21;
  while ((pre.scrollWidth > pre.clientWidth || pre.scrollHeight > pre.clientHeight) && size > 12) {
    size -= 1; pre.style.fontSize = size + 'px';
  }
}"""


# ---------------------------------------------------------------- cards
CARDS: dict[str, str] = {
    "release-new-look": release(
        "Today in agentl",
        "Same YAML. <em>New look.</em> Twenty-plus features merged.",
        [
            "Crawl a site, extract with CSS selectors, skip what you already saw",
            "Upserts that keep history (SCD2) or write only what changed",
            "Run outcomes: success, partial, no change, skipped, failed",
            "Golden sets: <code>agentl eval</code> scores a prompt or model change",
            "Concurrency keys, cache TTLs, fetch windows and gap reports",
            "Schedules by publication calendar, with holidays",
        ],
    ),
    "feature-crawl": feature(
        "Scraping, politely",
        "Crawl a site into <em>records</em>",
        """
- id: reports
  use: crawl
  with:
    start: https://example.org/reports
    list: { select: "div.item a", attr: href, next_page: "a.next" }
    detail:
      - use: html.select
        with:
          fields:
            title: h2.title
            price: { select: .price, type: money }
""",
        '{"title": "Q3 report", "price": 1290.0,\n "url": "https://example.org/r/42"}\n\nrobots.txt obeyed · rate limited\nadd skip_seen: new pages only',
    ),
    "feature-changeset": feature(
        "Writes that respect your table",
        "Write <em>only what changed</em>",
        """
- id: save
  use: sqlite.write
  input: rows
  with:
    db: out/facts.db
    table: facts
    key: [country, month]
    mode: changeset        # or scd2 to keep every version
    partition_by: month
    deletes: { when: partition_replaced }
""",
        "inserted   12\nupdated     3\ndeleted     1\nunchanged 984\n\noutcome: changed",
    ),
    "feature-outcomes": feature(
        "Runs that tell the truth",
        "Not every run is <em>success</em> or <em>failed</em>",
        """
outcomes:
  on_transport_error: skipped_upstream
  on_empty: no_change
  on_rejected: partial
  alert_on: [failed, partial]
  exit_codes: { partial: 0, skipped: 75 }
""",
        "success    data written\npartial    some records quarantined\nno_change  source had nothing new\nskipped    source unreachable\nfailed     something broke",
    ),
    "feature-eval": feature(
        "Change prompts without fear",
        "Score a model change <em>before</em> it ships",
        """
- id: classify
  agent: Classify the expense.
  output: { category: str }
  golden:
    file: golden/classify.jsonl
    min_accuracy: 0.95
""",
        "$ agentl eval pipeline.yaml --compare model=small\n\nclassify   current  0.97\n           small    0.91  below 0.95",
    ),
    "tip-cron": before_after(
        "Tip",
        "Stop babysitting cron jobs",
        "*/5 * * * * flock -n /tmp/job.lock \\\n  python job.py || true\n# overlaps? retries? late data?\n# who gets paged?",
        """
concurrency: { key: "{pipeline}", on_overlap: skip }
window: { lookback: 10d, max_span: 31d }
outcomes: { alert_on: [failed, partial] }
""",
    ),
}


def main(prefix: str = "") -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(exist_ok=True)
    exe = (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome") or [None])[0]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        tab = browser.new_page(viewport={"width": W, "height": H})
        for name, body in CARDS.items():
            if not name.startswith(prefix):
                continue
            tab.set_content(page(body))
            tab.wait_for_timeout(150)
            tab.screenshot(path=str(OUT / f"{name}.png"))
            print(OUT / f"{name}.png")
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
