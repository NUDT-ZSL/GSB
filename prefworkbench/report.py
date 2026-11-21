"""Self-contained local diagnostic HTML report."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import atomic_write_text, read_json


def _embed_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Preference Alignment Diagnostics Workbench</title>
<style>
:root {
  --bg:#f4f1ea; --panel:#fffdf8; --ink:#17211f; --muted:#65706c; --line:#d9d4c8;
  --accent:#116a5c; --accent2:#bb5a2a; --bad:#a83232; --good:#24714f; --chip:#e8eee9;
}
* { box-sizing:border-box; }
body { margin:0; background:linear-gradient(120deg,#f4f1ea,#eef2ed 55%,#f7efe4); color:var(--ink);
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
header { padding:28px clamp(18px,4vw,58px) 18px; border-bottom:1px solid var(--line); }
h1 { margin:0 0 8px; font-size:clamp(24px,3vw,42px); letter-spacing:-.04em; }
h2 { margin:0 0 14px; font-size:18px; }
h3 { margin:0 0 10px; font-size:14px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }
.sub { color:var(--muted); max-width:1100px; line-height:1.55; }
.mono { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }
.chips { display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }
.chip { background:var(--chip); border:1px solid var(--line); border-radius:999px; padding:5px 10px; font-size:12px; }
main { padding:22px clamp(18px,4vw,58px) 60px; display:grid; gap:18px; }
.grid { display:grid; grid-template-columns:repeat(12,1fr); gap:16px; }
.panel { background:rgba(255,253,248,.94); border:1px solid var(--line); border-radius:16px; padding:18px;
  box-shadow:0 8px 30px rgba(41,51,45,.055); overflow:hidden; }
.span-12 { grid-column:span 12; } .span-8 { grid-column:span 8; } .span-6 { grid-column:span 6; }
.span-4 { grid-column:span 4; } .span-3 { grid-column:span 3; }
@media (max-width:950px) { .span-8,.span-6,.span-4,.span-3 { grid-column:span 12; } }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
.card { border:1px solid var(--line); border-radius:12px; padding:13px; background:#fff; }
.card .value { font-size:28px; font-weight:700; letter-spacing:-.04em; }
.card .label { color:var(--muted); font-size:12px; margin-top:4px; }
.controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
input,select { border:1px solid var(--line); background:#fff; color:var(--ink); border-radius:9px; padding:8px 10px; }
input { min-width:230px; }
table { width:100%; border-collapse:collapse; font-size:12.5px; }
th,td { text-align:left; padding:8px 9px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--panel); z-index:1; }
.scroll { max-height:460px; overflow:auto; border:1px solid var(--line); border-radius:10px; }
.barrow { display:grid; grid-template-columns:80px 1fr 42px; gap:8px; align-items:center; margin:6px 0; font-size:12px; }
.bar { height:12px; background:#e8e4da; border-radius:999px; overflow:hidden; }
.bar > span { display:block; height:100%; background:linear-gradient(90deg,var(--accent),#4e9f83); }
.badge { display:inline-block; padding:2px 7px; border-radius:999px; font-size:11px; border:1px solid var(--line); }
.ok { color:var(--good); background:#e7f2eb; } .fail { color:var(--bad); background:#f8e7e7; }
.warn { color:#80531f; background:#f6ecd9; }
.chain { display:flex; flex-wrap:wrap; gap:8px; align-items:stretch; }
.chain .node { flex:1 1 145px; border:1px solid var(--line); border-radius:11px; padding:11px; background:#fff; }
.chain .arrow { align-self:center; color:var(--accent2); font-weight:700; }
.muted { color:var(--muted); } .small { font-size:12px; }
.split { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
@media (max-width:760px) { .split { grid-template-columns:1fr; } }
.legend { display:flex; gap:10px; flex-wrap:wrap; color:var(--muted); font-size:12px; }
.dot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:4px; }
details { border:1px solid var(--line); border-radius:10px; padding:10px; background:#fff; margin:8px 0; }
summary { cursor:pointer; font-weight:600; }
.error { color:var(--bad); }
.tag { font-size:11px; padding:2px 6px; border-radius:6px; background:#edf1ee; margin-right:4px; }
</style>
</head>
<body>
<header>
  <h1>偏好对齐数据构造诊断台</h1>
  <div class="sub">Deterministic preference construction · precision cascade · incremental invalidation. This page is generated from persisted local snapshot categories and makes no network requests.</div>
  <div class="chips" id="metaChips"></div>
</header>
<main>
  <section class="panel span-12">
    <div class="controls">
      <label>精度档 <select id="precisionSelect"></select></label>
      <label>样本过滤 <input id="sampleFilter" placeholder="pair_id / group / band"></label>
      <span class="small muted">三档共享同一参与集合与级联顺序；误差以 float64 为参考真值。</span>
    </div>
  </section>

  <section class="grid span-12" id="cards"></section>

  <section class="panel span-12">
    <h2>误差传播链</h2>
    <div class="chain" id="errorChain"></div>
    <div class="split" style="margin-top:14px">
      <div>
        <h3>主导误差来源</h3>
        <div id="dominantSources"></div>
      </div>
      <div>
        <h3>各级偏差摘要</h3>
        <div class="scroll" style="max-height:250px"><table id="stageTable"></table></div>
      </div>
    </div>
  </section>

  <section class="panel span-4">
    <h2>质量分分布</h2>
    <div id="qualityHist"></div>
  </section>
  <section class="panel span-4">
    <h2>一致性权重分布</h2>
    <div id="consistencyHist"></div>
  </section>
  <section class="panel span-4">
    <h2>最终采样权重分布</h2>
    <div id="weightHist"></div>
  </section>

  <section class="panel span-12">
    <h2>清单与逐级追踪</h2>
    <div class="small muted" style="margin-bottom:8px">每一行均可由质量贡献、去重权重、一致性权重、归一化分母独立重算最终权重；置信区间以 float64 为参考。</div>
    <div class="scroll"><table id="manifestTable"></table></div>
  </section>

  <section class="panel span-6">
    <h2>保留集 / 剔除集</h2>
    <div id="decisionSets"></div>
  </section>
  <section class="panel span-6">
    <h2>配额达成与欠配归因</h2>
    <div class="scroll" style="max-height:330px"><table id="quotaTable"></table></div>
  </section>

  <section class="panel span-6">
    <h2>失效传播覆盖范围</h2>
    <div id="invalidation"></div>
  </section>
  <section class="panel span-6">
    <h2>业务异常与健康检查</h2>
    <div id="errorsAndChecks"></div>
  </section>

  <section class="panel span-12">
    <h2>样本级误差归因</h2>
    <div class="scroll"><table id="sampleErrorTable"></table></div>
  </section>
</main>
<script id="diagnostic-data" type="application/json">__DATA__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById('diagnostic-data').textContent);
  const manifest = data.manifest, stats = data.statistics, spectrum = data.error_spectrum;
  const tiers = stats.precision_order;
  const fmt = (v, digits=6) => v === null || v === undefined ? '—' : Number(v).toFixed(digits);
  const pct = v => `${(100*Number(v||0)).toFixed(1)}%`;
  const el = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; };
  const chip = text => { const n=el('span','chip',text); return n; };

  document.getElementById('metaChips').append(
    chip(`snapshot v${data.version}`),
    chip(`checksum ${data.checksum.slice(0,16)}…`),
    chip(`logic ${stats.logic_digest.slice(0,16)}…`),
    chip(`valid ${stats.valid_sample_count}`),
    chip(`invalid ${stats.invalid_sample_count}`),
    chip(`mode ${stats.execution_mode}`)
  );

  const select = document.getElementById('precisionSelect');
  tiers.forEach(t => { const o=el('option',null,t); o.value=t; select.append(o); });
  select.value = stats.reference_precision;

  function currentTier() { return select.value; }
  function hist(container, distribution, color) {
    container.replaceChildren();
    if (!distribution || !distribution.counts.length) { container.append(el('div','muted','no data')); return; }
    const max = Math.max(...distribution.counts, 1);
    distribution.counts.forEach((count, i) => {
      const row = el('div','barrow');
      const lo = distribution.bins[i];
      const hi = i+1 < distribution.bins.length ? distribution.bins[i+1] : distribution.max;
      row.append(el('span','mono',`${fmt(lo,3)}–${fmt(hi,3)}`));
      const bar = el('div','bar'); const fill = el('span'); fill.style.width = `${100*count/max}%`; fill.style.background=color; bar.append(fill);
      row.append(bar, el('span',null,String(count))); container.append(row);
    });
    container.append(el('div','small muted',`min ${fmt(distribution.min)} · max ${fmt(distribution.max)}`));
  }

  function renderCards(tier) {
    const result = stats[tier === 'float64' ? 'quality' : 'quality'][tier];
    const part = manifest.participation[tier];
    const tierSpectrum = spectrum.tiers[tier];
    const finalError = tierSpectrum.selected_weight_error;
    const cards = [
      ['有效样本', stats.valid_sample_count, '异常样本不进入清单和误差统计'],
      ['质量门限后', part.quality_retained.length, '质量门限参与判定由 float64 固定'],
      ['近重复保留', part.dedup_retained.length, '同分按 pair_id 升序打破平局'],
      ['最终清单', part.selected.length, `配额实际 ${stats.sampling[tier].sampling_plan.achieved_total}`],
      ['清单校验和', manifest.precision_manifests[tier].manifest_checksum.slice(0,10)+'…', '内容寻址且可重算'],
      ['平均最终权重误差', fmt(finalError.mean_absolute, 8), `最大 ${fmt(finalError.max_absolute, 8)}`],
      ['最终误差放大比', fmt(tierSpectrum.final_amplification_ratio.mean, 3), `最大 ${fmt(tierSpectrum.final_amplification_ratio.max, 3)}`],
      ['失效覆盖', pct(stats.invalidation.coverage.affected_ratio), `${stats.invalidation.coverage.affected_samples}/${stats.invalidation.coverage.valid_samples} valid pair_id`]
    ];
    const root = document.getElementById('cards');
    root.replaceChildren();
    cards.forEach(([label,value,note]) => {
      const card = el('div','card'); card.append(el('div','value',String(value)),el('div','label',label),el('div','small muted',note)); root.append(card);
    });
  }

  function renderChain(tier) {
    const stageSummaries = spectrum.tiers[tier].stage_summaries;
    const container = document.getElementById('errorChain'); container.replaceChildren();
    const labels = {weighted_scoring:'质量加权',threshold_comparison:'门限比较',consistency_smoothing:'一致性平滑',normalization:'采样归一化',quantization:'精度量化'};
    stats.stage_widths[tier] && Object.keys(stats.stage_widths[tier]);
    const order = spectrum.stage_order;
    order.forEach((stage, idx) => {
      const s = stageSummaries[stage];
      const node = el('div','node');
      node.append(el('div','small muted',labels[stage]||stage), el('div','',`max |Δ| ${fmt(s.max_absolute_deviation,8)}`),
        el('div','small muted',`rel ${fmt(s.max_relative_deviation,6)}`), el('div','small muted',`amp ${fmt(s.max_amplification_ratio,3)}`));
      container.append(node); if (idx < order.length-1) container.append(el('span','arrow','→'));
    });
    const dom = document.getElementById('dominantSources'); dom.replaceChildren();
    Object.entries(spectrum.tiers[tier].dominant_source_counts).sort().forEach(([source,count]) => {
      const row = el('div','barrow'); row.append(el('span',null,source), (()=>{const b=el('div','bar'); const f=el('span'); f.style.width=`${100*count/Math.max(partCount(tier),1)}%`; f.style.background=source==='none'?'#8ca69a':source==='quantization'?'#bb5a2a':'#116a5c'; b.append(f); return b;})(), el('span',null,String(count))); dom.append(row);
    });
    const table = document.getElementById('stageTable'); table.replaceChildren();
    const head = el('tr'); ['stage','max abs','max rel','max amp','interval'].forEach(t=>head.append(el('th',null,t))); table.append(head);
    order.forEach(stage => { const s=stageSummaries[stage]; const tr=el('tr'); tr.append(el('td',null,stage),el('td',null,fmt(s.max_absolute_deviation,8)),el('td',null,fmt(s.max_relative_deviation,6)),el('td',null,fmt(s.max_amplification_ratio,3)),el('td','mono',`${fmt(s.error_interval_union[0],6)} … ${fmt(s.error_interval_union[1],6)}`)); table.append(tr); });
  }
  function partCount(tier){ return manifest.participation[tier].selected.length; }

  function renderManifest(tier) {
    const tierSpectrum = spectrum.tiers[tier];
    const entries = manifest.precision_manifests[tier].entries;
    const filter = document.getElementById('sampleFilter').value.toLowerCase().trim();
    const table = document.getElementById('manifestTable'); table.replaceChildren();
    const head = el('tr'); ['pair_id','group','band','quality','dedup','consistency','raw','normalized','final','95% error interval','checksum trace'].forEach(t=>head.append(el('th',null,t))); table.append(head);
    entries.filter(e => !filter || `${e.pair_id} ${e.annotator_group} ${e.quality_band}`.toLowerCase().includes(filter)).forEach(e => {
      const detail = tierSpectrum.per_sample[e.pair_id]; const ci = detail && detail.weight_confidence_interval;
      const tr=el('tr'); tr.append(el('td','mono',e.pair_id),el('td',null,e.annotator_group),el('td',null,e.quality_band),el('td',null,fmt(e.quality_score)),el('td',null,fmt(e.dedup_weight)),el('td',null,fmt(e.consistency_weight)),el('td',null,fmt(e.raw_sampling_weight)),el('td',null,fmt(e.normalized_sampling_weight)),el('td',null,fmt(e.final_weight)),el('td','mono',ci?`[${fmt(ci.interval[0])}, ${fmt(ci.interval[1])}]`:fmt(e.final_weight)),el('td','mono small',(e.trace.stage_cache_keys.quality||'').slice(0,12)+'…'));
      table.append(tr);
    });
  }

  function renderDecisions(tier) {
    const part = manifest.participation[tier];
    const root = document.getElementById('decisionSets'); root.replaceChildren();
    const sets = [
      ['valid',part.valid],['selected / retained',part.selected],['quality excluded',part.quality_excluded],['duplicate removed',part.dedup_removed],['sampling unselected',part.sampling_unselected]
    ];
    sets.forEach(([name,ids]) => {
      const d=el('details'); d.append(el('summary',null,`${name} · ${ids.length}`)); d.append(el('div','mono small',ids.join(', ')||'—')); root.append(d);
    });
    const inter = stats.diagnostics.decision_sets.retained_excluded_intersection;
    root.append(el('div', inter.length?'error small':'ok small', inter.length?`交集异常: ${inter.join(',')}`:'保留集与剔除集无交集 · partition complete'));
  }

  function renderQuota(tier) {
    const quota = stats.sampling[tier].sampling_plan.quota_results;
    const table=document.getElementById('quotaTable'); table.replaceChildren();
    const head=el('tr'); ['scope','target','actual','available','reason'].forEach(t=>head.append(el('th',null,t))); table.append(head);
    quota.groups.forEach(q=>{const tr=el('tr');tr.append(el('td',null,'group '+q.group),el('td',null,q.target),el('td',null,q.actual),el('td',null,q.available),el('td',null,q.reason||'met'));table.append(tr);});
    quota.quality.forEach(q=>{const tr=el('tr');tr.append(el('td',null,'quality '+q.quality_band),el('td',null,q.target),el('td',null,q.actual),el('td',null,q.available),el('td',null,q.reason||'met'));table.append(tr);});
    const t=quota.total; const tr=el('tr');tr.append(el('td',null,'total'),el('td',null,t.target),el('td',null,t.actual),el('td',null,t.effective_target),el('td',null,t.reason||'met'));table.append(tr);
  }

  function renderInvalidation() {
    const inv=stats.invalidation; const root=document.getElementById('invalidation'); root.replaceChildren();
    root.append(el('div','small','mode: '+inv.mode),el('div','small','changes: '+(inv.input_changes.join(', ')||'none')));
    root.append(el('div','small','recomputed: '+(inv.recomputed_levels.join(' → ')||'none')),el('div','small','reused: '+(inv.reused_levels.join(', ')||'none')));
    root.append(el('div','small',`affected ${inv.coverage.affected_samples}/${inv.coverage.valid_samples} (${pct(inv.coverage.affected_ratio)})`));
    const table=el('table'); const head=el('tr'); ['stage','affected pair_ids','count'].forEach(t=>head.append(el('th',null,t)));table.append(head);
    Object.entries(inv.affected_pair_ids).forEach(([stage,ids])=>{const tr=el('tr');tr.append(el('td',null,stage),el('td','mono small',ids.slice(0,10).join(', ')+(ids.length>10?' …':'')),el('td',null,ids.length));table.append(tr);});
    root.append(table);
    if (inv.mode==='incremental') root.append(el('div','small muted',`previous logic ${String(inv.previous_logic_digest).slice(0,16)}…`));
  }

  function renderErrorsChecks() {
    const root=document.getElementById('errorsAndChecks'); root.replaceChildren();
    const checks=stats.diagnostics.checks;
    Object.entries(checks).forEach(([name,ok])=>{const d=el('div',ok?'ok small':'fail small',`${ok?'PASS':'FAIL'} · ${name}`);root.append(d);});
    const codes=stats.diagnostics.recent_error_codes;
    root.append(el('h3',null,'最近业务错误码'));
    if (data.recent_business_error) {
      const e=data.recent_business_error;
      root.append(el('div','small fail',`last fatal: ${e.code} · ${e.field||''} · ${e.pair_id||''}`));
    }
    if (!codes.length) root.append(el('div','small muted','none'));
    codes.forEach(item=>root.append(el('div','small error',`${item.code} × ${item.count}`)));
    if (stats.errors.length) {
      const d=el('details'); d.append(el('summary',null,`样本异常明细 · ${stats.errors.length}`));
      stats.errors.forEach(e=>d.append(el('div','small error mono',`${e.code} ${e.pair_id||''} ${e.field||''} ${e.message}`))); root.append(d);
    }
    root.append(el('div','small muted',`manifest traceability: ${JSON.stringify(stats.diagnostics.manifest_traceability)}`));
  }

  function renderSampleErrors(tier) {
    const tierSpectrum=spectrum.tiers[tier]; const table=document.getElementById('sampleErrorTable'); table.replaceChildren();
    const filter=document.getElementById('sampleFilter').value.toLowerCase().trim();
    const head=el('tr');['pair_id','selected','dominant','stage','ref','tier','abs','rel','amplification'].forEach(t=>head.append(el('th',null,t)));table.append(head);
    Object.entries(tierSpectrum.per_sample).filter(([pid])=>!filter||pid.includes(filter)).forEach(([pid,record])=>{
      const rows=record.attribution_details.length?record.attribution_details:[null];
      rows.forEach((s,index)=>{const tr=el('tr');tr.append(el('td','mono',index===0?pid:''),el('td',null,index===0?String(record.selected):''),el('td',null,index===0?record.dominant_source:''),el('td',null,s?s.stage:''),el('td',null,s?fmt(s.reference,8):''),el('td',null,s?fmt(s.tier_value,8):''),el('td',null,s?fmt(s.absolute_deviation,8):''),el('td',null,s?fmt(s.relative_deviation,6):''),el('td',null,s?fmt(s.amplification_vs_weighted_scoring,3):''));table.append(tr);});
    });
  }

  function renderAll(){
    const tier=currentTier();
    renderCards(tier); renderChain(tier); renderManifest(tier); renderDecisions(tier); renderQuota(tier); renderInvalidation(); renderErrorsChecks(); renderSampleErrors(tier);
    hist(document.getElementById('qualityHist'), stats.diagnostics.distributions[tier].quality, '#116a5c');
    hist(document.getElementById('consistencyHist'), stats.diagnostics.distributions[tier].consistency, '#bb5a2a');
    hist(document.getElementById('weightHist'), stats.diagnostics.distributions[tier].final_weight, '#4d6d9a');
  }
  select.addEventListener('change',renderAll);
  document.getElementById('sampleFilter').addEventListener('input',()=>{renderManifest(currentTier());renderSampleErrors(currentTier());});
  renderAll();
})();
</script>
</body>
</html>
"""


def write_snapshot_report(snapshot: dict[str, Any], path: str | Path) -> None:
    recent_error = None
    journal_path = Path(path).parent / "last_error.json"
    if journal_path.exists():
        try:
            journal = read_json(journal_path)
            recent_error = journal.get("latest_business_error")
        except (OSError, ValueError):
            recent_error = {
                "code": "E_CACHE_CORRUPT",
                "message": "last_error.json is unreadable",
            }
    payload = {
        "version": snapshot["version"],
        "checksum": snapshot["checksum"],
        "manifest": snapshot["categories"]["manifest"]["payload"],
        "statistics": snapshot["categories"]["statistics"]["payload"],
        "error_spectrum": snapshot["categories"]["error_spectrum"]["payload"],
        "recent_business_error": recent_error,
    }
    html = HTML_TEMPLATE.replace("__DATA__", _embed_json(payload))
    atomic_write_text(path, html)

