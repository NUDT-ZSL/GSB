const LEVEL_META = {
  quantization_rounding: { label: "量化舍入", color: "#7ba7ff", cls: "quant" },
  group_scaling: { label: "分组缩放", color: "#4ee1c8", cls: "scale" },
  block_accumulation: { label: "分块累加", color: "#f0b35c", cls: "block" },
  layer_cascade: { label: "逐层串联", color: "#d18cff", cls: "cascade" },
};

let currentDashboard = null;
let chartResizeBound = false;

const el = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmt(value, digits = 4) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (n === 0) return "0";
  if (Math.abs(n) < 0.001 || Math.abs(n) >= 10000) return n.toExponential(2);
  return n.toFixed(digits);
}

function shortHash(value, size = 12) {
  if (!value) return "—";
  return String(value).slice(0, size);
}

function setChip(id, text, kind = "muted") {
  const node = el(id);
  if (!node) return;
  node.textContent = text;
  node.className = `chip ${kind}`;
}

function showToast(message, isError = false) {
  const toast = el("toast");
  toast.textContent = message;
  toast.className = `toast ${isError ? "error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.add("hidden"), 6500);
}

function setBusy(active, title = "正在执行") {
  el("busy-overlay").classList.toggle("hidden", !active);
  el("busy-title").textContent = title;
  for (const id of ["demo-btn", "revision-btn", "refresh-btn"]) {
    el(id).disabled = active;
  }
}

async function loadDashboard() {
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) {
      el("empty-state").classList.remove("hidden");
      el("dashboard").classList.add("hidden");
      return;
    }
    currentDashboard = await response.json();
    renderDashboard(currentDashboard);
    el("empty-state").classList.add("hidden");
    el("dashboard").classList.remove("hidden");
  } catch (error) {
    showToast(`读取诊断结果失败：${error}`, true);
  }
}

function renderDashboard(data) {
  const summary = data.summary || {};
  el("model-chip").textContent = data.model_tag || "local model";
  el("model-chip").className = "chip muted";
  el("hero-copy").textContent =
    `模型 ${data.model_tag} · ${summary.layer_count} 层 · 全链路由固定种子确定性生成。` +
    ` 当前主导误差来源为 ${summary.dominant_source}，最新提交版本 v${summary.latest_version}。`;
  el("orbit-core").textContent = (summary.dominant_source || "—").replaceAll("_", " ");

  el("metric-consistency").textContent = summary.consistency_ok ? "PASS" : "FAIL";
  el("metric-consistency").className = `metric-value ${summary.consistency_ok ? "" : "accent"}`;
  el("metric-dominant").textContent = summary.dominant_source || "—";
  el("metric-version").textContent = summary.latest_version ? `v${summary.latest_version}` : "—";
  const comparison = data.recovery_comparison || data.comparison;
  el("metric-recovery").textContent = comparison
    ? (comparison.all_equal ? "MATCH" : "DIFF")
    : (data.recovery?.mode || "—").toUpperCase();
  el("metric-recovery-sub").textContent = comparison
    ? "恢复结果与完整运行逐字节一致"
    : "尚未执行强杀恢复对比";
  const degradations = data.budget?.degradations?.length || 0;
  el("metric-budget").textContent = degradations ? `${degradations} 项` : "无";
  el("metric-budget-sub").textContent = degradations
    ? (data.budget.degradation_categories || []).join(" / ")
    : "全部阶段在预算内";
  el("metric-error").textContent = summary.last_error_code || "OK";
  el("metric-error-sub").textContent = data.last_error
    ? `${data.last_error.stage || "unknown"} · ${data.last_error.message || ""}`
    : "无未归因异常";

  renderSpectrum(data.diagnostics);
  renderStages(data.progress_stages || []);
  renderRecovery(data);
  renderInvalidation(data.invalidation);
  renderBudget(data.budget);
  renderConsistency(data.consistency);
  renderPrediction(data.inference);
  renderErrors(data.recent_errors || []);
  renderManifest(data.manifest || []);
  setChip("progress-view-chip", `view ${shortHash(data.progress?.view_checksum, 10)}`, "muted");
  el("manifest-hash").textContent = shortHash(data.manifest_digest, 20);
}

function renderSpectrum(diagnostics) {
  if (!diagnostics) return;
  const levels = diagnostics.error_spectrum || [];
  const body = el("spectrum-body");
  body.innerHTML = levels
    .flatMap((layer) =>
      layer.levels.map((item, index) => {
        const meta = LEVEL_META[item.level] || { label: item.level };
        return `<tr>
          <td>${index === 0 ? `L${layer.layer_index} · ${escapeHtml(layer.layer_id)}` : ""}</td>
          <td><span class="swatch ${meta.cls}"></span> ${escapeHtml(meta.label)}</td>
          <td class="num">${fmt(item.max_abs)}</td>
          <td class="num">${fmt(item.mean_abs)}</td>
          <td class="num">${fmt(item.mean_relative)}</td>
          <td class="num">[${fmt(item.interval[0])}, ${fmt(item.interval[1])}]</td>
          <td class="num">${fmt(item.amplification_ratio, 2)}×</td>
        </tr>`;
      }),
    )
    .join("");
  drawErrorChart(levels, diagnostics.dominant_source);
  if (!chartResizeBound) {
    window.addEventListener("resize", () => {
      if (currentDashboard?.diagnostics) {
        drawErrorChart(
          currentDashboard.diagnostics.error_spectrum || [],
          currentDashboard.diagnostics.dominant_source,
        );
      }
    });
    chartResizeBound = true;
  }
}

function drawErrorChart(layers, dominant) {
  const canvas = el("error-chart");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(320, rect.width);
  const height = 300;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const levels = Object.keys(LEVEL_META);
  const values = layers.flatMap((layer) =>
    layer.levels.map((item) => Math.abs(item.max_abs || 0)),
  );
  const maxValue = Math.max(1e-18, ...values);
  const minExp = -18;
  const maxExp = Math.max(-12, Math.log10(maxValue) + 0.8);
  const left = 58;
  const right = 18;
  const top = 18;
  const bottom = 46;
  const plotW = width - left - right;
  const plotH = height - top - bottom;
  const groupW = plotW / Math.max(1, layers.length);
  const barW = Math.min(18, (groupW - 18) / levels.length);

  ctx.font = "10px 'Cascadia Code', Consolas, monospace";
  ctx.strokeStyle = "rgba(144,173,204,.12)";
  ctx.fillStyle = "#536176";
  ctx.lineWidth = 1;
  for (let tick = 0; tick <= 4; tick += 1) {
    const y = top + (plotH * tick) / 4;
    const exponent = maxExp - ((maxExp - minExp) * tick) / 4;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(width - right, y);
    ctx.stroke();
    ctx.fillText(`1e${exponent.toFixed(0)}`, 8, y + 3);
  }

  layers.forEach((layer, layerIndex) => {
    const groupCenter = left + groupW * (layerIndex + 0.5);
    layer.levels.forEach((item, levelIndex) => {
      const value = Math.abs(item.max_abs || 0);
      const exponent = value > 0 ? Math.log10(value) : minExp;
      const normalized = Math.max(0, Math.min(1, (exponent - minExp) / (maxExp - minExp)));
      const barH = normalized * plotH;
      const x = groupCenter - (barW * levels.length) / 2 + levelIndex * barW + 1;
      const y = top + plotH - barH;
      const meta = LEVEL_META[item.level] || { color: "#7ba7ff" };
      ctx.fillStyle = meta.color;
      ctx.globalAlpha = item.level === dominant ? 1 : 0.72;
      ctx.fillRect(x, y, Math.max(2, barW - 2), Math.max(1, barH));
      ctx.globalAlpha = 1;
    });
    ctx.fillStyle = "#8695a9";
    ctx.textAlign = "center";
    ctx.fillText(`L${layer.layer_index}`, groupCenter, height - 18);
  });
  ctx.textAlign = "left";
}

function renderStages(stages) {
  el("stage-list").innerHTML = stages
    .map(
      (stage) => `
      <div class="stage-card">
        <div>
          <div class="stage-title">${escapeHtml(stage.label)}</div>
          <div class="stage-meta">next layer index ${stage.next_layer_index ?? "—"}</div>
        </div>
        <div class="layer-pills">
          ${(stage.completed_layers || [])
            .map((layer) => `<span class="layer-pill">${escapeHtml(layer)}</span>`)
            .join("") || '<span class="stage-meta">暂无已提交层</span>'}
        </div>
        <div class="version-chip">v${stage.version ?? "—"}<br>${stage.stage_complete ? "COMPLETE" : "PARTIAL"}</div>
      </div>`,
    )
    .join("");
}

function renderRecovery(data) {
  const recovery = data.recovery || {};
  const comparison = data.recovery_comparison || data.comparison;
  const badge = el("recovery-badge");
  if (comparison) {
    badge.textContent = comparison.all_equal ? "MATCH" : "DIFF";
    badge.className = `chip ${comparison.all_equal ? "ok" : "bad"}`;
  } else {
    badge.textContent = (recovery.mode || "unknown").toUpperCase();
    badge.className = "chip muted";
  }
  const resumeFrom = comparison?.resume_from || recovery.resume_from;
  const fullHash = comparison?.full_prediction_checksum;
  const resumedHash = comparison?.resumed_prediction_checksum;
  const rows = [
    ["当前模式", recovery.mode || "—"],
    ["恢复起点", resumeFrom ? `${resumeFrom.stage} · v${resumeFrom.version}` : "无（完整运行）"],
    [
      "推理复用层",
      (comparison?.reused_inference_layers || recovery.reused_layers_by_stage?.inference || []).join(", ") || "—",
    ],
    [
      "推理重算层",
      (comparison?.recomputed_inference_layers || recovery.recomputed_layers_by_stage?.inference || []).join(", ") || "—",
    ],
    ["完整运行预测", shortHash(fullHash, 22)],
    ["恢复运行预测", shortHash(resumedHash, 22)],
    ["激活校验和一致", comparison ? (comparison.activation_checksums_equal ? "是" : "否") : "待演示"],
    ["最终预测一致", comparison ? (comparison.prediction_equal ? "是" : "否") : "待演示"],
  ];
  el("recovery-content").innerHTML = rows
    .map(
      ([label, value]) =>
        `<div class="detail-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`,
    )
    .join("");
}

function renderInvalidation(invalidation) {
  const panel = el("invalidation-panel");
  if (!invalidation) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  const card = (title, layers, cls = "good") => `
    <div class="invalid-card ${cls}">
      <h4>${escapeHtml(title)}</h4>
      <div class="layer-pills">
        ${(layers || []).map((layer) => `<span class="layer-pill">${escapeHtml(layer)}</span>`).join("") || '<span class="stage-meta">无</span>'}
      </div>
    </div>`;
  el("invalidation-content").innerHTML = [
    card("受影响层（本层及全部下游）", invalidation.affected_layers, "bad"),
    card("重算层", invalidation.recomputed_layers, "bad"),
    card("复用层", invalidation.reused_layers, "good"),
  ].join("");
}

function renderBudget(budget) {
  if (!budget) return;
  const usage = budget.usage || {};
  const limits = budget.limits || {};
  const degradationRows = (budget.degradations || []).length
    ? budget.degradations
        .map(
          (item) =>
            `<div class="detail-row"><span>${escapeHtml(item.category)} · ${escapeHtml(item.layer_id || "stage")}</span><strong>${escapeHtml(JSON.stringify(item.applied))}</strong></div>`,
        )
        .join("")
    : '<div class="detail-row"><span>降级记录</span><strong>无</strong></div>';
  el("budget-content").innerHTML = `
    <div class="detail-row"><span>驻留反量化权重</span><strong>${fmt(usage.resident_dequantized_weight_bytes / 1024, 1)} KiB / ${fmt(limits.max_resident_weight_bytes / 1024, 0)} KiB</strong></div>
    <div class="detail-row"><span>校准比较次数</span><strong>${usage.calibration_comparisons?.toLocaleString() || 0} / ${limits.max_calibration_comparisons?.toLocaleString() || 0}</strong></div>
    <div class="detail-row"><span>单阶段层数</span><strong>${usage.layer_count} / ${limits.max_layers_per_stage}</strong></div>
    <div class="detail-row"><span>分块元素上限</span><strong>${limits.max_block_elements}</strong></div>
    ${degradationRows}`;
}

function renderConsistency(consistency) {
  if (!consistency) return;
  setChip(
    "consistency-badge",
    consistency.ok ? "ALL PASS" : "FAILED",
    consistency.ok ? "ok" : "bad",
  );
  el("consistency-list").innerHTML = (consistency.checks || [])
    .map(
      (check) => `
      <div class="check-row ${check.passed ? "" : "fail"}">
        <i class="check-dot"></i>
        <span>${escapeHtml(check.name)}</span>
      </div>`,
    )
    .join("");
}

function renderPrediction(inference) {
  if (!inference) return;
  el("prediction-hash").textContent = shortHash(inference.final_prediction_checksum, 16);
  el("prediction-values").innerHTML = (inference.final_prediction || [])
    .map(
      (value, index) => `
      <div class="prediction-value">
        <small>OUTPUT ${index}</small>
        <strong>${fmt(value, 6)}</strong>
      </div>`,
    )
    .join("");
  el("prediction-note").textContent =
    `归约顺序：${inference.reduction_order}。归一化范围：${inference.normalization_scope}。`;
}

function renderErrors(errors) {
  if (!errors.length) {
    el("error-log").innerHTML = '<p class="error-ok">✓ 当前快照链路没有业务错误记录</p>';
    return;
  }
  el("error-log").innerHTML = errors
    .slice()
    .reverse()
    .map(
      (error) => `
      <div class="error-card">
        <code>${escapeHtml(error.code)} · ${escapeHtml(error.stage || "unknown")}</code>
        <p>${escapeHtml(error.message || "")}${error.layer_id ? ` · ${escapeHtml(error.layer_id)}` : ""}</p>
      </div>`,
    )
    .join("");
}

function renderManifest(manifest) {
  el("manifest-body").innerHTML = manifest
    .map(
      (entry) => `
      <tr>
        <td>L${entry.layer_index} · ${escapeHtml(entry.layer_id)}</td>
        <td class="num">${entry.layer_shape.join(" × ")}</td>
        <td class="num">${entry.weight_revision}</td>
        <td><code>${escapeHtml(entry.quant_dtype)}</code></td>
        <td class="num">${fmt(entry.scale_hint)}</td>
        <td class="num">${entry.zero_point}</td>
        <td class="num">${escapeHtml(JSON.stringify(entry.block_policy))}</td>
        <td class="num">${entry.calib_seed}</td>
      </tr>`,
    )
    .join("");
}

async function runAction(url, title) {
  setBusy(true, title);
  try {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      const code = payload.code || "E_HTTP";
      throw new Error(`${code}: ${payload.message || response.statusText}`);
    }
    if (payload.dashboard) {
      currentDashboard = payload.dashboard;
      renderDashboard(payload.dashboard);
      el("empty-state").classList.add("hidden");
      el("dashboard").classList.remove("hidden");
    } else {
      await loadDashboard();
    }
    showToast("操作完成，快照视图已刷新。");
  } catch (error) {
    showToast(String(error.message || error), true);
    await loadDashboard();
  } finally {
    setBusy(false);
  }
}

el("demo-btn").addEventListener("click", () =>
  runAction("/api/demo", "完整运行 → 强制终止 → 恢复比对"),
);
el("revision-btn").addEventListener("click", () =>
  runAction("/api/revision-demo", "计算失效依赖并替换快照"),
);
el("refresh-btn").addEventListener("click", loadDashboard);

loadDashboard();
