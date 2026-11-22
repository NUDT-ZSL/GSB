const state = {
  dashboard: null,
  status: null,
  polling: null,
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmt(value, digits = 4) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(digits);
  }
  return String(value);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!payload.ok) {
    throw Object.assign(new Error(payload.error?.message || "请求失败"), {
      envelope: payload,
    });
  }
  return payload;
}

function showError(error) {
  const code = error.envelope?.error?.code || "E_CLIENT";
  $("error-list").insertAdjacentHTML(
    "afterbegin",
    `<div class="event bad">
      <div class="event-top"><code>${esc(code)}</code><span>${new Date().toLocaleTimeString()}</span></div>
      <div>${esc(error.message || error)}</div>
    </div>`,
  );
}

function setBusy(button, busy, text) {
  if (!button) return;
  if (busy) {
    button.dataset.original = button.textContent;
    button.textContent = text || "处理中…";
    button.disabled = true;
  } else {
    button.textContent = button.dataset.original || button.textContent;
    button.disabled = false;
  }
}

async function loadDashboard() {
  state.dashboard = await api("/api/dashboard");
  renderDashboard(state.dashboard);
}

async function loadStatus() {
  state.status = await api("/api/status");
  renderStatus(state.status);
}

function renderStatus(status) {
  const healthy = status.ok && status.initialized;
  const pill = $("health-pill");
  pill.textContent = healthy ? "服务正常" : "未初始化";
  pill.className = `pill ${healthy ? "pill-good" : "pill-bad"}`;
  $("mode-pill").textContent = `mode: ${status.config?.validation_mode || "-"}`;
  renderSnapshots(status.snapshots || state.dashboard?.snapshots || {});
  renderDiagnostics(status.diagnostics || {});
  if (!document.querySelector("#config-form:focus-within")) {
    renderConfig(status.config || state.dashboard?.config);
  }
  renderCache(status.cache_report || state.dashboard?.cache_report, status.cache);
  renderDegradation(status.degradation || state.dashboard?.degradation);
}

function renderDashboard(data) {
  const totals = data.totals || {};
  const metrics = [
    ["文档总数", totals.doc_count, "确定性合成分片"],
    ["候选桶", totals.bucket_count, `${totals.empty_bucket_count || 0} 个空候选桶`],
    ["候选对", totals.candidate_pair_count, `${totals.comparison_count || 0} 次实际比较`],
    ["近重复对", totals.near_duplicate_pair_count, "阈值判定为近重复"],
    ["剔除", totals.removed_count, "可追溯到候选对"],
    ["保留", totals.kept_count, "质量分代表优先"],
  ];
  $("metrics").innerHTML = metrics
    .map(
      ([label, value, hint]) => `<article class="metric">
        <div class="label">${esc(label)}</div>
        <div class="value">${esc(fmt(value))}</div>
        <div class="hint">${esc(hint)}</div>
      </article>`,
    )
    .join("");
  $("mode-pill").textContent = `mode: ${data.config?.validation_mode || "-"}`;
  renderConfig(data.config);
  renderSnapshots(data.snapshots || {});
  renderHistogram(data.signature_block_count_histogram || {});
  renderShardStats(data.shard_stats || {});
  $("signature-total").textContent = `${fmt(totals.signature_block_count)} 个块签名`;
  renderBuckets(data.buckets || [], data.bucket_total);
  renderCandidates(data.candidates || [], data.candidate_total);
  renderRemoved(data.removed || []);
  renderKept(data.kept || []);
  renderObservedDocs(data.docs || []);
  renderCache(data.cache_report, data.cache);
  renderDiagnostics(data.diagnostics || {});
  renderDegradation(data.degradation);
  $("bucket-total").textContent = `显示 ${(data.buckets || []).length} / ${data.bucket_total}`;
  $("candidate-total").textContent = `显示 ${(data.candidates || []).length} / ${data.candidate_total}`;
}

function renderConfig(config) {
  if (!config) return;
  $("cfg-signature-length").value = config.signature_length;
  $("cfg-threshold").value = config.similarity_threshold;
  $("cfg-window").value = config.candidate_window;
  $("cfg-mode").value = config.validation_mode;
  $("cfg-threshold-policy").value = config.threshold_policy;
  $("cfg-latency").value = config.latency_budget_ms;
  $("cfg-max-docs").value = config.max_docs_per_batch;
  $("cfg-max-candidates").value = config.max_candidate_comparisons;
  $("cfg-max-cache").value = config.max_cache_entries;
  $("cfg-resource-policy").value = config.resource_policy;
}

function renderSnapshots(snapshots) {
  const labels = {
    shard_statistics: "分片统计量",
    removed_list: "剔除清单",
  };
  $("snapshot-grid").innerHTML = Object.entries(snapshots)
    .map(
      ([kind, item]) => `<div class="snapshot">
        <div class="name">${esc(labels[kind] || kind)}</div>
        <div class="version">v${esc(item.version)}</div>
        <div class="checksum">${esc(item.checksum)}</div>
      </div>`,
    )
    .join("");
}

function renderHistogram(histogram) {
  const entries = Object.entries(histogram).map(([key, value]) => [Number(key), value]);
  const max = Math.max(1, ...entries.map(([, value]) => value));
  if (!entries.length) {
    $("histogram").innerHTML = '<div class="empty-state">暂无签名分布</div>';
    return;
  }
  $("histogram").innerHTML = entries
    .map(
      ([key, value]) => `<div class="bar-wrap" title="${value} 篇文档有 ${key} 个签名块">
        <div class="bar-value">${value}</div>
        <div class="bar" style="height:${Math.max(4, Math.round((value / max) * 112))}px"></div>
        <div class="bar-label">${key} 块</div>
      </div>`,
    )
    .join("");
}

function renderShardStats(shardStats) {
  const entries = Object.entries(shardStats);
  $("shard-stat-note").textContent = `${entries.length} 个分片`;
  const rows = entries
    .map(
      ([shardId, stats]) => `<tr>
        <td class="mono">${esc(shardId)}</td>
        <td>${esc(stats.doc_count)}</td>
        <td>${esc(stats.kept_count)}</td>
        <td>${esc(stats.removed_count)}</td>
        <td>${esc(stats.bucket_count)}</td>
        <td>${esc(stats.empty_bucket_count)}</td>
        <td>${esc(stats.generated_candidate_pairs)} / ${esc(stats.candidate_pair_count)}</td>
        <td>${esc(stats.skipped_candidate_count)}</td>
        <td>${esc(stats.signature_block_count)}</td>
      </tr>`,
    )
    .join("");
  $("shard-stats-table").innerHTML = `<thead><tr><th>分片</th><th>文档</th><th>保留</th><th>剔除</th><th>桶</th><th>空桶</th><th>生成 / 实际候选</th><th>跳过</th><th>签名块</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="9">无分片统计</td></tr>'}</tbody>`;
}

function renderBuckets(buckets, total) {
  const rows = buckets
    .map(
      (bucket) => `<tr>
        <td class="mono">${esc(bucket.bucket_key.slice(0, 24))}…</td>
        <td>${esc(bucket.shard_id)}</td>
        <td>${esc(bucket.size)}</td>
        <td>${esc(bucket.candidate_count)}</td>
        <td>${bucket.empty_candidates ? '<span class="badge badge-skip">空候选</span>' : '<span class="badge">有候选</span>'}</td>
      </tr>`,
    )
    .join("");
  $("bucket-table").innerHTML = `<thead><tr><th>桶键</th><th>分片</th><th>文档</th><th>候选</th><th>状态</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5">无桶记录</td></tr>'}</tbody>`;
}

function renderCandidates(candidates, total) {
  const rows = candidates
    .map(
      (candidate) => `<tr>
        <td class="mono">${esc(candidate.a_doc)}<br>${esc(candidate.b_doc)}</td>
        <td>${esc(candidate.summary_relation)} / ${esc(candidate.window_distance)}</td>
        <td class="mono">${esc(candidate.matched_blocks)}/${esc(candidate.compared_blocks)}</td>
        <td class="mono">${esc(fmt(candidate.similarity))}</td>
        <td>${candidate.skipped ? `<span class="badge badge-skip">${esc(candidate.skip_reason)}</span>` : candidate.near_duplicate ? '<span class="badge badge-removed">近重复</span>' : '<span class="badge badge-kept">低于阈值</span>'}</td>
      </tr>`,
    )
    .join("");
  $("candidate-table").innerHTML = `<thead><tr><th>候选文档</th><th>摘要 / 窗口</th><th>命中</th><th>相似度</th><th>判定</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5">无候选对</td></tr>'}</tbody>`;
}

function renderRemoved(removed) {
  $("removed-total").textContent = `${removed.length} 篇`;
  const rows = removed
    .map(
      (item) => `<tr>
        <td class="mono">${esc(item.doc_id)}</td>
        <td class="mono">${esc(item.neighbor_doc_id)}</td>
        <td class="mono">${esc(item.similarity_fraction)}</td>
        <td class="mono">${esc((item.hit_positions || []).length)} 块</td>
        <td class="mono">${esc(item.candidate_id)}</td>
      </tr>`,
    )
    .join("");
  $("removed-table").innerHTML = `<thead><tr><th>被剔除</th><th>邻居代表</th><th>相似度</th><th>命中</th><th>候选对</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5">无剔除</td></tr>'}</tbody>`;
}

function renderKept(kept) {
  $("kept-total").textContent = `${kept.length} 篇`;
  const rows = kept
    .map(
      (item) => `<tr>
        <td class="mono">${esc(item.doc_id)}</td>
        <td>${esc(item.shard_id)}</td>
        <td class="mono">${esc(fmt(item.quality_score, 2))}</td>
        <td>${esc(item.block_count)}</td>
        <td>${esc(item.source)}</td>
      </tr>`,
    )
    .join("");
  $("kept-table").innerHTML = `<thead><tr><th>doc_id</th><th>分片</th><th>质量分</th><th>块数</th><th>来源</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5">无保留文档</td></tr>'}</tbody>`;
}

function renderObservedDocs(docs) {
  $("doc-id-options").innerHTML = docs
    .slice(0, 200)
    .map((doc) => `<option value="${esc(doc.doc_id)}">${esc(doc.shard_id)} · ${doc.removed ? "剔除" : "保留"}</option>`)
    .join("");
}

function renderCache(report, stats) {
  const cache = stats || report?.cache_stats || {};
  const rows = [
    ["命中", cache.hits],
    ["未命中", cache.misses],
    ["写入", cache.writes],
    ["失效", cache.invalidations],
    ["LRU 逐出", cache.evictions],
    ["当前条目", cache.lru_size],
    ["本次重算分片", (report?.recomputed_shards || []).join(", ") || "无"],
    ["本次复用分片", (report?.reused_shards || []).join(", ") || "无"],
  ];
  $("cache-box").innerHTML = rows
    .map(([key, value]) => `<div class="kv"><span>${esc(key)}</span><span class="mono">${esc(value ?? "-")}</span></div>`)
    .join("");
}

function renderDegradation(degradation) {
  const box = $("degradation-box");
  if (!degradation?.degraded) {
    box.classList.add("hidden");
    box.textContent = "";
    return;
  }
  box.classList.remove("hidden");
  box.textContent = `已降级：${(degradation.categories || []).join(", ")}`;
}

function renderDiagnostics(diagnostics) {
  const latencies = diagnostics.latencies || [];
  $("latency-list").innerHTML = latencies.length
    ? latencies
        .map(
          (item) => `<div class="event good">
            <div class="event-top"><code>${esc(item.doc_id)}</code><span>${esc(fmt(item.actual_ms))} ms</span></div>
            <div>估算 ${esc(fmt(item.estimated_ms))} ms · 预算 ${esc(fmt(item.budget_ms))} ms · ${item.removed ? "剔除" : "保留"}</div>
            <div class="muted">${(item.stages || []).map((stage) => `${esc(stage.name)}=${esc(fmt(stage.elapsed_ms))}ms`).join(" · ")}</div>
          </div>`,
        )
        .join("")
    : '<div class="empty-state">尚未发起查询。</div>';
  const errors = diagnostics.errors || [];
  $("error-list").innerHTML = errors.length
    ? errors
        .map(
          (item) => `<div class="event bad">
            <div class="event-top"><code>${esc(item.code)}</code><span>${esc(item.at)}</span></div>
            <div>${esc(item.message)} · stage=${esc(item.stage)}</div>
            <div class="muted">${(item.completed_stages || []).map((stage) => esc(stage.name)).join(" → ") || "无已完成阶段"}</div>
          </div>`,
        )
        .join("")
    : '<div class="empty-state">暂无业务错误。</div>';
}

function renderModeDiff(result) {
  const diff = result.removed_set_difference || {};
  const attribution = result.attribution_summary || {};
  $("mode-diff").innerHTML = `全量剔除 ${result.full_mode.removed.length} · 摘要剔除 ${result.digest_mode.removed.length}<br>
    仅全量剔除：<span class="mono">${esc((diff.only_full_mode_removed || []).join(", ") || "无")}</span><br>
    仅摘要剔除：<span class="mono">${esc((diff.only_digest_mode_removed || []).join(", ") || "无")}</span><br>
    归因：摘要冲突遗漏 ${esc(attribution.summary_conflict_omission || 0)} · 命中块减少 ${esc(attribution.hit_block_reduction || 0)}<br>
    命中位置差异 ${esc((result.hit_position_differences || []).length)} 对`;
}

async function queryDoc(event) {
  event.preventDefault();
  const docId = $("query-doc-id").value.trim();
  const budget = Number($("query-budget").value);
  if (!docId) return;
  try {
    const result = await api(`/api/query?doc_id=${encodeURIComponent(docId)}&budget_ms=${encodeURIComponent(budget)}`);
    renderQuery(result);
    await loadStatus();
  } catch (error) {
    renderQueryError(error.envelope?.error || { code: "E_CLIENT", message: String(error) });
    showError(error);
    await loadStatus().catch(() => {});
  }
}

function renderQueryError(error) {
  $("query-result").innerHTML = `<div class="notice">
    <strong>${esc(error.code)}</strong> · ${esc(error.message || "")}<br>
    阶段：${esc(error.stage || "-")}<br>
    已完成：${(error.completed_stages || []).map((item) => `${esc(item.name)} (${esc(fmt(item.elapsed_ms))} ms)`).join(" → ") || "无"}
  </div>
  <details><summary>异常明细</summary><pre>${esc(JSON.stringify(error, null, 2))}</pre></details>`;
}

function renderQuery(result) {
  const decision = result.similar_decision || {};
  const reason = result.removal_reason;
  const latency = result.latency || {};
  const summary = result.summary || {};
  const signatures = (result.block_signatures || [])
    .map((sig, index) => `<span class="signature-chip"><b>#${index}</b> ${esc(sig)}</span>`)
    .join("");
  const traces = (result.candidate_trace || [])
    .map((trace) => {
      const cls = trace.skipped ? "skipped" : trace.near_duplicate ? "near" : "kept";
      const hits = (trace.hit_positions || []).map((hit) => hit.own_position).join(", ") || "无";
      return `<div class="trace">
        <div>
          <div><span class="${cls}">${trace.skipped ? esc(trace.skip_reason) : trace.near_duplicate ? "近重复" : "非近重复"}</span>
            · <span class="mono">${esc(trace.peer_doc_id)}</span></div>
          <div class="meta">${esc(trace.summary_relation)} · 窗口 ${esc(trace.window_distance)} · 命中位置 ${esc(hits)}</div>
          <div class="meta">${esc(trace.threshold_trace?.rule || "")} · tolerance-band=${esc(trace.threshold_trace?.in_tolerance_band)}</div>
        </div>
        <div class="score">${esc(trace.similarity_fraction || fmt(trace.similarity))}<br><span class="meta">${esc(fmt(trace.similarity, 6))}</span></div>
      </div>`;
    })
    .join("") || '<div class="empty-state">该文档没有候选比较。</div>';
  const removal = reason
    ? `<div class="notice">
        剔除原因：${esc(reason.reason)} · 代表 ${esc(reason.neighbor_doc_id)} · 候选 ${esc(reason.candidate_id)}<br>
        命中位置：${esc((reason.hit_positions || []).map((hit) => hit.a_position).join(", "))} ·
        相似度 ${esc(reason.similarity_fraction)} · 阈值 ${esc(reason.threshold_trace?.threshold)}
      </div>`
    : '<div class="notice" style="border-color:rgba(109,226,163,.35);background:rgba(109,226,163,.06);color:var(--green)">该文档被保留，无剔除原因。</div>';
  $("query-result").innerHTML = `
    <div class="query-summary">
      <div class="query-stat"><div class="k">doc_id</div><div class="v mono">${esc(result.doc_id)}</div></div>
      <div class="query-stat"><div class="k">shard / bucket</div><div class="v mono">${esc(result.shard_id)}<br>${esc(result.bucket_key.slice(0, 26))}…</div></div>
      <div class="query-stat"><div class="k">判定</div><div class="v ${result.removed ? "near" : "kept"}">${esc(decision.outcome)}</div></div>
      <div class="query-stat"><div class="k">实际 / 预算</div><div class="v mono">${esc(fmt(latency.actual_ms))} / ${esc(fmt(latency.budget_ms))} ms</div></div>
    </div>
    <div>
      <div class="subsection-title">块签名（${(result.block_signatures || []).length}）</div>
      <div class="signature-strip">${signatures || "无"}</div>
    </div>
    <div>
      <div class="subsection-title">文档级签名摘要</div>
      <div class="signature-strip">${(summary.bands || []).map((band, index) => `<span class="signature-chip"><b>band ${index}</b> ${esc(band)}</span>`).join("")}</div>
    </div>
    <div>
      <div class="subsection-title">候选比较轨迹（${(result.candidate_trace || []).length}）</div>
      <div class="trace-list">${traces}</div>
    </div>
    ${removal}
    <div class="query-summary">
      <div class="query-stat"><div class="k">估算</div><div class="v mono">${esc(fmt(latency.estimated_ms))} ms</div></div>
      <div class="query-stat"><div class="k">候选 / 块数</div><div class="v mono">${esc(latency.estimate_basis?.candidate_comparisons)} / ${esc(latency.estimate_basis?.signature_blocks)}</div></div>
      <div class="query-stat"><div class="k">一致性</div><div class="v kept">${result.consistency?.checked ? "PASS" : "NOT CHECKED"}</div></div>
      <div class="query-stat"><div class="k">声明精度</div><div class="v mono">${esc(result.consistency?.precision || "-")}</div></div>
    </div>
    <details><summary>完整查询 JSON</summary><pre>${esc(JSON.stringify(result, null, 2))}</pre></details>`;
}

async function applyConfig() {
  const button = $("apply-config");
  setBusy(button, true, "失效并重算…");
  const updates = {
    signature_length: Number($("cfg-signature-length").value),
    similarity_threshold: $("cfg-threshold").value.trim(),
    candidate_window: Number($("cfg-window").value),
    validation_mode: $("cfg-mode").value,
    threshold_policy: $("cfg-threshold-policy").value,
    latency_budget_ms: Number($("cfg-latency").value),
    max_docs_per_batch: Number($("cfg-max-docs").value),
    max_candidate_comparisons: Number($("cfg-max-candidates").value),
    max_cache_entries: Number($("cfg-max-cache").value),
    resource_policy: $("cfg-resource-policy").value,
  };
  try {
    const result = await api("/api/config", {
      method: "POST",
      body: JSON.stringify(updates),
    });
    const transition = result.transition || {};
    $("transition-box").classList.remove("hidden");
    $("transition-box").textContent = result.changed
      ? `配置已整体切换。字段：${(transition.changed_fields || []).join(", ")}
受影响文档 ${transition.affected_docs?.length || 0} 篇 · 失效缓存条目 ${transition.invalidated_entries?.length || 0} · 重算 ${transition.recomputed_docs?.length || 0} · 复用 ${transition.reused_docs?.length || 0}`
      : "配置未变化，无需重算。";
    await loadDashboard();
    await loadStatus();
  } catch (error) {
    showError(error);
    const code = error.envelope?.error?.code;
    $("transition-box").classList.remove("hidden");
    $("transition-box").textContent = `${code || "E_CLIENT"}：${error.message}`;
  } finally {
    setBusy(button, false);
  }
}

async function refreshCache() {
  const button = $("refresh-btn");
  setBusy(button, true, "缓存重跑…");
  try {
    const result = await api("/api/refresh", { method: "POST", body: "{}" });
    await loadDashboard();
    await loadStatus();
    $("transition-box").classList.remove("hidden");
    $("transition-box").textContent = `复用分片：${(result.cache_report?.reused_shards || []).join(", ") || "无"}；重算分片：${(result.cache_report?.recomputed_shards || []).join(", ") || "无"}`;
  } catch (error) {
    showError(error);
  } finally {
    setBusy(button, false);
  }
}

async function compareModes() {
  const button = $("compare-btn");
  setBusy(button, true, "比较中…");
  try {
    const result = await api("/api/compare-modes");
    renderModeDiff(result);
  } catch (error) {
    showError(error);
  } finally {
    setBusy(button, false);
  }
}

async function runConsistency() {
  const button = $("consistency-btn");
  setBusy(button, true, "校验中…");
  try {
    const result = await api("/api/consistency");
    $("mode-diff").innerHTML = `一致性：<span class="kept">PASS</span>，${esc(result.checked_docs)} 篇文档，精度 ${esc(result.precision)}，最大查询 ${esc(fmt(result.max_actual_query_ms))} ms`;
    await loadStatus();
  } catch (error) {
    showError(error);
  } finally {
    setBusy(button, false);
  }
}

async function boot() {
  $("query-form").addEventListener("submit", queryDoc);
  $("apply-config").addEventListener("click", applyConfig);
  $("refresh-btn").addEventListener("click", refreshCache);
  $("compare-btn").addEventListener("click", compareModes);
  $("consistency-btn").addEventListener("click", runConsistency);
  try {
    await Promise.all([loadDashboard(), loadStatus()]);
    const firstDoc = state.dashboard?.docs?.[0]?.doc_id;
    if (firstDoc) $("query-doc-id").value = firstDoc;
  } catch (error) {
    showError(error);
    $("health-pill").textContent = "初始化失败";
    $("health-pill").className = "pill pill-bad";
  }
  state.polling = setInterval(() => {
    loadStatus().catch(() => {});
  }, 5000);
}

document.addEventListener("DOMContentLoaded", boot);

