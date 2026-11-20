from __future__ import annotations

import json
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import load_config, resolve_paths
from .jsonio import read_json


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>TraceLab · 多目标跟踪一致性编辑器</title>
  <style>
    :root {
      --bg:#08111f; --panel:#0e1b2d; --panel2:#12233a; --line:#223a56;
      --text:#e7eef8; --muted:#8297b2; --cyan:#39d5d0; --lime:#a6e35b;
      --orange:#ffb55c; --red:#ff6b7a; --blue:#65a9ff; --violet:#c693ff;
      --shadow: 0 18px 48px rgba(0,0,0,.28);
    }
    * { box-sizing:border-box; }
    body {
      margin:0; color:var(--text); background:
        radial-gradient(circle at 10% -10%, rgba(57,213,208,.16), transparent 34%),
        radial-gradient(circle at 88% 8%, rgba(101,169,255,.13), transparent 30%),
        var(--bg);
      font-family:"Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
      min-height:100vh;
    }
    .shell { max-width:1480px; margin:0 auto; padding:24px 28px 40px; }
    header { display:flex; align-items:center; justify-content:space-between; gap:20px; margin-bottom:20px; }
    .brand { display:flex; align-items:center; gap:13px; }
    .mark { width:42px; height:42px; border-radius:13px; background:linear-gradient(135deg,var(--cyan),var(--blue));
      display:grid; place-items:center; color:#07121f; font-weight:900; box-shadow:0 8px 28px rgba(57,213,208,.28); }
    h1 { margin:0; font-size:20px; letter-spacing:.3px; }
    .sub { color:var(--muted); font-size:12px; margin-top:4px; }
    .actions { display:flex; align-items:center; gap:10px; }
    button, select {
      background:var(--panel2); border:1px solid var(--line); color:var(--text);
      border-radius:10px; padding:9px 12px; font:inherit; outline:none;
    }
    button { cursor:pointer; transition:.18s ease; }
    button:hover { border-color:var(--cyan); transform:translateY(-1px); }
    button.primary { background:linear-gradient(135deg,#1ab7b2,#377ed0); border:0; color:white; font-weight:650; }
    button:disabled { opacity:.45; cursor:wait; transform:none; }
    .grid { display:grid; grid-template-columns:minmax(0,1.7fr) minmax(310px,.72fr); gap:18px; }
    .stack { display:flex; flex-direction:column; gap:18px; min-width:0; }
    .panel { background:linear-gradient(180deg,rgba(18,35,58,.96),rgba(13,27,45,.97));
      border:1px solid var(--line); border-radius:16px; box-shadow:var(--shadow); overflow:hidden; }
    .panel-head { display:flex; align-items:center; justify-content:space-between; gap:12px;
      padding:14px 17px; border-bottom:1px solid var(--line); }
    .panel-title { font-size:13px; font-weight:700; letter-spacing:.5px; color:#dfeaf8; }
    .hint { color:var(--muted); font-size:11px; }
    .metrics { display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin-bottom:18px; }
    .metric { padding:15px 16px; background:linear-gradient(150deg,rgba(18,35,58,.93),rgba(9,20,35,.95));
      border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow); }
    .metric .k { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.8px; }
    .metric .v { font-size:24px; font-weight:750; margin-top:7px; font-variant-numeric:tabular-nums; }
    .metric .n { color:var(--muted); font-size:11px; margin-top:3px; }
    .stage { padding:14px; }
    canvas { width:100%; aspect-ratio:16/9; display:block; border-radius:12px; background:#07111e;
      border:1px solid #1c344f; cursor:crosshair; }
    .controls { display:flex; align-items:center; gap:14px; padding:13px 16px; border-top:1px solid var(--line); flex-wrap:wrap; }
    .controls label { color:var(--muted); font-size:12px; }
    input[type=range] { flex:1; min-width:180px; accent-color:var(--cyan); }
    .frame-badge { font-family:ui-monospace, Consolas, monospace; color:var(--cyan); background:#0a192a;
      border:1px solid #20415f; border-radius:8px; padding:5px 9px; font-size:12px; }
    .legend { display:flex; gap:14px; flex-wrap:wrap; color:var(--muted); font-size:11px; padding:0 16px 13px; }
    .dot { display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px; vertical-align:0; }
    .states { display:grid; grid-template-columns:repeat(4,1fr); gap:9px; padding:14px; }
    .state { border:1px solid var(--line); border-radius:11px; padding:11px; background:#0b192a; }
    .state .k { font-size:11px; color:var(--muted); }
    .state .v { margin-top:4px; font-size:21px; font-weight:750; }
    .state.new .v { color:var(--blue); } .state.tracked .v { color:var(--lime); }
    .state.lost .v { color:var(--orange); } .state.terminated .v { color:var(--muted); }
    .kv { padding:6px 17px 15px; }
    .kv-row { display:flex; justify-content:space-between; gap:15px; padding:8px 0; border-bottom:1px dashed rgba(65,92,124,.36); font-size:12px; }
    .kv-row:last-child { border-bottom:0; }
    .kv-row .k { color:var(--muted); }
    .kv-row .v { text-align:right; font-family:ui-monospace, Consolas, monospace; }
    .ok { color:var(--lime); } .warn { color:var(--orange); } .bad { color:var(--red); }
    .error-list { padding:8px 13px 14px; max-height:275px; overflow:auto; }
    .error { border:1px solid rgba(255,107,122,.23); background:rgba(255,107,122,.055); border-radius:10px; padding:10px; margin:7px 0; }
    .error code { color:var(--red); font-weight:700; font-size:11px; }
    .error .meta { color:var(--muted); font-size:10px; margin-top:4px; }
    .error .msg { font-size:11px; color:#cbd8e7; margin-top:5px; line-height:1.45; }
    .revision-tag { color:var(--violet); font-family:ui-monospace, Consolas, monospace; }
    .empty { padding:28px 18px; color:var(--muted); font-size:12px; line-height:1.7; }
    #toast { position:fixed; right:24px; bottom:24px; max-width:440px; background:#12243a;
      border:1px solid #2a4969; border-radius:12px; padding:13px 15px; font-size:12px;
      box-shadow:var(--shadow); opacity:0; transform:translateY(12px); pointer-events:none; transition:.2s; z-index:9; }
    #toast.show { opacity:1; transform:translateY(0); }
    @media (max-width:1000px) {
      .grid { grid-template-columns:1fr; }
      .metrics { grid-template-columns:repeat(2,1fr); }
    }
  </style>
</head>
<body>
<div class="shell">
  <header>
    <div class="brand">
      <div class="mark">TL</div>
      <div><h1>TraceLab <span style="color:var(--cyan)">/</span> 多目标跟踪一致性编辑器</h1>
        <div class="sub">确定性匹配 · 原子检查点 · 中断恢复 · revision 后缀重算</div></div>
    </div>
    <div class="actions">
      <select id="seqSelect" aria-label="选择序列"></select>
      <button class="primary" id="demoBtn">触发一次演示运行</button>
    </div>
  </header>

  <section class="metrics">
    <div class="metric"><div class="k">MOTA</div><div class="v" id="mMota">—</div><div class="n" id="mMotaNote">状态演进评分</div></div>
    <div class="metric"><div class="k">IDF1</div><div class="v" id="mIdf1">—</div><div class="n">身份集合一致性</div></div>
    <div class="metric"><div class="k">ID switches</div><div class="v" id="mSwitch">—</div><div class="n">轨迹切换次数</div></div>
    <div class="metric"><div class="k">Checkpoint</div><div class="v" id="mVersion">—</div><div class="n" id="mVersionNote">当前帧提交版本</div></div>
    <div class="metric"><div class="k">Revision</div><div class="v revision-tag" id="mRevision">—</div><div class="n" id="mMode">版本与运行模式</div></div>
  </section>

  <div class="grid">
    <div class="stack">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">当前帧视图</div>
          <div class="hint" id="frameHint">检测框 · 真值 · 补全框</div>
        </div>
        <div class="stage"><canvas id="canvas" width="960" height="540"></canvas></div>
        <div class="controls">
          <span class="frame-badge" id="frameBadge">frame 000</span>
          <input id="frameSlider" type="range" min="0" max="0" value="0">
          <label><input id="truthToggle" type="checkbox" checked> 显示真值</label>
          <label><input id="completionToggle" type="checkbox" checked> 显示补全</label>
        </div>
        <div class="legend">
          <span><i class="dot" style="background:var(--lime)"></i>预测匹配</span>
          <span><i class="dot" style="background:var(--orange)"></i>遮挡检测</span>
          <span><i class="dot" style="background:var(--violet)"></i>补全轨迹</span>
          <span><i class="dot" style="background:#71859d"></i>真值</span>
          <span id="frameCountHint"></span>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><div class="panel-title">轨迹状态分布</div><div class="hint">来自当前帧已提交检查点</div></div>
        <div class="states">
          <div class="state new"><div class="k">NEW 新建</div><div class="v" id="sNew">0</div></div>
          <div class="state tracked"><div class="k">TRACKED 保持</div><div class="v" id="sTracked">0</div></div>
          <div class="state lost"><div class="k">LOST 丢失</div><div class="v" id="sLost">0</div></div>
          <div class="state terminated"><div class="k">TERMINATED 终结</div><div class="v" id="sTerminated">0</div></div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><div class="panel-title">最近业务错误码</div><div class="hint" id="errorCount">0 项</div></div>
        <div class="error-list" id="errorList"><div class="empty">运行演示后展示异常输入归因与业务码。</div></div>
      </section>
    </div>

    <div class="stack">
      <section class="panel">
        <div class="panel-head"><div class="panel-title">检查点与恢复</div><div class="hint" id="integrity">INTEGRITY</div></div>
        <div class="kv" id="recoveryKv"></div>
      </section>

      <section class="panel">
        <div class="panel-head"><div class="panel-title">Revision 影响面</div><div class="hint">受影响后缀重算</div></div>
        <div class="kv" id="revisionKv"></div>
      </section>

      <section class="panel">
        <div class="panel-head"><div class="panel-title">资源上限与降级</div><div class="hint" id="policyHint">policy</div></div>
        <div class="kv" id="resourceKv"></div>
      </section>

      <section class="panel">
        <div class="panel-head"><div class="panel-title">轨迹片段 / 补全</div><div class="hint">gaps</div></div>
        <div class="kv" id="gapKv"></div>
      </section>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
let state = null;
let busy = false;

function fmt(value, digits=4) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(digits);
  return String(value);
}
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function toast(text) {
  const el = $('toast'); el.textContent = text; el.classList.add('show');
  clearTimeout(el._timer); el._timer = setTimeout(() => el.classList.remove('show'), 4600);
}
async function getStatus(seqId, frame) {
  const params = new URLSearchParams();
  if (seqId) params.set('seq_id', seqId);
  if (frame !== undefined && frame !== null) params.set('frame', String(frame));
  const response = await fetch('/api/status?' + params.toString(), {cache:'no-store'});
  if (!response.ok) throw new Error('status ' + response.status);
  return await response.json();
}
function populateSequences() {
  const options = state.summary?.sequences || [];
  const current = state.selected?.seq_id;
  $('seqSelect').innerHTML = options.map(item =>
    `<option value="${esc(item.seq_id)}" ${item.seq_id===current?'selected':''}>${esc(item.seq_id)} · rev ${item.revision}</option>`
  ).join('');
}
async function selectSequence(seqId) {
  const previous = state?.selected?.frame_count ? Math.min(state.selected.frame, 20) : 20;
  state = await getStatus(seqId, previous);
  render();
}
async function selectFrame(frame) {
  if (!state?.selected?.seq_id) return;
  state = await getStatus(state.selected.seq_id, frame);
  render();
}
function render() {
  const sel = state.selected;
  if (!sel || !sel.available) {
    $('recoveryKv').innerHTML = `<div class="empty">${esc(state.message || '尚未运行演示。')}<br><br>点击右上角“触发一次演示运行”。</div>`;
    return;
  }
  const m = sel.metrics || {};
  $('mMota').textContent = fmt(m.mota);
  $('mIdf1').textContent = fmt(m.idf1);
  $('mSwitch').textContent = fmt(m.id_switches, 0);
  $('mVersion').textContent = 'v' + fmt(sel.checkpoint?.version, 0);
  $('mRevision').textContent = 'r' + fmt(sel.revision, 0);
  $('mMode').textContent = sel.run_report?.mode || '—';
  $('mMotaNote').textContent = `${fmt(m.true_positives,0)} TP / ${fmt(m.false_negatives,0)} FN`;
  $('mVersionNote').textContent = `frame ${fmt(sel.checkpoint?.frame_index,0)} 已原子提交`;

  populateSequences();
  $('frameSlider').max = Math.max(0, sel.frame_count - 1);
  $('frameSlider').value = sel.frame;
  $('frameBadge').textContent = `frame ${String(sel.frame).padStart(3,'0')}`;
  $('frameCountHint').textContent = `共 ${sel.frame_count} 帧 · 当前检查点 v${sel.checkpoint?.version}`;

  const dist = sel.state_distribution || {};
  $('sNew').textContent = dist.new || 0;
  $('sTracked').textContent = dist.tracked || 0;
  $('sLost').textContent = dist.lost || 0;
  $('sTerminated').textContent = dist.terminated || 0;

  const rr = sel.run_report || {};
  const rec = rr.recovery || {};
  $('integrity').textContent = rec.integrity_ok ? 'INTEGRITY OK' : 'CHECK';
  $('integrity').className = rec.integrity_ok ? 'hint ok' : 'hint bad';
  $('recoveryKv').innerHTML = [
    ['运行模式', rr.mode],
    ['恢复起点帧', rec.start_frame],
    ['运行前最后提交帧', rec.last_committed_frame_before_run],
    ['运行前检查点版本', rec.last_checkpoint_version_before_run],
    ['本次提交帧数', rec.committed_frames_this_run],
    ['当前检查点校验和', (rr.checkpoint_checksum || '').slice(0,16) + '…'],
    ['恢复状态', rec.resumed_from_checkpoint ? '从已提交检查点继续' : '无中断 / 已完成']
  ].map(([k,v]) => `<div class="kv-row"><span class="k">${k}</span><span class="v">${esc(v ?? '—')}</span></div>`).join('');

  const vr = rr.revision_report || {};
  $('revisionKv').innerHTML = [
    ['incoming / previous', `r${fmt(vr.incoming_revision,0)} / ${vr.previous_revision == null ? '—' : 'r'+vr.previous_revision}`],
    ['受影响帧', (vr.affected_frames || []).join(', ') || '无'],
    ['受影响真值轨迹', (vr.affected_truth_tracks || []).join(', ') || '无'],
    ['受影响预测轨迹', (vr.affected_prediction_tracks || []).join(', ') || '无'],
    ['复用前缀帧', fmt(vr.reused_frames,0)],
    ['重算后缀帧', fmt(vr.recomputed_frames,0)],
    ['丢弃旧后缀帧', fmt(vr.discarded_old_suffix_frames,0)]
  ].map(([k,v]) => `<div class="kv-row"><span class="k">${k}</span><span class="v">${esc(v)}</span></div>`).join('');

  const resource = rr.resource_report || {};
  $('policyHint').textContent = resource.policy || '—';
  const limits = resource.limits || {};
  $('resourceKv').innerHTML = [
    ['每帧检测框上限', limits.max_detections_per_frame],
    ['外观缓存上限', limits.max_appearance_cache_entries],
    ['并行补全轨迹上限', limits.max_parallel_gap_tracks],
    ['降级类别', (resource.degraded_categories || []).join(', ') || '无'],
    ['丢框帧数', (resource.frames_with_dropped_detections || []).length],
    ['策略', resource.policy]
  ].map(([k,v]) => `<div class="kv-row"><span class="k">${k}</span><span class="v">${esc(v ?? '—')}</span></div>`).join('');

  $('gapKv').innerHTML = [
    ['轨迹数', rr.track_count],
    ['补全区间数', rr.gap_completion_count],
    ['超限跳过区间', rr.gap_skipped_count],
    ['补全帧计分方式', '与真实命中分账'],
    ['真实命中帧', m.completion?.real_hits],
    ['补全命中帧', m.completion?.completed_hits]
  ].map(([k,v]) => `<div class="kv-row"><span class="k">${k}</span><span class="v">${esc(v ?? '—')}</span></div>`).join('');

  const errors = state.recent_errors || [];
  $('errorCount').textContent = `${errors.length} 项`;
  $('errorList').innerHTML = errors.length ? errors.map(item => {
    const actual = item.actual || {};
    const location = [actual.seq_id, actual.frame_index != null ? 'frame '+actual.frame_index : null].filter(Boolean).join(' · ');
    return `<div class="error"><code>${esc(actual.code || 'NO_ERROR')}</code>
      <span class="meta">${esc(item.case)} · ${esc(location || 'global')} · expected ${esc(item.expected_code)}</span>
      <div class="msg">${esc(actual.message || '')}</div></div>`;
  }).join('') : '<div class="empty">暂无错误。</div>';

  drawFrame(sel);
}
function drawFrame(sel) {
  const canvas = $('canvas');
  const ctx = canvas.getContext('2d');
  const width = canvas.width, height = canvas.height;
  const canvasSpec = sel.canvas || [640,360];
  const sx = width / canvasSpec[0], sy = height / canvasSpec[1];
  ctx.clearRect(0,0,width,height);
  const gradient = ctx.createLinearGradient(0,0,width,height);
  gradient.addColorStop(0,'#081827'); gradient.addColorStop(1,'#0a1424');
  ctx.fillStyle = gradient; ctx.fillRect(0,0,width,height);
  ctx.strokeStyle = 'rgba(62,100,137,.16)'; ctx.lineWidth = 1;
  for (let x=0; x<width; x+=width/16) { ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,height); ctx.stroke(); }
  for (let y=0; y<height; y+=height/9) { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(width,y); ctx.stroke(); }
  const showTruth = $('truthToggle').checked;
  const showCompletion = $('completionToggle').checked;
  if (showTruth) {
    (sel.truth_boxes || []).forEach(box => {
      ctx.strokeStyle = 'rgba(147,166,188,.66)'; ctx.setLineDash([7,5]); ctx.lineWidth = 1.5;
      ctx.strokeRect(box.x1*sx, box.y1*sy, (box.x2-box.x1)*sx, (box.y2-box.y1)*sy);
      ctx.fillStyle = 'rgba(207,218,232,.82)'; ctx.font = '12px ui-monospace, Consolas';
      ctx.fillText('GT ' + box.track_id_true, box.x1*sx + 4, Math.max(14, box.y1*sy - 5));
    });
    ctx.setLineDash([]);
  }
  (sel.detections || []).forEach(box => {
    const color = box.occluded ? '#ffb55c' : '#a6e35b';
    ctx.strokeStyle = color; ctx.lineWidth = 2.2;
    ctx.strokeRect(box.x1*sx, box.y1*sy, (box.x2-box.x1)*sx, (box.y2-box.y1)*sy);
    const label = `#${box.pred_track_id} ← T${box.track_id_true} · ${Number(box.score).toFixed(2)}`;
    ctx.font = '600 13px ui-monospace, Consolas';
    const textWidth = ctx.measureText(label).width;
    const labelY = Math.max(18, box.y1*sy - 8);
    ctx.fillStyle = 'rgba(7,17,30,.88)'; ctx.fillRect(box.x1*sx, labelY-15, textWidth+9, 20);
    ctx.fillStyle = color; ctx.fillText(label, box.x1*sx+5, labelY);
  });
  if (showCompletion) {
    (sel.completion_boxes || []).forEach(box => {
      ctx.strokeStyle = '#c693ff'; ctx.setLineDash([6,4]); ctx.lineWidth = 2;
      ctx.strokeRect(box.x1*sx, box.y1*sy, (box.x2-box.x1)*sx, (box.y2-box.y1)*sy);
      ctx.setLineDash([]);
      ctx.fillStyle = '#e1c8ff'; ctx.font = '12px ui-monospace, Consolas';
      ctx.fillText('FILL #' + box.pred_track_id, box.x1*sx+4, Math.min(height-5, box.y2*sy+14));
    });
  }
  ctx.fillStyle = '#5f7893'; ctx.font = '12px ui-monospace, Consolas';
  ctx.fillText(`${sel.selected_seq_id} · revision ${sel.revision} · checkpoint v${sel.checkpoint?.version}`, 14, height-14);
}
async function runDemo() {
  if (busy) return;
  busy = true; $('demoBtn').disabled = true; $('demoBtn').textContent = '演示运行中…';
  toast('正在生成样本、模拟强制终止、执行恢复与 revision 重算，请稍候。');
  try {
    const response = await fetch('/api/demo', {method:'POST'});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.message || 'demo failed');
    state = await getStatus(state?.selected?.seq_id || 'seq-000', 20);
    render();
    toast('演示完成：恢复一致性、revision 重算和异常码检查均已写入 artifacts/demo/verification.json。');
  } catch (error) {
    toast('演示失败：' + error.message);
  } finally {
    busy = false; $('demoBtn').disabled = false; $('demoBtn').textContent = '触发一次演示运行';
  }
}
async function boot() {
  try {
    state = await getStatus(undefined, 20);
    render();
    if (!state.selected?.available) toast('尚无演示结果。点击右上角运行一次。');
  } catch (error) {
    $('recoveryKv').innerHTML = `<div class="empty">无法读取本地状态：${esc(error.message)}</div>`;
  }
}
$('seqSelect').addEventListener('change', event => selectSequence(event.target.value));
$('frameSlider').addEventListener('change', event => selectFrame(Number(event.target.value)));
$('frameSlider').addEventListener('input', event => { $('frameBadge').textContent = `frame ${String(event.target.value).padStart(3,'0')}`; });
$('truthToggle').addEventListener('change', () => state?.selected && drawFrame(state.selected));
$('completionToggle').addEventListener('change', () => state?.selected && drawFrame(state.selected));
$('demoBtn').addEventListener('click', runDemo);
boot();
</script>
</body>
</html>
"""


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class AppState:
    def __init__(self, base_dir: Path, config_path: str) -> None:
        self.base_dir = base_dir
        self.config_path = config_path
        self.config = load_config(config_path)
        self.paths = resolve_paths(base_dir, self.config)
        self.demo_root = base_dir / "artifacts" / "demo"
        self.run_summary_path = self.paths["output"] / "run_summary.json"
        self.demo_lock = threading.Lock()

    def _source_data_dir(self, revision: int) -> Path:
        verification_path = self.demo_root / "verification.json"
        data_dir = self.paths["data"]
        if verification_path.exists():
            try:
                verification = read_json(verification_path)
                source = verification.get("source_data", {})
                revision_data = source.get("revision_data")
                if revision > 1 and revision_data and Path(revision_data).exists():
                    data_dir = Path(revision_data)
            except (OSError, ValueError):
                pass
        return data_dir

    def status(self, seq_id: str | None, frame: int | None) -> dict[str, Any]:
        if not self.run_summary_path.exists():
            return {
                "available": False,
                "message": "尚未发现 run_summary.json，请先触发演示运行。",
                "summary": {"sequences": []},
                "selected": None,
                "recent_errors": self._errors()[:8],
            }
        summary = read_json(self.run_summary_path)
        sequences = summary.get("sequences", [])
        if not sequences:
            return {
                "available": False,
                "message": "运行摘要为空。",
                "summary": summary,
                "selected": None,
                "recent_errors": self._errors()[:8],
            }
        ids = [item["seq_id"] for item in sequences]
        if seq_id not in ids:
            seq_id = ids[0]
        sequence_summary = next(item for item in sequences if item["seq_id"] == seq_id)
        output_dir = Path(sequence_summary["output_dir"])
        metrics = read_json(output_dir / "metrics.json") if (output_dir / "metrics.json").exists() else {}
        run_report = read_json(output_dir / "run_report.json") if (output_dir / "run_report.json").exists() else {}
        checkpoint_index = (
            read_json(output_dir / "checkpoint_index.json")
            if (output_dir / "checkpoint_index.json").exists()
            else []
        )
        state_rows = _read_jsonl_rows(output_dir / "track_states.jsonl")
        if state_rows:
            frame_count = max(int(row["frame_index"]) for row in state_rows) + 1
        else:
            frame_count = int(sequence_summary.get("frame_count", 0))
        selected_frame = 0 if frame is None else max(0, min(int(frame), max(0, frame_count - 1)))
        checkpoint = next(
            (
                item
                for item in checkpoint_index
                if int(item["frame_index"]) == selected_frame
            ),
            None,
        )
        frame_states = [
            row for row in state_rows if int(row["frame_index"]) == selected_frame
        ]
        distribution = (
            frame_states[0].get("state_distribution", {})
            if frame_states
            else {"new": 0, "tracked": 0, "lost": 0, "terminated": 0}
        )
        assignments = [
            row
            for row in _read_jsonl_rows(output_dir / "assignments.jsonl")
            if int(row["frame_index"]) == selected_frame
        ]
        completions = [
            row
            for row in _read_jsonl_rows(output_dir / "gap_completions.jsonl")
            if int(row["frame_index"]) == selected_frame
        ]
        revision = int(sequence_summary["revision"])
        data_dir = self._source_data_dir(revision)
        detections = [
            row
            for row in _read_jsonl_rows(data_dir / "detections.jsonl")
            if row["seq_id"] == seq_id
            and int(row.get("revision", revision)) == revision
            and int(row["frame_index"]) == selected_frame
        ]
        truth = [
            row
            for row in _read_jsonl_rows(data_dir / "truth.jsonl")
            if row["seq_id"] == seq_id
            and int(row.get("revision", revision)) == revision
            and int(row["frame_index"]) == selected_frame
        ]
        assignment_by_index = {
            int(row["detection_index"]): row
            for row in assignments
            if row.get("detection_index") is not None
        }
        detection_boxes = []
        for index, row in enumerate(detections):
            assigned = assignment_by_index.get(index, {})
            copy = dict(row)
            copy["pred_track_id"] = assigned.get("pred_track_id")
            copy["track_event"] = assigned.get("track_event")
            detection_boxes.append(copy)
        selected = {
            "available": True,
            "selected_seq_id": seq_id,
            "revision": revision,
            "frame": selected_frame,
            "frame_count": frame_count,
            "canvas": summary.get("config", {}).get("generator", {}).get("canvas", [640, 360]),
            "checkpoint": checkpoint,
            "state_distribution": distribution,
            "track_states": frame_states,
            "detections": detection_boxes,
            "truth_boxes": truth,
            "completion_boxes": completions,
            "metrics": metrics,
            "run_report": run_report,
        }
        return {
            "available": True,
            "summary": summary,
            "selected": selected,
            "recent_errors": self._errors()[:10],
            "verification": self._verification_summary(),
        }

    def _errors(self) -> list[dict[str, Any]]:
        path = self.demo_root / "error_catalog.json"
        if not path.exists():
            return []
        try:
            return read_json(path).get("errors", [])
        except (OSError, ValueError):
            return []

    def _verification_summary(self) -> dict[str, Any]:
        path = self.demo_root / "verification.json"
        if not path.exists():
            return {}
        try:
            verification = read_json(path)
            return {
                "schema": verification.get("schema"),
                "checks": verification.get("checks", {}),
                "environment": verification.get("environment", {}),
            }
        except (OSError, ValueError):
            return {}

    def run_demo(self) -> dict[str, Any]:
        if not self.demo_lock.acquire(blocking=False):
            return {"ok": False, "message": "演示正在运行，请勿重复触发"}
        try:
            environment = dict(**__import__("os").environ)
            environment["PYTHONPATH"] = str(self.base_dir)
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tracker_app",
                    "--base-dir",
                    str(self.base_dir),
                    "demo",
                    "--config",
                    self.config_path,
                ],
                cwd=str(self.base_dir),
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            if process.returncode != 0:
                return {
                    "ok": False,
                    "message": process.stderr.strip() or process.stdout.strip()
                    or f"demo exit code {process.returncode}",
                }
            verification = read_json(self.demo_root / "verification.json")
            return {
                "ok": True,
                "checks": verification.get("checks", {}),
                "message": "演示运行完成",
            }
        finally:
            self.demo_lock.release()


def make_handler(app_state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TraceLab/1.0"

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._send_html(INDEX_HTML)
                return
            if parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            if parsed.path == "/api/health":
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/status":
                query = parse_qs(parsed.query)
                seq_id = query.get("seq_id", [None])[0]
                frame_value = query.get("frame", [None])[0]
                try:
                    frame = int(frame_value) if frame_value is not None else None
                except ValueError:
                    frame = None
                try:
                    self._send_json(app_state.status(seq_id, frame))
                except Exception as exc:
                    self._send_json(
                        {"available": False, "message": str(exc), "summary": {"sequences": []}},
                        status=500,
                    )
                return
            self._send_json({"error": "not_found"}, status=404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/demo":
                try:
                    result = app_state.run_demo()
                    self._send_json(result, status=200 if result.get("ok") else 500)
                except Exception as exc:
                    self._send_json({"ok": False, "message": str(exc)}, status=500)
                return
            self._send_json({"error": "not_found"}, status=404)

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(
                f"[web] {self.address_string()} - {format % args}\n"
            )

    return Handler


def run_server(
    *,
    base_dir: str | Path,
    config_path: str | Path,
    host: str,
    port: int,
    open_browser: bool = True,
) -> int:
    base = Path(base_dir).resolve()
    config_file = Path(config_path).resolve()
    app_state = AppState(base, str(config_file))
    server = ThreadingHTTPServer((host, port), make_handler(app_state))
    url = f"http://{host}:{port}/"
    print(f"TraceLab listening on {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping TraceLab.")
    finally:
        server.server_close()
    return 0
