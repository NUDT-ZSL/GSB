(() => {
  "use strict";

  const state = {
    overview: null,
    samples: null,
    currentResult: null,
    compare: null,
    verify: null,
    activeView: "dashboard",
    selectedVersion: null,
    loading: 0,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const changeLabels = {
    initial: "初始快照",
    incremental_append: "增量追加",
    full_replace: "完整替换",
    noop: "无需操作",
  };

  const metricLabels = {
    threshold: "过滤阈值",
    effective_weight: "有效权重",
    expected_batch: "期望批大小",
    eligible_count: "可过滤事件",
    processed_count: "批内事件",
    weighted_sum: "加权聚合",
    output_count: "输出数量",
  };

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;",
    })[char]);
  }

  function num(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function shortDigest(value, size = 13) {
    const text = String(value || "");
    if (!text) return "—";
    return text.length > size * 2 ? `${text.slice(0, size)}…${text.slice(-6)}` : text;
  }

  function pretty(value) {
    return JSON.stringify(value, null, 2);
  }

  function addClass(value) {
    return String(value || "0").trim();
  }

  function setLoading(active) {
    state.loading += active ? 1 : -1;
    state.loading = Math.max(0, state.loading);
    $("#loadingLine").classList.toggle("active", state.loading > 0);
  }

  function toast(title, message = "", type = "info", timeout = 4400) {
    const stack = $("#toastStack");
    const node = document.createElement("div");
    node.className = `toast ${type}`;
    node.innerHTML = `<strong>${esc(title)}</strong><span>${esc(message)}</span>`;
    stack.appendChild(node);
    window.setTimeout(() => {
      node.style.opacity = "0";
      node.style.transform = "translateY(6px)";
      window.setTimeout(() => node.remove(), 180);
    }, timeout);
  }

  function showError(error) {
    const code = error?.code || "E_CLIENT";
    const message = error?.message || error?.error?.message || String(error);
    toast(code, message, "error", 6200);
  }

  async function api(path, options = {}) {
    const { method = "GET", body = undefined, silent = false } = options;
    setLoading(true);
    try {
      const response = await fetch(path, {
        method,
        headers: body === undefined ? {} : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      let payload;
      try {
        payload = await response.json();
      } catch {
        throw { code: "E_HTTP", message: `HTTP ${response.status} 响应不是 JSON` };
      }
      if (!response.ok || payload.ok === false) {
        throw payload.error || { code: "E_HTTP", message: `HTTP ${response.status}` };
      }
      return payload;
    } catch (error) {
      if (!silent) showError(error);
      throw error;
    } finally {
      setLoading(false);
    }
  }

  async function apiText(path, text, options = {}) {
    const { method = "POST", silent = false } = options;
    setLoading(true);
    try {
      const response = await fetch(path, {
        method,
        headers: { "Content-Type": "application/json" },
        body: text,
      });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) {
        throw payload.error || { code: "E_HTTP", message: `HTTP ${response.status}` };
      }
      return payload;
    } catch (error) {
      if (!silent) showError(error);
      throw error;
    } finally {
      setLoading(false);
    }
  }

  function switchView(name) {
    state.activeView = name;
    $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
    $$(".rail-button").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (name === "verify" && !state.verify) runVerify(true);
  }

  function bindEvents() {
    $$(".rail-button").forEach((button) => {
      button.addEventListener("click", () => switchView(button.dataset.view));
    });
    $$("[data-jump]").forEach((button) => {
      button.addEventListener("click", () => switchView(button.dataset.jump));
    });
    $("#refreshButton").addEventListener("click", () => refreshAll(false));
    $("#resetButton").addEventListener("click", resetState);
    $("#bootstrapButton").addEventListener("click", () => bootstrapDemo(true));
    $("#bootstrapSnapshotOnly").addEventListener("click", () => bootstrapDemo(false));
    $("#loadPatch2").addEventListener("click", () => loadPatchSample("patch_002"));
    $("#loadPatch3").addEventListener("click", () => loadPatchSample("patch_003"));
    $("#digestButton").addEventListener("click", digestPatch);
    $("#applyButton").addEventListener("click", applyPatch);
    $("#computeButton").addEventListener("click", runCompute);
    $("#loadComputeSample").addEventListener("click", loadComputeSample);
    $("#resumeButton").addEventListener("click", resumePending);
    $("#copyConfigButton").addEventListener("click", copyConfig);
    $("#compareButton").addEventListener("click", runCompare);
    $("#compareFromVersionsButton").addEventListener("click", async () => {
      switchView("diagnostics");
      const versions = state.overview?.versions || [];
      if (versions.length >= 2) {
        $("#compareFrom").value = String(versions[0].version);
        $("#compareTo").value = String(versions[versions.length - 1].version);
        await runCompare();
      }
    });
    $("#verifyButton").addEventListener("click", () => runVerify(false));
    $("#configTableBody").addEventListener("click", handleClientResultClick);
    $("#clientGrid").addEventListener("click", handleClientResultClick);
  }

  async function init() {
    bindEvents();
    try {
      state.samples = await api("/api/samples", { silent: true });
      loadPatchSample("patch_002", false);
      loadComputeSample(false);
      loadCompareSample(false);
    } catch (error) {
      showError(error);
    }
    await refreshAll(true);
    window.setInterval(() => refreshAll(true), 20000);
  }

  async function refreshAll(silent = true) {
    try {
      const overview = await api("/api/overview", { silent });
      state.overview = overview;
      renderHeader();
      renderStats();
      renderConfigs();
      renderTimeline();
      renderClients();
      renderLedger();
      renderVersionControls();
      renderVersionList();
      $("#bootOverlay").hidden = Boolean(overview.initialized);
      $("#healthBadge").classList.add("live");
      $("#healthBadge").innerHTML = "<span></span>LOCAL LIVE";
      $("#railPulse").classList.add("live");
      if (overview.initialized && !state.selectedVersion) {
        state.selectedVersion = overview.pointer.version;
        renderVersionInspector();
      }
      if (overview.initialized) {
        $("#versionInspectorBadge").textContent = `v${overview.pointer.version} head`;
      }
    } catch (error) {
      $("#healthBadge").textContent = "连接失败";
      if (!silent) showError(error);
    }
  }

  function renderHeader() {
    const overview = state.overview;
    if (!overview) return;
    const status = overview.status || {};
    $("#headerNamespace").textContent = overview.namespace || "—";
    $("#headerVersion").textContent = overview.pointer ? `v${overview.pointer.version}` : "—";
    $("#headerClock").textContent = num(status.logical_clock_watermark, "—");
  }

  function renderStats() {
    const overview = state.overview;
    if (!overview) return;
    const status = overview.status || {};
    const configs = overview.configs || [];
    const rounds = (status.clients || []).reduce((total, client) => total + client.rounds.length, 0);
    const sensitive = status.precision_sensitive_count || 0;
    const pending = (status.pending_jobs || []).length;
    $("#statVersion").textContent = overview.pointer ? `v${overview.pointer.version}` : "—";
    $("#statVersionNote").textContent = overview.pointer
      ? `clock ${overview.pointer.logical_clock_ms} / ${shortDigest(overview.pointer.digest, 8)}`
      : "等待初始化";
    $("#statFixedKeys").textContent = String(configs.filter((config) => config.conversion_id !== null).length);
    $("#statRounds").textContent = String(rounds);
    $("#statRoundsNote").textContent = rounds ? `${(status.clients || []).length} 个客户端` : "已提交结果";
    $("#statSensitive").textContent = String(sensitive);
    $("#statSensitiveNote").textContent = sensitive ? "存在舍入翻转" : "当前无敏感结论";
    $("#configCountBadge").textContent = `${configs.length} keys`;
    $("#pendingBadge").textContent = `${pending} pending`;
    const boundClock = $("#boundClockBadge");
    boundClock.textContent = `clock ${num(status.logical_clock_watermark, "—")}`;
  }

  function renderConfigs() {
    const configs = state.overview?.configs || [];
    const body = $("#configTableBody");
    if (!configs.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty-cell">尚未加载配置</td></tr>`;
      return;
    }
    body.innerHTML = configs.map((config) => {
      const residual = Number(config.residual_units || 0);
      return `
        <tr>
          <td><code>${esc(config.key)}</code></td>
          <td><span class="role-tag">${esc(config.role || "—")}</span></td>
          <td class="num">${esc(config.value)}</td>
          <td class="num">${esc(config.scale)}</td>
          <td class="num">${esc(num(config.fixed_value))}</td>
          <td class="num"><span class="residual-tag ${residual > 0 ? "" : "zero"}">${esc(config.residual_units)}</span></td>
          <td class="num">${esc(num(config.conversion_id))}${config.conversion_version ? `<span class="role-tag"> @v${esc(config.conversion_version)}</span>` : ""}</td>
        </tr>
      `;
    }).join("");
  }

  function renderTimeline() {
    const versions = state.overview?.versions || [];
    const target = $("#miniTimeline");
    if (!versions.length) {
      target.innerHTML = `<div class="empty-state">版本链为空</div>`;
      return;
    }
    target.innerHTML = versions.map((version) => `
      <div class="timeline-item ${version.is_current ? "current" : ""}">
        <span class="timeline-node"></span>
        <div>
          <div class="timeline-version">
            <strong>v${esc(version.version)}</strong>
            <span>${esc(version.logical_clock_ms)} ms</span>
          </div>
          <div class="timeline-meta">
            <span class="change-tag ${esc(version.change_kind)}">${esc(changeLabels[version.change_kind] || version.change_kind)}</span>
            &nbsp; ${esc(version.config_count)} keys · ${esc(shortDigest(version.digest, 8))}
          </div>
        </div>
      </div>
    `).join("");
  }

  function renderClients() {
    const clients = state.overview?.status?.clients || [];
    const target = $("#clientGrid");
    if (!clients.length) {
      target.innerHTML = `<div class="empty-state">还没有客户端计算结果</div>`;
      return;
    }
    target.innerHTML = clients.map((client) => `
      <article class="client-card">
        <div class="client-card-head">
          <strong>${esc(client.client_id)}</strong>
          <span class="tiny-badge">${client.rounds.length} rounds</span>
        </div>
        ${client.rounds.map((round) => `
          <div class="client-round">
            <div class="client-round-main">
              <code><span class="status-dot ${round.precision_sensitive ? "warn" : ""}"></span>${esc(round.round_key)}</code>
              <div class="client-round-values">
                v${esc(round.bound_version)} · θ ${esc(num(round.derived.threshold))} · batch ${esc(num(round.derived.expected_batch))}
              </div>
            </div>
            <button class="text-button client-result-button"
              data-client-id="${esc(client.client_id)}"
              data-round-key="${esc(round.round_key)}">打开</button>
          </div>
        `).join("")}
      </article>
    `).join("");
  }

  function renderLedger() {
    const configs = state.overview?.configs || [];
    const target = $("#ledgerSummary");
    if (!configs.length) {
      target.innerHTML = `<div class="empty-state">暂无换算登记</div>`;
      return;
    }
    const unique = new Map();
    configs.forEach((config) => {
      if (config.conversion_id !== null) unique.set(config.conversion_id, config);
    });
    const rows = Array.from(unique.values()).sort((a, b) => {
      const ar = Number(a.residual_units || 0);
      const br = Number(b.residual_units || 0);
      return br - ar || String(a.key).localeCompare(String(b.key));
    });
    target.innerHTML = rows.slice(0, 7).map((config) => {
      const residual = Math.max(0, Math.min(1, Number(config.residual_units || 0)));
      return `
        <div class="ledger-row">
          <div>
            <code>${esc(config.key)}</code>
            <div class="ledger-bar"><span style="width:${(residual * 100).toFixed(1)}%"></span></div>
          </div>
          <div style="text-align:right">
            <code>${esc(config.residual_units)} units</code>
            <div class="role-tag">#${esc(config.conversion_id)} · v${esc(config.conversion_version)}</div>
          </div>
        </div>
      `;
    }).join("");
  }

  function renderVersionControls() {
    const versions = state.overview?.versions || [];
    const from = $("#compareFrom");
    const to = $("#compareTo");
    const previousFrom = from.value;
    const previousTo = to.value;
    const options = versions.map((version) => `<option value="${version.version}">v${version.version} · ${esc(changeLabels[version.change_kind] || version.change_kind)}</option>`).join("");
    from.innerHTML = options;
    to.innerHTML = options;
    if (versions.length) {
      from.value = previousFrom || String(versions[0].version);
      to.value = previousTo || String(versions[versions.length - 1].version);
    }
  }

  function renderVersionList() {
    const versions = state.overview?.versions || [];
    const target = $("#versionList");
    if (!versions.length) {
      target.innerHTML = `<div class="empty-state">版本链为空</div>`;
      return;
    }
    target.innerHTML = versions.map((version) => `
      <button class="version-card ${state.selectedVersion === version.version ? "active" : ""}" data-version="${version.version}">
        <div class="version-card-top">
          <strong>v${version.version}</strong>
          <span class="change-tag ${esc(version.change_kind)}">${esc(changeLabels[version.change_kind] || version.change_kind)}</span>
        </div>
        <div class="version-card-meta">
          parent ${esc(num(version.parent_version))} · clock ${esc(version.logical_clock_ms)} · ${esc(version.config_count)} keys<br>
          ${esc(version.digest)}
        </div>
      </button>
    `).join("");
    $$(".version-card", target).forEach((button) => {
      button.addEventListener("click", () => {
        state.selectedVersion = Number(button.dataset.version);
        renderVersionList();
        renderVersionInspector();
      });
    });
  }

  async function renderVersionInspector() {
    if (!state.selectedVersion || !state.overview?.namespace) return;
    const target = $("#versionInspector");
    $("#versionInspectorTitle").textContent = `版本 v${state.selectedVersion} 快照`;
    try {
      const payload = await api(`/api/version/${state.selectedVersion}?namespace=${encodeURIComponent(state.overview.namespace)}`, { silent: true });
      const snapshot = payload.snapshot;
      const configs = payload.configs;
      target.innerHTML = `
        <div class="inspector-meta">
          <div><span>DIGEST</span><strong>${esc(shortDigest(snapshot.digest, 11))}</strong></div>
          <div><span>CLOCK</span><strong>${esc(snapshot.logical_clock_ms)} ms</strong></div>
          <div><span>CONFIGS</span><strong>${esc(configs.length)}</strong></div>
        </div>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>键</th><th>值</th><th class="num">标度</th><th class="num">定点</th><th class="num">余量</th></tr></thead>
            <tbody>
              ${configs.map((config) => `
                <tr>
                  <td><code>${esc(config.key)}</code></td>
                  <td>${esc(config.value)}</td>
                  <td class="num">${esc(config.scale)}</td>
                  <td class="num">${esc(num(config.fixed_value))}</td>
                  <td class="num">${esc(config.residual_units)}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      `;
    } catch (error) {
      target.innerHTML = `<div class="empty-state">${esc(error.code || "E_CLIENT")}: ${esc(error.message || error)}</div>`;
    }
  }

  function loadPatchSample(key, announce = true) {
    const patch = state.samples?.[key];
    if (!patch) {
      if (announce) toast("E_SAMPLE", "样本补丁尚未加载", "warn");
      return;
    }
    $("#patchEditor").value = pretty(patch);
    $("#patchStateBadge").textContent = `v${patch.target_version} · digest ready`;
    if (announce) toast("SAMPLE LOADED", `${patch.target_version} 版本补丁已载入`);
  }

  async function digestPatch() {
    const text = $("#patchEditor").value.trim();
    try {
      JSON.parse(text);
      const result = await apiText("/api/digest", text);
      $("#patchEditor").value = result.patch_text;
      $("#patchStateBadge").textContent = `digest ${shortDigest(result.content_digest, 8)}`;
      toast("DIGEST VERIFIED", result.content_digest);
    } catch (error) {
      if (!error.code) showError({ code: "E_BAD_JSON", message: "补丁文本不是合法 JSON" });
    }
  }

  async function applyPatch() {
    const text = $("#patchEditor").value.trim();
    try {
      JSON.parse(text);
      const result = await apiText("/api/apply", text);
      renderApplyResult(result);
      toast(result.idempotent ? "IDEMPOTENT" : "VERSION COMMITTED", `v${result.version} ${changeLabels[result.change_kind] || result.change_kind}`, result.idempotent ? "warn" : "info");
      await refreshAll(true);
    } catch (error) {
      if (!error.code) showError({ code: "E_BAD_JSON", message: "补丁文本不是合法 JSON" });
    }
  }

  function renderApplyResult(result) {
    const target = $("#applyResult");
    const detail = result.change_detail || {};
    $("#patchStateBadge").textContent = result.status === "noop" ? "幂等 noop" : `v${result.version} committed`;
    target.innerHTML = `
      <div class="result-hero">
        <div>
          <div class="eyebrow">${esc(result.status.toUpperCase())}</div>
          <h4>版本 v${esc(result.version)} · ${esc(changeLabels[result.change_kind] || result.change_kind)}</h4>
          <code>${esc(result.digest)}</code>
        </div>
        <div class="result-flags">
          <span class="flag ok">${result.idempotent ? "IDEMPOTENT" : "ATOMIC"}</span>
          ${result.parent_version ? `<span class="flag">parent v${esc(result.parent_version)}</span>` : ""}
        </div>
      </div>
      <div class="inspector-meta" style="padding:16px 16px 0">
        <div><span>ADDED</span><strong>${esc((detail.added_keys || []).join(", ") || "—")}</strong></div>
        <div><span>REMOVED</span><strong>${esc((detail.removed_keys || []).join(", ") || "—")}</strong></div>
        <div><span>CHANGED</span><strong>${esc((detail.changed_keys || []).join(", ") || "—")}</strong></div>
      </div>
      <div class="inline-code-block">PATCH ${esc(shortDigest(result.patch_digest, 20))}\nDIGEST ${esc(shortDigest(result.digest, 20))}\nCLOCK  ${esc(result.logical_clock_ms ?? "—")} ms</div>
    `;
  }

  function loadComputeSample(announce = true) {
    const sample = state.samples?.compute_input;
    if (!sample) {
      if (announce) toast("E_SAMPLE", "计算样本尚未加载", "warn");
      return;
    }
    $("#computeEditor").value = pretty(sample);
    $("#computeClient").value = sample.client_id || "worker-a";
    $("#computeRound").value = sample.round_key || "round-v1";
    $("#computeTolerance").value = sample.tolerance || "0.001";
    if (announce) toast("SAMPLE LOADED", "round-v1 计算输入已载入");
  }

  function loadCompareSample(announce = true) {
    const sample = state.samples?.compute_input;
    if (!sample) return;
    $("#compareEditor").value = pretty(sample);
    if (announce) toast("SAMPLE LOADED", "版本比较输入已载入");
  }

  async function runCompute() {
    let input;
    try {
      input = JSON.parse($("#computeEditor").value);
    } catch {
      showError({ code: "E_BAD_JSON", message: "计算输入不是合法 JSON" });
      return;
    }
    input.client_id = $("#computeClient").value.trim() || input.client_id;
    input.round_key = $("#computeRound").value.trim() || input.round_key;
    input.tolerance = $("#computeTolerance").value.trim() || "0.001";
    try {
      const response = await api("/api/compute", { method: "POST", body: input });
      state.currentResult = response.result;
      renderComputeResult(response);
      toast("COMPUTE COMMITTED", `结果绑定 v${response.bound_version} · ${shortDigest(response.result.result_digest, 9)}`);
      await refreshAll(true);
    } catch {
      /* handled by api */
    }
  }

  function renderComputeResult(response) {
    const result = response.result;
    const sensitivity = result.sensitivity || {};
    const metrics = result.metrics || {};
    $("#resultBoundBadge").textContent = `bound v${result.bound_version}`;
    $("#computeResult").innerHTML = `
      <div class="result-hero">
        <div>
          <div class="eyebrow">BOUND VERSION ${esc(result.bound_version)}</div>
          <h4>${sensitivity.precision_sensitive ? "精度敏感结论" : "定点通路稳定"}</h4>
          <code>${esc(result.result_digest)}</code>
        </div>
        <div class="result-flags">
          <span class="flag ${sensitivity.precision_sensitive ? "sensitive" : "ok"}">${sensitivity.precision_sensitive ? "SENSITIVE" : "BOUNDED"}</span>
          <span class="flag">${esc(result.logical_clock_ms)} ms</span>
          ${response.idempotent ? `<span class="flag">IDEMPOTENT</span>` : ""}
        </div>
      </div>
      <div class="table-wrap">
        <table class="metric-table">
          <thead><tr><th>派生量</th><th>定点</th><th>decimal 真值</th><th>偏差</th><th>误差上界</th></tr></thead>
          <tbody>
            ${(result.metric_order || Object.keys(metrics)).map((name) => {
              const metric = metrics[name];
              return `
                <tr>
                  <td>${esc(metricLabels[name] || name)}</td>
                  <td class="num-strong">${esc(metric.fixed)}</td>
                  <td>${esc(metric.exact)}</td>
                  <td class="${Number(metric.deviation) < 0 ? "negative" : ""}">${esc(metric.deviation)}</td>
                  <td><span class="flag ${metric.bound_ok ? "ok" : "sensitive"}">${esc(metric.error_bound)}</span></td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
      <div class="sensitivity-callout ${sensitivity.precision_sensitive ? "" : "ok"}">
        <strong>${sensitivity.precision_sensitive ? "精度敏感：固定路径改变了离散结论" : "精度稳定：固定路径的离散结论与真值一致"}</strong>
        <p>${sensitivity.precision_sensitive
          ? esc((sensitivity.reasons || []).map((reason) => reason.detail).join(" "))
          : "所有误差上界覆盖当前偏差；过滤集合、批内集合与输出数量与 decimal 真值一致。"}</p>
      </div>
      <div class="conclusion-grid">
        <div class="conclusion-card">
          <span>FIXED CONCLUSION</span>
          <code>filtered: ${esc(JSON.stringify(result.fixed_conclusion.filtered_ids))}\nprocessed: ${esc(JSON.stringify(result.fixed_conclusion.processed_ids))}\noutput: ${esc(result.fixed_conclusion.output_count)}</code>
        </div>
        <div class="conclusion-card">
          <span>DECIMAL CONCLUSION</span>
          <code>filtered: ${esc(JSON.stringify(result.exact_conclusion.filtered_ids))}\nprocessed: ${esc(JSON.stringify(result.exact_conclusion.processed_ids))}\noutput: ${esc(result.exact_conclusion.output_count)}</code>
        </div>
      </div>
    `;
    renderSourceChain(result);
  }

  function sourceChainHtml(metrics) {
    const names = Object.keys(metrics || {});
    if (!names.length) return `<div class="empty-state">没有误差来源</div>`;
    return names.map((name) => {
      const metric = metrics[name];
      const sources = metric.sources || [];
      return `
        <details class="source-detail">
          <summary>
            <span>${esc(metricLabels[name] || name)} · ${esc(metric.formula)}</span>
            <span>Δ ${esc(metric.absolute_deviation)} / bound ${esc(metric.error_bound)}</span>
          </summary>
          <div class="table-wrap">
            <table class="source-table">
              <thead><tr><th>配置键</th><th>版本</th><th>换算序号</th><th>标度</th><th>carry in</th><th>discarded</th><th>剩余</th><th>贡献</th><th>说明</th></tr></thead>
              <tbody>
                ${sources.map((source) => `
                  <tr>
                    <td>${esc(source.config_key)}</td>
                    <td>${esc(num(source.version))}</td>
                    <td>#${esc(num(source.conversion_id))}</td>
                    <td>${esc(source.scale)}</td>
                    <td>${esc(source.carried_in_units)}</td>
                    <td>${esc(source.discarded_units)}</td>
                    <td>${esc(source.residual_units)} u / ${esc(source.residual)}</td>
                    <td>${esc(source.contribution)}</td>
                    <td>${esc(source.note)}</td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          </div>
        </details>
      `;
    }).join("");
  }

  function renderSourceChain(result) {
    const metrics = result.metrics || {};
    const count = Object.values(metrics).reduce((total, metric) => total + (metric.sources?.length || 0), 0);
    $("#sourceChainCount").textContent = `${count} sources`;
    $("#sourceChain").innerHTML = sourceChainHtml(metrics);
  }

  async function runCompare() {
    let input;
    try {
      input = JSON.parse($("#compareEditor").value);
    } catch {
      showError({ code: "E_BAD_JSON", message: "比较输入不是合法 JSON" });
      return;
    }
    const body = {
      namespace: state.overview?.namespace,
      from_version: Number($("#compareFrom").value),
      to_version: Number($("#compareTo").value),
      tolerance: $("#compareTolerance").value,
      scale_factor: Number($("#compareScaleFactor").value),
      input,
    };
    try {
      const result = await api("/api/compare", { method: "POST", body });
      state.compare = result;
      renderCompare(result);
      toast("COMPARISON READY", `v${result.from_version} → v${result.to_version}`);
    } catch {
      /* handled */
    }
  }

  function renderCompare(result) {
    const comparison = result.comparison;
    const high = result.higher_scale;
    const sensitiveRows = comparison.metrics.filter((row) => row.precision_sensitive);
    $("#compareOutput").innerHTML = `
      <div class="compare-summary">
        <div class="compare-kpi"><span>FROM / TO</span><strong>v${esc(result.from_version)} → v${esc(result.to_version)}</strong></div>
        <div class="compare-kpi"><span>PRECISION SENSITIVE</span><strong class="${comparison.precision_sensitive ? "warn" : "ok"}">${comparison.precision_sensitive ? "YES" : "NO"}</strong></div>
        <div class="compare-kpi"><span>HIGHER SCALE FLIP</span><strong class="${high.flips_conclusion ? "warn" : "ok"}">${high.flips_conclusion ? "YES" : "NO"}</strong></div>
        <div class="compare-kpi"><span>TOLERANCE</span><strong>${esc(comparison.tolerance)}</strong></div>
      </div>
      <div class="compare-callout ${comparison.precision_sensitive ? "" : "ok"}">
        <div class="eyebrow">${high.flips_conclusion ? "SCALE-SENSITIVE CONCLUSION" : "NUMERIC CONCLUSION"}</div>
        <h3>${comparison.precision_sensitive ? "版本差异由余量传播触发" : "未发现精度敏感差异"}</h3>
        <p>${esc(high.explanation)}</p>
      </div>
      <article class="panel">
        <div class="panel-head">
          <div><div class="eyebrow">METRIC DELTA</div><h3>两版本派生量对照</h3></div>
          <span class="tiny-badge">${sensitiveRows.length} sensitive rows</span>
        </div>
        <div class="table-wrap">
          <table class="metric-table compare-table">
            <thead><tr><th>派生量</th><th>左定点</th><th>右定点</th><th>定点差</th><th>真值差</th><th>结论变化</th><th>精度敏感</th></tr></thead>
            <tbody>
              ${comparison.metrics.map((row) => `
                <tr>
                  <td>${esc(metricLabels[row.metric] || row.metric)}</td>
                  <td>${esc(row.left_fixed)}</td>
                  <td>${esc(row.right_fixed)}</td>
                  <td>${esc(row.fixed_delta)}</td>
                  <td>${esc(row.exact_delta)}</td>
                  <td>${row.conclusion_changed ? "YES" : "NO"}</td>
                  <td class="${row.precision_sensitive ? "sensitive" : ""}">${row.precision_sensitive ? "YES" : "NO"}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </article>
      <div class="version-detail-grid" style="margin-top:14px">
        <article class="panel">
          <div class="panel-head"><div><div class="eyebrow">HIGH SCALE PROBE</div><h3>${esc(high.scale_factor)}× 标度</h3></div></div>
          <div class="conclusion-grid" style="padding-top:16px">
            <div class="conclusion-card">
              <span>LEFT PROBE</span>
              <code>${esc(JSON.stringify(high.left_fixed_conclusion))}</code>
            </div>
            <div class="conclusion-card">
              <span>RIGHT PROBE</span>
              <code>${esc(JSON.stringify(high.right_fixed_conclusion))}</code>
            </div>
          </div>
        </article>
        <article class="panel">
          <div class="panel-head"><div><div class="eyebrow">RIGHT SOURCES</div><h3>v${esc(result.to_version)} 误差链</h3></div></div>
          <div class="source-chain">${sourceChainHtml(result.right.metrics)}</div>
        </article>
      </div>
    `;
  }

  async function runVerify(silent = false) {
    const button = $("#verifyButton");
    button.disabled = true;
    try {
      const namespace = state.overview?.namespace || "";
      const result = await api(`/api/verify?namespace=${encodeURIComponent(namespace)}`, { silent });
      state.verify = result;
      renderVerify(result);
      toast(result.passed ? "VERIFY PASSED" : "VERIFY FAILED", `${result.summary.passed}/${result.summary.checks} checks passed`, result.passed ? "info" : "error");
    } catch {
      /* handled */
    } finally {
      button.disabled = false;
    }
  }

  function renderVerify(result) {
    const errorCheck = (result.checks || []).find((check) => check.name === "business_error_contract");
    const errorCases = errorCheck?.detail || [];
    $("#verifyOutput").innerHTML = `
      <div class="verify-summary">
        <div class="verify-card"><span>CHECKS</span><strong>${esc(result.summary.checks)}</strong></div>
        <div class="verify-card"><span>PASSED</span><strong style="color:var(--green)">${esc(result.summary.passed)}</strong></div>
        <div class="verify-card"><span>FAILED</span><strong style="color:${result.summary.failed ? "var(--red)" : "var(--muted)"}">${esc(result.summary.failed)}</strong></div>
        <div class="verify-card"><span>WARNINGS</span><strong style="color:var(--amber)">${esc(result.summary.warnings)}</strong></div>
      </div>
      <div class="verify-list">
        ${(result.checks || []).map((check) => {
          const detail = typeof check.detail === "string" ? check.detail : summarizeCheck(check);
          return `
            <div class="verify-check">
              <span class="check-status ${esc(check.status)}">${esc(check.status.toUpperCase())}</span>
              <div>
                <strong>${esc(check.name)}</strong>
                <p>${esc(detail)}</p>
              </div>
              <code>${esc(check.code || "")}</code>
            </div>
          `;
        }).join("")}
      </div>
      <article class="panel" style="margin-top:14px">
        <div class="panel-head"><div><div class="eyebrow">ERROR CONTRACT</div><h3>业务错误码复现</h3></div></div>
        <div class="error-code-grid">
          ${errorCases.map((item) => `
            <div class="error-code">
              <code>${esc(item.expected_code || item.actual_code)}</code>
              <span>${esc(item.case)} · ${esc(item.status)} · ${esc(item.message || "")}</span>
            </div>
          `).join("") || `<div class="empty-state">没有错误码检查结果</div>`}
        </div>
      </article>
      <details class="raw-details">
        <summary>查看 verify 原始 JSON</summary>
        <pre class="raw-json">${esc(pretty(result))}</pre>
      </details>
    `;
  }

  function summarizeCheck(check) {
    const detail = check.detail;
    if (!detail) return "";
    if (Array.isArray(detail)) return detail.join("; ");
    if (typeof detail === "object") {
      if (Array.isArray(detail.errors) && detail.errors.length) return detail.errors.join("; ");
      const parts = [];
      ["namespace", "version_count", "current_version", "recomputed_conversions", "inherited_versions", "results", "pending_jobs", "pointer_version", "precision_sensitive", "higher_scale_flip", "checked_metrics"].forEach((key) => {
        if (detail[key] !== undefined) parts.push(`${key}=${detail[key]}`);
      });
      if (detail.failed_metrics?.length) parts.push(`failed_metrics=${detail.failed_metrics.join(",")}`);
      return parts.join(" · ");
    }
    return String(detail);
  }

  function handleClientResultClick(event) {
    const button = event.target.closest(".client-result-button");
    if (!button) return;
    openClientResult(button.dataset.clientId, button.dataset.roundKey);
  }

  async function openClientResult(clientId, roundKey) {
    if (!state.overview?.namespace) return;
    try {
      const payload = await api(`/api/result?namespace=${encodeURIComponent(state.overview.namespace)}&client_id=${encodeURIComponent(clientId)}&round_key=${encodeURIComponent(roundKey)}`);
      state.currentResult = payload.result;
      renderComputeResult({
        result: payload.result,
        bound_version: payload.bound_version,
        idempotent: true,
      });
      switchView("compute");
    } catch {
      /* handled */
    }
  }

  async function resumePending() {
    try {
      const result = await api("/api/resume", { method: "POST", body: { namespace: state.overview?.namespace } });
      toast("RESUME COMPLETE", `重放 ${result.resumed_count} 个未提交作业`, result.resumed_count ? "info" : "warn");
      await refreshAll(true);
    } catch {
      /* handled */
    }
  }

  async function bootstrapDemo(includePatches) {
    try {
      await api("/api/bootstrap", {
        method: "POST",
        body: { confirm: "BOOTSTRAP", include_patches: includePatches },
      });
      toast("SAMPLE READY", includePatches ? "快照与两个补丁已载入" : "初始快照已载入");
      await refreshAll(true);
      switchView("dashboard");
    } catch {
      /* handled */
    }
  }

  async function resetState() {
    if (!window.confirm("清空所有版本、客户端结果与误差账本？此操作不可撤销。")) return;
    try {
      await api("/api/reset", { method: "POST", body: { confirm: "RESET" } });
      state.selectedVersion = null;
      state.currentResult = null;
      state.compare = null;
      state.verify = null;
      $("#computeResult").innerHTML = `<div class="result-placeholder"><div class="coordinate-mark">02</div><h4>等待计算</h4><p>结果会同时输出定点值、decimal 真值、偏差、误差上界与来源链。</p></div>`;
      $("#compareOutput").innerHTML = `<div class="empty-state panel">尚未运行版本比较</div>`;
      $("#verifyOutput").innerHTML = `<div class="verify-placeholder panel"><div class="coordinate-mark">03</div><h4>等待验证</h4><p>验证器会重放所有换算登记，并检查指针、结果、作业与误差账本是否一致。</p></div>`;
      toast("RESET COMPLETE", "业务状态已清空", "warn");
      await refreshAll(true);
    } catch {
      /* handled */
    }
  }

  async function copyConfig() {
    const configs = state.overview?.configs || [];
    const payload = {};
    configs.forEach((config) => {
      payload[config.key] = {
        type: config.type,
        scale: config.scale,
        role: config.role,
        value: config.value,
      };
    });
    try {
      await navigator.clipboard.writeText(pretty(payload));
      toast("COPIED", `${configs.length} 个配置已复制`);
    } catch {
      toast("COPY FAILED", "浏览器未授权剪贴板", "warn");
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
