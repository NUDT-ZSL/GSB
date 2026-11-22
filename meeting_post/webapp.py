"""Zero-dependency local HTTP dashboard."""

from __future__ import annotations

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .app_state import DashboardState
from .errors import MeetingError


def render_dashboard_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>会议同传后处理控制台</title>
<style>
:root{
  --bg:#0b1018; --panel:#111925; --panel2:#0e1621; --line:#223247;
  --text:#e8eef7; --muted:#8ea0b8; --cyan:#44d7ce; --blue:#69a7ff;
  --amber:#f6bd60; --red:#ff6b7d; --green:#5ad39a; --violet:#b38cff;
  --shadow:0 18px 55px rgba(0,0,0,.28);
}
*{box-sizing:border-box}
body{margin:0;background:
  radial-gradient(circle at 10% -10%,rgba(68,215,206,.12),transparent 32rem),
  radial-gradient(circle at 95% 2%,rgba(105,167,255,.12),transparent 30rem),
  var(--bg);
  color:var(--text);font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:14px;line-height:1.45;min-height:100vh}
button,select,input{font:inherit}
button{cursor:pointer}
.shell{max-width:1540px;margin:0 auto;padding:24px 26px 48px}
header{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:20px}
.eyebrow{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--cyan);font-weight:700}
h1{font-size:28px;line-height:1.15;margin:4px 0 5px;font-weight:720;letter-spacing:-.03em}
.subtitle{color:var(--muted);max-width:850px}
.status-cluster{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end;min-width:280px}
.chip{border:1px solid var(--line);background:var(--panel);border-radius:999px;padding:7px 12px;
  color:var(--muted);white-space:nowrap;font-size:12px}
.chip strong{color:var(--text);font-weight:650}
.chip.ok{border-color:rgba(90,211,154,.45);color:var(--green)}
.chip.bad{border-color:rgba(255,107,125,.52);color:var(--red)}
.grid{display:grid;gap:16px}
.metrics{grid-template-columns:repeat(6,minmax(0,1fr));margin-bottom:16px}
.metric,.panel{background:linear-gradient(180deg,rgba(255,255,255,.025),transparent),var(--panel);
  border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow)}
.metric{padding:14px 15px;min-height:90px}
.metric .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.metric .value{font-size:23px;font-weight:700;margin-top:8px;letter-spacing:-.03em}
.metric .note{font-size:11px;color:var(--muted);margin-top:3px;overflow:hidden;text-overflow:ellipsis}
.panel{padding:17px 18px}
.panel h2{font-size:14px;margin:0 0 12px;font-weight:680;letter-spacing:.01em}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:13px}
.panel-head h2{margin:0}
.two-col{grid-template-columns:minmax(0,1.65fr) minmax(330px,.8fr)}
.section{margin-top:16px}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.btn{background:#182536;border:1px solid #2d4058;color:var(--text);border-radius:9px;padding:8px 12px;
  transition:.15s;font-size:12px}
.btn:hover{border-color:var(--blue);transform:translateY(-1px)}
.btn.primary{background:rgba(68,215,206,.13);border-color:rgba(68,215,206,.55);color:#bffcf7}
.btn.warn{background:rgba(246,189,96,.11);border-color:rgba(246,189,96,.45);color:#ffd898}
.btn.danger{background:rgba(255,107,125,.1);border-color:rgba(255,107,125,.45);color:#ffbdc5}
.btn.small{padding:5px 9px;font-size:11px}
.select{background:#0c141f;color:var(--text);border:1px solid #2d4058;border-radius:8px;padding:7px 9px}
.check{display:inline-flex;gap:6px;align-items:center;color:var(--muted);font-size:12px}
.canvas-wrap{position:relative;background:#09111a;border:1px solid #1c2a3b;border-radius:12px;overflow:hidden}
canvas{display:block;width:100%;height:245px}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:11px;margin-top:9px}
.legend span::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}
.legend .truth::before{background:rgba(105,167,255,.42)}
.legend .sep::before{background:var(--cyan)}
.legend .mask::before{background:rgba(246,189,96,.8)}
.hash-list{display:grid;gap:7px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:10.5px;color:var(--muted)}
.hash-row{display:grid;grid-template-columns:90px 1fr;gap:7px;align-items:baseline}
.hash-row span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#b7c7db}
.tabs{display:flex;gap:6px;margin-bottom:9px}
.tab{background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:7px;padding:5px 9px;font-size:11px}
.tab.active{color:#aef8f3;border-color:rgba(68,215,206,.55);background:rgba(68,215,206,.1)}
pre{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;line-height:1.55;
  white-space:pre-wrap;word-break:break-word;background:#09111a;border:1px solid #1b2a3b;border-radius:10px;
  padding:12px;margin:0;max-height:245px;overflow:auto;color:#c8d8ea}
.view-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:11px}
.view-card{background:var(--panel2);border:1px solid #1d2c3f;border-radius:11px;padding:10px}
.view-card h3{font-size:12px;margin:0 0 7px;display:flex;justify-content:space-between;gap:6px;align-items:center}
.badge{font-size:9px;border:1px solid var(--line);border-radius:999px;padding:2px 6px;color:var(--muted)}
.badge.match{color:var(--green);border-color:rgba(90,211,154,.42)}
table{width:100%;border-collapse:collapse;font-size:11.5px}
th{color:var(--muted);font-weight:600;text-align:left;border-bottom:1px solid var(--line);padding:8px 8px;white-space:nowrap}
td{border-bottom:1px solid rgba(34,50,71,.62);padding:8px;vertical-align:top}
tbody tr:hover{background:rgba(105,167,255,.04)}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.muted{color:var(--muted)}
.tiny{font-size:10px}
.ok{color:var(--green)} .bad{color:var(--red)} .warn{color:var(--amber)} .cyan{color:var(--cyan)}
.table-wrap{max-height:310px;overflow:auto;border:1px solid rgba(34,50,71,.65);border-radius:10px}
.table-wrap table thead{position:sticky;top:0;background:#111925;z-index:1}
.audit-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.audit-box{background:var(--panel2);border:1px solid #1d2c3f;border-radius:11px;padding:11px}
.audit-box h3{font-size:12px;margin:0 0 8px}
.audit-list{display:grid;gap:5px;max-height:170px;overflow:auto}
.audit-row{display:flex;justify-content:space-between;gap:10px;font-size:10.5px;border-bottom:1px solid rgba(34,50,71,.5);padding:4px 0}
.audit-row span:first-child{color:var(--muted)}
.check-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.check-item{background:var(--panel2);border:1px solid #1d2c3f;border-radius:9px;padding:9px}
.check-item .name{font-size:10.5px;color:var(--muted)}
.check-item .result{font-weight:700;margin-top:3px}
.error-box{background:rgba(255,107,125,.06);border:1px solid rgba(255,107,125,.25);border-radius:10px;
  padding:10px 12px;margin-bottom:8px}
.error-code{font-family:ui-monospace,monospace;color:#ff9daa;font-weight:700;font-size:11px}
.error-detail{font-size:10.5px;color:var(--muted);word-break:break-word}
.toast{position:fixed;right:22px;bottom:22px;max-width:440px;background:#1a2432;border:1px solid #354a64;
  border-radius:12px;padding:12px 14px;box-shadow:0 20px 60px rgba(0,0,0,.45);z-index:9;display:none}
.toast.show{display:block}
.toast.ok{border-color:rgba(90,211,154,.55)} .toast.bad{border-color:rgba(255,107,125,.55)}
.empty{padding:22px;text-align:center;color:var(--muted)}
@media(max-width:1180px){.metrics{grid-template-columns:repeat(3,1fr)}.two-col{grid-template-columns:1fr}
  .view-grid{grid-template-columns:1fr}.audit-grid{grid-template-columns:1fr}}
@media(max-width:700px){.shell{padding:16px}.metrics{grid-template-columns:repeat(2,1fr)}
  header{flex-direction:column}.status-cluster{justify-content:flex-start}.check-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="shell">
  <header>
    <div>
      <div class="eyebrow">Local deterministic post-production</div>
      <h1>会议同传后处理控制台</h1>
      <div class="subtitle">固定种子合成 · 样本/帧唯一基准 · 三版本配置互操作 · 滞回分段与说话人掩码 · 注册声纹重识别 · 整体快照防护</div>
    </div>
    <div class="status-cluster" id="statusCluster"></div>
  </header>

  <div class="grid metrics" id="metrics"></div>

  <div class="grid two-col">
    <section class="panel">
      <div class="panel-head">
        <h2>会话波形与时间轴</h2>
        <div class="toolbar">
          <span class="badge" id="waveMeta"></span>
          <button class="btn small" onclick="act('reprocess')">重跑全链路</button>
        </div>
      </div>
      <div class="canvas-wrap"><canvas id="waveCanvas"></canvas></div>
      <div class="legend">
        <span class="truth">真值语音段</span>
        <span class="sep">分离段（颜色按说话人）</span>
        <span class="mask">帧能量</span>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>操作台</h2><span class="badge">仅本机</span></div>
      <div class="toolbar" style="margin-bottom:10px">
        <select class="select" id="configSelect">
          <option value="v1">v1 位置列</option>
          <option value="v2">v2 字段行</option>
          <option value="v3" selected>v3 结构化</option>
          <option value="v3-stress">v3 有损压力样例</option>
        </select>
        <button class="btn primary" onclick="act('switch_config',{version:val('configSelect')})">切换并重算</button>
      </div>
      <div class="toolbar" style="margin-bottom:10px">
        <select class="select" id="migrateSelect">
          <option value="v1">降级到 v1</option>
          <option value="v2">迁移到 v2</option>
          <option value="v3">升级到 v3</option>
        </select>
        <label class="check"><input type="checkbox" id="lossyCheck"> 允许显式有损</label>
        <button class="btn" onclick="act('migrate',{target:val('migrateSelect'),lossy:document.getElementById('lossyCheck').checked})">执行迁移</button>
      </div>
      <div class="toolbar" style="margin-bottom:12px">
        <button class="btn warn" onclick="act('replace_snapshots')">整体替换快照</button>
        <button class="btn danger" onclick="act('probe_rollback')">回退探针</button>
        <button class="btn danger" onclick="act('probe_corruption')">损坏探针</button>
      </div>
      <div class="hash-list" id="hashList"></div>
    </section>
  </div>

  <section class="panel section">
    <div class="panel-head">
      <h2>当前配置与三视图</h2>
      <div class="status-cluster" id="configMeta"></div>
    </div>
    <div class="tabs" id="viewTabs"></div>
    <div class="view-grid" id="configViews"></div>
  </section>

  <div class="grid two-col section">
    <section class="panel">
      <div class="panel-head"><h2>分离段与逐段身份</h2><span class="badge" id="segmentBadge"></span></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>段</th><th>掩码声纹</th><th>指派身份</th><th>置信区间</th><th>帧区间</th><th>样本区间</th><th>毫秒交付</th><th>真值</th></tr></thead>
          <tbody id="segmentRows"></tbody>
        </table>
      </div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>毫秒 ↔ 帧 ↔ 样本对照</h2><span class="badge">half-up · 0.001 ms</span></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>段</th><th>起始帧</th><th>结束帧</th><th>起始样本</th><th>结束样本</th><th>反算帧</th></tr></thead>
          <tbody id="timebaseRows"></tbody>
        </table>
      </div>
    </section>
  </div>

  <section class="panel section">
    <div class="panel-head"><h2>迁移补全与丢弃明细</h2><span class="badge" id="migrationBadge"></span></div>
    <div class="audit-grid" id="migrationGrid"></div>
  </section>

  <div class="grid two-col section">
    <section class="panel">
      <div class="panel-head"><h2>快照版本与内容校验和</h2><span class="badge">整体替换</span></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>快照项</th><th>版本</th><th>内容校验和</th><th>业务载荷</th></tr></thead>
          <tbody id="snapshotRows"></tbody>
        </table>
      </div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>跨模块一致性自检</h2><span class="badge" id="selfCheckBadge"></span></div>
      <div class="check-grid" id="checkGrid"></div>
      <div id="checkIssues" style="margin-top:10px"></div>
    </section>
  </div>

  <div class="grid two-col section">
    <section class="panel">
      <div class="panel-head"><h2>最近业务错误码</h2><span class="badge" id="errorBadge"></span></div>
      <div id="errorList"></div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>操作事件</h2><span class="badge">结果不可静默沿用</span></div>
      <div id="eventList"></div>
    </section>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const COLORS = ["#44d7ce","#69a7ff","#f6bd60","#b38cff","#5ad39a","#ff8ea1"];
let appState = null;
let activeView = "v3";

function val(id){ return document.getElementById(id).value; }
function esc(value){
  return String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
}
function fmt(value, digits=3){
  return Number(value).toLocaleString("zh-CN",{maximumFractionDigits:digits,minimumFractionDigits:0});
}
function shortHash(value, n=18){ return value ? value.slice(0,n)+"…" : "—"; }

async function loadState(){
  const response = await fetch("/api/state", {cache:"no-store"});
  if(!response.ok){ throw new Error("状态读取失败 HTTP "+response.status); }
  appState = await response.json();
  document.title = appState.title;
  render();
}

async function act(action, payload={}){
  try{
    const response = await fetch("/api/action",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({action,...payload})
    });
    const data = await response.json();
    await loadState();
    if(data.ok){
      showToast(action+" 已完成", true);
    }else{
      const err = data.error || {};
      showToast((err.code||"E_UNKNOWN")+" · "+(err.message||"操作失败"), false);
    }
  }catch(error){ showToast(error.message,false); }
}

function showToast(message, ok){
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.className = "toast show "+(ok?"ok":"bad");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(()=>{toast.className="toast";},4200);
}

function render(){
  const s = appState;
  renderStatus(s);
  renderMetrics(s);
  renderHashes(s);
  renderConfig(s);
  drawWaveform(s);
  renderSegments(s);
  renderTimebase(s);
  renderMigrations(s);
  renderSnapshots(s);
  renderSelfCheck(s);
  renderErrors(s);
  renderEvents(s);
}

function renderStatus(s){
  const status=s.status;
  const ok=status.self_check_ok;
  document.getElementById("statusCluster").innerHTML = [
    `<span class="chip ${ok?"ok":"bad"}">自检 <strong>${ok?"PASS":"FAIL"}</strong></span>`,
    `<span class="chip">配置 <strong>${esc(status.current_version)}</strong></span>`,
    `<span class="chip">样例 <strong>${esc(status.sample_key)}</strong></span>`,
    `<span class="chip">结果 <strong class="mono">${shortHash(status.result_digest,16)}</strong></span>`,
    `<span class="chip ${status.last_error_code?"bad":""}">最近错误 <strong>${esc(status.last_error_code||"NONE")}</strong></span>`
  ].join("");
}

function renderMetrics(s){
  const m=s.metrics;
  const items=[
    ["帧数",fmt(m.frame_count,0),"20 ms 帧长 / 20 ms 帧移"],
    ["总时长",fmt(m.duration_ms/1000,2)+" s",`${fmt(m.duration_ms,0)} ms`],
    ["候选段",fmt(m.candidate_segments,0),"双门限滞回"],
    ["分离段",fmt(m.separated_segments,0),"无重叠重建"],
    ["身份核对",`${fmt(m.verified_identities,0)} / ${fmt(m.separated_segments,0)}`,"逐段对真值"],
    ["真值记录",fmt(m.truth_records,0),"逐说话人逐帧"]
  ];
  document.getElementById("metrics").innerHTML=items.map(([label,value,note])=>
    `<div class="metric"><div class="label">${label}</div><div class="value">${value}</div><div class="note">${esc(note)}</div></div>`
  ).join("");
}

function renderHashes(s){
  const m=s.metrics;
  const rows=[
    ["PCM",m.pcm_sha256],["特征",m.feature_sha256],["掩码",m.mask_sha256],
    ["段列表",m.segment_sha256],["快照集",s.status.snapshot_digest]
  ];
  document.getElementById("hashList").innerHTML=rows.map(([name,hash])=>
    `<div class="hash-row"><span>${name}</span><span class="mono" title="${esc(hash)}">${esc(hash)}</span></div>`
  ).join("");
}

function renderConfig(s){
  const c=s.config;
  const select=document.getElementById("configSelect");
  if([...select.options].some(option=>option.value===s.status.sample_key)){
    select.value=s.status.sample_key;
  }
  document.getElementById("configMeta").innerHTML = [
    `<span class="chip">语义摘要 <strong class="mono">${shortHash(c.semantic_digest,16)}</strong></span>`,
    `<span class="chip">${esc(c.version)}</span>`,
    `<span class="chip">样例 ${esc(c.sample_key)}</span>`
  ].join("");
  document.getElementById("viewTabs").innerHTML=["v1","v2","v3"].map(v=>
    `<button class="tab ${v===activeView?"active":""}" onclick="activeView='${v}';renderConfig(appState)">${v.toUpperCase()}</button>`
  ).join("");
  document.getElementById("configViews").innerHTML=["v1","v2","v3"].map(v=>{
    const view=c.views[v]||{};
    const match=view.matches_current;
    const text=view.text||JSON.stringify(view.error||{},null,2);
    return `<div class="view-card"><h3><span>${v.toUpperCase()} 视图</span>
      <span class="badge ${match?"match":""}">${match?"语义一致":"有损视图"}</span></h3>
      <pre style="max-height:210px">${esc(text)}</pre></div>`;
  }).join("");
}

function drawWaveform(s){
  const canvas=document.getElementById("waveCanvas");
  const rect=canvas.getBoundingClientRect();
  const dpr=Math.min(2,window.devicePixelRatio||1);
  canvas.width=Math.max(640,Math.round(rect.width*dpr));
  canvas.height=Math.round(245*dpr);
  const ctx=canvas.getContext("2d");
  const W=canvas.width,H=canvas.height,pad=22*dpr;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle="#09111a";ctx.fillRect(0,0,W,H);
  const points=s.waveform.points, mid=H/2, amp=(H/2-pad)*.72;
  const duration=s.waveform.duration_ms;
  const xAt=ms=>pad+(W-2*pad)*(ms/duration);
  ctx.strokeStyle="rgba(142,160,184,.15)";ctx.lineWidth=1*dpr;
  for(let i=0;i<=4;i++){
    const x=pad+(W-2*pad)*i/4;
    ctx.beginPath();ctx.moveTo(x,pad*.6);ctx.lineTo(x,H-pad*.6);ctx.stroke();
  }
  s.truth_segments.forEach(seg=>{
    const x1=xAt(seg.start_ms),x2=xAt(seg.end_ms);
    ctx.fillStyle="rgba(105,167,255,.12)";
    ctx.fillRect(x1,pad*.6,x2-x1,H-pad*1.2);
  });
  ctx.strokeStyle="rgba(142,160,184,.35)";ctx.beginPath();ctx.moveTo(pad,mid);ctx.lineTo(W-pad,mid);ctx.stroke();
  ctx.fillStyle="rgba(68,215,206,.82)";
  points.forEach((pair,i)=>{
    const x=pad+(W-2*pad)*i/(points.length-1);
    const y1=mid-pair[1]*amp,y2=mid-pair[0]*amp;
    ctx.fillRect(x,y1,Math.max(1*dpr,(W-2*pad)/points.length+.6*dpr),Math.max(1*dpr,y2-y1));
  });
  const energies=s.frame_energy;
  if(energies.length){
    const maxE=Math.max(...energies,1e-12);
    ctx.strokeStyle="rgba(246,189,96,.9)";ctx.lineWidth=1.3*dpr;ctx.beginPath();
    energies.forEach((energy,i)=>{
      const x=pad+(W-2*pad)*i/(energies.length-1);
      const y=H-pad-(energy/maxE)*(H*.23);
      i?ctx.lineTo(x,y):ctx.moveTo(x,y);
    });
    ctx.stroke();
  }
  s.segments.forEach((seg,i)=>{
    const x1=xAt(seg.start_ms),x2=xAt(seg.end_ms);
    const y=pad+((i%3)+.45)*14*dpr;
    ctx.fillStyle=COLORS[(seg.identity.speaker_id-1)%COLORS.length];
    ctx.fillRect(x1,y,Math.max(2*dpr,x2-x1),5*dpr);
  });
  ctx.fillStyle="rgba(232,238,247,.55)";ctx.font=`${10*dpr}px ui-monospace,monospace`;
  ctx.fillText("0 ms",pad,H-4*dpr);
  ctx.fillText(fmt(duration,0)+" ms",W-pad-60*dpr,H-4*dpr);
  document.getElementById("waveMeta").textContent=`${fmt(duration/1000,2)} s · ${fmt(s.metrics.frame_count,0)} 帧`;
}

function renderSegments(s){
  const rows=s.segments.map(seg=>{
    const id=seg.identity;
    return `<tr>
      <td class="mono">${esc(seg.segment_id)}</td>
      <td>${seg.mask_speaker_id}</td>
      <td><strong>SPK ${id.speaker_id}</strong> <span class="muted tiny">${id.accepted?"accepted":"below threshold"}</span></td>
      <td class="mono">[${fmt(id.confidence_interval[0],3)}, ${fmt(id.confidence_interval[1],3)}]</td>
      <td class="mono">[${seg.start_frame}, ${seg.end_frame})</td>
      <td class="mono">[${seg.start_sample}, ${seg.end_sample})</td>
      <td class="mono">${fmt(seg.start_ms,3)} → ${fmt(seg.end_ms,3)}</td>
      <td class="${id.matches_truth?"ok":"bad"}">${id.matches_truth?"MATCH":"MISMATCH"}</td>
    </tr>`;
  }).join("");
  document.getElementById("segmentRows").innerHTML=rows||`<tr><td colspan="8" class="empty">无分离段</td></tr>`;
  document.getElementById("segmentBadge").textContent=`${s.segments.length} 段`;
}

function renderTimebase(s){
  document.getElementById("timebaseRows").innerHTML=s.timebase.map(row=>`<tr>
    <td class="mono">${esc(row.segment_id)}</td><td>${row.start_frame}</td><td>${row.end_frame}</td>
    <td>${row.start_sample}</td><td>${row.end_sample}</td>
    <td class="mono ok">[${row.inverse_start_frame}, ${row.inverse_end_frame}]</td>
  </tr>`).join("");
}

function auditRows(items, type){
  if(!items||!items.length) return `<div class="muted tiny">无${type}</div>`;
  return items.map(item=>{
    const detail=item.rule||item.source||"";
    return `<div class="audit-row"><span class="mono">${esc(item.field)}</span><span>${esc(detail)}</span></div>`;
  }).join("");
}

function renderMigrations(s){
  const m=s.migrations;
  const cards=[];
  m.upgrades.forEach(up=>{
    cards.push(`<div class="audit-box"><h3>${esc(up.audit.source_version)} → ${esc(up.target)} <span class="badge">补全 ${up.audit.additions.length}</span></h3>
      <div class="audit-list">${auditRows(up.audit.additions,"补全项")}</div></div>`);
  });
  m.downgrades.forEach(down=>{
    cards.push(`<div class="audit-box"><h3>${esc(down.audit.source_version)} → ${esc(down.target)} <span class="badge ${down.mode==="lossy_explicit"?"":"match"}">${esc(down.mode)}</span></h3>
      <div class="audit-list">${auditRows(down.audit.drops,"丢弃项")}</div></div>`);
  });
  if(m.errors.length){
    cards.push(`<div class="audit-box"><h3 class="bad">严格降级拦截</h3><div class="audit-list">
      ${m.errors.map(e=>`<div class="error-box"><div class="error-code">${esc(e.code)}</div><div class="error-detail">${esc(e.message)}<br>${esc(JSON.stringify(e.details.fields||e.details,null,0))}</div></div>`).join("")}
    </div></div>`);
  }
  document.getElementById("migrationGrid").innerHTML=cards.join("");
  document.getElementById("migrationBadge").textContent=`${m.upgrades.length} 升级 · ${m.downgrades.length} 降级`;
}

function renderSnapshots(s){
  const rows=Object.entries(s.snapshots).map(([item,rec])=>{
    if(!rec) return `<tr><td>${esc(item)}</td><td colspan="3" class="muted">未初始化</td></tr>`;
    const compact=Object.entries(rec.payload).map(([k,v])=>`${k}=${typeof v==="object"?JSON.stringify(v):v}`).join(" · ");
    return `<tr><td><strong>${esc(item)}</strong></td><td class="mono">${rec.version}</td>
      <td class="mono tiny" title="${esc(rec.checksum)}">${shortHash(rec.checksum,20)}</td>
      <td class="muted tiny">${esc(compact)}</td></tr>`;
  }).join("");
  document.getElementById("snapshotRows").innerHTML=rows;
}

function renderSelfCheck(s){
  const check=s.self_check;
  document.getElementById("selfCheckBadge").innerHTML=`<span class="${check.ok?"ok":"bad"}">${check.ok?"PASS":"FAIL"}</span> · ${check.issues.length} issues`;
  const labels={
    segmentation_mask_coverage:"分段/掩码覆盖",
    separated_segment_mask_coverage:"分离段/掩码覆盖",
    identity_segment_one_to_one:"身份与段一一对应",
    timebase_roundtrip:"毫秒/帧反算",
    segments_within_audio:"段位于音频内",
    identity_matches_truth:"身份与真值一致",
    identity_labels_declared:"身份标签合法"
  };
  document.getElementById("checkGrid").innerHTML=Object.entries(check.checks).map(([key,value])=>
    `<div class="check-item"><div class="name">${labels[key]||esc(key)}</div><div class="result ${value?"ok":"bad"}">${value?"PASS":"FAIL"}</div></div>`
  ).join("");
  document.getElementById("checkIssues").innerHTML=check.issues.length?
    check.issues.map(issue=>`<div class="error-box"><div class="error-code">E_INCONSISTENT</div><div class="error-detail">${esc(JSON.stringify(issue))}</div></div>`).join(""):
    `<div class="muted tiny">覆盖帧 ${check.counts.candidate_frames} · 段 ${check.counts.segments} · 身份 ${check.counts.identities} · 时间基准行 ${check.counts.timebase_rows}</div>`;
}

function renderErrors(s){
  document.getElementById("errorBadge").textContent=`${s.errors.length} 条`;
  document.getElementById("errorList").innerHTML=s.errors.length?s.errors.slice().reverse().map(err=>`
    <div class="error-box"><div class="error-code">#${err.sequence} ${esc(err.code)} · ${esc(err.action)}</div>
    <div class="error-detail">${esc(err.message)}<br>${esc(JSON.stringify(err.details))}</div></div>`).join(""):
    `<div class="muted">尚无业务错误。可运行回退、损坏或有损迁移探针。</div>`;
}

function renderEvents(s){
  document.getElementById("eventList").innerHTML=s.events.slice().reverse().map(event=>`
    <div class="audit-row"><span>#${event.sequence} ${esc(event.message)}</span><span class="mono">${esc(event.kind)}</span></div>`).join("");
}

window.addEventListener("resize",()=>{ if(appState) drawWaveform(appState); });
loadState().catch(error=>showToast(error.message,false));
setInterval(()=>{ if(!document.hidden) loadState().catch(()=>{}); },15000);
</script>
</body>
</html>
"""


def make_handler(state: DashboardState) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "MeetingPost/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/", "/index.html"}:
                self._send_html(render_dashboard_html())
                return
            if self.path == "/api/state":
                try:
                    self._send_json(state.state_payload())
                except MeetingError as exc:
                    self._send_json({"ok": False, "error": exc.to_dict()}, status=500)
                return
            if self.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self._send_json({"ok": False, "error": {"code": "E_NOT_FOUND"}}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/action":
                self._send_json({"ok": False, "error": {"code": "E_NOT_FOUND"}}, status=404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 1_000_000:
                self._send_json(
                    {
                        "ok": False,
                        "error": {
                            "code": "E_FORMAT_CORRUPT",
                            "message": "invalid request body length",
                            "details": {},
                        },
                    },
                    status=400,
                )
                return
            try:
                request = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": {
                            "code": "E_FORMAT_CORRUPT",
                            "message": f"invalid JSON request: {exc}",
                            "details": {},
                        },
                    },
                    status=400,
                )
                return
            action = request.get("action")
            if action == "switch_config":
                payload = state.switch_config(str(request.get("version", "")))
            elif action == "reprocess":
                payload = state.reprocess()
            elif action == "migrate":
                payload = state.migrate_to(
                    str(request.get("target", "")),
                    bool(request.get("lossy", False)),
                )
            elif action == "replace_snapshots":
                payload = state.replace_snapshots()
            elif action == "probe_rollback":
                payload = state.probe_rollback()
            elif action == "probe_corruption":
                payload = state.probe_corruption()
            else:
                payload = {
                    "ok": False,
                    "error": {
                        "code": "E_ACTION_UNKNOWN",
                        "message": f"unknown action {action}",
                        "details": {},
                    },
                }
            self._send_json(payload)

    return DashboardHandler


def run_server(
    state: DashboardState,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(state))
    url = f"http://{host}:{port}/"
    print(f"会议同传后处理控制台: {url}")
    print("按 Ctrl+C 停止。所有数据与快照均保存在本地。")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止本地服务…")
    finally:
        server.server_close()
