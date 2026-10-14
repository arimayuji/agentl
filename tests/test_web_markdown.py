"""Pages as clean Markdown, evidence of what each run saw, retry on empty (#87)."""

import datetime as dt
import http.server
import os
import threading
from pathlib import Path

import pytest

pytest.importorskip("trafilatura")

from agentl.connectors import web
from agentl.registry import get

ARTICLE = """<html><head><title>Fuel prices</title></head><body>
<nav><a href="/">Home</a> | <a href="/about">About us</a></nav>
<main><h1>Fuel prices in October</h1>
<p>Prices went <b>up</b> 3% this week. See the <a href="/report.pdf">full report</a>.</p>
<h2>By state</h2><ul><li>RJ: 6.10</li><li>SP: 5.90</li></ul>
<table><tr><th>State</th><th>Price</th></tr><tr><td>RJ</td><td>6.10</td></tr></table>
<p>Prices are collected every Monday from 400 stations across the country by the agency.</p></main>
<footer>Cookie settings. Privacy policy.</footer><script>var tracking = 1;</script></body></html>"""

EMPTY = "<html><body><div id='app'></div></body></html>"


def test_markdown_keeps_content_and_drops_the_chrome(tmp_path):
    (tmp_path / "fuel.html").write_text(ARTICLE)
    [page] = get("html.read")(None, path=str(tmp_path / "fuel.html"), format="markdown")
    md = page["markdown"]
    assert page["title"].startswith("Fuel prices") and md.startswith("# Fuel prices in October")
    assert "**up**" in md and "- RJ: 6.10" in md and "| State | Price |" in md
    assert "About us" not in md and "Cookie" not in md and "tracking" not in md


def test_tables_stay_the_default(tmp_path):
    (tmp_path / "fuel.html").write_text(ARTICLE)
    rows = get("html.read")(None, path=str(tmp_path / "fuel.html"))
    assert rows == [{"table": 0, "State": "RJ", "Price": "6.10"}]
    with pytest.raises(ValueError, match="format must be"):
        get("html.read")(None, path=str(tmp_path / "fuel.html"), format="pdf")


class _Flaky(http.server.BaseHTTPRequestHandler):
    """Serves an empty shell on the first request, the article afterwards."""

    hits = 0
    always_empty = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).hits += 1
        body = (EMPTY if type(self).hits == 1 or self.always_empty else ARTICLE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def flaky():
    _Flaky.hits, _Flaky.always_empty = 0, False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Flaky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/fuel.html"
    srv.shutdown()
    srv.server_close()


def test_retry_if_empty_and_evidence_from_a_server(flaky, tmp_path):
    ev = tmp_path / "evidence"
    [page] = get("html.read")(None, url=flaky, format="markdown", retry_if_empty=2, evidence=str(ev))
    assert _Flaky.hits == 2 and page["_source"] == flaky
    assert "[full report](http://127.0.0.1" in page["markdown"]  # links made absolute
    html, md = page["_evidence_html"], page["_evidence_markdown"]
    assert html.startswith(str(ev / dt.date.today().isoformat())) and "127-0-0-1" in html
    assert Path(html).read_text() == ARTICLE and Path(md).read_text() == page["markdown"]


def test_empty_after_retries_is_reported_with_its_evidence(flaky, tmp_path):
    _Flaky.always_empty = True
    [page] = get("html.read")(None, url=flaky, format="markdown", retry_if_empty=1, evidence=str(tmp_path))
    assert _Flaky.hits == 2
    assert page["markdown"] == "" and Path(page["_evidence_html"]).read_text() == EMPTY


def test_evidence_retention(tmp_path):
    old = dt.datetime(2026, 9, 1, 8)
    web.save_evidence(str(tmp_path), "https://a.example/x", "<p>old</p>", "old", b"png", now=old)
    web.save_evidence(str(tmp_path), "https://a.example/x", "<p>new</p>", now=dt.datetime(2026, 10, 13))
    (tmp_path / "notes").mkdir()  # not a date folder: left alone
    assert web.prune_evidence(str(tmp_path), keep_days=7, today=dt.date(2026, 10, 14)) == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-10-13", "notes"]


# ---------------------------------------------------------------- browser
LATE = """<html><head><title>Late</title></head><body><div id="app"></div><script>
setTimeout(() => { document.getElementById('app').innerHTML = '<article><h1>Rendered late</h1>' +
  '<p>This paragraph only exists after JavaScript ran, which is why a plain HTTP read misses it.</p>' +
  '</article>'; }, 200);
</script></body></html>"""


def test_browser_markdown_with_screenshot(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        with sync_api.sync_playwright() as pw:
            pw.chromium.launch(executable_path=os.environ.get("AGENTL_BROWSER_EXECUTABLE")).close()
    except Exception as e:
        pytest.skip(f"no browser: {e}")
    (tmp_path / "late.html").write_text(LATE)
    url = (tmp_path / "late.html").as_uri()
    [page] = get("browser.scrape")(
        None, url=url, extract="markdown", wait_for="article", evidence=str(tmp_path / "ev")
    )
    assert page["markdown"].startswith("# Rendered late") and page["_url"] == url
    assert Path(page["_evidence_screenshot"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
