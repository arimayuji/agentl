"""Local dashboard (stdlib only): runs, step status, live Mermaid diagram."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .store import Store

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentl dashboard</title>
<style>
:root{--bg:#fafafa;--fg:#18181b;--muted:#71717a;--card:#fff;--line:#e4e4e7;--ok:#16a34a;--bad:#dc2626;--run:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0b0b0d;--fg:#f4f4f5;--muted:#a1a1aa;--card:#18181b;--line:#27272a}}
body{margin:0;font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:16px;border-bottom:1px solid var(--line)}h1{font-size:18px;margin:0}
main{display:grid;grid-template-columns:minmax(220px,300px) 1fr;gap:16px;padding:16px}
@media (max-width:720px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto}
.run{padding:8px;border-radius:6px;cursor:pointer}.run:hover,.run.sel{background:var(--line)}
.s-succeeded{color:var(--ok)}.s-failed{color:var(--bad)}.s-running{color:var(--run)}
small,.muted{color:var(--muted)}table{border-collapse:collapse;width:100%}
td{padding:4px 8px;border-top:1px solid var(--line);vertical-align:top;font-variant-numeric:tabular-nums}
pre{white-space:pre-wrap;margin:0}
</style></head><body>
<header><h1>agentl</h1><small>local runs, refreshed every 2 s</small></header>
<main><section class="card" id="runs"></section>
<section><div class="card" id="diagram"></div><div class="card" style="margin-top:16px"><table id="events"></table></div></section></main>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
<script>
mermaid.initialize({startOnLoad:false,theme:matchMedia('(prefers-color-scheme: dark)').matches?'dark':'default'});
let sel=null,lastDiagram='';
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function tick(){
  const runs=await (await fetch('/api/runs')).json();
  if(!sel&&runs.length)sel=runs[0].id;
  document.getElementById('runs').innerHTML=runs.map(r=>`<div class="run ${r.id===sel?'sel':''}" onclick="sel='${r.id}';tick()">
    <b>${esc(r.pipeline)}</b> <span class="s-${r.status}">${r.status}</span><br><small>${r.id}</small></div>`).join('')||'<span class="muted">no runs yet: agentl run pipeline.yaml</span>';
  if(!sel)return;
  const d=await (await fetch('/api/runs/'+sel)).json();
  if(d.diagram!==lastDiagram){lastDiagram=d.diagram;const {svg}=await mermaid.render('g'+Date.now(),d.diagram);document.getElementById('diagram').innerHTML=svg;}
  document.getElementById('events').innerHTML=d.events.map(e=>`<tr><td><small>${new Date(e.ts*1000).toLocaleTimeString()}</small></td>
    <td>${esc(e.type)}</td><td>${esc(e.step)}</td><td><pre>${esc(JSON.stringify(e.data))}</pre></td></tr>`).join('');
}
tick();setInterval(tick,2000);
</script></body></html>"""


def _status(events: list[dict]) -> dict[str, str]:
    status: dict[str, str] = {}
    for e in events:
        if e["step"] and e["type"].startswith("step."):
            status[e["step"]] = e["type"].split(".", 1)[1].replace("started", "running")
    return status


def run_detail(store: Store, run_id: str) -> dict | None:
    run = store.run(run_id)
    if not run:
        return None
    events = store.events(run_id)
    diagram = run["diagram"] or "flowchart LR"
    for name, style in (("running", "fill:#dbeafe,stroke:#2563eb"), ("succeeded", "fill:#dcfce7,stroke:#16a34a"),
                        ("failed", "fill:#fee2e2,stroke:#dc2626")):
        diagram += f"\n  classDef {name} {style}"
    for step, st in _status(events).items():
        diagram += f"\n  class {step} {st}"
    return {**run, "diagram": diagram, "events": events}


def serve(store: Store, host: str = "127.0.0.1", port: int = 7070) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: str, ctype: str) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/api/runs":
                return self._send(200, json.dumps(store.runs()), "application/json")
            if path.startswith("/api/runs/"):
                detail = run_detail(store, path.rsplit("/", 1)[1])
                if detail:
                    return self._send(200, json.dumps(detail, default=str), "application/json")
            self._send(404, '{"error":"not found"}', "application/json")

    print(f"agentl dashboard: http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
