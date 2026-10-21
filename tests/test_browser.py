"""browser.scrape against local pages that need JavaScript. Skipped when Playwright or a
browser is not available (CI installs both in the `browser` job)."""

import http.server
import threading
from functools import partial

import pytest

from agentl.registry import get

sync_api = pytest.importorskip("playwright.sync_api")

REPORT = """<html><body><div id="app">loading...</div><script>
setTimeout(() => { document.getElementById('app').innerHTML =
  '<table class="results"><tr><th>id</th><th>total</th></tr><tr><td>1</td><td>10</td></tr>' +
  '<tr><td>2</td><td> 20 </td></tr></table>'; }, 300);
</script></body></html>"""

LOGIN = """<html><body><form onsubmit="event.preventDefault();
  if (document.getElementById('u').value === 'ana' && document.getElementById('p').value === 's3cret') {
    document.body.innerHTML = '<a id=\\'dl\\' href=\\'/export.csv\\' download>export</a>' +
      '<ul><li><a href=\\'/a\\'>A</a></li></ul><p class=\\'msg\\'>welcome ana</p>';
  }">
<input id="u"><input id="p" type="password"><button id="go">go</button></form></body></html>"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    (root / "report.html").write_text(REPORT)
    (root / "login.html").write_text(LOGIN)
    (root / "export.csv").write_text("id,v\n1,a\n")
    for i in (1, 2):
        (root / f"item{i}.html").write_text(f"<html><body><p class='name'>item {i}</p></body></html>")
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None  # type: ignore[attr-defined]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with sync_api.sync_playwright() as pw:
            import os

            pw.chromium.launch(executable_path=os.environ.get("AGENTL_BROWSER_EXECUTABLE")).close()
    except Exception as e:  # no browser installed
        srv.shutdown()
        pytest.skip(f"no browser: {e}")
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_table_rendered_by_javascript(site):
    rows = get("browser.scrape")(
        None, url=f"{site}/report.html", wait_for="table.results", selector="table.results"
    )
    assert [(r["id"], r["total"]) for r in rows] == [("1", "10"), ("2", "20")]
    assert rows[0]["_url"].endswith("/report.html")


def test_login_steps_text_links_and_download(site):
    login = [
        {"goto": f"{site}/login.html"},
        {"fill": "#u", "value": "ana"},
        {"fill": "#p", "value": "s3cret"},
        {"click": "#go"},
        {"wait_for": "p.msg"},
    ]
    text = get("browser.scrape")(None, steps=login, selector="p.msg", extract="text")
    assert text[0]["text"] == "welcome ana"
    links = get("browser.scrape")(None, steps=login, selector="ul", extract="links")
    assert links[0]["text"] == "A" and links[0]["href"].endswith("/a")
    files = get("browser.scrape")(None, steps=login, download="#dl")
    assert files[0]["filename"] == "export.csv"
    assert get("file.read")(files)[0]["v"] == "a"


def test_one_visit_per_upstream_record(site):
    rows = get("browser.scrape")(
        [{"n": 1}, {"n": 2}], url=site + "/item{n}.html", selector="p.name", extract="text"
    )
    assert [r["text"] for r in rows] == ["item 1", "item 2"]


def test_bad_extract():
    with pytest.raises(ValueError, match="extract"):
        get("browser.scrape")(None, url="http://x", extract="pdf")


def test_evidence_store_keeps_html_and_screenshot(site, tmp_path):
    rows = get("browser.scrape")(
        None,
        url=f"{site}/item1.html",
        selector="p.name",
        extract="text",
        evidence={"raw": True, "screenshot": True, "path": str(tmp_path)},
    )
    from agentl import evidence

    shot = rows[0]["_evidence_screenshot"]
    png = evidence.Backend(str(tmp_path)).get(shot, ".png")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    html = evidence.Backend(str(tmp_path)).get(rows[0]["_evidence"], ".body")
    assert b"item 1" in html
