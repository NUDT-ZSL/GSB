const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const app = {
  busy: false,
  run: null,
  state: null,
  logs: [],
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function shortHash(value, size = 12) {
  if (!value) return "—";
  return String(value).slice(0, size);
}

function sci(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number === 0) return "0";
  if (Math.abs(number) >= 0.001) return number.toFixed(6);
  return number.toExponential(2);
}

function pct(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function setBusy(value, label = "RUNNING") {
  app.busy = value;
  $$("button").forEach((button) => {
    button.disabled = value;
  });
  $("#run-state").textContent = value ? label : "READY";
  $("#run-state").className = `chip ${value ? "warn" : "neutral"}`;
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 4200);
}

function addLog(code, message, ok = false) {
  app.logs.unshift({ code, message, ok });
  app.logs = app.logs.slice(0, 80);
  renderLogs();
}

function renderLogs() {
  const container = $("#activity-log");
  if (!app.logs.length) {
    container.innerHTML = '<div class="log-line muted">等待本地诊断事件…</div>';
    return;
  }
  container.innerHTML = app.logs
    .map(
      (entry, index) => `
        <div class="log-line">
          <span class="log-index">${String(app.logs.length - index).padStart(2, "0")}</span>
          <span><span class="log-code ${entry.ok ? "ok" : ""}">${escapeHtml(entry.code)}</span>${escapeHtml(entry.message)}</span>
        </div>
      `,
    )
    .join("");
}

async function api(path, body = null) {
  const options = body
    ? {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    : {};
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    const error = payload.error || { code: "E_HTTP", message: "请求失败", details: {} };
    const failure = new Error(error.message || error.code);
    failure.code = error.code;
    failure.details = error.details;
    throw failure;
  }
  return payload;
}

function setImage(id, base64) {
  const element = $(id);
  if (!base64) {
    element.removeAttribute("src");
    return;
  }
  element.src = `data:image/png;base64,${base64}`;
}

function renderMiniSpec(sample, run) {
  const style = sample.style || {};
  const rows = [
    ["seed", sample.seed],
    ["尺寸", `${sample.width} × ${sample.height}`],
    ["风格强度", Number(style.strength ?? 0).toFixed(3)],
    ["边缘保留", Number(style.edge_preserve ?? 0).toFixed(3)],
    ["迭代步数", style.iterations ?? "—"],
    ["定点缩放", style.fixed_scale ?? "—"],
    ["舍入模式", style.fixed_rounding ?? "—"],
    ["区域数量", (sample.regions || []).length],
  ];
  if (run) {
    rows.push(["本次精度", run.precision]);
    rows.push(["Run key", shortHash(run.run_key)]);
  }
  $("#mini-spec").innerHTML = rows
    .map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`)
    .join("");
}

function renderSnapshots(snapshots) {
  if (!snapshots) return;
  const rows = ["mask", "style", "result"].map((kind) => {
    const item = snapshots[kind];
    if (!item) {
      return `
        <div class="snapshot-row">
          <span class="snapshot-kind">${kind}</span>
          <span class="snapshot-version">—</span>
          <span class="snapshot-checksum">尚未提交</span>
        </div>`;
    }
    return `
      <div class="snapshot-row">
        <span class="snapshot-kind">${kind}</span>
        <span class="snapshot-version">v${item.version}</span>
        <span class="snapshot-checksum" title="${escapeHtml(item.checksum)}">${escapeHtml(shortHash(item.checksum, 30))}</span>
      </div>`;
  });
  $("#snapshot-grid").innerHTML = rows.join("");
  const versions = ["mask", "style", "result"]
    .map((kind) => (snapshots[kind] ? `v${snapshots[kind].version}` : "—"))
    .join(" / ");
  $("#kpi-snapshot").textContent = versions;
  $("#snapshot-version-chip").textContent = versions;
}

function renderStageErrors(cascade) {
  const container = $("#stage-errors");
  if (!cascade || !cascade.length) {
    container.innerHTML = '<div class="log-line muted">尚无误差数据</div>';
    return;
  }
  const values = cascade.map((row) => Math.max(Number(row.abs_max), 0));
  const positive = values.filter((value) => value > 0);
  const minimum = positive.length ? Math.min(...positive) : 1e-12;
  const maximum = positive.length ? Math.max(...positive) : 1;
  const logMin = Math.log10(minimum);
  const logMax = Math.log10(Math.max(maximum, minimum * 10));
  const labels = {
    boundary_sampling: "边界采样",
    block_completion: "分块补全",
    style_coloring: "风格着色",
    fixed_quantization: "定点量化",
    feather_fusion: "融合羽化",
  };
  container.innerHTML = cascade
    .map((row) => {
      const value = Number(row.abs_max);
      let width = 0;
      if (value > 0) {
        const ratio = (Math.log10(value) - logMin) / Math.max(logMax - logMin, 1e-9);
        width = Math.max(4, Math.min(100, 8 + ratio * 92));
      }
      return `
        <div class="bar-row">
          <span class="bar-label">${labels[row.level] || row.level}</span>
          <span class="bar-track"><span class="bar-fill ${value > 0.01 ? "warn" : ""}" style="width:${width}%"></span></span>
          <span class="bar-value">${sci(value)}</span>
        </div>`;
    })
    .join("");
}

function renderBoundarySpectrum(rows) {
  const container = $("#boundary-spectrum");
  if (!rows || !rows.length) {
    container.innerHTML = '<div class="log-line muted">尚无区域边界数据</div>';
    return;
  }
  container.innerHTML = rows
    .map(
      (row) => `
        <div class="region-card">
          <div class="region-head">
            <b>${escapeHtml(row.region)}</b>
            <span>${row.area} px · ${row.perimeter} edge</span>
          </div>
          <div class="region-metrics">
            <div class="region-metric"><span>边界采样</span><strong>${sci(row.boundary_max_abs)}</strong></div>
            <div class="region-metric"><span>最终偏差</span><strong>${sci(row.final_max_abs)}</strong></div>
            <div class="region-metric"><span>块/整幅</span><strong>${sci(row.block_vs_whole_max_abs)}</strong></div>
          </div>
        </div>`,
    )
    .join("");
}

function renderCacheEvents(events) {
  const container = $("#cache-events");
  if (!events || !events.length) {
    container.innerHTML = '<div class="log-line muted">尚无区域缓存事件</div>';
    return;
  }
  container.innerHTML = events
    .map((event) => {
      const reused = event.status === "cache_hit" || event.status === "resumed_checkpoint";
      return `
        <div class="event-row ${reused ? "reused" : "recomputed"}">
          <span class="event-region">${escapeHtml(event.region)}</span>
          <span class="event-stage">${escapeHtml(event.stage)}</span>
          <span class="event-hash" title="${escapeHtml(event.content_hash)}">${escapeHtml(shortHash(event.content_hash, 18))}</span>
          <span class="chip ${reused ? "good" : "warn"}">${escapeHtml(event.status)}</span>
        </div>`;
    })
    .join("");
}

function renderStageLedger(records) {
  const container = $("#stage-ledger");
  if (!records || !records.length) {
    container.innerHTML = '<tr><td colspan="4">尚无五阶段记录</td></tr>';
    return;
  }
  container.innerHTML = records
    .map((record) => {
      const features = record.features || {};
      let summary = "";
      if (record.stage === "parse") {
        summary = `${(features.regions || []).length} 个封闭区域`;
      } else if (record.stage === "boundary") {
        summary = `r=${features.radius} · σ=${features.sigma} · ${features.sampled_pixels} px`;
      } else if (record.stage === "completion") {
        summary = `${features.iterations} 次迭代 · ${features.completed_pixels} px`;
      } else if (record.stage === "style") {
        summary = `${Object.keys(features.stats || {}).length} 组归一化统计 · ${features.styled_pixels} px`;
      } else if (record.stage === "quantization") {
        summary = `${features.precision} · scale=${features.fixed_scale} · ${features.fixed_rounding}`;
      } else if (record.stage === "fusion") {
        summary = `feather=${features.feather_radius} · ${features.fused_pixels} px`;
      }
      return `
        <tr>
          <td><span class="chip neutral">${escapeHtml(record.stage)}</span></td>
          <td class="hash" title="${escapeHtml(record.input_hash)}">${escapeHtml(shortHash(record.input_hash, 24))}</td>
          <td class="hash" title="${escapeHtml(record.output_hash)}">${escapeHtml(shortHash(record.output_hash, 24))}</td>
          <td>${escapeHtml(summary)}</td>
        </tr>`;
    })
    .join("");
}

function renderRun(run, state = app.state) {
  app.run = run;
  if (!run) return;
  state = state || app.state;
  if (state) renderMiniSpec(state.sample || {}, run);
  setImage("#img-base", run.images?.base);
  setImage("#img-mask", run.images?.mask);
  setImage("#img-whole", run.images?.whole_png);
  setImage("#img-block", run.images?.block_png);
  setImage("#img-diff", run.images?.diff);
  setImage("#img-error", run.images?.error);

  $("#kpi-match").textContent = run.block_match?.bitwise_equal ? "BITWISE = 0" : "MISMATCH";
  $("#kpi-match").className = `kpi-value ${run.block_match?.bitwise_equal ? "good" : ""}`;
  $("#kpi-match-sub").textContent = `${run.block_match?.compared_pixels ?? 0} px · max Δ ${sci(run.block_match?.max_abs_delta)}`;
  $("#kpi-dominant").textContent = run.diagnostics?.dominant_source || "—";
  $("#kpi-dominant-sub").textContent = `max |Δ| ${sci(run.diagnostics?.cascade?.at(-1)?.abs_max)} · tol ${sci(run.diagnostics?.tolerance)}`;

  const cache = run.cache_events || {};
  const cacheTotal = Number(cache.total || 0);
  const reused = Number(cache.reused || 0);
  $("#kpi-cache").textContent = cacheTotal ? pct(reused / cacheTotal) : "—";
  $("#kpi-cache-sub").textContent = `${reused} reused / ${cache.recomputed || 0} recomputed`;
  $("#cache-summary-chip").textContent = `${reused}/${cacheTotal} reused`;

  $("#image-run-id").textContent = shortHash(run.run_key, 10);
  $("#image-final-hash").textContent = shortHash(run.block?.final_sha256, 18);
  renderSnapshots(run.snapshots || state?.snapshots);
  renderStageErrors(run.diagnostics?.cascade);
  renderBoundarySpectrum(run.boundary_spectrum);
  renderCacheEvents(run.block?.region_events);
  renderStageLedger(run.block?.stage_records);

  const errorState = $("#error-state");
  if (run.error) {
    errorState.textContent = run.error.code;
    errorState.className = "chip bad";
    addLog(run.error.code, `候选路径超出误差容差，主导来源 ${run.diagnostics?.dominant_source}。`);
  } else if (run.diagnostics?.within_tolerance) {
    errorState.textContent = "WITHIN TOLERANCE";
    errorState.className = "chip good";
    addLog("OK", `双路径一致；主导来源 ${run.diagnostics?.dominant_source}；误差 ${sci(run.diagnostics?.cascade?.at(-1)?.abs_max)}。`, true);
  }
  if (run.whole) {
    addLog("STAGE_HASH", `五阶段摘要已记录：${run.block?.stage_records?.length || 0} 个节点。`, true);
  }
}

async function loadState() {
  try {
    const state = await api("/api/state");
    app.state = state;
    $("#server-status").textContent = "本地引擎已连接";
    $("#sample-seed").textContent = `seed ${state.sample?.seed ?? "—"}`;
    renderMiniSpec(state.sample || {}, app.run);
    renderSnapshots(state.snapshots);
    if (state.error_log?.length && !app.logs.length) {
      state.error_log.forEach((error) => addLog(error.code, error.message));
    }
    return state;
  } catch (error) {
    $("#server-status").textContent = "引擎连接失败";
    throw error;
  }
}

async function runComparison() {
  if (app.busy) return;
  setBusy(true);
  try {
    const payload = await api("/api/run", {
      precision: $("#precision").value,
      focus_path: $("#focus-path").value,
      policy: $("#policy").value,
      threshold_tolerance: Number($("#threshold-tolerance").value),
      error_tolerance: Number($("#error-tolerance").value),
    });
    if (!app.state) await loadState();
    renderRun(payload);
  } catch (error) {
    addLog(error.code || "E_RUN", error.message || String(error));
    toast(`${error.code || "E_RUN"} · ${error.message || error}`);
  } finally {
    setBusy(false);
  }
}

async function runDemo(path, label) {
  if (app.busy) return;
  setBusy(true, label);
  try {
    const payload = await api(path, {});
    await loadState();
    if (payload.incremental_run) {
      renderRun(payload.incremental_run);
      addLog(
        "INCREMENTAL",
        `重算 ${payload.recomputed_regions?.join(",") || "—"}；复用 ${payload.reused_regions?.join(",") || "—"}；增量=全量 ${payload.incremental_equal_full}。`,
        true,
      );
    }
    if (payload.recovered_run) {
      renderRun(payload.recovered_run);
      addLog(
        "RECOVERY",
        `子进程退出码 ${payload.terminated_process_return_code}；恢复检查点 ${payload.resumed_checkpoint_events}；结果=全量 ${payload.recovery_equal_full}。`,
        true,
      );
    }
    if (payload.revised_run) {
      renderRun(payload.revised_run);
      addLog(
        "STYLE_REVISION",
        `上游复用 ${payload.upstream_reused}；下游重算 ${payload.downstream_recomputed}；修订=全量 ${payload.revision_equal_full}。`,
        true,
      );
    }
    if (payload.scenario === "rollback") {
      addLog(
        payload.error?.code,
        `回退版本 v${payload.attempted_version} 被拒绝；当前 v${payload.current_version} 未被覆盖：${payload.head_unchanged}。`,
      );
      toast(`${payload.error?.code} · 旧版本回退已拒绝`);
    }
    if (payload.scenario === "stage_busy") {
      addLog(payload.error?.code, "跨进程后到提交者立即获得阶段占用错误。");
      toast(`${payload.error?.code} · 阶段已被占用`);
    }
    app.state = await api("/api/state");
    renderSnapshots(app.state.snapshots);
    renderCacheEvents(app.run?.block?.region_events);
  } catch (error) {
    addLog(error.code || "E_DEMO", error.message || String(error));
    toast(`${error.code || "E_DEMO"} · ${error.message || error}`);
  } finally {
    setBusy(false);
  }
}

async function resetSample() {
  if (app.busy) return;
  setBusy(true, "RESETTING");
  try {
    await api("/api/reset", {});
    await loadState();
    setBusy(false);
    await runComparison();
    addLog("RESET", "已恢复固定种子底图、掩码和风格参数。", true);
  } catch (error) {
    addLog(error.code || "E_RESET", error.message || String(error));
    toast(`${error.code || "E_RESET"} · ${error.message || error}`);
  } finally {
    setBusy(false);
  }
}

$("#btn-run").addEventListener("click", runComparison);
$("#btn-incremental").addEventListener("click", () => runDemo("/api/demo/incremental", "INCREMENTAL"));
$("#btn-style").addEventListener("click", () => runDemo("/api/demo/style", "STYLE REVISION"));
$("#btn-rollback").addEventListener("click", () => runDemo("/api/demo/rollback", "ROLLBACK"));
$("#btn-busy").addEventListener("click", () => runDemo("/api/demo/busy", "STAGE BUSY"));
$("#btn-recovery").addEventListener("click", () => runDemo("/api/demo/recovery", "RECOVERY"));
$("#btn-state-refresh").addEventListener("click", async () => {
  try {
    await loadState();
    addLog("STATE", "已刷新快照、缓存与检查点状态。", true);
  } catch (error) {
    addLog(error.code || "E_STATE", error.message || String(error));
  }
});
$("#btn-reset").addEventListener("click", resetSample);
$("#btn-clear-log").addEventListener("click", () => {
  app.logs = [];
  renderLogs();
});

window.addEventListener("DOMContentLoaded", async () => {
  try {
    await loadState();
    await runComparison();
  } catch (error) {
    addLog(error.code || "E_INIT", error.message || String(error));
    toast(`${error.code || "E_INIT"} · ${error.message || error}`);
  }
});
