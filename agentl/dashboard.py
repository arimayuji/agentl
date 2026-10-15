"""Local dashboard (stdlib only, no CDN): runs, live pipeline graph, KPIs, spend and events."""

from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .store import Store

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentl dashboard</title>
<style>
:root{--bg:#f5f8fb;--panel:#ffffff;--panel2:#eef3f8;--line:#e2e9f0;--fg:#0b1b2b;--muted:#3d5266;--faint:#8aa0b4;
--violet:#0070c9;--indigo:#005ea8;--teal:#009c3b;--green:#009c3b;--red:#d93a2b;--amber:#c99700;--blue:#0070c9;--sun:#ffd400;--sunset:#ff6b3d;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#08131f;--panel:#0d1a28;--panel2:#112134;--line:#1b2d42;--fg:#f2f6fa;--muted:#9fb3c6;--faint:#5a7189;
--violet:#4aa8ff;--indigo:#2f8fe6;--teal:#2fc46a;--green:#2fc46a;--red:#ff7a6b;--amber:#ffd400;--blue:#4aa8ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background:radial-gradient(900px 600px at 15% -10%,#0070c91f,transparent 70%),radial-gradient(800px 600px at 100% 110%,#ffd40026,transparent 70%)}
header{position:relative;display:flex;align-items:center;gap:14px;padding:18px 28px;border-bottom:1px solid var(--line)}
.wm{font:800 26px/1 Outfit,Inter,system-ui,sans-serif;letter-spacing:-.5px;color:var(--fg)}.wm:after{content:"";display:inline-block;width:7px;height:7px;margin-left:2px;border-radius:50%;background:var(--sun)}
.sub{color:var(--muted);font-weight:500}.live{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 #009c3b88;animation:pulse 2s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 8px #009c3b00}100%{box-shadow:0 0 0 0 #009c3b00}}
main{position:relative;display:grid;grid-template-columns:300px 1fr;gap:20px;padding:20px 28px 40px}
@media (max-width:900px){main{grid-template-columns:1fr}}main>*,.grid2>*{min-width:0}
.card{background:color-mix(in srgb,var(--panel) 92%,transparent);border:1px solid var(--line);border-radius:16px;padding:16px;backdrop-filter:blur(6px)}
h2{margin:0 0 12px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:600}
.runs{display:flex;flex-direction:column;gap:8px;max-height:calc(100vh - 140px);overflow:auto}
.run{padding:12px;border-radius:12px;border:1px solid transparent;cursor:pointer;transition:.15s}
.run:hover{background:var(--panel2)}.run.sel{background:var(--panel2);border-color:var(--line)}
.run b{font-size:15px}.meta{display:flex;gap:10px;color:var(--muted);font-size:12px;margin-top:4px;font-variant-numeric:tabular-nums}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;text-transform:uppercase;letter-spacing:.04em}
.pill:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.s-succeeded{color:var(--green);background:#009c3b1a}.s-failed{color:var(--red);background:#d93a2b1a}.s-running{color:var(--blue);background:#0070c91a}
.s-running:before{animation:blink 1s infinite}@keyframes blink{50%{opacity:.25}}
.kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:20px}@media (max-width:1200px){.kpis{grid-template-columns:repeat(3,1fr)}}
.kpi{padding:14px 16px}.kpi .v{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font:700 24px/1.2 Outfit,Inter,system-ui,sans-serif;font-variant-numeric:tabular-nums;margin-top:4px}.kpi .l{color:var(--muted);font-size:12px}
.title{display:flex;align-items:baseline;gap:12px;margin:0 0 16px}.title h1{margin:0;font:700 22px Outfit,Inter,system-ui,sans-serif}.title code{color:var(--muted);font-family:var(--mono);font-size:12px}
.err{margin-bottom:20px;border-color:#d93a2b66;background:#d93a2b0f;color:var(--red);font-family:var(--mono);font-size:13px;white-space:pre-wrap}
svg text{font-family:Inter,system-ui,sans-serif}
.node rect{fill:var(--panel2);stroke:var(--line);stroke-width:1.5;transition:.3s}
.node .name{fill:var(--fg);font-weight:700;font-size:14px}.node .kind{fill:var(--muted);font-family:var(--mono);font-size:11px}.node .stat{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.node.succeeded rect{stroke:var(--green);filter:drop-shadow(0 0 10px #009c3b55)}
.node.failed rect{stroke:var(--red);filter:drop-shadow(0 0 12px #d93a2b77)}
.node.running rect{stroke:var(--blue);animation:glow 1.2s ease-in-out infinite}
@keyframes glow{50%{filter:drop-shadow(0 0 14px #0070c9)}}
.edge{fill:none;stroke:var(--line);stroke-width:2}.edge.done{stroke:var(--faint)}
.edge.flow{stroke:var(--blue);stroke-dasharray:6 6;animation:flow .6s linear infinite}@keyframes flow{to{stroke-dashoffset:-12}}
.graph{overflow:auto;display:flex;justify-content:center;padding:8px 0}
.grid2{display:grid;grid-template-columns:1fr 1.4fr;gap:20px;margin-top:20px}@media (max-width:1200px){.grid2{grid-template-columns:1fr}}
.bar{display:grid;grid-template-columns:110px 1fr 90px;gap:10px;align-items:center;margin:10px 0;font-size:13px}
.bar .track{height:10px;border-radius:5px;background:var(--panel2);overflow:hidden}.bar .fill{height:100%;border-radius:5px;background:linear-gradient(90deg,var(--teal),var(--violet))}
.bar .n{font-family:var(--mono);color:var(--muted);text-align:right;font-size:12px}
.events{max-height:420px;overflow:auto;font-size:13px}
.ev{display:grid;grid-template-columns:64px 128px 92px 1fr;gap:10px;padding:7px 4px;border-bottom:1px solid var(--line);align-items:baseline}
.ev .t{color:var(--faint);font-family:var(--mono);font-size:12px;font-variant-numeric:tabular-nums}
.ev .k{font-weight:600}.ev .k.failed,.ev .k.exceeded{color:var(--red)}.ev .k.rejected{color:var(--amber)}.ev .k.succeeded{color:var(--green)}.ev .k.started{color:var(--muted)}
.ev .st{color:var(--violet);font-family:var(--mono);font-size:12px}.ev .d{color:var(--muted);font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
.tabs{display:flex;gap:6px;margin-left:auto}.tabs button{background:none;border:1px solid var(--line);color:var(--muted);border-radius:8px;padding:2px 10px;font:inherit;font-size:12px;cursor:pointer}
.tabs button.on{color:var(--fg);background:var(--panel2)}
.ps{padding:8px 4px;border-bottom:1px solid var(--line);font-size:13px}.ps .row{display:flex;justify-content:space-between;gap:8px}.ps .rate{font-family:var(--mono)}.ps .why{color:var(--muted);font-size:12px;margin-top:4px;overflow-wrap:anywhere}
.ps .track{height:6px;border-radius:3px;background:var(--red);overflow:hidden;margin-top:6px}.ps .ok{height:100%;background:var(--green)}
.empty{color:var(--muted);padding:40px;text-align:center}.empty code{color:var(--fg)}
</style></head><body>
<header><span class="wm">agentl</span><span class="sub">dashboard</span><span class="live"><span class="dot"></span>live · <span id="count">0</span> runs</span></header>
<main><aside class="card"><h2>Last 30 days</h2><div id="stats"></div><h2>Runs</h2><div class="runs" id="runs"></div></aside><section id="detail"></section></main>
<script>
let sel=null,pinned=false,filter='all';
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=n=>Number(n||0).toLocaleString();
const usd=n=>'$'+Number(n||0).toFixed(Number(n)>=1?2:4);
const dur=s=>s==null?'—':s<60?s.toFixed(1)+'s':Math.floor(s/60)+'m '+Math.round(s%60)+'s';
const ago=t=>{const s=Date.now()/1000-t;return s<60?'just now':s<3600?Math.floor(s/60)+'m ago':s<86400?Math.floor(s/3600)+'h ago':Math.floor(s/86400)+'d ago'};
function layout(g){
  const layer={},pred={};g.nodes.forEach(n=>{pred[n.id]=[]});g.edges.forEach(([a,b])=>pred[b]&&pred[b].push(a));
  const L=id=>layer[id]??(layer[id]=pred[id].length?Math.max(...pred[id].map(L))+1:0);g.nodes.forEach(n=>L(n.id));
  const cols={};g.nodes.forEach(n=>(cols[layer[n.id]]=cols[layer[n.id]]||[]).push(n.id));
  const W=176,H=74,GX=64,GY=26,maxRows=Math.max(...Object.values(cols).map(c=>c.length));const pos={};
  Object.entries(cols).forEach(([c,ids])=>{const off=(maxRows-ids.length)*(H+GY)/2;ids.forEach((id,i)=>pos[id]={x:16+c*(W+GX),y:16+off+i*(H+GY)})});
  return {pos,W,H,width:32+Object.keys(cols).length*(W+GX)-GX,height:32+maxRows*(H+GY)-GY};
}
function graph(d){
  const g=d.graph;if(!g.nodes.length)return '';const {pos,W,H,width,height}=layout(g);
  const edges=g.edges.map(([a,b])=>{const p=pos[a],q=pos[b];if(!p||!q)return '';const x1=p.x+W,y1=p.y+H/2,x2=q.x,y2=q.y+H/2,m=(x1+x2)/2;
    const st=d.steps[b]?.status,cls=st==='running'?'flow':st?'done':'';return `<path class="edge ${cls}" d="M${x1},${y1} C${m},${y1} ${m},${y2} ${x2},${y2}"/>`}).join('');
  const nodes=g.nodes.map(n=>{const p=pos[n.id],st=d.steps[n.id]||{};const r=n.kind==='agent'?H/2:12;
    const stat=st.records!=null?`${num(st.records)} rec · ${dur(st.seconds)}${st.cost_usd?' · '+usd(st.cost_usd):''}`:st.status==='running'?'running…':'';
    return `<g class="node ${st.status||''}" transform="translate(${p.x},${p.y})"><rect width="${W}" height="${H}" rx="${r}"/>
      <text class="name" x="${W/2}" y="27" text-anchor="middle">${esc(n.id)}</text><text class="kind" x="${W/2}" y="45" text-anchor="middle">${esc(n.detail)}</text>
      <text class="stat" x="${W/2}" y="62" text-anchor="middle">${esc(stat)}</text></g>`}).join('');
  return `<svg viewBox="0 0 ${width} ${height}" style="width:100%;max-width:${width}px;height:auto">${edges}${nodes}</svg>`;
}
function render(d){
  const run=d,sp=d.spend,started=run.started,secs=(run.finished||Date.now()/1000)-started;
  const out=Object.values(d.steps).reduce((a,s)=>s.last?a+(s.records||0):a,0);
  const rejected=d.events.filter(e=>e.type==='item.rejected').reduce((a,e)=>a+(e.data.count||0),0);
  const kpi=(l,v,c='')=>`<div class="card kpi"><div class="l">${l}</div><div class="v" style="${c}">${v}</div></div>`;
  const evs=d.events.filter(e=>filter==='all'||/failed|rejected|exceeded/.test(e.type));
  const steps=Object.entries(sp.steps),max=Math.max(1e-9,...steps.map(([,v])=>v.cost_usd||v.input_tokens+v.output_tokens));
  $('#detail').innerHTML=`<div class="title"><h1>${esc(run.pipeline)}</h1><span class="pill s-${run.status}">${run.status}</span><code>${esc(run.id)}</code></div>
  <div class="kpis">${kpi('Duration',dur(secs))}${kpi('Steps',`${Object.values(d.steps).filter(s=>s.status==='succeeded').length}/${d.graph.nodes.length}`)}
  ${kpi('Records out',num(out))}${kpi('Rejected',num(rejected),rejected?'color:var(--amber)':'')}${kpi('Tokens',num(sp.total.input_tokens+sp.total.output_tokens))}${kpi('Cost',usd(sp.total.cost_usd))}</div>
  ${run.error?`<div class="card err">${esc(run.error)}</div>`:''}
  <div class="card"><h2>Pipeline</h2><div class="graph">${graph(d)}</div></div>
  <div class="grid2"><div class="card"><h2>Spend per step</h2>${steps.length?steps.map(([k,v])=>`<div class="bar"><span>${esc(k)}</span>
    <div class="track"><div class="fill" style="width:${100*(v.cost_usd||v.input_tokens+v.output_tokens)/max}%"></div></div><span class="n">${v.cost_usd?usd(v.cost_usd):num(v.input_tokens+v.output_tokens)+' tok'}</span></div>`).join(''):'<div class="empty">No agent steps in this run.</div>'}</div>
  <div class="card"><div style="display:flex;align-items:center"><h2 style="margin:0">Events</h2><div class="tabs"><button class="${filter==='all'?'on':''}" onclick="filter='all';tick()">All</button><button class="${filter==='issues'?'on':''}" onclick="filter='issues';tick()">Issues</button></div></div>
  <div class="events">${evs.map(e=>`<div class="ev"><span class="t">+${(e.ts-started).toFixed(1)}s</span><span class="k ${e.type.split('.')[1]}">${esc(e.type)}</span><span class="st">${esc(e.step||'')}</span>
    <span class="d">${esc(Object.entries(e.data).filter(([k])=>k!=='summary').map(([k,v])=>k+'='+(typeof v==='object'?JSON.stringify(v):v)).join('  '))}</span></div>`).join('')}</div></div></div>`;
}
async function tick(){
  const runs=await (await fetch('/api/runs')).json();$('#count').textContent=runs.length;
  if(!pinned&&runs.length)sel=runs[0].id;  // follow the newest run until the user picks one
  $('#runs').innerHTML=runs.map(r=>`<div class="run ${r.id===sel?'sel':''}" onclick="sel='${r.id}';pinned=true;tick()"><div style="display:flex;justify-content:space-between;align-items:center"><b>${esc(r.pipeline)}</b><span class="pill s-${r.status}">${r.status}</span></div>
    <div class="meta"><span>${ago(r.started)}</span><span>${dur(r.finished?r.finished-r.started:null)}</span></div></div>`).join('')||'<div class="empty">No runs yet.<br><br><code>agentl run pipeline.yaml</code></div>';
  if(!sel){$('#detail').innerHTML='<div class="card empty">Run a pipeline and it shows up here live.</div>';return}
  const r=await fetch('/api/runs/'+sel);if(r.ok)render(await r.json());
}
async function stats(){
  const r=await fetch('/api/stats');if(!r.ok)return;const ps=(await r.json()).pipelines;
  $('#stats').innerHTML=ps.map(p=>{const rate=p.success_rate==null?'—':Math.round(p.success_rate*100)+'%',f=p.failures[0];
    return `<div class="ps"><div class="row"><b>${esc(p.pipeline)}</b><span class="rate">${rate} · ${p.succeeded}/${p.succeeded+p.failed}</span></div>
    <div class="track"><div class="ok" style="width:${(p.success_rate??1)*100}%"></div></div>
    ${f?`<div class="why">${f.count}× ${f.step?'['+esc(f.step)+'] ':''}${esc(f.reason)}</div>`:''}</div>`}).join('')||'<div class="empty">No runs in the last 30 days.</div>';
}
tick();setInterval(tick,1000);stats();setInterval(stats,15000);
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
    for name, style in (
        ("running", "fill:#dbeafe,stroke:#2563eb"),
        ("succeeded", "fill:#dcfce7,stroke:#16a34a"),
        ("failed", "fill:#fee2e2,stroke:#dc2626"),
    ):
        diagram += f"\n  classDef {name} {style}"
    for step, st in _status(events).items():
        diagram += f"\n  class {step} {st}"
    return {
        **run,
        "diagram": diagram,
        "graph": (g := parse_graph(run["diagram"] or "")),
        "steps": step_stats(events, g),
        "events": events,
        "spend": spend(events),
    }


_NODE = re.compile(r'^\s*(\w+)\W*"(\w+)<br/><small>(.*?)</small>"')
_EDGE = re.compile(r"^\s*(\w+) --> (\w+)\s*$")


def parse_graph(diagram: str) -> dict:
    """Nodes and edges from the Mermaid text the runner stores (see graph.mermaid)."""
    nodes, edges = [], []
    for line in diagram.splitlines():
        if m := _NODE.match(line):
            detail = m.group(3)
            kind = detail if detail in ("agent", "check", "wait") else "fn" if ":" in detail else "use"
            nodes.append({"id": m.group(1), "detail": m.group(3), "kind": kind})
        elif m := _EDGE.match(line):
            edges.append([m.group(1), m.group(2)])
    return {"nodes": nodes, "edges": edges}


def step_stats(events: list[dict], graph: dict | None = None) -> dict:
    """Status, records, seconds and cost per step; `last` marks steps nothing else consumes."""
    out: dict[str, dict] = {}
    for e in events:
        step = e["step"]
        if not step or not e["type"].startswith("step."):
            continue
        d = e["data"]
        st = out.setdefault(step, {})
        st["status"] = e["type"].split(".", 1)[1].replace("started", "running")
        for k in ("records", "seconds", "cost_usd", "error"):
            if k in d:
                st[k] = d[k]
        if "written" in d:  # a writer: show records written, not its one summary record per file
            st["records"] = d["written"]
    consumed = {a for a, _ in (graph or {}).get("edges", [])}
    for step, st in out.items():
        st["last"] = step not in consumed
    return out


def spend(events: list[dict]) -> dict:
    """Tokens and cost per agent step, plus the run total, from the run's events."""
    steps: dict[str, dict] = {}
    for e in events:
        if e["step"] and e["type"] in ("step.succeeded", "step.failed") and "input_tokens" in e["data"]:
            d = e["data"]
            steps[e["step"]] = {k: d.get(k, 0) for k in ("input_tokens", "output_tokens", "cost_usd")}
    total = {
        "input_tokens": sum(s["input_tokens"] for s in steps.values()),
        "output_tokens": sum(s["output_tokens"] for s in steps.values()),
        "cost_usd": round(sum(s["cost_usd"] for s in steps.values()), 6),
    }
    return {"steps": steps, "total": total}


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
            if path == "/api/stats":
                from . import stats

                report = stats.compute(store, time.time() - 30 * 86400)
                return self._send(200, json.dumps(report, default=str), "application/json")
            if path.startswith("/api/runs/"):
                detail = run_detail(store, path.rsplit("/", 1)[1])
                if detail:
                    return self._send(200, json.dumps(detail, default=str), "application/json")
            self._send(404, '{"error":"not found"}', "application/json")

    print(f"agentl dashboard: http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
