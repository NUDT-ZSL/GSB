from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import atomic_write_text, canonical_json_text


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transformer 算子诊断工作台</title>
<style>
:root {
  color-scheme: dark;
  --bg: #0d1117;
  --panel: #161b22;
  --panel2: #1f2630;
  --line: #30363d;
  --text: #e6edf3;
  --muted: #8b949e;
  --accent: #58a6ff;
  --ok: #3fb950;
  --warn: #d29922;
  --bad: #f85149;
  --hit: #a371f7;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
}
header {
  padding: 22px 28px 16px; border-bottom: 1px solid var(--line);
  background: linear-gradient(120deg, #111827, #0d1117 55%, #101a2b);
}
h1 { font-size: 22px; margin: 0 0 6px; letter-spacing: .2px; }
.sub { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.wrap { padding: 20px 28px 40px; max-width: 1680px; margin: 0 auto; }
.kpis { display: grid; grid-template-columns: repeat(6, minmax(110px, 1fr)); gap: 10px; margin: 16px 0 18px; }
.kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.kpi .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .6px; }
.kpi .value { font-size: 22px; font-weight: 650; margin-top: 3px; }
.tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 16px; }
.tab {
  background: var(--panel); color: var(--muted); border: 1px solid var(--line);
  padding: 8px 13px; border-radius: 8px; cursor: pointer; font: inherit;
}
.tab.active { color: var(--text); border-color: var(--accent); background: #17233a; }
.tab:hover { color: var(--text); }
.panel { display: none; }
.panel.active { display: block; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 14px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px; min-width: 0; }
.card h2, .card h3 { margin: 0 0 10px; font-size: 15px; }
.job-head { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; margin-bottom: 8px; }
.job-id { font-family: Consolas, "Cascadia Mono", monospace; font-weight: 650; }
.badge {
  display: inline-flex; align-items: center; gap: 5px; border-radius: 999px;
  padding: 2px 8px; font-size: 11px; border: 1px solid var(--line); white-space: nowrap;
}
.status-ok, .status-recomputed { color: var(--ok); border-color: #246b35; background: #102619; }
.status-hit { color: var(--hit); border-color: #613b9e; background: #20172e; }
.status-failed { color: var(--bad); border-color: #8b2f2b; background: #2b1515; }
.status-skipped, .status-degraded { color: var(--warn); border-color: #72551b; background: #261f0e; }
.status-pending, .status-missing { color: var(--muted); }
.chain { display: flex; align-items: stretch; gap: 8px; overflow-x: auto; padding: 4px 0 8px; }
.node {
  min-width: 144px; flex: 1; background: var(--panel2); border: 1px solid var(--line);
  border-radius: 9px; padding: 9px; position: relative;
}
.node:not(:last-child)::after {
  content: "→"; position: absolute; right: -13px; top: 50%; transform: translateY(-50%);
  color: var(--muted); z-index: 2;
}
.node .stage { font-size: 12px; font-weight: 650; }
.node .hash { font: 10px/1.35 Consolas, monospace; color: var(--muted); margin-top: 5px; overflow-wrap: anywhere; }
.node .err { color: var(--bad); font-size: 11px; margin-top: 4px; }
.meta { color: var(--muted); font-size: 11px; margin-top: 7px; overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 550; }
code, .mono { font-family: Consolas, "Cascadia Mono", monospace; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 12px; }
.chip { background: #17233a; border: 1px solid #27446b; border-radius: 7px; padding: 3px 7px; font: 11px Consolas, monospace; }
.chip.bad { background: #2b1515; border-color: #8b2f2b; }
.chip.warn { background: #261f0e; border-color: #72551b; }
.chip.ok { background: #102619; border-color: #246b35; }
.bar-row { display: grid; grid-template-columns: 78px 1fr 120px; gap: 8px; align-items: center; font-size: 11px; margin: 4px 0; }
.bar { height: 8px; background: #0b0f14; border-radius: 999px; overflow: hidden; }
.bar > i { display: block; height: 100%; background: var(--accent); }
.empty { color: var(--muted); font-style: italic; padding: 12px 0; }
.error-item { border-left: 3px solid var(--bad); background: #1d1415; padding: 9px 11px; border-radius: 6px; margin: 8px 0; }
.error-item.warn { border-color: var(--warn); background: #211d12; }
.error-item .code { color: var(--bad); font-weight: 650; }
.error-item.warn .code { color: var(--warn); }
.detail { color: var(--muted); font-size: 11px; margin-top: 4px; overflow-wrap: anywhere; }
.footer { color: var(--muted); font-size: 11px; margin-top: 24px; }
@media (max-width: 900px) { .kpis { grid-template-columns: repeat(2, 1fr); } .wrap { padding: 14px; } header { padding: 18px; } }
</style>
</head>
<body>
<header>
  <h1>Transformer 算子增量计算与诊断工作台</h1>
  <div class="sub" id="header-sub"></div>
</header>
<main class="wrap">
  <section class="kpis" id="kpis"></section>
  <nav class="tabs" id="tabs">
    <button class="tab active" data-tab="topology">计算图拓扑</button>
    <button class="tab" data-tab="invalidation">失效传播</button>
    <button class="tab" data-tab="deviation">偏差谱</button>
    <button class="tab" data-tab="consistency">一致性裁决</button>
    <button class="tab" data-tab="errors">错误与降级</button>
    <button class="tab" data-tab="snapshots">快照版本</button>
  </nav>
  <section class="panel active" id="panel-topology"><div class="grid" id="topology"></div></section>
  <section class="panel" id="panel-invalidation"><div class="grid" id="invalidation"></div></section>
  <section class="panel" id="panel-deviation"><div class="grid" id="deviation"></div></section>
  <section class="panel" id="panel-consistency"><div class="card"><h2>增量与从零全量重算裁决</h2><div id="consistency"></div></div></section>
  <section class="panel" id="panel-errors"><div class="grid" id="errors"></div></section>
  <section class="panel" id="panel-snapshots"><div class="card"><h2>快照整体替换记录</h2><div id="snapshots"></div></div></section>
  <div class="footer">本地静态报告，无外部依赖。数据来源：.workbench/state/report.json。</div>
</main>
<script type="application/json" id="report-data">__REPORT_JSON__</script>
<script>
const report = JSON.parse(document.getElementById('report-data').textContent);
const stages = [
  ['validate_plan', '输入校验与分块计划'],
  ['block_stats', '分块统计'],
  ['normalize', '归一化累加'],
  ['output', '输出写回']
];
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function badge(text, status) {
  return el('span', 'badge status-' + String(status || 'pending'), text);
}
function shortHash(value) {
  return value ? String(value).slice(0, 16) + '…' : '—';
}
function addKpi(label, value, cls) {
  const box = el('div', 'kpi');
  box.appendChild(el('div', 'label', label));
  box.appendChild(el('div', 'value ' + (cls || ''), value));
  $('kpis').appendChild(box);
}
function listCard(title, values, emptyText) {
  const card = el('div', 'card');
  card.appendChild(el('h3', '', title));
  if (!values || !values.length) {
    card.appendChild(el('div', 'empty', emptyText || '无记录'));
    return card;
  }
  const chips = el('div', 'chips');
  values.forEach(v => chips.appendChild(el('span', 'chip', v)));
  card.appendChild(chips);
  return card;
}
function renderHeader() {
  $('header-sub').textContent =
    `run_id=${report.run_id || '—'} · mode=${report.mode || '—'} · ${report.generated_at || ''} · ${report.engine || ''}`;
  const inv = report.invalidation || {};
  const consistency = report.consistency || {};
  addKpi('状态', report.status || '—', 'status-' + (report.status || 'pending'));
  addKpi('受影响 Job', (inv.affected_jobs || []).length);
  addKpi('重算节点', (inv.recomputed_nodes || []).length);
  addKpi('复用节点', (inv.reused_nodes || []).length);
  const byteVerdict = consistency.all_byte_identical === null ? 'SKIP' : (consistency.all_byte_identical ? 'PASS' : 'FAIL');
  addKpi('字节一致', byteVerdict,
    consistency.all_byte_identical === null ? 'status-skipped' : (consistency.all_byte_identical ? 'status-ok' : 'status-failed'));
  addKpi('错误记录', (report.errors || []).length,
    (report.errors || []).length ? 'status-failed' : 'status-ok');
}
function renderTopology() {
  const jobs = Object.values(report.jobs || {});
  if (!jobs.length) {
    $('topology').appendChild(el('div', 'empty', '没有 job 记录'));
    return;
  }
  jobs.sort((a, b) => String(a.job_id).localeCompare(String(b.job_id)));
  jobs.forEach(job => {
    const card = el('div', 'card');
    const head = el('div', 'job-head');
    head.appendChild(el('div', 'job-id', job.job_id));
    head.appendChild(badge(job.status || 'unknown', job.status));
    card.appendChild(head);
    const meta = el('div', 'meta');
    meta.textContent = `${job.spec?.op_kind || '?'} · shape=${JSON.stringify(job.spec?.input_shape || [])} · axis=${job.spec?.axis_normalized ?? job.spec?.axis ?? '?'} · block=${job.spec?.block_size ?? '?'} · dtype=${job.spec?.dtype || '?'} · input=${shortHash(job.input_hash)}`;
    card.appendChild(meta);
    const chain = el('div', 'chain');
    stages.forEach(([stage, label]) => {
      const node = job.nodes?.[stage] || {status: 'missing'};
      const box = el('div', 'node');
      const line = el('div', 'stage', label);
      box.appendChild(line);
      box.appendChild(badge(node.status || 'missing', node.status));
      box.appendChild(el('div', 'hash', `hash ${shortHash(node.content_hash)}`));
      if (node.artifact_version !== undefined) box.appendChild(el('div', 'hash', `v${node.artifact_version}`));
      if (node.reason) box.appendChild(el('div', 'hash', node.reason));
      if (node.error_code) box.appendChild(el('div', 'err', node.error_code));
      if (node.status === 'hit' || node.status === 'recomputed') {
        box.style.borderColor = node.status === 'hit' ? '#613b9e' : '#246b35';
      }
      if (node.status === 'failed') box.style.borderColor = '#8b2f2b';
      chain.appendChild(box);
    });
    card.appendChild(chain);
    $('topology').appendChild(card);
  });
}
function renderInvalidation() {
  const inv = report.invalidation || {};
  const reasons = inv.skip_reasons || [];
  $('invalidation').appendChild(listCard('受影响 job', inv.affected_jobs, '没有失效 job'));
  $('invalidation').appendChild(listCard('未受影响 job', inv.unaffected_jobs, '无'));
  $('invalidation').appendChild(listCard('本次重算 job', inv.recomputed_jobs, '无'));
  $('invalidation').appendChild(listCard('命中复用 job', inv.reused_jobs, '无'));
  $('invalidation').appendChild(listCard('重算节点', inv.recomputed_nodes, '无'));
  $('invalidation').appendChild(listCard('复用节点', inv.reused_nodes, '无'));
  const reasonCard = el('div', 'card');
  reasonCard.appendChild(el('h3', '', '跳过 / 复用记录理由'));
  const table = el('table');
  const thead = el('thead');
  const hr = el('tr');
  ['Job', '阶段', '理由', '上游'].forEach(v => hr.appendChild(el('th', '', v)));
  thead.appendChild(hr); table.appendChild(thead);
  const tbody = el('tbody');
  reasons.forEach(item => {
    const tr = el('tr');
    tr.appendChild(el('td', 'mono', item.job_id || '—'));
    tr.appendChild(el('td', '', item.stage || '—'));
    tr.appendChild(el('td', '', item.reason || item.message || '—'));
    tr.appendChild(el('td', 'mono', item.upstream_code || '—'));
    tbody.appendChild(tr);
  });
  if (!reasons.length) {
    const tr = el('tr'); const td = el('td', 'empty', '无跳过记录'); td.colSpan = 4; tr.appendChild(td); tbody.appendChild(tr);
  }
  table.appendChild(tbody); reasonCard.appendChild(table);
  $('invalidation').appendChild(reasonCard);
}
function renderDeviation() {
  const jobs = Object.values(report.jobs || {}).filter(job => job.diagnostics);
  if (!jobs.length) {
    $('deviation').appendChild(el('div', 'empty', '没有偏差记录'));
    return;
  }
  jobs.sort((a, b) => String(a.job_id).localeCompare(String(b.job_id)));
  jobs.forEach(job => {
    const d = job.diagnostics;
    const dev = d.deviation || {};
    const card = el('div', 'card');
    const head = el('div', 'job-head');
    head.appendChild(el('h3', 'job-id', job.job_id));
    head.appendChild(badge(dev.exceed_count ? '超出容差' : '容差内', dev.exceed_count ? 'failed' : 'ok'));
    card.appendChild(head);
    card.appendChild(el('div', 'meta',
      `max_abs=${dev.max_abs ?? '—'} @ ${JSON.stringify(dev.max_abs_position || [])} · max_rel=${dev.max_rel ?? '—'} · 越界元素=${dev.exceed_count ?? 0} · atol=${dev.atol ?? '?'} rtol=${dev.rtol ?? '?'}`));
    const spectrum = dev.spectrum || [];
    if (spectrum.length) {
      const max = Math.max(...spectrum.map(x => Number(x.abs) || 0), Number.MIN_VALUE);
      const label = el('div', 'meta', '偏差谱（按 |Δ| 排序，前 64 项）');
      card.appendChild(label);
      spectrum.slice(0, 16).forEach(item => {
        const row = el('div', 'bar-row');
        row.appendChild(el('div', 'mono', JSON.stringify(item.position)));
        const bar = el('div', 'bar');
        const fill = el('i');
        fill.style.width = `${Math.max(0.5, (Number(item.abs) / max) * 100)}%`;
        bar.appendChild(fill); row.appendChild(bar);
        row.appendChild(el('div', 'mono', Number(item.abs).toExponential(3)));
        card.appendChild(row);
      });
    }
    const stat = d.stat_consistency || {};
    card.appendChild(el('div', 'meta',
      `统计合并还原：${stat.passed ? 'PASS' : 'FAIL'} · 输出字节一致：${d.byte_identical_to_whole_path ? 'PASS' : 'FAIL'} · 明细：${d.deviation_detail?.report_path || d.deviation_detail?.path || '—'}`));
    $('deviation').appendChild(card);
  });
}
function renderConsistency() {
  const box = $('consistency');
  const consistency = report.consistency || {};
  box.appendChild(el('div', 'meta',
    `总体裁决：${consistency.all_byte_identical === null ? '本次运行跳过独立全量裁决' : (consistency.all_byte_identical ? '增量结果与从零全量重算逐字节一致' : '存在不一致')} · 已完成 job=${(report.completed_job_ids || []).length}`));
  const table = el('table');
  const thead = el('thead'); const hr = el('tr');
  ['Job', '裁决', '增量输出 SHA256', '全量重算 SHA256'].forEach(v => hr.appendChild(el('th', '', v)));
  thead.appendChild(hr); table.appendChild(thead);
  const tbody = el('tbody');
  Object.entries(consistency.jobs || {}).forEach(([jobId, item]) => {
    const tr = el('tr');
    tr.appendChild(el('td', 'mono', jobId));
    const tdStatus = el('td'); tdStatus.appendChild(badge(item.byte_identical ? 'PASS' : 'FAIL', item.byte_identical ? 'ok' : 'failed')); tr.appendChild(tdStatus);
    tr.appendChild(el('td', 'mono', item.incremental_sha256 || '—'));
    tr.appendChild(el('td', 'mono', item.full_recompute_sha256 || '—'));
    tbody.appendChild(tr);
  });
  if (!Object.keys(consistency.jobs || {}).length) {
    const tr = el('tr'); const td = el('td', 'empty', '没有完成一致性裁决的 job'); td.colSpan = 4; tr.appendChild(td); tbody.appendChild(tr);
  }
  table.appendChild(tbody); box.appendChild(table);
}
function renderErrors() {
  const errors = report.errors || [];
  const chains = report.degradation_chains || [];
  const injections = report.injection_records || [];
  const errorCard = el('div', 'card');
  errorCard.appendChild(el('h3', '', `错误记录 (${errors.length})`));
  if (!errors.length) errorCard.appendChild(el('div', 'empty', '本次运行没有业务错误'));
  errors.forEach(error => {
    const box = el('div', 'error-item' + (String(error.code || '').includes('UNATTRIBUTED') ? '' : ' warn'));
    box.appendChild(el('div', 'code', `${error.code || 'UNKNOWN'} · ${error.stage || '—'} · ${error.job_id || 'global'}`));
    box.appendChild(el('div', '', error.message || ''));
    if (error.field) box.appendChild(el('div', 'detail', `field=${error.field}`));
    errorCard.appendChild(box);
  });
  $('errors').appendChild(errorCard);
  const injectionCard = el('div', 'card');
  injectionCard.appendChild(el('h3', '', `异常注入 (${injections.length})`));
  if (!injections.length) injectionCard.appendChild(el('div', 'empty', '无注入'));
  injections.forEach(item => {
    const box = el('div', 'error-item');
    box.appendChild(el('div', 'code', `${item.code} · ${item.stage}`));
    box.appendChild(el('div', 'detail', `job_id=${item.job_id || 'all'} · source=${item.source || '—'}`));
    injectionCard.appendChild(box);
  });
  $('errors').appendChild(injectionCard);
  const chainCard = el('div', 'card');
  chainCard.appendChild(el('h3', '', `显式降级链路 (${chains.length})`));
  if (!chains.length) chainCard.appendChild(el('div', 'empty', '无降级'));
  chains.forEach(item => {
    const box = el('div', 'error-item warn');
    box.appendChild(el('div', 'code', `${item.job_id} · ${item.upstream_stage} → ${item.stage}`));
    box.appendChild(el('div', '', `category=${item.category} · upstream_code=${item.upstream_code} · policy=${item.policy}`));
    chainCard.appendChild(box);
  });
  $('errors').appendChild(chainCard);
  const recentCard = el('div', 'card');
  recentCard.appendChild(el('h3', '', '最近业务错误码'));
  const recent = report.recent_error_codes || [];
  if (!recent.length) recentCard.appendChild(el('div', 'empty', '无历史错误'));
  recent.forEach(item => {
    recentCard.appendChild(el('div', 'detail',
      `${item.at || '—'} · ${item.code || '—'} · ${item.stage || '—'} · ${item.job_id || 'global'} · ${item.message || ''}`));
  });
  $('errors').appendChild(recentCard);
}
function renderSnapshots() {
  const items = report.snapshot_inventory || report.snapshots || [];
  const table = el('table');
  const thead = el('thead'); const hr = el('tr');
  ['类型', 'Key', '版本', '内容校验和', '整体替换', '变更'].forEach(v => hr.appendChild(el('th', '', v)));
  thead.appendChild(hr); table.appendChild(thead);
  const tbody = el('tbody');
  items.forEach(item => {
    const tr = el('tr');
    tr.appendChild(el('td', '', item.kind || '—'));
    tr.appendChild(el('td', 'mono', item.key || '—'));
    tr.appendChild(el('td', '', item.version ?? '—'));
    tr.appendChild(el('td', 'mono', shortHash(item.content_checksum)));
    tr.appendChild(el('td', '', item.replacement || (item.replaced_whole ? 'atomic-whole' : '—')));
    tr.appendChild(el('td', '', item.changed === undefined ? '—' : String(item.changed)));
    tbody.appendChild(tr);
  });
  if (!items.length) {
    const tr = el('tr'); const td = el('td', 'empty', '无快照'); td.colSpan = 6; tr.appendChild(td); tbody.appendChild(tr);
  }
  table.appendChild(tbody); $('snapshots').appendChild(table);
}
document.querySelectorAll('.tab').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    button.classList.add('active');
    $('panel-' + button.dataset.tab).classList.add('active');
  });
});
renderHeader();
renderTopology();
renderInvalidation();
renderDeviation();
renderConsistency();
renderErrors();
renderSnapshots();
</script>
</body>
</html>
"""


def render_workbench(report: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_text(report)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = HTML_TEMPLATE.replace("__REPORT_JSON__", payload)
    atomic_write_text(target, html)
    return target


def load_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

