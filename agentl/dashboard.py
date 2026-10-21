"""Local dashboard (stdlib only, no CDN): runs, live pipeline graph, KPIs, spend and events,
grouped by run labels. `export` writes the same page as static files for any bucket."""

from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .labels import parse_filters
from .store import Store

DAY = 86400.0

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentl dashboard</title>
<style>
:root{--bg:#f5f8fb;--panel:#ffffff;--panel2:#eef3f8;--line:#e2e9f0;--fg:#0b1b2b;--muted:#3d5266;--faint:#8aa0b4;
--violet:#0070c9;--indigo:#005ea8;--teal:#003f7d;--green:#009c3b;--red:#d93a2b;--amber:#c99700;--blue:#0070c9;--sun:#ffd400;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#08131f;--panel:#0d1a28;--panel2:#112134;--line:#1b2d42;--fg:#f2f6fa;--muted:#9fb3c6;--faint:#5a7189;
--violet:#4aa8ff;--indigo:#2f8fe6;--teal:#9fd0f5;--green:#2fc46a;--red:#ff7a6b;--amber:#ffd400;--blue:#4aa8ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background:radial-gradient(900px 600px at 15% -10%,#0070c91f,transparent 70%),radial-gradient(800px 600px at 100% 110%,#ffd40026,transparent 70%)}
header{position:relative;overflow:hidden;display:flex;align-items:center;gap:14px;padding:18px 28px;background:#0070c9;color:#fff}header:before{content:"";position:absolute;right:-60px;top:-110px;width:240px;height:240px;border-radius:50%;background:#005ea8;opacity:.6}header>*{position:relative}
.wm{font:800 26px/1 Outfit,Inter,system-ui,sans-serif;letter-spacing:-.5px;color:#fff}.wm:after{content:"";display:inline-block;width:7px;height:7px;margin-left:2px;border-radius:50%;background:var(--sun)}
.sub{color:#ffffffcc;font-weight:500}.live{margin-left:auto;display:flex;align-items:center;gap:8px;color:#ffffffdd;font-size:13px}
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
.s-succeeded{color:var(--green);background:#009c3b1a}.s-failed{color:var(--red);background:#d93a2b1a}.s-running{color:var(--blue);background:#0070c91a}.s-skipped,.s-no_change{color:var(--muted);background:#8881}.s-partial{color:var(--amber);background:#e8a3001a}.s-success{color:var(--green);background:#009c3b1a}
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
.bar .track{height:10px;border-radius:5px;background:var(--panel2);overflow:hidden}.bar .fill{height:100%;border-radius:5px;background:linear-gradient(90deg,var(--violet),var(--sun))}
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
.ck{display:grid;grid-template-columns:140px 1fr auto;gap:10px;align-items:center;padding:5px 4px;border-bottom:1px solid var(--line);font-size:13px}.ck .st{color:var(--violet);font-family:var(--mono);font-size:12px}
.r-passed{color:var(--green);background:#009c3b1a}.r-failed{color:var(--red);background:#d93a2b1a}.r-warn,.r-reject{color:var(--amber);background:#e8a3001a}
.empty{color:var(--muted);padding:40px;text-align:center}.empty code{color:var(--fg)}
.ctl{display:flex;gap:6px;margin-bottom:10px}.ctl select,.ctl input{flex:1;min-width:0;background:var(--panel2);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:4px 8px;font:inherit;font-size:12px}
.grp{border-left:3px solid var(--sun);padding:6px 0 6px 10px;margin:8px 0}.grp .gh{display:flex;justify-content:space-between;gap:8px}.grp .gh b{color:var(--indigo)}
.grp .agg{display:flex;flex-wrap:wrap;gap:4px 10px;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;margin:2px 0 4px}.grp .agg .bad{color:var(--red);font-weight:600}
.gp{display:flex;justify-content:space-between;gap:8px;padding:3px 6px;border-radius:6px;cursor:pointer;font-size:13px}.gp:hover{background:var(--panel2)}.gp span{color:var(--muted);font-size:12px}
.lbl{display:inline-block;font-size:11px;color:var(--indigo);background:#0070c914;border-radius:6px;padding:0 6px;margin:2px 4px 0 0;font-family:var(--mono)}
</style></head><body>
<header><span class="wm">agentl</span><span class="sub">dashboard</span><span class="live"><span class="dot" id="dot"></span><span id="mode">live</span> · <span id="count">0</span> runs</span></header>
<main><aside class="card"><h2>Groups</h2><div class="ctl"><select id="by" onchange="by=this.value;remember();groupsView()" aria-label="Group by label"></select>
<input id="flt" placeholder="team=sales, domain=orders" onchange="flt=this.value;remember();refresh()" aria-label="Filter by labels"></div><div id="groups"></div>
<h2>Last 30 days</h2><div id="stats"></div><h2>Runs</h2><div class="runs" id="runs"></div></aside><section id="detail"></section></main>
<script>
let sel=null,pinned=false,filter='all',by='',flt='',rows=[];
const S=window.AGENTL_STATIC||null;  // set by `agentl dashboard export`: no server, data inlined
const $=s=>document.querySelector(s);
try{const m=new URLSearchParams(location.hash.slice(1));by=m.get('by')||'';flt=m.get('label')||''}catch(e){}
function remember(){try{history.replaceState(null,'','#'+new URLSearchParams({by,label:flt}))}catch(e){}}
const filters=()=>flt.split(',').map(x=>x.trim()).filter(x=>x.includes('=')).map(x=>[x.split('=')[0].trim(),x.slice(x.indexOf('=')+1).trim()]);
const match=l=>filters().every(([k,v])=>l&&l[k]!=null&&(l[k]===v||String(l[k]).split(',').includes(v)));
const qs=()=>filters().map(([k,v])=>'label='+encodeURIComponent(k+'='+v)).join('&');
async function api(path){  // the live server, or the snapshot inlined by the export
  if(!S){const r=await fetch(path+(path.includes('/runs/')?'':(qs()?'?'+qs():'')));return r.ok?r.json():null}
  if(path==='/api/runs'||path==='/api/summary')return S.runs.filter(r=>match(r.labels));
  if(path==='/api/stats')return S.stats;
  return S.details[path.split('/').pop()]||null;
}
const labels=l=>Object.entries(l||{}).map(([k,v])=>`<span class="lbl">${esc(k)}=${esc(v)}</span>`).join('');
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
function chainCard(c){
  if(!c||(!c.parent&&!c.children.length))return '';
  const link=(r,rel)=>`<div class="ck" style="cursor:pointer" onclick="sel='${esc(r.run_id)}';pinned=true;tick()"><span class="st">${rel}</span><span><b>${esc(r.pipeline)}</b> <code>${esc(r.run_id)}</code></span><span class="pill s-${esc(r.status)}">${esc(r.status)}</span></div>`;
  return `<div class="card" style="margin-bottom:20px"><h2>Chain</h2>${c.parent?link(c.parent,'started by'):''}${c.children.map(r=>link(r,'started')).join('')}</div>`;
}
function render(d){
  const run=d,sp=d.spend,started=run.started,secs=(run.finished||Date.now()/1000)-started;
  const out=Object.values(d.steps).reduce((a,s)=>s.last?a+(s.records||0):a,0);
  const rejected=d.events.filter(e=>e.type==='item.rejected').reduce((a,e)=>a+(e.data.count||0),0);
  const kpi=(l,v,c='')=>`<div class="card kpi"><div class="l">${l}</div><div class="v" style="${c}">${v}</div></div>`;
  const evs=d.events.filter(e=>filter==='all'||/failed|rejected|exceeded/.test(e.type));
  const steps=Object.entries(sp.steps),max=Math.max(1e-9,...steps.map(([,v])=>v.cost_usd||v.input_tokens+v.output_tokens));
  $('#detail').innerHTML=`<div class="title"><h1>${esc(run.pipeline)}</h1>${labels(run.labels)}<span class="pill s-${run.status}">${run.status}</span>${run.outcome&&run.outcome!=='success'&&run.outcome!=='failed'?`<span class="pill s-${run.outcome}">${run.outcome}</span>`:''}<code>${esc(run.id)}</code></div>
  <div class="kpis">${kpi('Duration',dur(secs))}${kpi('Steps',`${Object.values(d.steps).filter(s=>s.status==='succeeded').length}/${d.graph.nodes.length}`)}
  ${kpi('Records out',num(out))}${kpi('Rejected',num(rejected),rejected?'color:var(--amber)':'')}${kpi('Tokens',num(sp.total.input_tokens+sp.total.output_tokens))}${kpi('Cost',usd(sp.total.cost_usd))}</div>
  ${run.error?`<div class="card err">${esc(run.error)}</div>`:''}
  ${chainCard(d.chain)}
  <div class="card"><h2>Pipeline</h2><div class="graph">${graph(d)}</div></div>
  ${(d.checks||[]).length?`<div class="card" style="margin-top:20px"><h2>Checks</h2>${d.checks.map(c=>`<div class="ck"><span class="st">${esc(c.step)}</span><span>${esc(c.rule)}</span><span class="pill r-${esc(c.result)}">${esc(c.result)}</span></div>`).join('')}</div>`:''}
  <div class="grid2"><div class="card"><h2>Spend per step</h2>${steps.length?steps.map(([k,v])=>`<div class="bar"><span>${esc(k)}</span>
    <div class="track"><div class="fill" style="width:${100*(v.cost_usd||v.input_tokens+v.output_tokens)/max}%"></div></div><span class="n">${v.cost_usd?usd(v.cost_usd):num(v.input_tokens+v.output_tokens)+' tok'}</span></div>`).join(''):'<div class="empty">No agent steps in this run.</div>'}</div>
  <div class="card"><div style="display:flex;align-items:center"><h2 style="margin:0">Events</h2><div class="tabs"><button class="${filter==='all'?'on':''}" onclick="filter='all';tick()">All</button><button class="${filter==='issues'?'on':''}" onclick="filter='issues';tick()">Issues</button></div></div>
  <div class="events">${evs.map(e=>`<div class="ev"><span class="t">+${(e.ts-started).toFixed(1)}s</span><span class="k ${e.type.split('.')[1]}">${esc(e.type)}</span><span class="st">${esc(e.step||'')}</span>
    <span class="d">${esc(Object.entries(e.data).filter(([k])=>k!=='summary').map(([k,v])=>k+'='+(typeof v==='object'?JSON.stringify(v):v)).join('  '))}</span></div>`).join('')}</div></div></div>`;
}
function group(rs,key){  // same aggregates as agentl.dashboard.groups
  const now=S?S.generated:Date.now()/1000,out={};
  [...rs].sort((a,b)=>a.started-b.started).forEach(r=>{const v=key?((r.labels||{})[key]??'(none)'):'all';
    const g=out[v]=out[v]||{value:v,runs:0,records:0,c7:0,c30:0,p:{}};g.runs++;g.records+=r.records||0;const age=now-r.started;
    if(age<=30*86400)g.c30+=r.cost_usd||0;if(age<=7*86400)g.c7+=r.cost_usd||0;
    const p=g.p[r.pipeline]=g.p[r.pipeline]||{pipeline:r.pipeline,runs:0};p.runs++;p.last=r.status;p.id=r.id;p.labels=r.labels});
  return Object.values(out).sort((a,b)=>(a.value==='(none)')-(b.value==='(none)')||String(a.value).localeCompare(b.value));
}
function groupsView(){
  const keys=[...new Set(rows.flatMap(r=>Object.keys(r.labels||{})))].sort();
  $('#by').innerHTML='<option value="">No grouping</option>'+keys.map(k=>`<option ${k===by?'selected':''}>${esc(k)}</option>`).join('');
  $('#groups').innerHTML=group(rows,by).map(g=>{const ps=Object.values(g.p).sort((a,b)=>a.pipeline.localeCompare(b.pipeline)),bad=ps.filter(p=>p.last==='failed').length;
    return `<div class="grp"><div class="gh"><b>${esc(by?g.value:'All pipelines')}</b><span class="pill s-${bad?'failed':'succeeded'}">${bad?bad+' failing':'ok'}</span></div>
    <div class="agg"><span>${ps.length} pipelines</span><span>${num(g.runs)} runs</span><span>${num(g.records)} records</span><span title="spend 7 / 30 days">${usd(g.c7)} / ${usd(g.c30)}</span>${bad?`<span class="bad">${bad} failing</span>`:''}</div>
    ${ps.map(p=>`<div class="gp" onclick="sel='${esc(p.id)}';pinned=true;tick()"><b>${esc(p.pipeline)}</b><span>${p.runs} runs <span class="pill s-${p.last}">${p.last}</span></span></div>`).join('')}</div>`}).join('')||'<div class="empty">No runs match.</div>';
}
async function refresh(){rows=await api('/api/summary')||[];groupsView();tick();stats()}
async function tick(){
  const runs=await api('/api/runs')||[];$('#count').textContent=runs.length;
  if(!pinned&&runs.length)sel=runs[0].id;  // follow the newest run until the user picks one
  $('#runs').innerHTML=runs.map(r=>`<div class="run ${r.id===sel?'sel':''}" onclick="sel='${r.id}';pinned=true;tick()"><div style="display:flex;justify-content:space-between;align-items:center"><b>${esc(r.pipeline)}</b><span class="pill s-${r.outcome||r.status}">${r.outcome||r.status}</span></div>
    <div class="meta"><span>${ago(r.started)}</span><span>${dur(r.finished?r.finished-r.started:null)}</span></div><div>${labels(r.labels)}</div></div>`).join('')||'<div class="empty">No runs yet.<br><br><code>agentl run pipeline.yaml</code></div>';
  if(!sel){$('#detail').innerHTML='<div class="card empty">Run a pipeline and it shows up here live.</div>';return}
  const d=await api('/api/runs/'+sel);if(d)render(d);
}
async function stats(){
  const st=await api('/api/stats');if(!st)return;const ps=st.pipelines.filter(p=>!S||match(p.labels));
  $('#stats').innerHTML=ps.map(p=>{const rate=p.success_rate==null?'—':Math.round(p.success_rate*100)+'%',f=p.failures[0];
    return `<div class="ps"><div class="row"><b>${esc(p.pipeline)}</b><span class="rate">${rate} · ${p.succeeded}/${p.succeeded+p.failed}</span></div>
    <div class="track"><div class="ok" style="width:${(p.success_rate??1)*100}%"></div></div>
    ${f?`<div class="why">${f.count}× ${f.step?'['+esc(f.step)+'] ':''}${esc(f.reason)}</div>`:''}
    ${p.review&&p.review.pending?`<div class="why">${p.review.pending} waiting for review (agentl review list)</div>`:''}</div>`}).join('')||'<div class="empty">No runs in the last 30 days.</div>';
}
$('#flt').value=flt;
if(S){$('#mode').textContent='snapshot '+new Date(S.generated*1000).toLocaleString();$('#dot').style.animation='none';refresh()}
else{refresh();setInterval(tick,1000);setInterval(()=>{api('/api/summary').then(r=>{rows=r||[];groupsView()});stats()},15000)}
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
        "checks": check_results(events),
        "chain": chain_links(events),
    }


def chain_links(events: list[dict]) -> dict:
    """The runs this one is chained to under `agentl serve`: the parent that started it and the
    children it started (latest status of each)."""
    parent = None
    children: dict[str, dict] = {}
    for e in events:
        if e["type"] == "chain.parent":
            parent = e["data"]
        elif e["type"] == "chain.child" and e["data"].get("run_id"):
            children[e["data"]["run_id"]] = e["data"]
    return {"parent": parent, "children": list(children.values())}


def check_results(events: list[dict]) -> list[dict]:
    """Each check rule's result in this run (passed, warn, reject or failed), per check step."""
    return [
        {"step": e["step"], "rule": rule, "result": result}
        for e in events
        if e["type"] in ("check.passed", "check.failed")
        for rule, result in (e["data"].get("rules") or {}).items()
    ]


_NODE = re.compile(r'^\s*(\w+)\W*"(\w+)<br/><small>(.*?)</small>"')
_EDGE = re.compile(r"^\s*(\w+) -->(?:\|[^|]*\|)? (\w+)\s*$")


def parse_graph(diagram: str) -> dict:
    """Nodes and edges from the Mermaid text the runner stores (see graph.mermaid)."""
    nodes, edges = [], []
    for line in diagram.splitlines():
        if m := _NODE.match(line):
            detail = m.group(3)
            kind = (
                "agent"
                if detail == "route"
                else detail
                if detail in ("agent", "check", "wait")
                else "fn"
                if ":" in detail
                else "use"
            )
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


def summary(store: Store, since: float, filters: dict[str, str] | None = None) -> list[dict]:
    """One row per run started since `since`, newest first: status, labels, records written and
    cost. The dashboard groups these rows; no record payloads."""
    runs = store.history(since, labels=filters)
    ids = {r["id"] for r in runs}
    written: dict[str, int] = {}
    cost: dict[str, float] = {}
    for e in store.events_of(since, ("step.succeeded", "run.succeeded", "run.failed")):
        if e["run_id"] not in ids:
            continue
        d = e["data"] or {}
        if e["type"] == "step.succeeded":
            written[e["run_id"]] = written.get(e["run_id"], 0) + int(d.get("written") or 0)
        else:
            cost[e["run_id"]] = float(d.get("cost_usd") or 0)
    rows = [
        {
            **{k: r[k] for k in ("id", "pipeline", "status", "outcome", "started", "finished")},
            "error": redact(r["error"]),
            "labels": r["labels"],
            "records": written.get(r["id"], 0),
            "cost_usd": round(cost.get(r["id"], 0.0), 6),
        }
        for r in runs
    ]
    return sorted(rows, key=lambda r: -r["started"])


def groups(rows: list[dict], by: str | None, now: float | None = None) -> list[dict]:
    """Aggregates per value of label `by` (one group for all when None): pipelines, failing
    pipelines (latest run failed), runs, records written and spend over 7 and 30 days, then the
    pipelines of the group with their runs. Runs without the label go under "(none)"."""
    now = now or time.time()
    out: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r["started"]):
        key = (r["labels"] or {}).get(by, "(none)") if by else "all"
        g = out.setdefault(
            key,
            {"key": by, "value": key, "runs": 0, "records": 0, "cost_7d": 0.0, "cost_30d": 0.0, "_p": {}},
        )
        g["runs"] += 1
        g["records"] += r["records"]
        age = now - r["started"]
        if age <= 30 * DAY:
            g["cost_30d"] += r["cost_usd"]
        if age <= 7 * DAY:
            g["cost_7d"] += r["cost_usd"]
        p = g["_p"].setdefault(r["pipeline"], {"pipeline": r["pipeline"], "runs": [], "labels": {}})
        p["runs"].insert(0, r["id"])
        p["last_status"], p["last_run"], p["labels"] = r["status"], r["id"], r["labels"]
    result = []
    for g in out.values():
        pipelines = sorted(g.pop("_p").values(), key=lambda p: p["pipeline"])
        g["pipelines"] = pipelines
        g["pipeline_count"] = len(pipelines)
        g["failing"] = sum(1 for p in pipelines if p["last_status"] == "failed")
        g["cost_7d"], g["cost_30d"] = round(g["cost_7d"], 6), round(g["cost_30d"], 6)
        result.append(g)
    return sorted(result, key=lambda g: (g["value"] == "(none)", g["value"]))


def label_keys(rows: list[dict]) -> list[str]:
    return sorted({k for r in rows for k in (r["labels"] or {})})


_SECRET_KEY = re.compile(r"(pass(word)?|secret|token|api[_-]?key|auth|credential|dsn|cookie)", re.I)
_URL_CREDS = re.compile(r"(\w+://)[^/\s:@]+(?::[^/\s@]*)?@")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[\w\-.~+/=]{8,}")
_KEYLIKE = re.compile(r"\b(sk|pk|rk|xox[abpr]|ghp|gho|github_pat|AKIA)[\w\-]{12,}")


def redact(value: Any) -> Any:
    """What leaves the store in an export: secret-looking keys dropped to "***", credentials in
    URLs and bearer tokens or API-key-shaped strings masked, sample `summary` text removed."""
    if isinstance(value, str):
        value = _URL_CREDS.sub(r"\1***@", value)
        value = _BEARER.sub(r"\1 ***", value)
        return _KEYLIKE.sub("***", value)
    if isinstance(value, dict):
        return {
            k: "***" if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v)
            for k, v in value.items()
            if k != "summary"
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def export(
    store: Store,
    out_dir: str | Path,
    since: float,
    filters: dict[str, str] | None = None,
    max_runs: int = 200,
    now: float | None = None,
) -> dict[str, Any]:
    """Write a static dashboard to `out_dir`: index.html with the data inlined (works opened from
    disk) and the same data as JSON snapshots (runs.json, groups.json, stats.json, runs/<id>.json).
    Run metadata, stats and events only, redacted; never records, samples or rejected rows."""
    from . import stats

    now = now or time.time()
    out = Path(out_dir)
    (out / "runs").mkdir(parents=True, exist_ok=True)
    rows = summary(store, since, filters)
    details = {}
    for r in rows[:max_runs]:
        d = run_detail(store, r["id"])
        if d:
            d.pop("diagram", None)
            details[r["id"]] = redact(d)
    report = redact(stats.compute(store, since, now=now, labels=filters))
    data = {
        "generated": now,
        "since": since,
        "filters": filters or {},
        "runs": rows,
        "details": details,
        "stats": report,
    }
    files = {
        "runs.json": rows,
        "stats.json": report,
        "groups.json": {k: groups(rows, k, now) for k in label_keys(rows)},
    }
    for name, body in files.items():
        (out / name).write_text(json.dumps(body, default=str, indent=1), encoding="utf-8")
    for run_id, d in details.items():
        (out / "runs" / f"{run_id}.json").write_text(json.dumps(d, default=str), encoding="utf-8")
    blob = json.dumps(data, default=str).replace("</", "<\\/")  # no early </script>
    page = PAGE.replace("<script>", f"<script>window.AGENTL_STATIC={blob};</script>\n<script>", 1)
    (out / "index.html").write_text(page, encoding="utf-8")
    return {"dir": str(out), "runs": len(rows), "details": len(details)}


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
            url = urlparse(self.path)
            path = url.path
            try:
                filters = parse_filters(parse_qs(url.query).get("label"))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}), "application/json")
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/api/runs":
                return self._send(200, json.dumps(store.runs(labels=filters)), "application/json")
            if path == "/api/summary":
                rows = summary(store, time.time() - 30 * DAY, filters)
                return self._send(200, json.dumps(rows, default=str), "application/json")
            if path == "/api/groups":
                by = (parse_qs(url.query).get("by") or [None])[0]
                rows = summary(store, time.time() - 30 * DAY, filters)
                return self._send(200, json.dumps(groups(rows, by), default=str), "application/json")
            if path == "/api/stats":
                from . import stats

                report = stats.compute(store, time.time() - 30 * DAY, labels=filters)
                return self._send(200, json.dumps(report, default=str), "application/json")
            if path.startswith("/api/runs/"):
                detail = run_detail(store, path.rsplit("/", 1)[1])
                if detail:
                    return self._send(200, json.dumps(detail, default=str), "application/json")
            self._send(404, '{"error":"not found"}', "application/json")

    print(f"agentl dashboard: http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
