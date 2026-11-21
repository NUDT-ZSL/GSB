const state = {
  data: null,
  busy: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const sourceLabels = {
  quantization_rounding: "量化舍入",
  group_scaling: "分组缩放",
  activation_accumulation: "激活累加",
};

const stageLabels = {
  model_package_validation: "模型包校验",
  layer_quantization: "逐层量化",
  calibration_statistics: "校准统计",
  layer_inference: "逐层推理",
};

function fmt(value, digits = 5) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (!Number.isFinite(Number(value))) return String(value);
  const number = Number(value);
  if (number !== 0 && Math.abs(number) < 0.001) return number.toExponential(2);
  return number.toFixed(digits);
}

function shortHash(value, length = 12) {
  if (!value) return "—";
  return `${String(value).slice(0, length)}…`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function setBusy(busy) {
  state.busy = busy;
  $$("button[data-action]").forEach((button) => (button.disabled = busy));
  $("#progressLine").classList.toggle("loading", busy);
  $("#healthText").textContent = busy ? "诊断中" : "就绪";
}

async function refresh(showToast = false) {
  try {
    const data = await api("/api/overview");
    state.data = data;
    render(data);
    if (showToast) toast("已刷新", "ok");
  } catch (error) {
    toast(`读取失败: ${error.message}`, "bad");
  }
}

async function runAction(action) {
  if (state.busy) return;
  setBusy(true);
  const payload = { action };
  if (action === "inject") {
    payload.kind = $("#injectKind").value;
    payload.layer_id = $("#injectLayer").value;
  }
  try {
    const result = await api("/api/action", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (result.overview) {
      state.data = result.overview;
      render(result.overview);
    }
    renderActionResult(result);
    if (!result.ok) {
      const err = result.result?.error;
      toast(err ? `${err.code} · ${err.stage || ""} · ${err.layer_id || ""}` : "操作失败", "bad");
    } else if (action === "inject") {
      toast(result.result?.raised ? "异常已注入并产生业务码" : "注入未触发", result.result?.raised ? "good" : "bad");
    } else if (action === "recover") {
      toast(result.comparison?.byte_equivalent_result ? "恢复结果与完整运行逐字节一致" : "恢复结果不一致", result.comparison?.byte_equivalent_result ? "good" : "bad");
    } else {
      toast("操作完成", "good");
    }
  } catch (error) {
    toast(`请求失败: ${error.message}`, "bad");
  } finally {
    setBusy(false);
  }
}

function toast(message, kind = "ok") {
  let node = $("#toast");
  if (!node) {
    node = document.createElement("div");
    node.id = "toast";
    node.className = "toast";
    document.body.appendChild(node);
  }
  node.textContent = message;
  node.dataset.kind = kind;
  node.classList.add("show");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("show"), 3800);
}

function render(data) {
  const view = data.view || {};
  const diagnostics = view.diagnostics || {};
  const layers = diagnostics.layers || [];
  const selfcheck = view.self_check || {};
  const finalError = diagnostics.final_serial_error || {};
  const healthOk = data.ok && layers.length > 0;
  $("#healthDot").className = `status-dot ${healthOk ? "ok" : data.error ? "bad" : ""}`;
  $("#healthText").textContent = data.error ? "快照异常" : healthOk ? "就绪" : "等待运行";
  $("#runtimeLabel").textContent = view.runtime_dir || data.runtime_dir || "runtime";
  $("#mVersion").textContent = view.version ?? "—";
  $("#mStage").textContent = stageLabels[view.stage] || view.stage || "等待运行";
  $("#mDominant").textContent = sourceLabels[diagnostics.overall_dominant_source] || "—";
  $("#mRelative").textContent = finalError.relative !== undefined ? `${(finalError.relative * 100).toFixed(2)}%` : "—";
  $("#mAbs").textContent = finalError.abs_mean !== undefined ? `绝对偏差均值 ${fmt(finalError.abs_mean, 6)}` : "绝对偏差 —";
  $("#mSelfcheck").textContent = selfcheck.passed ? "PASS" : selfcheck.error ? "FAIL" : "—";
  $("#mSelfcheck").style.color = selfcheck.passed ? "var(--green)" : selfcheck.error ? "var(--red)" : "";
  $("#mResult").textContent = view.result_digest ? `result ${shortHash(view.result_digest)}` : "result digest —";
  $("#footerDigest").textContent = view.content_checksum || view.result_digest || "—";

  const completed = Object.keys(view.stages || {}).length;
  $$("#progressLine span").forEach((span, index) => span.classList.toggle("done", index < completed));

  renderSpectrumTable(layers);
  drawSpectrum(layers);
  drawPrediction(diagnostics.reference?.reference_prediction || [], diagnostics.operational?.prediction || view.final_prediction || []);
  const interval = finalError.error_interval_90;
  $("#errorInterval").textContent = interval ? `${fmt(interval[0], 6)} – ${fmt(interval[1], 6)}` : "—";
  $("#errorMax").textContent = fmt(finalError.abs_max, 6);
  $("#toleranceBadge").textContent = diagnostics.passed ? "TOLERANCE PASS" : "TOLERANCE FAIL";
  $("#toleranceBadge").classList.toggle("bad", !diagnostics.passed);

  renderStages(view.stages || {});
  renderDependency(view.stages || {});
  renderSelfcheck(selfcheck);
  renderEvents(view, data);
  renderRecovery(data);
  renderHistory(view.history || []);
  $("#rawOutput").textContent = JSON.stringify(data.last_error || data.last_report || view.diagnostics || {}, null, 2);
}

function renderSpectrumTable(layers) {
  const body = $("#spectrumRows");
  if (!layers.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">尚无快照</td></tr>';
    return;
  }
  body.innerHTML = layers.map((layer) => {
    const sources = layer.sources || {};
    return `<tr>
      <td>${escapeHtml(layer.layer_id)}</td>
      <td>${sourceLabels[layer.dominant_source] || escapeHtml(layer.dominant_source)}</td>
      <td>${fmt(sources.quantization_rounding?.abs_mean, 6)}</td>
      <td>${fmt(sources.group_scaling?.abs_mean, 6)}</td>
      <td>${fmt(sources.activation_accumulation?.abs_mean, 6)}</td>
      <td>${fmt(layer.layer_serial?.abs_mean, 6)}</td>
      <td>${(Number(layer.layer_serial?.relative || 0) * 100).toFixed(2)}%</td>
      <td>${fmt(layer.layer_serial?.amplification_ratio, 3)}</td>
    </tr>`;
  }).join("");
}

function setupCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width: rect.width, height: rect.height };
}

function drawSpectrum(layers) {
  const canvas = $("#spectrumChart");
  const { ctx, width, height } = setupCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  if (!layers.length) {
    drawEmpty(ctx, width, height, "运行后显示逐层误差");
    return;
  }
  const pad = { left: 44, right: 14, top: 18, bottom: 42 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const sources = ["quantization_rounding", "group_scaling", "activation_accumulation"];
  const colors = ["#ffb454", "#9d8cff", "#46d7e8"];
  const values = layers.map((layer) => sources.map((source) => layer.sources?.[source]?.abs_mean || 0));
  const maxValue = Math.max(...values.flat(), ...layers.map((layer) => layer.layer_serial?.abs_mean || 0), 0.000001);
  const axisMax = maxValue * 1.18;
  const grid = 4;
  ctx.font = "10px Cascadia Code, Consolas, monospace";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= grid; i += 1) {
    const y = pad.top + (innerH * i) / grid;
    const value = axisMax * (1 - i / grid);
    ctx.strokeStyle = "rgba(148,177,214,0.09)";
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillStyle = "rgba(141,162,189,0.7)";
    ctx.fillText(value.toExponential(1), pad.left - 7, y);
  }
  const groupW = innerW / layers.length;
  const barW = Math.min(22, groupW / 5.3);
  const gap = 3;
  layers.forEach((layer, layerIndex) => {
    const groupX = pad.left + groupW * layerIndex;
    const totalWidth = barW * 3 + gap * 2;
    const startX = groupX + (groupW - totalWidth) / 2;
    values[layerIndex].forEach((value, sourceIndex) => {
      const barH = (value / axisMax) * innerH;
      const x = startX + sourceIndex * (barW + gap);
      const y = pad.top + innerH - barH;
      const gradient = ctx.createLinearGradient(0, y, 0, pad.top + innerH);
      gradient.addColorStop(0, colors[sourceIndex]);
      gradient.addColorStop(1, `${colors[sourceIndex]}55`);
      ctx.fillStyle = gradient;
      roundedRect(ctx, x, y, barW, Math.max(1, barH), 3);
      ctx.fill();
    });
    const serial = layer.layer_serial?.abs_mean || 0;
    const serialY = pad.top + innerH - (serial / axisMax) * innerH;
    ctx.strokeStyle = "#ff6b82";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(groupX + groupW * 0.15, serialY);
    ctx.lineTo(groupX + groupW * 0.85, serialY);
    ctx.stroke();
    ctx.fillStyle = "rgba(233,241,251,0.82)";
    ctx.textAlign = "center";
    ctx.font = "10px Cascadia Code, Consolas, monospace";
    ctx.fillText(layer.layer_id.replace("layer_", "L"), groupX + groupW / 2, height - 19);
  });
  ctx.lineWidth = 1;
}

function roundedRect(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + width, y, x + width, y + height, r);
  ctx.arcTo(x + width, y + height, x, y + height, r);
  ctx.arcTo(x, y + height, x, y, r);
  ctx.arcTo(x, y, x + width, y, r);
  ctx.closePath();
}

function drawPrediction(reference, operational) {
  const canvas = $("#predictionChart");
  const { ctx, width, height } = setupCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  if (!reference.length || !operational.length) {
    drawEmpty(ctx, width, height, "运行后显示预测向量");
    return;
  }
  const pad = { left: 26, right: 12, top: 18, bottom: 26 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const maxAbs = Math.max(...reference.map(Math.abs), ...operational.map(Math.abs), 0.001);
  const slotW = innerW / Math.max(reference.length, operational.length);
  const barW = Math.max(2, Math.min(8, slotW * 0.34));
  const zeroY = pad.top + innerH / 2;
  ctx.strokeStyle = "rgba(148,177,214,0.14)";
  ctx.beginPath();
  ctx.moveTo(pad.left, zeroY);
  ctx.lineTo(width - pad.right, zeroY);
  ctx.stroke();
  reference.forEach((value, index) => {
    const x = pad.left + slotW * index + slotW / 2 - barW;
    const barH = (Math.abs(value) / maxAbs) * (innerH / 2 - 3);
    ctx.fillStyle = "rgba(114,135,165,0.8)";
    ctx.fillRect(x, value >= 0 ? zeroY - barH : zeroY, barW, barH);
  });
  operational.forEach((value, index) => {
    const x = pad.left + slotW * index + slotW / 2;
    const barH = (Math.abs(value) / maxAbs) * (innerH / 2 - 3);
    ctx.fillStyle = value >= 0 ? "#5ee0a0" : "#46d7e8";
    ctx.fillRect(x, value >= 0 ? zeroY - barH : zeroY, barW, barH);
  });
  ctx.fillStyle = "rgba(141,162,189,0.72)";
  ctx.font = "9px Cascadia Code, Consolas, monospace";
  ctx.textAlign = "center";
  ctx.fillText("element 0", pad.left + slotW / 2, height - 7);
  ctx.fillText(`element ${reference.length - 1}`, width - pad.right - slotW / 2, height - 7);
}

function drawEmpty(ctx, width, height, message) {
  ctx.fillStyle = "rgba(141,162,189,0.55)";
  ctx.font = "12px Segoe UI, Microsoft YaHei UI, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(message, width / 2, height / 2);
}

function renderStages(stages) {
  const grid = $("#stageGrid");
  const keys = ["model_package_validation", "layer_quantization", "calibration_statistics", "layer_inference"];
  grid.innerHTML = keys.map((key, index) => {
    const stage = stages[key];
    if (!stage) {
      return `<div class="stage-card"><span class="stage-num">0${index + 1}</span><h3>${stageLabels[key]}</h3><p class="empty">未提交</p></div>`;
    }
    const chips = (stage.completed_layers || []).map((layer) => `<span class="layer-chip">${escapeHtml(layer)}</span>`).join("");
    return `<div class="stage-card complete">
      <span class="stage-num">0${index + 1}</span>
      <h3>${stageLabels[key]}</h3>
      <p>已提交 ${stage.completed_layers?.length || 0} 层 · 复用 ${stage.reused_layers?.length || 0} · 重算 ${stage.rebuilt_layers?.length || 0}</p>
      ${chips}
    </div>`;
  }).join("");
}

function renderDependency(stages) {
  const box = $("#dependencyBox");
  const seed = stages.layer_quantization;
  const inference = stages.layer_inference;
  if (!seed) {
    box.innerHTML = '<p class="empty">尚无依赖审计</p>';
    return;
  }
  const row = (label, items) => `<div class="detail-row"><span>${label}</span><strong>${(items || []).join(" · ") || "—"}</strong></div>`;
  box.innerHTML = [
    row("受影响层", seed.affected_layers),
    row("复用层", seed.reused_layers),
    row("重算层", seed.rebuilt_layers),
    row("推理复用", inference?.reused_layers),
    row("推理重算", inference?.rebuilt_layers),
  ].join("");
}

function renderSelfcheck(selfcheck) {
  const box = $("#selfcheckBox");
  if (!selfcheck || (!selfcheck.checks && !selfcheck.error)) {
    box.innerHTML = '<p class="empty">尚无自检结果</p>';
    return;
  }
  if (selfcheck.error) {
    const error = selfcheck.error;
    box.innerHTML = `<div class="check-item bad"><i></i><span>${escapeHtml(error.code)} · ${escapeHtml(error.details?.category || "")}</span></div>`;
    return;
  }
  box.innerHTML = (selfcheck.checks || []).map((check) => `
    <div class="check-item ${check.passed ? "" : "bad"}"><i></i>
      <span>${escapeHtml(check.name)} · ${check.passed ? "通过" : "失败"}${check.layers ? ` · ${check.layers} 层` : ""}${check.stages ? ` · ${check.stages} 阶段` : ""}</span>
    </div>`).join("");
}

function renderEvents(view, data) {
  const box = $("#eventsBox");
  const degradations = view.degradations || [];
  const events = view.events || [];
  const items = [];
  degradations.forEach((item) => items.push(`<div class="event"><code>DEGRADED · ${escapeHtml(item.category)}</code><p>${escapeHtml(item.detail)}</p></div>`));
  (data.last_error?.raised ? [data.last_error] : []).forEach((item) => {
    const error = item.error || {};
    items.push(`<div class="event error"><code>${escapeHtml(error.code)} · ${escapeHtml(error.stage)} · ${escapeHtml(error.layer_id || "—")}</code><p>${escapeHtml(error.message)}<br>${escapeHtml((error.trace || []).join(" → "))}</p></div>`);
  });
  events.slice().reverse().forEach((event) => {
    if (event.kind === "run_complete") {
      items.push(`<div class="event complete"><code>COMMIT v${event.version}</code><p>result ${shortHash(event.result_digest)} · affected ${(event.affected_layers || []).join(", ")}</p></div>`);
    } else if (event.kind === "business_error") {
      const error = event.error || {};
      items.push(`<div class="event error"><code>${escapeHtml(error.code)} · ${escapeHtml(error.stage)}</code><p>${escapeHtml(error.message)}</p></div>`);
    }
  });
  box.innerHTML = items.length ? items.join("") : '<p class="empty">尚无事件</p>';
}

function renderRecovery(data) {
  const box = $("#recoveryBox");
  const report = data.last_report || {};
  const recovery = report.recovery;
  const comparison = data.last_report?.comparison;
  if (!recovery) {
    box.innerHTML = '<p class="empty">执行“强杀并恢复”查看报告</p>';
    return;
  }
  const committed = recovery.latest_committed;
  const discarded = recovery.discarded_uncommitted;
  box.innerHTML = `
    <div class="detail-row"><span>最近提交点</span><strong>${committed ? `v${committed.version} · ${stageLabels[committed.stage] || committed.stage}` : "—"}</strong></div>
    <div class="detail-row"><span>丢弃未提交结果</span><strong>${discarded ? `${discarded.stage} · ${discarded.layer_id}` : "无"}</strong></div>
    <div class="detail-row"><span>重建起点</span><strong>${recovery.resume_stage} · ${recovery.resume_layer_id ?? "—"} (index ${recovery.resume_layer_index ?? "—"})</strong></div>
    <div class="detail-row"><span>恢复策略</span><strong>阶段级全量重建</strong></div>
    ${comparison ? `<div class="detail-row"><span>强制终止退出码</span><strong>${comparison.kill_exit_code ?? "—"}</strong></div>` : ""}
  `;
}

function renderHistory(history) {
  const box = $("#historyBox");
  $("#historyMeta").textContent = `${history.length} commits`;
  if (!history.length) {
    box.innerHTML = '<p class="empty">尚无快照</p>';
    return;
  }
  box.innerHTML = history.slice().reverse().map((node, index) => `
    <div class="timeline-node ${index === 0 ? "current" : ""}">
      <b>v${node.version} · ${stageLabels[node.stage] || node.stage}</b>
      <span>${(node.completed_layers || []).length} layers</span><br>
      <span>${shortHash(node.result_digest, 10)}</span>
    </div>`).join("");
}

function renderActionResult(result) {
  if (result.action === "inject") {
    $("#rawOutput").textContent = JSON.stringify(result, null, 2);
  } else if (result.comparison) {
    const comparison = { ...result.comparison };
    $("#rawOutput").textContent = JSON.stringify(comparison, null, 2);
    if (result.action === "recover" && result.comparison.recovery_report) {
      const merged = {
        ...state.data.last_report,
        recovery: result.comparison.recovery_report,
        comparison: result.comparison,
      };
      renderRecovery({ last_report: merged });
    }
    if (result.action === "update") {
      toast(`受影响 ${result.comparison.affected_layers.length} 层，复用 ${result.comparison.reused_layers.length} 层`, "good");
    }
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (button) runAction(button.dataset.action);
});
$("#refreshBtn").addEventListener("click", () => refresh(true));
window.addEventListener("resize", () => {
  if (!state.data) return;
  const diagnostics = state.data.view?.diagnostics || {};
  drawSpectrum(diagnostics.layers || []);
  drawPrediction(diagnostics.reference?.reference_prediction || [], diagnostics.operational?.prediction || []);
});

const style = document.createElement("style");
style.textContent = `
.toast { position: fixed; right: 24px; bottom: 24px; z-index: 50; max-width: min(440px, calc(100vw - 48px)); padding: 13px 17px; border-radius: 12px; background: #101b2c; border: 1px solid var(--line-strong); color: var(--text); font-size: 12px; box-shadow: 0 18px 50px rgba(0,0,0,.4); opacity: 0; transform: translateY(12px); pointer-events: none; transition: .22s ease; }
.toast.show { opacity: 1; transform: translateY(0); }
.toast[data-kind="good"] { border-color: rgba(94,224,160,.45); }
.toast[data-kind="bad"] { border-color: rgba(255,107,130,.45); }
`;
document.head.appendChild(style);

refresh();
