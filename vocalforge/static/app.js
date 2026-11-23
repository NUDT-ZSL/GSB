const App = {
  state: null,
  alignment: null,
  spectrum: null,
  injectionResult: null,
  loading: 0,
};

const TITLES = {
  overview: "运行总览",
  embeddings: "音色嵌入",
  alignment: "逐帧对齐",
  precision: "精度档",
  errorprop: "误差谱",
  adaptive: "自适应路径",
  similarity: "相似度裁决",
  snapshot: "快照与诊断",
  consistency: "一致性自检",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmt(value, digits = 6) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number === 0) return "0";
  const absolute = Math.abs(number);
  if (absolute < 0.0001 || absolute >= 1000000) return number.toExponential(3);
  return number.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "");
}

function shortHash(value, head = 12) {
  if (!value) return "—";
  return `${String(value).slice(0, head)}…${String(value).slice(-6)}`;
}

function setLoading(active) {
  App.loading += active ? 1 : -1;
  App.loading = Math.max(0, App.loading);
  $("#loading-bar").classList.toggle("active", App.loading > 0);
}

async function api(path, options = {}) {
  setLoading(true);
  try {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const payload = await response.json();
    if (!response.ok && !payload.error) {
      throw new Error(`HTTP ${response.status}`);
    }
    return payload;
  } finally {
    setLoading(false);
  }
}

function toast(title, message = "", kind = "ok") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.innerHTML = `<strong>${esc(title)}</strong>${message ? `<small>${esc(message)}</small>` : ""}`;
  $("#toast-stack").appendChild(node);
  setTimeout(() => node.remove(), 4200);
}

function failToast(payload, fallback = "操作失败") {
  const error = payload?.error;
  toast(error?.code || fallback, error?.message || "", "bad");
}

async function loadState() {
  try {
    const state = await api("/api/state");
    App.state = state;
    renderAll();
  } catch (error) {
    toast("本地引擎连接失败", String(error), "bad");
  }
}

function renderAll() {
  const s = App.state;
  if (!s) return;
  $("#side-status").textContent = `快照 v${s.snapshot.current.version} · ${s.app.sample_rate / 1000} kHz`;
  $("#side-meta").textContent = `${s.dataset.utterance_count} utterances · ${s.dataset.total_frames} frames`;
  $("#footer-digest").textContent = `dataset ${shortHash(s.app.dataset_digest, 18)}`;
  $$("#profile-switch button").forEach((button) => {
    button.classList.toggle("active", button.dataset.profile === s.config.profile);
  });
  renderOverview();
  renderEmbeddings();
  renderPrecision();
  renderSpectrum(s.error_spectrum);
  renderAdaptive();
  renderSimilarity();
  renderSnapshot();
  renderConsistency();
  if (s.config.profile !== $("#alignment-profile").value) {
    $("#alignment-profile").value = s.config.profile;
  }
}

function metricCard(label, value, unit, foot) {
  return `
    <div class="metric-card">
      <div class="metric-label">${esc(label)}</div>
      <div class="metric-value">${esc(value)} <small>${esc(unit || "")}</small></div>
      <div class="metric-foot">${esc(foot || "")}</div>
    </div>`;
}

function renderOverview() {
  const s = App.state;
  $("#hero-chips").innerHTML = [
    `seed ${s.app.dataset_seed}`,
    `${s.app.sample_rate} Hz`,
    `${s.app.frame_ms} ms frame`,
    `${s.app.frame_hop_ms} ms hop`,
    `${s.dataset.controlled_phonemes.length} controlled phonemes`,
    `snapshot v${s.snapshot.current.version}`,
  ].map((item) => `<span class="chip">${esc(item)}</span>`).join("");

  $("#metric-grid").innerHTML = [
    metricCard("SPEAKERS", s.dataset.speakers.length, "人", "固定参考音色片段"),
    metricCard("FRAMES", s.dataset.total_frames, "帧", "逐帧真值已对齐"),
    metricCard("UTTERANCES", s.dataset.utterance_count, "条", "16 kHz mono PCM"),
    metricCard("SNAPSHOT", `v${s.snapshot.current.version}`, "", `校验 ${shortHash(s.snapshot.current.checksum)}`),
  ].join("");

  const pipeline = [
    ["01", "确定性 PCM 合成", `${s.dataset.speakers.length} speakers · seed ${s.app.dataset_seed}`],
    ["02", "帧级特征与对齐", `${s.dataset.total_frames} frames · ${s.precision.labels_invariant ? "label invariant" : "label drift"}`],
    ["03", "参考音色嵌入", `${Object.keys(s.embeddings.speakers).length} speaker summaries`],
    ["04", "跨精度误差谱", `dominant · ${s.error_spectrum.dominant_source}`],
    ["05", "路径与门限裁决", `${s.adaptive.decision} · ${s.similarity.strategy}`],
    ["06", "快照整体替换", `v${s.snapshot.current.version} · ${s.snapshot.verification.ok ? "verified" : "failed"}`],
  ];
  $("#pipeline").innerHTML = pipeline.map(([index, name, meta]) => `
    <div class="pipeline-row">
      <span class="pipeline-index">${index}</span>
      <span class="pipeline-name">${esc(name)}</span>
      <span class="pipeline-meta">${esc(meta)}</span>
    </div>`).join("");
  const allGood = s.precision.labels_invariant && s.snapshot.verification.ok;
  setBadge("#pipeline-status", allGood ? "READY" : "ATTENTION", allGood ? "ok" : "warn");
  $("#decision-pill").textContent = s.adaptive.decision;
  $("#adaptive-summary").innerHTML = [
    ["决策", s.adaptive.decision],
    ["比较缩减", `${(s.adaptive.comparison_estimate.reduction_ratio * 100).toFixed(2)}%`],
    ["内存余量", `${fmt(s.adaptive.observations.memory_margin_mb, 3)} MB`],
    ["音素覆盖", `${s.adaptive.phoneme_coverage.candidate_union_size}/${s.adaptive.phoneme_coverage.required}`],
    ["决策摘要", shortHash(s.adaptive.decision_digest, 18)],
  ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  renderRecentErrors();
}

function renderRecentErrors() {
  const errors = App.state.recent_errors || [];
  const node = $("#recent-errors");
  if (!errors.length) {
    node.innerHTML = `<div class="empty-state">当前快照没有记录业务错误。</div>`;
    return;
  }
  node.innerHTML = errors.slice().reverse().map((row) => `
    <div class="event-row">
      <span class="event-seq">#${row.sequence}</span>
      <span class="event-code">${esc(row.code)}</span>
      <span class="event-stage">${esc(row.stage)}${row.injection_stage ? ` · inject:${esc(row.injection_stage)}` : ""}</span>
      <span class="status-badge warn">${esc(row.degradation.category)}</span>
    </div>`).join("");
}

function renderEmbeddings() {
  const s = App.state;
  const summaries = s.embeddings.speakers;
  $("#embedding-grid").innerHTML = Object.values(summaries).map((summary) => {
    const max = Math.max(...summary.normalized.map((v) => Math.abs(v)), 1e-9);
    const bars = summary.normalized.map((value) => {
      const height = Math.max(3, Math.min(100, Math.abs(value) / max * 100));
      return `<div class="vector-bar" style="height:${height}%" title="${fmt(value, 8)}"></div>`;
    }).join("");
    const referenceCount = summary.reference_versions.length;
    return `
      <div class="embedding-card">
        <div class="embedding-head">
          <span class="speaker-id">S${String(summary.speaker_id).padStart(2, "0")}</span>
          <span class="status-badge ok">v${summary.embedding_version}</span>
        </div>
        <div class="vector-strip">${bars}</div>
        <div class="embedding-meta">
          <span class="hash">${shortHash(summary.embedding_digest, 16)}</span>
          <span class="dim">${referenceCount} reference clip${referenceCount > 1 ? "s" : ""} · content-addressed</span>
          <span class="dim">refs ${shortHash(summary.reference_content_hash, 10)}</span>
        </div>
      </div>`;
  }).join("");

  const current = $("#registration-speaker").value;
  $("#registration-speaker").innerHTML = s.dataset.speakers
    .map((id) => `<option value="${id}">speaker ${String(id).padStart(2, "0")}</option>`).join("");
  if (current && s.dataset.speakers.map(String).includes(current)) {
    $("#registration-speaker").value = current;
  }

  $("#reference-table").innerHTML = s.dataset.references.map((ref) => `
    <tr>
      <td class="mono">${esc(ref.ref_id)}</td>
      <td>S${String(ref.speaker_id).padStart(2, "0")}</td>
      <td>v${ref.version}</td>
      <td class="mono">${ref.clone_seed}</td>
      <td class="mono">${esc(ref.shape.join(" × "))}</td>
      <td class="mono">${shortHash(ref.content_hash, 14)}</td>
    </tr>`).join("");

  const registration = s.last_registration;
  setBadge("#drift-status", registration.drifts.length ? "DRIFT DETECTED" : "NO CHANGE", registration.drifts.length ? "warn" : "ok");
  if (!registration.drifts.length) {
    $("#registration-result").innerHTML = `<div class="empty-state">尚未追加新的注册片段。点击“追加注册样本”触发内容哈希比较、局部重估与漂移计算。</div>`;
  } else {
    const drift = registration.drifts[0];
    $("#registration-result").innerHTML = [
      ["变化说话人", registration.changed_speakers.join(", ")],
      ["复用说话人", registration.unchanged_speakers.join(", ")],
      ["L2 漂移", fmt(drift.drift_normalized_l2, 10)],
      ["相对漂移", fmt(drift.drift_relative, 10)],
      ["余弦距离", fmt(drift.cosine_distance, 10)],
      ["嵌入版本", `${drift.old_embedding_version} → ${drift.new_embedding_version}`],
      ["独立复算摘要", shortHash(drift.recompute_digest, 20)],
    ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  }
  const guard = s.recompute_guard;
  const guardOk = guard.disjoint && guard.covers_all;
  setBadge("#recompute-status", guardOk ? "GUARDED" : "BROKEN", guardOk ? "ok" : "bad");
  $("#recompute-guard").innerHTML = [
    ["重算帧", registration.recomputed_frames.length],
    ["复用帧", registration.reused_frames.length],
    ["受影响帧", registration.affected_frames.length],
    ["集合交集", guard.overlap.length],
    ["缺失帧", guard.missing.length],
    ["并集覆盖", guard.covers_all ? "complete" : "incomplete"],
  ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
}

function renderPrecision() {
  const s = App.state;
  $("#precision-active").textContent = s.config.profile;
  const fractions = { float64: "64 bit", float32: "32 bit", fixed: "scaled int" };
  $("#precision-grid").innerHTML = s.precision.profiles.map((profile) => `
    <div class="precision-card ${profile.profile === s.config.profile ? "active" : ""}">
      <div class="eyebrow">${esc(fractions[profile.profile] || "")}</div>
      <div class="precision-name">${esc(profile.profile)}</div>
      <div class="stack">
        <div class="kv"><span>label mismatch</span><span>${profile.alignment_label_mismatches}</span></div>
        <div class="kv"><span>feature</span><span>${shortHash(profile.feature_digest, 10)}</span></div>
        <div class="kv"><span>score</span><span>${shortHash(profile.score_digest, 10)}</span></div>
        <div class="kv"><span>fixed config</span><span>${profile.profile === "fixed" ? `${s.config.fixed_scale} / ${s.config.rounding_mode}` : "n/a"}</span></div>
      </div>
    </div>`).join("");
  $("#precision-table").innerHTML = s.precision.profiles.map((profile) => `
    <tr>
      <td class="mono">${esc(profile.profile)}</td>
      <td class="mono">${shortHash(profile.feature_digest, 14)}</td>
      <td class="mono">${shortHash(profile.alignment_digest, 14)}</td>
      <td class="mono">${shortHash(profile.score_digest, 14)}</td>
      <td>${profile.alignment_label_mismatches}</td>
      <td class="mono">${profile.profile === "fixed" ? `${s.config.fixed_scale} / ${s.config.rounding_mode}` : "—"}</td>
    </tr>`).join("");
}

function renderSpectrum(spectrum) {
  if (!spectrum) return;
  App.spectrum = spectrum;
  $("#spectrum-profile").value = spectrum.target_profile;
  $("#dominant-source").textContent = spectrum.dominant_source;
  const levels = spectrum.level_order;
  $("#spectrum-stats").innerHTML = levels.map((level) => `
    <div class="spectrum-stat">
      <span class="dim">${esc(level.replaceAll("_", " "))}</span>
      <strong>${fmt(spectrum.levels[level].absolute_deviation.p95, 5)}</strong>
    </div>`).join("");
  $("#spectrum-digest").textContent = `digest ${shortHash(spectrum.spectrum_digest, 16)}`;
  $("#spectrum-verified").textContent = "RECOMPUTED";

  const maxP95 = Math.max(...levels.map((level) => spectrum.levels[level].absolute_deviation.p95), 1e-12);
  $("#amplification-chain").innerHTML = levels.map((level) => {
    const p95 = spectrum.levels[level].absolute_deviation.p95;
    const value = p95 > 0 ? Math.max(2, p95 / maxP95 * 100) : 2;
    const ratio = spectrum.levels[level].amplification_ratio;
    return `
      <div class="amplification-row">
        <span class="dim">${esc(level.replaceAll("_", " "))}</span>
        <div class="bar-track"><div class="bar-fill ${ratio > 2 ? "warn" : ""}" style="width:${value}%"></div></div>
        <span class="mono">${fmt(ratio, 4)}×</span>
      </div>`;
  }).join("");

  $("#spectrum-table").innerHTML = levels.map((level) => {
    const item = spectrum.levels[level];
    return `
      <tr>
        <td class="mono">${esc(level)}</td>
        <td>${fmt(item.absolute_deviation.p50, 8)}</td>
        <td>${fmt(item.absolute_deviation.p95, 8)}</td>
        <td>${fmt(item.absolute_deviation.max, 8)}</td>
        <td>${fmt(item.relative_deviation.p95, 8)}</td>
        <td>${fmt(item.amplification_ratio, 6)}×</td>
        <td>${item.intervals.length}</td>
        <td>${item.detail_count}</td>
      </tr>`;
  }).join("");
  drawErrorChart(spectrum);
}

function drawErrorChart(spectrum) {
  const canvas = $("#error-chart");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(320, rect.width) * dpr;
  canvas.height = 260 * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const width = Math.max(320, rect.width);
  const height = 260;
  ctx.clearRect(0, 0, width, height);
  const levels = spectrum.level_order;
  const values = levels.map((level) => Math.max(spectrum.levels[level].absolute_deviation.p95, 1e-12));
  const maxLog = Math.max(...values.map((v) => Math.log10(v)), -5);
  const minLog = Math.min(...values.map((v) => Math.log10(v)), maxLog - 1);
  const bottom = 52;
  const top = 18;
  const chartHeight = height - bottom - top;
  const left = 42;
  const chartWidth = width - left - 12;
  ctx.strokeStyle = "rgba(144,177,190,.12)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = top + chartHeight * i / 4;
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(width - 8, y); ctx.stroke();
  }
  const barWidth = chartWidth / levels.length;
  levels.forEach((level, index) => {
    const logValue = Math.log10(values[index]);
    const ratio = (logValue - minLog) / Math.max(maxLog - minLog, 1e-9);
    const barHeight = Math.max(4, ratio * chartHeight);
    const x = left + index * barWidth + barWidth * 0.18;
    const y = top + chartHeight - barHeight;
    const gradient = ctx.createLinearGradient(0, y, 0, top + chartHeight);
    gradient.addColorStop(0, "#76e8cb");
    gradient.addColorStop(1, "#2e817e");
    ctx.fillStyle = gradient;
    roundRect(ctx, x, y, barWidth * 0.64, barHeight, 5);
    ctx.fill();
    ctx.fillStyle = "#9db6bb";
    ctx.font = "9px monospace";
    ctx.textAlign = "center";
    const label = level.replaceAll("_", " ");
    ctx.fillText(label.split(" ")[0], x + barWidth * 0.32, height - 28);
    ctx.fillText(label.split(" ")[1] || "", x + barWidth * 0.32, height - 17);
    ctx.fillStyle = "#d9efec";
    ctx.fillText(values[index].toExponential(2), x + barWidth * 0.32, Math.max(12, y - 6));
  });
  ctx.fillStyle = "#53656f";
  ctx.font = "9px monospace";
  ctx.textAlign = "left";
  ctx.fillText("log scale · p95", left, 12);
}

function roundRect(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + width, y, x + width, y + height, r);
  ctx.arcTo(x + width, y + height, x, y + height, r);
  ctx.arcTo(x, y + height, x, y, r);
  ctx.arcTo(x, y, x + width, y, r);
  ctx.closePath();
}

function renderAdaptive() {
  const s = App.state;
  const adaptive = s.adaptive;
  $("#adaptive-decision").textContent = adaptive.decision;
  const observations = adaptive.observations;
  $("#adaptive-metrics").innerHTML = [
    metricCard("SPEAKERS", observations.speaker_count, "人", `阈值 ≤ ${adaptive.thresholds.max_exact_speakers}`),
    metricCard("FRAMES", observations.total_frames, "帧", `阈值 ≤ ${adaptive.thresholds.max_exact_frames}`),
    metricCard("PHONEMES", observations.controlled_phoneme_count, "类", `阈值 ≤ ${adaptive.thresholds.max_exact_phonemes}`),
    metricCard("MEMORY MARGIN", fmt(observations.memory_margin_mb, 3), "MB", `budget ${fmt(observations.memory_budget_mb, 1)} MB`),
  ].join("");
  $("#adaptive-digest").textContent = `digest ${shortHash(adaptive.decision_digest, 15)}`;
  $("#adaptive-reasons").innerHTML = [
    ["决策", adaptive.decision],
    ["原因", adaptive.reasons.join(" · ")],
    ["帧移", `${adaptive.path_parameters.frame_hop_ms} ms`],
    ["相似度", adaptive.path_parameters.similarity_mode],
    ["归一化", adaptive.path_parameters.normalization_mode],
    ["候选/锚点", adaptive.path_parameters.candidate_count_per_anchor],
  ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  $("#phoneme-coverage").innerHTML = (s.selected_path.candidate_phonemes || []).map((id) => `
    <span class="phoneme-chip">P${esc(id)} <span class="dim">covered</span></span>`).join("");
  const exact = adaptive.comparison_estimate.exact;
  const approximate = adaptive.comparison_estimate.approximate;
  const selected = adaptive.comparison_estimate.selected;
  const max = Math.max(exact, approximate, 1);
  $("#comparison-bars").innerHTML = [
    ["exact", exact, "bar-fill"],
    ["approximate", approximate, "bar-fill warn"],
    ["selected", selected, "bar-fill"],
  ].map(([name, value, cls]) => `
    <div class="comparison-row">
      <span class="dim">${esc(name)}</span>
      <div class="bar-track"><div class="${cls}" style="width:${Math.max(2, value / max * 100)}%"></div></div>
      <span class="mono">${Number(value).toLocaleString()}</span>
    </div>`).join("");
  const path = s.path_consistency;
  $("#path-verdict").textContent = (path.verdict || "unknown").toUpperCase();
  setBadge("#path-verdict", (path.verdict || "unknown").toUpperCase(), path.verdict === "comparable" ? "ok" : "bad");
  $("#path-consistency").innerHTML = `
    <div class="metric-grid small">
      ${metricCard("SIM AGREEMENT", `${(path.similarity_agreement_rate * 100).toFixed(2)}%`, "", "within path tolerance")}
      ${metricCard("DECISION AGREE", `${(path.threshold_decision_agreement_rate * 100).toFixed(2)}%`, "", "threshold outcome")}
      ${metricCard("ALIGN OFFSET", `${(path.alignment_offset.mismatch_rate * 100).toFixed(2)}%`, "", `${path.alignment_offset.mismatch_frames} frames`)}
      ${metricCard("CONF p95", fmt(path.confidence_deviation_spectrum.p95, 8), "", "similarity delta")}
    </div>`;
}

function renderSimilarity() {
  const s = App.state;
  const sim = s.similarity;
  const focused = document.activeElement;
  if (!["threshold-input", "topk-input", "tolerance-input"].includes(focused?.id)) {
    $("#threshold-input").value = s.config.threshold;
    $("#topk-input").value = s.config.top_k;
    $("#tolerance-input").value = s.config.tolerance;
  }
  $("#strategy-input").value = s.config.comparison_strategy;
  $("#decision-policy").textContent = `${s.config.comparison_strategy} · quantum ${s.config.decision_quantum}`;
  const accepted = sim.accepted_utterance_ids.length;
  $("#similarity-summary").innerHTML = [
    ["门限", fmt(s.config.threshold, 9)],
    ["accepted", `${accepted} / ${sim.rows.length}`],
    ["top-k", sim.top_k_requested],
    ["策略", s.config.comparison_strategy],
    ["容差", fmt(s.config.tolerance, 9)],
    ["次正规保护", fmt(s.config.subnormal_floor, 12)],
    ["top-k 摘要", shortHash(sim.topk_digest, 15)],
  ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  const consistency = s.consistency;
  const stable = consistency?.checks?.threshold_stability?.stable === true;
  setBadge("#threshold-stability", consistency ? (stable ? "STABLE" : "FLIPPED") : "NOT RUN", consistency ? (stable ? "ok" : "bad") : "warn");
  $("#topk-digest").textContent = shortHash(sim.topk_digest, 17);
  $("#topk-table").innerHTML = sim.top_k.map((row) => `
    <tr>
      <td>${row.rank}</td>
      <td class="mono">${esc(row.utterance_id)}</td>
      <td>S${String(row.speaker_id).padStart(2, "0")}</td>
      <td>${fmt(row.similarity, 9)}</td>
      <td>${fmt(row.threshold_distance, 8)}</td>
      <td>${decisionBadge(row)}</td>
    </tr>`).join("");
  $("#similarity-table").innerHTML = sim.rows.map((row) => `
    <tr>
      <td>${row.rank}</td>
      <td class="mono">${esc(row.utterance_id)}</td>
      <td>S${String(row.speaker_id).padStart(2, "0")}</td>
      <td>${fmt(row.similarity, 10)}</td>
      <td>${fmt(row.similarity_quantized, 9)}</td>
      <td>${fmt(row.threshold_distance, 8)}</td>
      <td>${row.within_tolerance ? "yes" : "no"}</td>
      <td>${decisionBadge(row)}</td>
    </tr>`).join("");
  const path = s.path_consistency;
  $("#similarity-path").innerHTML = [
    ["相似度一致率", `${(path.similarity_agreement_rate * 100).toFixed(2)}%`],
    ["门限判定一致率", `${(path.threshold_decision_agreement_rate * 100).toFixed(2)}%`],
    ["对齐失配率", `${(path.alignment_offset.mismatch_rate * 100).toFixed(2)}%`],
    ["最大标签偏移", path.alignment_offset.max_label_offset],
    ["置信偏差 p50", fmt(path.confidence_deviation_spectrum.p50, 9)],
    ["置信偏差 p95", fmt(path.confidence_deviation_spectrum.p95, 9)],
    ["裁决", path.verdict],
  ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
}

function decisionBadge(row) {
  return row.accepted
    ? `<span class="status-badge ok">ACCEPT</span>`
    : `<span class="status-badge bad">REJECT</span>`;
}

function renderSnapshot() {
  const s = App.state;
  const snapshot = s.snapshot.current;
  $("#snapshot-version").textContent = `v${snapshot.version}`;
  $("#snapshot-checksum").textContent = snapshot.checksum;
  $("#snapshot-sections").innerHTML = Object.entries(snapshot.sections).map(([name, section]) => `
    <div class="section-card">
      <div class="name">${esc(name)}</div>
      <div class="ver">v${section.version}</div>
      <div class="check">${esc(shortHash(section.checksum, 20))}</div>
    </div>`).join("");
  const verify = s.snapshot.verification;
  setBadge("#snapshot-verify", verify.ok ? "VERIFIED" : "FAILED", verify.ok ? "ok" : "bad");
  $("#snapshot-verify-detail").innerHTML = [
    ["校验状态", verify.ok ? "pass" : "fail"],
    ["当前版本", `v${snapshot.version}`],
    ["父版本", `v${snapshot.parent_version}`],
    ["整体校验和", shortHash(snapshot.checksum, 22)],
    ["替换策略", "whole snapshot / atomic forward-only"],
  ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  const errors = s.recent_errors || [];
  $("#error-history").innerHTML = errors.length
    ? errors.slice().reverse().map((row) => `
      <div class="event-row">
        <span class="event-seq">#${row.sequence}</span>
        <span class="event-code">${esc(row.code)}</span>
        <span class="event-stage">${esc(row.field || row.stage)} · ${esc(row.message)}</span>
        <span class="status-badge warn">${esc(row.degradation.category)}</span>
      </div>`).join("")
    : `<div class="empty-state">尚未注入错误。选择一个阶段并触发注入以查看异常链路。</div>`;
  if (App.injectionResult) renderInjection(App.injectionResult);
}

function renderInjection(result) {
  const node = $("#injection-result");
  if (!result) {
    node.innerHTML = `<div class="empty-state">错误注入结果会显示在这里，包括阶段、业务码和下游降级类别。</div>`;
    return;
  }
  if (result.ok) {
    node.innerHTML = `<div class="empty-state">注入未触发预期错误。</div>`;
    return;
  }
  node.innerHTML = `
    <div class="kv"><span>injection stage</span><span>${esc(result.injection_stage)}</span></div>
    <div class="kv"><span>business code</span><span class="event-code">${esc(result.error.code)}</span></div>
    <div class="kv"><span>error field</span><span>${esc(result.error.field || "—")}</span></div>
    <div class="kv"><span>degradation</span><span>${esc(result.degradation.category)}</span></div>
    <div class="kv"><span>strategy</span><span>${esc(result.degradation.strategy)}</span></div>
    <div class="kv"><span>active state</span><span>${result.active_state_preserved ? "preserved" : "changed"}</span></div>
    <pre class="code-block">${esc(JSON.stringify(result.error.details, null, 2))}</pre>`;
  setBadge("#snapshot-verify", "ERROR PATH", "warn");
}

function renderConsistency() {
  const result = App.state.consistency;
  const orb = $(".check-orb");
  if (!result) {
    $("#check-title").textContent = "等待运行";
    $("#check-description").textContent = "点击“重新运行自检”执行全部跨级断言。";
    orb.classList.remove("bad");
    $("#check-grid").innerHTML = "";
    $("#first-difference").textContent = "null";
    return;
  }
  const ok = result.ok;
  orb.classList.toggle("bad", !ok);
  orb.innerHTML = `<span>${ok ? "✓" : "!"}</span>`;
  $("#check-title").textContent = ok ? "全部一致性断言通过" : `自检失败 · ${result.code}`;
  $("#check-description").textContent = ok
    ? "frame_index 集合、误差区间、定点重算、集合覆盖、门限与 top-k 均一致。"
    : (result.error?.message || "检测到首个差异位置。");
  const checks = result.checks || {};
  const names = [
    ["frame_index_sets", "frame_index 集合"],
    ["alignment_digest_invariance", "对齐标签不变"],
    ["error_interval_order", "误差区间顺序"],
    ["fixed_recompute", "定点重算"],
    ["recompute_reuse_disjoint_cover", "重算/复用覆盖"],
    ["controlled_phoneme_coverage", "受控音素覆盖"],
    ["threshold_stability", "门限稳定性"],
    ["topk_stability", "top-k 稳定性"],
  ];
  $("#check-grid").innerHTML = names.map(([key, label]) => {
    const value = checks[key];
    let passed = value === "pass";
    if (value && typeof value === "object") passed = value.stable === true;
    if (!result.ok && key === "frame_index_sets") passed = false;
    return `
      <div class="check-card ${passed ? "pass" : "fail"}">
        <div class="check-name">${esc(label)}</div>
        <div class="check-state">${passed ? "PASS" : "FAIL"}</div>
      </div>`;
  }).join("");
  const difference = result.first_difference ?? result.error?.details?.first_difference ?? null;
  $("#first-difference").textContent = JSON.stringify(difference, null, 2);
}

function setBadge(selector, text, kind) {
  const node = $(selector);
  if (!node) return;
  node.textContent = text;
  node.classList.remove("ok", "warn", "bad");
  if (kind) node.classList.add(kind);
}

async function loadAlignment() {
  const utteranceId = $("#alignment-utterance").value;
  const profile = $("#alignment-profile").value;
  if (!utteranceId) return;
  const query = new URLSearchParams({ profile, utterance_id: utteranceId });
  const payload = await api(`/api/alignment?${query.toString()}`);
  if (payload.error) {
    failToast(payload);
    return;
  }
  App.alignment = payload;
  const mismatches = payload.rows.filter((row) => row.aligned_phoneme_id !== row.truth_phoneme_id).length;
  const controlled = new Set(payload.controlled_phonemes);
  const outOfRange = payload.rows.filter((row) => !controlled.has(row.aligned_phoneme_id)).length;
  $("#alignment-metrics").innerHTML = [
    metricCard("FRAMES", payload.rows.length, "帧", utteranceId),
    metricCard("ALIGN MATCH", `${(100 * (payload.rows.length - mismatches) / Math.max(1, payload.rows.length)).toFixed(1)}%`, "", `${mismatches} 与真值不同`),
    metricCard("OUT OF RANGE", outOfRange, "帧", "受控音素集合"),
    metricCard("DIGEST", shortHash(payload.alignment_digest, 9), "", profile),
  ].join("");
  setBadge("#alignment-invariant", payload.canonical_label_invariant ? "LABEL INVARIANT" : "LABEL DRIFT", payload.canonical_label_invariant ? "ok" : "bad");
  $("#alignment-table").innerHTML = payload.rows.map((row) => {
    const mismatch = row.aligned_phoneme_id !== row.truth_phoneme_id;
    return `
      <tr>
        <td>${row.frame_index}</td>
        <td class="mono">${row.start_sample}–${row.end_sample}</td>
        <td><span class="label-pill ${mismatch ? "mismatch" : ""}">P${row.aligned_phoneme_id}</span></td>
        <td><span class="label-pill">P${row.truth_phoneme_id}</span></td>
        <td>${fmt(row.alignment_score, 8)}</td>
        <td>${fmt(row.margin, 8)}</td>
        <td>${fmt(row.f0_candidate, 2)}</td>
        <td>${fmt(row.f0_true, 2)}</td>
        <td>${fmt(row.energy, 7)}</td>
        <td>${fmt(row.zcr, 4)}</td>
      </tr>`;
  }).join("");
}

function populateAlignmentControls() {
  const s = App.state;
  const select = $("#alignment-utterance");
  const current = select.value;
  select.innerHTML = s.dataset.utterances.map((item) =>
    `<option value="${esc(item.utterance_id)}">${esc(item.utterance_id)} · S${String(item.speaker_id).padStart(2, "0")} · ${item.frame_count}f</option>`
  ).join("");
  if (current && s.dataset.utterances.some((item) => item.utterance_id === current)) {
    select.value = current;
  }
  select.onchange = () => loadAlignment().catch((error) => toast("对齐载入失败", String(error), "bad"));
  $("#alignment-profile").onchange = () => loadAlignment().catch((error) => toast("对齐载入失败", String(error), "bad"));
}

async function loadSpectrum() {
  const profile = $("#spectrum-profile").value;
  const payload = await api(`/api/error-spectrum?profile=${encodeURIComponent(profile)}`);
  if (payload.error) return failToast(payload);
  renderSpectrum(payload);
}

function bindEvents() {
  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      $$(".nav-item").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      const section = button.dataset.section;
      $$(".section").forEach((node) => node.classList.toggle("active", node.id === `section-${section}`));
      $("#page-title").textContent = TITLES[section] || section;
      if (section === "alignment" && !App.alignment) {
        loadAlignment().catch((error) => toast("对齐载入失败", String(error), "bad"));
      }
    });
  });

  $$("#profile-switch button").forEach((button) => {
    button.addEventListener("click", async () => {
      const payload = await api("/api/precision", {
        method: "POST",
        body: JSON.stringify({ profile: button.dataset.profile }),
      });
      if (!payload.ok) return failToast(payload);
      toast("精度档已切换", `${payload.active_profile} · snapshot v${payload.snapshot.version}`);
      await loadState();
      await loadAlignment().catch(() => {});
    });
  });

  $("#append-registration").addEventListener("click", async () => {
    const speaker = Number($("#registration-speaker").value);
    const payload = await api("/api/registration", {
      method: "POST",
      body: JSON.stringify({ speaker_id: speaker }),
    });
    if (!payload.ok) return failToast(payload);
    const drift = payload.registration.drifts[0];
    toast("注册样本已追加", `S${String(speaker).padStart(2, "0")} · drift ${fmt(drift?.drift_normalized_l2, 6)}`);
    await loadState();
  });

  $("#reload-alignment").addEventListener("click", () => loadAlignment().catch((error) => toast("对齐载入失败", String(error), "bad")));
  $("#reload-spectrum").addEventListener("click", () => loadSpectrum().catch((error) => toast("误差谱载入失败", String(error), "bad")));

  $("#similarity-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = await api("/api/similarity-config", {
      method: "POST",
      body: JSON.stringify({
        threshold: Number($("#threshold-input").value),
        top_k: Number($("#topk-input").value),
        strategy: $("#strategy-input").value,
        tolerance: Number($("#tolerance-input").value),
      }),
    });
    if (!payload.ok) return failToast(payload);
    toast("门限配置已应用", `${payload.config.comparison_strategy} · ${payload.config.threshold}`);
    await loadState();
  });

  $("#run-injection").addEventListener("click", async () => {
    const stage = $("#injection-stage").value;
    const payload = await api("/api/inject", {
      method: "POST",
      body: JSON.stringify({ stage }),
    });
    App.injectionResult = payload;
    renderInjection(payload);
    if (payload.ok) {
      toast("注入未命中预期错误", stage, "bad");
    } else {
      toast(payload.error.code, `${payload.degradation.category} · state preserved`, "bad");
    }
    await loadState();
    App.injectionResult = payload;
    renderInjection(payload);
  });

  $("#rollback-attempt").addEventListener("click", async () => {
    const current = App.state.snapshot.current.version;
    const payload = await api("/api/rollback", {
      method: "POST",
      body: JSON.stringify({ target_version: Math.max(1, current - 1) }),
    });
    if (payload.ok) {
      toast("回退意外成功", "", "bad");
    } else {
      toast(payload.error.code, payload.degradation.category, "bad");
    }
    await loadState();
  });

  const runCheck = async () => {
    const payload = await api("/api/consistency");
    if (payload.ok) {
      toast("一致性自检通过", `${payload.checks.threshold_stability.probe_count} threshold probes`);
    } else {
      toast(payload.code, payload.error?.message || "first difference detected", "bad");
    }
    await loadState();
  };
  $("#run-check").addEventListener("click", runCheck);
  $("#run-check-top").addEventListener("click", runCheck);
}

async function init() {
  bindEvents();
  await loadState();
  if (!App.state) return;
  populateAlignmentControls();
  await loadAlignment().catch((error) => toast("对齐载入失败", String(error), "bad"));
}

window.addEventListener("resize", () => {
  if (App.spectrum) drawErrorChart(App.spectrum);
});

init();
