from __future__ import annotations

from typing import Any

from flask import Flask, jsonify, request

from .canonical import json_safe
from .db import open_existing, read_transaction
from .diagnostics import build_status, diagnose
from .errors import WorkbenchError


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Experiment Trace Workbench</title>
<style>
:root{--bg:#f5f2ea;--ink:#17211b;--muted:#607067;--line:#d8d2c5;--card:#fffdf8;
--green:#176b4d;--amber:#9b5d12;--red:#a5332f;--blue:#2e5c8a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}
header{padding:22px 28px 14px;border-bottom:2px solid var(--ink);background:#ece7db}
h1{font-size:21px;margin:0 0 5px}small,.muted{color:var(--muted)}
main{padding:20px 28px 48px;max-width:1500px;margin:auto}
.bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:18px}
button{background:var(--ink);color:#fff;border:0;padding:8px 13px;cursor:pointer}
button.secondary{background:var(--card);color:var(--ink);border:1px solid var(--line)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.card{background:var(--card);border:1px solid var(--line);padding:14px}
.span4{grid-column:span 4}.span5{grid-column:span 5}.span6{grid-column:span 6}
.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}
h2{font-size:15px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.08em}
.pill{display:inline-block;border:1px solid var(--line);padding:1px 7px;margin:1px;
background:#f8f5ed;white-space:nowrap}.ok{color:var(--green);border-color:#9dc7b5}
.bad{color:var(--red);border-color:#e2aaa6}.warn{color:var(--amber);border-color:#dfc08a}
.tree{white-space:pre-wrap;line-height:1.7}.node{border-left:3px solid var(--blue);
padding:7px 10px;margin:6px 0;background:#f9f7f1}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 6px;
border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}
td.num{font-variant-numeric:tabular-nums;text-align:right}
details{margin:5px 0}summary{cursor:pointer}.scroll{max-height:480px;overflow:auto}
.mono{font-family:inherit}.up{color:var(--red)}.down{color:var(--blue)}.flat{color:var(--green)}
code{background:#eee9de;padding:1px 4px}@media(max-width:900px){.span4,.span5,.span6,
.span7,.span8{grid-column:span 12}main{padding:16px}}
</style>
</head>
<body>
<header><h1>Experiment Trace Workbench</h1>
<small>两代快照合并 · 注入逻辑时钟 · 失效闭包 · 全量真值诊断</small></header>
<main>
<div class="bar"><button id="reload">重新读取</button><button class="secondary" id="diag">运行诊断</button>
<span id="stamp" class="muted">尚未读取</span></div>
<div class="grid">
<section class="card span4"><h2>Snapshot</h2><div id="snapshot"></div></section>
<section class="card span4"><h2>Logical State</h2><div id="state"></div></section>
<section class="card span4"><h2>Diagnosis</h2><div id="diagnosis"></div></section>
<section class="card span5"><h2>Lineage</h2><div id="lineage" class="tree scroll"></div></section>
<section class="card span7"><h2>Invalidation Closure</h2><div id="invalidations" class="scroll"></div></section>
<section class="card span12"><h2>Metrics and Windows</h2><div id="metrics" class="scroll"></div></section>
</div>
</main>
<script>
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;',
'>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const es = v => esc(typeof v === 'object' ? JSON.stringify(v) : v);
async function get(path){const r=await fetch(path);const j=await r.json();if(!r.ok||j.ok===false)
throw new Error(JSON.stringify(j));return j;}
function tree(nodes){
  const by={}; nodes.forEach(n=>by[n.experiment_id]=n);
  const children={}; nodes.forEach(n=>{(children[n.parent_id||'']??=[]).push(n)});
  for(const k in children) children[k].sort((a,b)=>a.experiment_id.localeCompare(b.experiment_id));
  const lines=[]; const walk=(n,d)=>{lines.push('  '.repeat(d)+'└─ '+n.experiment_id+
    '  ['+esc(n.dataset_version)+']  '+n.metrics.map(m=>esc(m.metric)).join(', '));
    (children[n.experiment_id]||[]).forEach(c=>walk(c,d+1));};
  (children['']||[]).forEach(n=>walk(n,0)); return lines.join('\\n');
}
function renderStatus(s){
 document.querySelector('#snapshot').innerHTML =
  `<div><b>${esc(s.snapshot_version)}</b></div><div>来源格式裁决：${esc(s.snapshot.classification)}</div>
   <div>追踪代数：${esc(s.snapshot.trace_generation)} / 下限 ${esc(s.snapshot.trace_generation_floor)}</div>
   <div>上一版本：${esc(s.snapshot.previous_snapshot_version||'—')}</div>
   <div class="muted">${esc(s.snapshot.package_digest)}</div>`;
 document.querySelector('#state').innerHTML =
  `<div>逻辑时钟水位 <b>${esc(s.logical_clock_watermark_ms)}</b> ms</div>
   <div>数据代 <b>${esc(s.data_epoch)}</b></div><div>窗口 <b>${esc(s.window_ms)}</b> ms</div>
   <div>待恢复事件 <span class="pill ${s.pending_invalidation_events?'bad':'ok'}">
   ${esc(s.pending_invalidation_events)}</span></div>`;
 document.querySelector('#lineage').textContent = tree(s.experiments);
 const rows=[];
 for(const e of s.experiments) for(const m of e.metrics){
   const summary=m.summary?`μ ${esc(m.summary.mean)} · n=${esc(m.summary.record_count)}`:'无提交摘要';
   const src=m.source?`${esc(m.source.source_format)}:${esc(m.source.source_version)}`:'无快照条目';
   const cells=m.windows.map(w=>{const c=w.baseline_comparison; return `<tr>
    <td class="num">${esc(w.window_number)}</td><td class="num">${esc(w.mean)}</td>
    <td class="num">${esc(w.min)} … ${esc(w.max)}</td>
    <td>${c?`<span class="${esc(c.direction)}">${esc(c.direction)}</span> Δ ${esc(c.delta??'—')} (${esc(c.relative_delta??'—')})`:'—'}</td>
    <td>${w.precision_sensitive?'<span class="pill warn">精度敏感</span>':''}</td>
    <td class="muted">${esc(w.summary_digest.slice(0,26))}…</td></tr>`}).join('');
   rows.push(`<details ${e.experiment_id==='exp-base'?'open':''}><summary>
    <b>${esc(e.experiment_id)}</b> / ${esc(m.metric)} <span class="pill">${src}</span>
    <span class="pill">${summary}</span>${m.precision_sensitive?'<span class="pill warn">precision-sensitive</span>':''}</summary>
    <table><thead><tr><th>窗口号</th><th>均值</th><th>范围</th><th>相对基线</th><th>精度</th><th>摘要</th></tr></thead>
    <tbody>${cells||'<tr><td colspan="6" class="muted">尚无已提交窗口</td></tr>'}</tbody></table></details>`);
 }
 document.querySelector('#metrics').innerHTML=rows.join('')||'<span class="muted">无指标</span>';
 document.querySelector('#invalidations').innerHTML=s.invalidations.length?s.invalidations.map(ev=>
  `<details><summary><b>${esc(ev.patch_id)}</b> <span class="pill ${ev.status==='complete'?'ok':'warn'}">
   ${esc(ev.status)}</span> epoch ${esc(ev.data_epoch)} · ${ev.items.length} metrics</summary>
   ${ev.items.map(i=>`<div class="node"><b>${esc(i.experiment_id)} / ${esc(i.metric)}</b>
   <div>${esc(i.reason)}</div><div class="muted">链：${esc(i.source_chain.join(' → '))}</div>
   <div class="muted">否决复用候选 ${esc(i.rejected_reuse_candidates.length)} 条</div></div>`).join('')}
   </details>`).join(''):'<span class="muted">无失效事件</span>';
 document.querySelector('#stamp').textContent='已读取 '+s.snapshot_version;
}
function renderDiag(d){
 const el=document.querySelector('#diagnosis');
 if(d.consistent){el.innerHTML='<span class="pill ok">一致</span><div>增量状态与全量真值逐窗口一致。</div>';}
 else{const f=d.first_divergence;el.innerHTML=`<span class="pill bad">不一致</span>
  <div>分歧 ${d.divergences.length} 条</div><div>首个：${esc(f.experiment_id??'—')} /
  ${esc(f.metric??'—')} / 窗口 ${esc(f.window_number??'—')}</div>
  <div class="muted">快照 ${esc(f.snapshot_version)}</div>
  <details open><summary>首个分歧数值</summary><pre>${es({incremental:f.incremental_value,truth:f.truth_value})}</pre></details>`;}
}
async function loadAll(){
 try{renderStatus(await get('/api/status'));document.querySelector('#diagnosis').innerHTML=
  '<span class="muted">点击“运行诊断”进行全量真值对照。</span>';}
 catch(e){document.querySelector('#stamp').textContent='读取失败：'+e.message;}
}
document.querySelector('#reload').onclick=loadAll;
document.querySelector('#diag').onclick=async()=>{try{renderDiag(await get('/api/diagnose'));}
catch(e){document.querySelector('#diagnosis').innerHTML='<span class="pill bad">诊断失败</span><pre>'+esc(e.message)+'</pre>';}};
loadAll();
</script>
</body></html>"""


def create_app(db_path: str) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.json.sort_keys = True

    def with_connection(function):
        connection = open_existing(app.config["DB_PATH"], readonly=True)
        try:
            with read_transaction(connection):
                return function(connection)
        finally:
            connection.close()

    @app.get("/")
    def index():
        return INDEX_HTML

    @app.get("/health")
    def health():
        status = with_connection(build_status)
        return jsonify(
            {
                "ok": True,
                "snapshot_version": status["snapshot_version"],
                "pending_invalidation_events": status["pending_invalidation_events"],
            }
        )

    @app.get("/api/status")
    def api_status():
        return jsonify(json_safe(with_connection(build_status)))

    @app.get("/api/diagnose")
    def api_diagnose():
        now_raw = request.args.get("now_ms")
        window_raw = request.args.get("window_ms")
        now_ms = None if now_raw is None else int(now_raw)
        window_ms = None if window_raw is None else int(window_raw)
        result = with_connection(
            lambda connection: diagnose(
                connection, now_ms=now_ms, window_ms=window_ms
            )
        )
        return jsonify(json_safe(result))

    @app.get("/api/lineage")
    def api_lineage():
        status = with_connection(build_status)
        nodes = [
            {
                "experiment_id": experiment["experiment_id"],
                "parent_id": experiment["parent_id"],
                "dataset_version": experiment["dataset_version"],
                "metrics": [metric["metric"] for metric in experiment["metrics"]],
            }
            for experiment in status["experiments"]
        ]
        edges = [
            {"from": node["parent_id"], "to": node["experiment_id"]}
            for node in nodes
            if node["parent_id"] is not None
        ]
        return jsonify({"ok": True, "nodes": nodes, "edges": edges})

    @app.errorhandler(WorkbenchError)
    def handle_workbench_error(exc: WorkbenchError):
        return jsonify(json_safe(exc.as_dict())), 422

    @app.errorhandler(ValueError)
    def handle_value_error(exc: ValueError):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": "E_QUERY_ARGUMENT",
                        "message": str(exc),
                        "context": {},
                    },
                }
            ),
            400,
        )

    return app

