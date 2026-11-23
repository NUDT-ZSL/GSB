"""Local interactive HTML workbench report (no network services).

Generates a single self-contained HTML file with the run's report data
embedded as JSON.  Tabs: schedule plan, budget ledger, per-group
reliability curves (SVG), metrics table, error attribution, lineage.
"""

from __future__ import annotations

import json
import os

TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>校准工作台 · Calibration Workbench</title>
<style>
 body{font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
      margin:0;background:#f5f6f8;color:#1f2329}
 header{background:#1f2329;color:#fff;padding:14px 24px}
 header h1{font-size:18px;margin:0}
 header .meta{font-size:12px;color:#9aa0a6;margin-top:4px}
 nav{display:flex;gap:4px;background:#fff;padding:0 24px;border-bottom:1px solid #e0e0e0}
 nav button{border:0;background:none;padding:12px 14px;font-size:14px;cursor:pointer;
            border-bottom:2px solid transparent}
 nav button.active{border-bottom-color:#1a73e8;color:#1a73e8;font-weight:600}
 main{padding:20px 24px}
 .tab{display:none}.tab.active{display:block}
 table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
 th,td{border:1px solid #e5e7eb;padding:6px 10px;text-align:left}
 th{background:#f1f3f4}
 .card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:16px}
 .ok{color:#188038}.bad{color:#d93025}.muted{color:#5f6368}
 .grid{display:flex;flex-wrap:wrap;gap:16px}
 .chart{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:12px}
 code{background:#f1f3f4;padding:1px 4px;border-radius:4px;font-size:12px}
</style>
</head>
<body>
<header>
 <h1>概率校准诊断工作台</h1>
 <div class="meta" id="hdr"></div>
</header>
<nav id="nav"></nav>
<main>
 <section class="tab" id="tab-plan"><div class="card"><h3>调度计划</h3><div id="plan"></div></div></section>
 <section class="tab" id="tab-budget"><div class="card"><h3>预算账本</h3><div id="budget"></div></div></section>
 <section class="tab" id="tab-curves"><div class="grid" id="curves"></div></section>
 <section class="tab" id="tab-metrics"><div class="card"><h3>指标汇总</h3><div id="metrics"></div></div></section>
 <section class="tab" id="tab-errors"><div class="card"><h3>误差归因 / 异常</h3><div id="errors"></div></div></section>
 <section class="tab" id="tab-lineage"><div class="card"><h3>追溯链</h3><div id="lineage"></div></div></section>
</main>
<script id="report-data" type="application/json">__DATA__</script>
<script>
const R = JSON.parse(document.getElementById('report-data').textContent);
const TABS = [["plan","调度计划"],["budget","预算账本"],["curves","校准曲线"],
              ["metrics","指标"],["errors","误差归因"],["lineage","追溯链"]];
const nav = document.getElementById('nav');
TABS.forEach(([id,label],i)=>{
  const b=document.createElement('button'); b.textContent=label;
  b.onclick=()=>{document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.getElementById('tab-'+id).classList.add('active');
    nav.querySelectorAll('button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');};
  if(i===0)b.classList.add('active'); nav.appendChild(b);
});
document.getElementById('tab-plan').classList.add('active');
document.getElementById('hdr').textContent =
  `config_version=${R.config_version} · 状态=${R.status} · 变更类型=${R.change.kind} · ` +
  `快照 plan=v${R.snapshots.plan.version} calibrators=v${R.snapshots.calibrators.version} ` +
  `metrics=v${R.snapshots.metrics.version}`;

function table(el, cols, rows){
  let h='<table><tr>'+cols.map(c=>`<th>${c}</th>`).join('')+'</tr>';
  rows.forEach(r=>{h+='<tr>'+r.map(c=>`<td>${c}</td>`).join('')+'</tr>';});
  el.innerHTML=h+'</table>';
}

{
  const p=R.plan;
  const rows=p.combos.map(c=>{
    const led=p.ledger.find(l=>l.calibrator===c.calibrator&&l.group===c.group)||{};
    return [c.index, c.calibrator, c.group,
            p.worker_assignment[String(c.index)] ?? '-',
            led.decision||'-', led.code||'-'];
  });
  const el=document.getElementById('plan');
  el.innerHTML=`<p class="muted">workers=${p.workers} · batch_size=${p.batch_size} · `+
    `批次=${JSON.stringify(p.batches)}</p>`;
  const t=document.createElement('div'); el.appendChild(t);
  table(t,['#','校准器','group','worker','决策','业务码'],rows);
}

{
  const b=R.plan.budget;
  const el=document.getElementById('budget');
  el.innerHTML=`<p>迭代: <b>${b.est_iters}</b> / ${b.max_total_iters} · `+
    `驻留内存峰值: <b>${b.peak_resident_bytes}</b> / ${b.max_resident_bytes} 字节</p>`;
  const t=document.createElement('div'); el.appendChild(t);
  table(t,['校准器','group','决策','估计迭代','估计内存','业务码','缺口','说明'],
    R.plan.ledger.map(l=>[l.calibrator,l.group,l.decision,
      l.est_iters??'-',l.est_mem_bytes??'-',l.code||'-',
      l.deficit?JSON.stringify(l.deficit):'-',l.message]));
}

{
  const colors={platt:'#1a73e8',isotonic:'#188038',histogram:'#d93025'};
  const host=document.getElementById('curves');
  R.groups.forEach(g=>{
    const div=document.createElement('div'); div.className='chart';
    const W=300,H=300,P=36;
    let svg=`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`;
    svg+=`<line x1="${P}" y1="${H-P}" x2="${W-P}" y2="${P}" stroke="#bbb" stroke-dasharray="4"/>`;
    svg+=`<rect x="${P}" y="${P}" width="${W-2*P}" height="${H-2*P}" fill="none" stroke="#ddd"/>`;
    svg+=`<text x="${W/2}" y="${H-6}" font-size="11" text-anchor="middle">mean predicted</text>`;
    svg+=`<text x="12" y="${H/2}" font-size="11" transform="rotate(-90 12 ${H/2})" text-anchor="middle">empirical</text>`;
    const X=v=>P+v*(W-2*P), Y=v=>H-P-v*(H-2*P);
    R.calibrators.forEach(cal=>{
      const key=`${cal}:${g}`; const combo=R.per_combo[key]; if(!combo)return;
      const bins=combo.metrics.reliability.bins.filter(b=>b.count>0);
      const pts=bins.map(b=>`${X(b.mean_pred).toFixed(1)},${Y(b.mean_true).toFixed(1)}`).join(' ');
      svg+=`<polyline points="${pts}" fill="none" stroke="${colors[cal]||'#333'}" stroke-width="2"/>`;
      bins.forEach(b=>{svg+=`<circle cx="${X(b.mean_pred)}" cy="${Y(b.mean_true)}" r="3" fill="${colors[cal]||'#333'}"/>`;});
    });
    svg+='</svg>';
    let legend=R.calibrators.map(cal=>{
      const key=`${cal}:${g}`; const combo=R.per_combo[key];
      if(!combo)return '';
      return `<div><span style="color:${colors[cal]||'#333'}">●</span> ${cal} `+
        `<span class="muted">ECE=${combo.metrics.ece.toFixed(4)} Brier=${combo.metrics.brier.toFixed(4)}</span></div>`;
    }).join('');
    div.innerHTML=`<b>group: ${g}</b>${svg}${legend}`;
    host.appendChild(div);
  });
}

{
  const rows=[];
  Object.keys(R.per_combo).sort().forEach(k=>{
    const m=R.per_combo[k].metrics;
    rows.push([k, m.n, m.ece.toFixed(6), m.brier.toFixed(6),
               m.conventions.ece_binning+'/'+m.conventions.ece_weight]);
  });
  Object.keys(R.overall).sort().forEach(cal=>{
    const m=R.overall[cal];
    rows.push([`<b>${cal}:__overall__</b>`, m.n, m.ece.toFixed(6), m.brier.toFixed(6),'']);
  });
  table(document.getElementById('metrics'),['组合','N','ECE','Brier','ECE口径'],rows);
}

{
  const el=document.getElementById('errors');
  if(!R.errors.length){el.innerHTML='<p class="ok">无 worker 异常。</p>';}
  else{
    const t=document.createElement('div'); el.appendChild(t);
    table(t,['业务码','阶段','组合','信息'],
      R.errors.map(e=>[e.code,e.stage,
        `${e.details.calibrator||''}:${e.details.group||''}`,e.message]));
  }
  const reused=R.node_log.filter(n=>n.status==='reused').length;
  const computed=R.node_log.filter(n=>n.status==='computed').length;
  el.innerHTML+=`<p class="muted">节点复用 ${reused} · 重算 ${computed} · `+
    `变更分类: ${R.change.kind} (${R.change.details})</p>`;
}

{
  table(document.getElementById('lineage'),
    ['组合','config_version','ECE','Brier','节点路径','缓存键'],
    R.lineage.map(l=>[`${l.calibrator}:${l.group}`,l.config_version,
      l.ece.toFixed(6),l.brier.toFixed(6),
      `<code>${l.node_path.join(' → ')}</code>`,
      `<code>${(l.cache_keys.metrics||'').slice(0,16)}…</code>`]));
}
</script>
</body>
</html>
"""


def render_report(report: dict, out_dir) -> str:
    path = os.path.join(out_dir, "report.html")
    data = json.dumps(report, sort_keys=True, ensure_ascii=False)
    html = TEMPLATE.replace("__DATA__", data.replace("</", "<\\/"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path
